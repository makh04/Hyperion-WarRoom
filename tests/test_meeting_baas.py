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
        return {"success": True, "data": {"bot_id": "bot-test-123"}}


class FakeClient:
    def __init__(self):
        self.request = None

    async def post(self, url, headers, json):
        self.request = (url, headers, json)
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
        url, headers, payload = client.request
        assert url == "https://api.meetingbaas.com/v2/bots"
        assert headers["x-meeting-baas-api-key"] == "test-key"
        assert payload["streaming_config"]["output_url"] == "wss://tunnel.example.test/ws/meeting-baas/inc_test"
        assert payload["streaming_config"]["audio_frequency"] == 24000
        assert payload["allow_multiple_bots"] is False
        print("MEETING BAAS CONTRACT PASSED")
    finally:
        meeting_baas.settings = original_settings


if __name__ == "__main__":
    asyncio.run(main())
