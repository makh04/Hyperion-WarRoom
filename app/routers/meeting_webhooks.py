from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import state

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _find_incident(bot_id: str) -> state.Incident | None:
    return next(
        (incident for incident in state.store.list() if incident.meeting_baas_bot_id == bot_id),
        None,
    )


def _chat_message(event: dict[str, Any]) -> tuple[str, str, str | None] | None:
    if event.get("event") != "bot.chat_message":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    bot_id = data.get("bot_id")
    text = data.get("text")
    if not isinstance(bot_id, str) or not isinstance(text, str) or not text.strip():
        return None
    sender = data.get("sender_name")
    return bot_id, text.strip(), str(sender) if sender else None


@router.post("/meeting-baas")
async def meeting_baas_webhook(event: dict[str, Any]):
    chat = _chat_message(event)
    if chat is None:
        return {"received": True, "handled": False}

    bot_id, text, sender = chat
    incident = _find_incident(bot_id)
    if incident is None:
        raise HTTPException(404, "meeting bot is not linked to an incident")

    entry = state.record_timeline(
        incident,
        "meeting.chat",
        text,
        speaker=sender,
        meta={
            "source": "meeting_baas",
            "message_id": event.get("data", {}).get("message_id"),
            "sender_id": event.get("data", {}).get("sender_id"),
            "sent_at": event.get("data", {}).get("sent_at"),
        },
    )
    await state.broadcast(incident, {
        "type": "chat.message",
        "sender": sender,
        "text": text,
        "message_id": entry.meta["message_id"],
        "sent_at": entry.meta["sent_at"],
    })
    return {"received": True, "handled": True, "incident_id": incident.id}