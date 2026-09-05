"""Dependency-light tests for the MeetingBaaS -> Python -> AssemblyAI handoff."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state  # noqa: E402
from app.routers import meeting_stream  # noqa: E402


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def receive(self):
        try:
            return next(self.messages)
        except StopIteration:
            return {"type": "websocket.disconnect"}

    async def close(self, code=None):
        self.closed = code


class FakeTranscriber:
    instances = []

    def __init__(self, on_event):
        self.on_event = on_event
        self.audio = []
        self.connected = False
        self.closed = False
        self.instances.append(self)

    async def connect(self):
        self.connected = True

    async def send_audio(self, audio):
        self.audio.append(audio)

    async def close(self):
        self.closed = True


async def main() -> None:
    original_transcriber = meeting_stream.StreamingTranscriber
    meeting_stream.StreamingTranscriber = FakeTranscriber
    try:
        incident = await state.store.create("stream test", "https://meet.google.com/test")
        incident.meeting_baas_bot_id = "bot-test"
        websocket = FakeWebSocket([
            {"type": "websocket.receive", "text": '{"protocol_version": 2, "bot_id": "bot-test", "sample_rate": 24000}'},
            {"type": "websocket.receive", "text": '[{"name":"Alex","isSpeaking":true}]'},
            {"type": "websocket.receive", "bytes": b"\\x01\\x02\\x03\\x04"},
        ])

        await meeting_stream.meeting_baas_stream(websocket, incident.id)
        transcriber = FakeTranscriber.instances[-1]
        assert websocket.accepted is True
        assert transcriber.connected is True
        assert transcriber.audio == [b"\\x01\\x02\\x03\\x04"]
        assert transcriber.closed is True
        assert incident.recent_speaker.name == "Alex"
        assert meeting_stream._speaker_name({"speaker_name": "AssemblyAI person"}) == "AssemblyAI person"

        await meeting_stream._handle_transcript(incident, {
            "type": "Turn",
            "transcript": "partial words",
            "end_of_turn": False,
        })
        assert incident.timeline == []
        await meeting_stream._handle_transcript(incident, {
            "type": "Turn",
            "transcript": "final words",
            "end_of_turn": True,
            "utteranceStart": 1.0,
            "utteranceEnd": 2.0,
        })
        assert len(incident.timeline) == 1
        assert incident.timeline[0].text == "final words"
        assert incident.timeline[0].speaker == "Alex"
        assert incident.timeline[0].meta["speaker_source"] == "meeting_baas"
        assert incident.timeline[0].meta["utterance_start"] == 1.0
        await meeting_stream._handle_transcript(incident, {
            "type": "Turn",
            "transcript": "named AssemblyAI turn",
            "speaker_name": "AssemblyAI person",
            "end_of_turn": True,
        })
        assert incident.timeline[-1].speaker == "AssemblyAI person"
        assert incident.timeline[-1].meta["speaker_source"] == "assemblyai"
        print("MEETING STREAM HANDOFF PASSED")
    finally:
        meeting_stream.StreamingTranscriber = original_transcriber


if __name__ == "__main__":
    asyncio.run(main())
