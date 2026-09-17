"""Dependency-light contract test for MeetingBaaS bot creation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations import meeting_baas  # noqa: E402


class FakeResponse:
    status_code = 200

    def json(self):
        return {"success": True, "data": {"bot_id": "bot-test-123", "status": "in_call"}}


class FakeClient:
    def __init__(self):
        self.requests = []

    async def get(self, url, headers):
        self.requests.append(("GET", url, headers, None))
        return FakeResponse()

    async def post(self, url, headers, json):
        self.requests.append(("POST", url, headers, json))
        return FakeResponse()


async def main() -> None:
    original_settings = meeting_baas.settings
    meeting_baas.settings = SimpleNamespace(
        meeting_baas_api_key="test-key",
        meeting_baas_api_base_url="https://api.meetingbaas.com/v2",
        public_base_url="https://tunnel.example.test",
        streaming_sample_rate=24000,
    )
    try:
        client = FakeClient()
        result = await meeting_baas.MeetingBaaSClient(client).create_audio_bot(
            "https://meet.google.com/abc-def-ghi",
            "inc_test",
            "SentinelVoice",
        )
        assert result.bot_id == "bot-test-123"
        _, url, headers, payload = client.requests[0]
        assert url == "https://api.meetingbaas.com/v2/bots"
        assert headers["x-meeting-baas-api-key"] == "test-key"
        assert payload["streaming_config"]["output_url"] == "wss://tunnel.example.test/ws/meeting-baas/inc_test"
        assert payload["streaming_config"]["audio_frequency"] == 24000
        assert payload["allow_multiple_bots"] is False

        chat_client = FakeClient()
        await meeting_baas.MeetingBaaSClient(chat_client).send_chat_message(
            "bot-test-123", "Hello from the bot!"
        )
        assert chat_client.requests[0][0:2] == (
            "GET", "https://api.meetingbaas.com/bots/bot-test-123"
        )
        assert chat_client.requests[1][0:2] == (
            "POST", "https://api.meetingbaas.com/bots/bot-test-123/send_chat_message"
        )
        assert chat_client.requests[1][3] == {"message": "Hello from the bot!"}
        print("MEETING BAAS CONTRACT PASSED")
    finally:
        meeting_baas.settings = original_settings


if __name__ == "__main__":
    asyncio.run(main())
