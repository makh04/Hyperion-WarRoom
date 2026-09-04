from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("sentinelvoice.meeting_baas")


class MeetingBaaSError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BotCreation:
    bot_id: str
    raw: dict[str, Any]


class MeetingBaaSClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def create_audio_bot(self, meeting_url: str, incident_id: str, bot_name: str) -> BotCreation:
        if not settings.meeting_baas_api_key:
            raise MeetingBaaSError("MEETING_BAAS_KEY is not configured")

        stream_base_url = settings.public_base_url.rstrip("/")
        if stream_base_url.startswith("https://"):
            stream_base_url = "wss://" + stream_base_url.removeprefix("https://")
        elif stream_base_url.startswith("http://"):
            stream_base_url = "ws://" + stream_base_url.removeprefix("http://")

        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_mode": "audio_only",
            "streaming_enabled": True,
            "streaming_config": {
                "mode": "audio",
                "output_url": f"{stream_base_url}/ws/meeting-baas/{incident_id}",
                "input_url": None,
                "audio_frequency": settings.streaming_sample_rate,
            },
            "allow_multiple_bots": False,
            "extra": {"incident_id": incident_id},
        }
        logger.info("MeetingBaaS bot %s will stream audio to %s", bot_name, payload["streaming_config"]["output_url"])
        headers = {
            "Content-Type": "application/json",
            "x-meeting-baas-api-key": settings.meeting_baas_api_key,
        }

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            response = await client.post(
                f"{settings.meeting_baas_api_base_url.rstrip('/')}/bots",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise MeetingBaaSError("MeetingBaaS request failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code >= 400:
            if response.status_code == 429:
                message = "MeetingBaaS rate limit reached"
            elif response.status_code == 403:
                message = "MeetingBaaS API key is not allowed to create bots"
            else:
                message = f"MeetingBaaS bot creation failed ({response.status_code})"
            raise MeetingBaaSError(message, response.status_code)

        try:
            body = response.json()
            bot_id = body["data"]["bot_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise MeetingBaaSError("MeetingBaaS returned an invalid bot response") from exc
        return BotCreation(bot_id=bot_id, raw=body)
