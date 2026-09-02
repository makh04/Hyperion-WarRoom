from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import state
from .agent_session import AgentSession

logger = logging.getLogger("sentinelvoice.bridge")
router = APIRouter()


@router.websocket("/ws/bot/{incident_id}")
async def bot_bridge(websocket: WebSocket, incident_id: str):
    """The Playwright Meet bot connects here. Messages it sends:

        {"type": "audio.chunk", "audio": "<base64 PCM16 24kHz mono>"}
        {"type": "speaker.active", "name": "Alex"}
        {"type": "meet.left"}

    Messages it receives (to play into the virtual mic feeding the Meet call):

        {"type": "reply.audio", "data": "<base64 PCM16 24kHz mono>"}
    """
    incident = state.store.get(incident_id)
    if incident is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    async def send_agent_audio_to_bot(audio_b64: str) -> None:
        try:
            await websocket.send_text(json.dumps({"type": "reply.audio", "data": audio_b64}))
        except Exception:
            pass

    session = AgentSession(incident_id, on_agent_audio=send_agent_audio_to_bot)
    incident.agent_session = session

    try:
        await session.connect()
    except Exception:
        logger.exception("Failed to connect to AssemblyAI for incident %s", incident_id)
        await websocket.close(code=1011)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "audio.chunk":
                await session.send_user_audio(msg["audio"])
            elif mtype == "speaker.active":
                state.set_active_speaker(incident, msg["name"])
            elif mtype == "meet.left":
                break
            else:
                logger.debug("Ignoring unknown bot message type: %s", mtype)

    except WebSocketDisconnect:
        logger.info("Bot disconnected for incident %s", incident_id)
    finally:
        await session.close()
        incident.agent_session = None
