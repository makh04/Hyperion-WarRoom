from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


WINDOW_SECONDS = 180


@dataclass
class TranscriptMessage:
    speaker: str
    message: str
    received_at: float = field(default_factory=time.time)


class TranscriptBuffer:
    def __init__(self, window_seconds: int = WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self._messages: list[TranscriptMessage] = []
        self._started_at = time.time()
        self._next_segment_id = 1
        self._accepting = True
        self._lock = asyncio.Lock()

    async def add(self, speaker: str, message: str) -> dict:
        async with self._lock:
            if not self._accepting:
                return {"accepted": False, "message": None, "window": self._snapshot(), "completed_window": None}
            entry = TranscriptMessage(speaker=speaker, message=message)
            self._messages.append(entry)
            completed = None
            completed_at = time.time()
            if completed_at - self._started_at >= self.window_seconds:
                completed = self._completed_snapshot(completed_at)
                self._messages = []
                self._started_at = completed_at
                self._next_segment_id += 1

            return {
                "accepted": True,
                "message": self._message_to_dict(entry),
                "window": self._snapshot(),
                "completed_window": completed,
            }

    async def current(self) -> dict:
        async with self._lock:
            return self._snapshot()

    async def reset(self) -> None:
        """Clear the current window; useful for isolated consumers and tests."""
        async with self._lock:
            self._messages = []
            self._started_at = time.time()
            self._next_segment_id = 1
            self._accepting = True

    async def finish(self) -> None:
        async with self._lock:
            self._accepting = False

    def _snapshot(self) -> dict:
        return {
            "started_at": self._started_at,
            "elapsed_seconds": max(0, time.time() - self._started_at),
            "window_seconds": self.window_seconds,
            "messages": [self._message_to_dict(item) for item in self._messages],
        }

    def _completed_snapshot(self, end_time: float) -> dict:
        snapshot = self._snapshot()
        snapshot.update({
            "segment_id": self._next_segment_id,
            "start_time": self._started_at,
            "end_time": end_time,
        })
        return snapshot

    @staticmethod
    def _message_to_dict(entry: TranscriptMessage) -> dict:
        return {
            "speaker": entry.speaker,
            "message": entry.message,
            "received_at": entry.received_at,
        }


buffer = TranscriptBuffer()