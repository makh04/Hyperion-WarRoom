from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

from .. import state
from ..config import settings
from ..tools.dispatcher import ToolError, execute_tool
from ..tools.schemas import AGENT_SYSTEM_PROMPT, build_inline_tools
from .provisioning import ensure_agent_id

logger = logging.getLogger("sentinelvoice.agent_session")

AGENT_WS_URL = "wss://agents.assemblyai.com/v1/ws"

AudioSink = Callable[[str], Awaitable[None]]


class AgentSession:
    """One AssemblyAI Voice Agent WebSocket connection, mapped 1:1 to one incident call.

    Bridges audio in both directions and dispatches tool calls. Kept intentionally
    lightweight: STT, LLM reasoning (via Groq when configured), and TTS all happen on
    AssemblyAI's / Groq's side - this class only moves JSON + base64 audio bytes, so it costs
    almost nothing in CPU or memory locally, however many incidents are running.
    """

    def __init__(self, incident_id: str, on_agent_audio: Optional[AudioSink] = None):
        self.incident_id = incident_id
        self._on_agent_audio = on_agent_audio
        self._ws = None
        self._session_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_tool_results: list = []

    @property
    def ready(self) -> bool:
        return self._session_id is not None

    async def connect(self) -> None:
        headers = {"Authorization": f"Bearer {settings.assemblyai_api_key}"}
        self._ws = await self._open_ws(headers)

        if settings.use_stored_agent:
            agent_id = await ensure_agent_id()
            session_config = {"agent_id": agent_id}
            logger.info("Session for incident %s binding to stored agent %s", self.incident_id, agent_id)
        else:
            # No custom LLM configured -> configure everything inline, no Agents REST call
            # needed at all. AssemblyAI's own managed model does the reasoning in this mode.
            session_config = {
                "system_prompt": AGENT_SYSTEM_PROMPT,
                "input": {
                    "format": {"encoding": "audio/pcm"},
                    "turn_detection": {
                        "vad_threshold": 0.55,
                        "min_silence": 700,
                        "max_silence": 4000,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "voice": settings.agent_voice,
                    "format": {"encoding": "audio/pcm"},
                    "volume": 100,
                },
                "tools": build_inline_tools(),
            }

        await self._send({"type": "session.update", "session": session_config})
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"agent-session-{self.incident_id}"
        )

    @staticmethod
    async def _open_ws(headers: dict):
        try:
            # Newer `websockets` releases (>=13) use `additional_headers`.
            return await websockets.connect(
                AGENT_WS_URL, additional_headers=headers, max_size=None, ping_interval=20, ping_timeout=20,
            )
        except TypeError:
            # Older releases use `extra_headers` instead.
            return await websockets.connect(
                AGENT_WS_URL, extra_headers=headers, max_size=None, ping_interval=20, ping_timeout=20,
            )

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            # Stops billing immediately; a bare socket close leaves the session resumable
            # (and billable) for another 30 seconds.
            await self._send({"type": "session.end"})
        except Exception:
            pass
        if self._reader_task:
            self._reader_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass

    async def send_user_audio(self, audio_b64: str) -> None:
        if not self.ready:
            return
        await self._send({"type": "input.audio", "audio": audio_b64})

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("AgentSession not connected")
        await self._ws.send(json.dumps(payload))

    async def _read_loop(self) -> None:
        incident = state.store.get(self.incident_id)
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                await self._handle_event(incident, event)
        except websockets.ConnectionClosed:
            logger.info("AssemblyAI session closed for incident %s", self.incident_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in AssemblyAI read loop for incident %s", self.incident_id)

    async def _handle_event(self, incident, event: dict) -> None:
        etype = event.get("type")

        if etype == "session.ready":
            self._session_id = event["session_id"]
            logger.info("Session ready for incident %s (session_id=%s)", self.incident_id, self._session_id)
            return

        if etype in ("session.error", "error"):
            logger.warning("AssemblyAI session error [%s]: %s", event.get("code"), event.get("message"))
            if incident:
                await state.broadcast(incident, {
                    "type": "agent.error", "code": event.get("code"), "message": event.get("message"),
                })
            return

        if etype == "input.speech.started":
            if incident:
                await state.broadcast(incident, {"type": "input.speech.started"})
            return

        if etype == "transcript.user.delta":
            if incident:
                await state.broadcast(incident, {"type": "transcript.delta", "text": event["text"]})
            return

        if etype == "transcript.user":
            if incident:
                speaker = state.attribute_speaker(incident)
                entry = state.record_timeline(incident, "transcript.user", event["text"], speaker=speaker)
                await state.broadcast(incident, {"type": "timeline.entry", "entry": state.entry_to_dict(entry)})
            return

        if etype == "transcript.agent":
            if incident:
                entry = state.record_timeline(incident, "transcript.agent", event["text"], speaker="SentinelVoice")
                await state.broadcast(incident, {"type": "timeline.entry", "entry": state.entry_to_dict(entry)})
            return

        if etype == "reply.audio":
            if self._on_agent_audio is not None:
                await self._on_agent_audio(event["data"])
            return

        if etype == "reply.done":
            # Per AssemblyAI's tool-calling contract: only send accumulated tool.result
            # events once the turn that triggered them has actually completed, and drop
            # them if the user barged in first.
            if self._pending_tool_results and event.get("status") == "completed":
                for result in self._pending_tool_results:
                    await self._send(result)
            self._pending_tool_results = []
            return

        if etype == "tool.call":
            await self._handle_tool_call(event)
            return

        # session.updated, input.speech.stopped, reply.started, session.ended: no action needed
        logger.debug("Unhandled event type: %s", etype)

    async def _handle_tool_call(self, event: dict) -> None:
        call_id = event["call_id"]
        name = event["name"]
        arguments = event.get("arguments", {})
        try:
            result = await execute_tool(self.incident_id, name, arguments)
            payload = {"call_id": call_id, "result": json.dumps(result)}
        except ToolError as exc:
            payload = {"call_id": call_id, "result": json.dumps({"error": str(exc)})}
        except Exception:
            logger.exception("Tool '%s' raised an unexpected error", name)
            payload = {"call_id": call_id, "result": json.dumps({"error": "internal_error"})}
        self._pending_tool_results.append({"type": "tool.result", **payload})
