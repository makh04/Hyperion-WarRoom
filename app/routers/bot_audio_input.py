from __future__ import annotations

import asyncio
import logging
import struct
from typing import Callable, Awaitable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("hyperion_warroom.bot_audio_input")
router = APIRouter()

# incident_id -> BotAudioSession
_sessions: dict[str, "BotAudioSession"] = {}

# 20ms of silence at 24 kHz 16-bit mono = 480 samples = 960 bytes
_SILENCE_FRAME = b"\x00" * 960
_HEARTBEAT_INTERVAL = 0.02  # seconds — 20ms to match sample rate


class BotAudioSession:
    """
    Holds a queue of PCM frames to inject into the meeting via MeetingBaaS.
    When the queue is empty the WebSocket send loop sends silence so the
    bot's mic channel stays open and latency stays low.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._websocket: WebSocket | None = None

    def attach(self, ws: WebSocket) -> None:
        self._websocket = ws

    def detach(self) -> None:
        self._websocket = None

    async def push_audio(self, pcm_frames: bytes) -> None:
        """Enqueue raw PCM (16-bit signed LE, mono, 24 kHz) to send to the bot."""
        pcm_frames = bytes(pcm_frames)
        # Chunk into 20ms frames
        chunk_size = 960
        for i in range(0, len(pcm_frames), chunk_size):
            chunk = pcm_frames[i : i + chunk_size]
            if len(chunk) < chunk_size:
                # Pad last frame with silence
                chunk = chunk + b"\x00" * (chunk_size - len(chunk))
            await self._queue.put(chunk)

    async def _run_send_loop(self, websocket: WebSocket) -> None:
        """Continuously send frames (or silence) to the connected WebSocket."""
        self.attach(websocket)
        try:
            while True:
                try:
                    frame = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    frame = _SILENCE_FRAME
                await websocket.send_bytes(frame)
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
        except Exception:
            pass
        finally:
            self.detach()


def get_or_create_session(incident_id: str) -> BotAudioSession:
    if incident_id not in _sessions:
        _sessions[incident_id] = BotAudioSession()
    return _sessions[incident_id]


def remove_session(incident_id: str) -> None:
    _sessions.pop(incident_id, None)


async def push_tts_audio(incident_id: str, pcm_bytes: bytes) -> None:
    """Called by TTS module to inject audio into an active session."""
    session = _sessions.get(incident_id)
    if session:
        await session.push_audio(pcm_bytes)


@router.websocket("/ws/bot-input/{incident_id}")
async def bot_audio_input(websocket: WebSocket, incident_id: str) -> None:
    """
    MeetingBaaS connects here as the bot's audio input_url.
    We keep the channel open (sending silence) and inject TTS PCM when available.
    """
    session = get_or_create_session(incident_id)
    await websocket.accept()
    logger.info("[%s] Bot audio input channel connected", incident_id)
    try:
        await session._run_send_loop(websocket)
    except WebSocketDisconnect:
        logger.info("[%s] Bot audio input channel disconnected", incident_id)
    finally:
        session.detach()
