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

    def _bot_api_base_url(self) -> str:
        base_url = settings.meeting_baas_api_base_url.rstrip("/")
        return base_url.removesuffix("/v2")

    async def create_audio_bot(self, meeting_url: str, incident_id: str, bot_name: str) -> BotCreation:
        if not settings.meeting_baas_api_key:
            raise MeetingBaaSError("MEETING_BAAS_KEY is not configured")

        stream_base_url = settings.public_base_url.rstrip("/")
        if stream_base_url.startswith("https://"):
            stream_base_url = "wss://" + stream_base_url.removeprefix("https://")
        elif stream_base_url.startswith("http://"):
            stream_base_url = "ws://" + stream_base_url.removeprefix("http://")

        # Build input_url so the bot's mic channel stays open for TTS audio injection.
        # The /ws/bot-input/{incident_id} endpoint sends silence until TTS audio is queued.
        input_url = f"{stream_base_url}/ws/bot-input/{incident_id}"

        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_mode": "audio_only",
            "streaming_enabled": True,
            "webhook_url": f"{settings.public_base_url.rstrip('/')}/webhooks/meeting-baas",
            "streaming_config": {
                "mode": "audio",
                "output_url": f"{stream_base_url}/ws/meeting-baas/{incident_id}",
                "input_url": input_url,
                "audio_frequency": settings.streaming_sample_rate,
            },
            "allow_multiple_bots": False,
            "extra": {"incident_id": incident_id},
        }
        logger.info(
            "MeetingBaaS bot %s → out: %s | in: %s | webhook: %s",
            bot_name,
            payload["streaming_config"]["output_url"],
            payload["streaming_config"]["input_url"],
            payload["webhook_url"],
        )
        print(f"[MeetingBaaS] Creating bot '{bot_name}' for meeting {meeting_url} with webhook {payload['webhook_url']}", flush=True)
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

    async def send_chat_message(self, bot_id: str, message: str) -> None:
        """Send a chat message into the meeting via MeetingBaaS."""
        if not settings.meeting_baas_api_key:
            logger.warning("MEETING_BAAS_KEY not set — cannot send chat message")
            return
        headers = {
            "Content-Type": "application/json",
            "x-meeting-baas-api-key": settings.meeting_baas_api_key,
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            logger.info(
                "MeetingBaaS chat send starting: bot=%s message=%r",
                bot_id,
                message,
            )
            status_response = await client.get(
                f"{self._bot_api_base_url()}/bots/{bot_id}",
                headers=headers,
            )
            logger.info(
                "MeetingBaaS bot status response: bot=%s HTTP %s",
                bot_id,
                status_response.status_code,
            )
            if status_response.status_code >= 400:
                logger.warning(
                    "MeetingBaaS bot status lookup failed: HTTP %s — %s",
                    status_response.status_code, status_response.text[:200],
                )
                return
            try:
                status_body = status_response.json()
                status_data = status_body.get("data", {})
                status = status_data.get("status") if isinstance(status_data, dict) else None
                if not status and isinstance(status_data, dict):
                    bot_data = status_data.get("bot")
                    if isinstance(bot_data, dict):
                        status = bot_data.get("status") or bot_data.get("state")
                    status = status or status_data.get("state")
                status = status or status_body.get("status")
                status = str(status).lower() if status else None
            except (ValueError, TypeError, AttributeError):
                status = None
            logger.info("MeetingBaaS bot %s status lookup returned status=%s", bot_id, status)
            if status not in {"in_call", "joined"}:
                logger.warning(
                    "MeetingBaaS bot %s is not active; chat message was not sent (status=%s)",
                    bot_id, status,
                )
                return

            resp = await client.post(
                f"{self._bot_api_base_url()}/bots/{bot_id}/send_chat_message",
                headers=headers,
                json={"message": message},
            )
            logger.info(
                "MeetingBaaS chat send response: bot=%s HTTP %s",
                bot_id,
                resp.status_code,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "MeetingBaaS chat send failed: HTTP %s — %s",
                    resp.status_code, resp.text[:200],
                )
            else:
                logger.info(
                    "MeetingBaaS chat message sent: bot=%s message=%r",
                    bot_id,
                    message,
                )
        except httpx.HTTPError as exc:
            logger.warning("MeetingBaaS chat send error: %s", exc)
        finally:
            if owns_client:
                await client.aclose()

