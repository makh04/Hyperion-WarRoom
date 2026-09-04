from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import websockets

from ..config import settings

logger = logging.getLogger("sentinelvoice.streaming_transcriber")

TranscriptSink = Callable[[dict], Awaitable[None]]


class StreamingTranscriber:
    def __init__(self, on_event: TranscriptSink):
        self._on_event = on_event
        self._ws = None
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    async def connect(self) -> None:
        if not settings.assemblyai_api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY is not configured")
        query = urlencode({
            "speech_model": settings.assemblyai_streaming_model,
            "sample_rate": settings.streaming_sample_rate,
            "encoding": "pcm_s16le",
        })
        url = f"wss://streaming.assemblyai.com/v3/ws?{query}"
        headers = {"Authorization": settings.assemblyai_api_key}
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
        except TypeError:
            self._ws = await websockets.connect(
                url,
                extra_headers=headers,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, audio: bytes) -> None:
        if self._ws is not None and not self._closed:
            await self._ws.send(audio)

    async def close(self) -> None:
        if self._ws is None or self._closed:
            return
        self._closed = True
        try:
            await self._ws.send(json.dumps({"type": "Terminate"}))
        except Exception:
            pass
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()
            except Exception:
                logger.exception("AssemblyAI streaming reader failed during shutdown")
        try:
            await self._ws.close()
        except Exception:
            pass
        self._ws = None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed AssemblyAI streaming event")
                    continue
                await self._on_event(event)
        except websockets.ConnectionClosed:
            logger.info("AssemblyAI streaming connection closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AssemblyAI streaming reader failed")
