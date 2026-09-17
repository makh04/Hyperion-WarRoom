from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import state
from ..integrations.step4_report import FinalReportError, finalize_incident
from ..integrations.meeting_baas import MeetingBaaSClient
from ..transcript_buffer import buffer
from .transcripts import process_completed_window
from ..agent_dispatch import detect_wake_word, dispatch_agent_query
from ..voice.tts import speak_in_meeting


router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _handle_agent_query_chat(incident: state.Incident, query: str) -> None:
    """Background task: agent query triggered via meeting chat message."""
    print(f"[_handle_agent_query_chat] Chat wake query: '{query}' for incident {incident.id}", flush=True)
    try:
        reply = await dispatch_agent_query(query, incident)
        print(f"[_handle_agent_query_chat] Reply: '{reply}'", flush=True)
        if incident.meeting_baas_bot_id:
            await MeetingBaaSClient().send_chat_message(incident.meeting_baas_bot_id, f"[SentinelVoice] {reply}")
        await state.broadcast(incident, {"type": "agent.reply", "query": query, "reply": reply})
        await speak_in_meeting(incident.id, reply)
    except Exception as exc:
        print(f"[_handle_agent_query_chat ERROR] {exc}", flush=True)


def _find_incident(bot_id: str) -> state.Incident | None:

    return next(
        (incident for incident in state.store.list() if incident.meeting_baas_bot_id == bot_id),
        None,
    )


def _chat_message(event: dict[str, Any]) -> tuple[str, str, str | None] | None:
    event_type = str(event.get("event", "")).lower()
    if event_type not in {"bot.chat_message", "chat_message", "chat.message", "meeting.chat", "bot.chat"}:
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    bot_id = data.get("bot_id") or event.get("bot_id")
    text = data.get("text") or data.get("message") or data.get("content")
    if not isinstance(bot_id, str) or not isinstance(text, str) or not text.strip():
        return None
    sender = data.get("sender_name") or data.get("sender") or data.get("speaker") or data.get("from")
    return bot_id, text.strip(), str(sender) if sender else None



def _call_ended_bot_id(event: dict[str, Any]) -> str | None:
    event_name = str(event.get("event", "")).upper()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    status = str(data.get("status", data.get("state", ""))).upper()
    if event_name not in {"CALL_ENDED", "BOT.CALL_ENDED", "BOT_CALL_ENDED"} and status != "CALL_ENDED":
        return None
    bot_id = data.get("bot_id") or event.get("bot_id")
    return bot_id if isinstance(bot_id, str) else None


@router.post("/meeting-baas")
async def meeting_baas_webhook(event: dict[str, Any]):
    ended_bot_id = _call_ended_bot_id(event)
    if ended_bot_id is not None:
        incident = _find_incident(ended_bot_id)
        if incident is None:
            raise HTTPException(404, "meeting bot is not linked to an incident")
        try:
            result = await finalize_incident(incident, event, buffer)
        except FinalReportError as exc:
            print(f"[{incident.id}] Reasoning model failed: {exc}", flush=True)
            await state.broadcast(incident, {
                "type": "agent.error",
                "code": "reasoning_model_failed",
                "message": str(exc),
            })
            raise HTTPException(502, str(exc)) from exc
        await buffer.finish()
        incident.meeting_baas_status = "call_ended"
        incident.status = "resolved"
        await state.broadcast(incident, {"type": "incident.final_report", **result})
        return {"received": True, "handled": True, "event": "CALL_ENDED", "incident_id": incident.id, **result}

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
    buffered = await buffer.add(sender or "unknown", text)
    if buffered["completed_window"] is not None:
        await process_completed_window(buffered["completed_window"], incident.id)

    # ── Wake word detection (chat path) ─────────────────────────────────
    wake_query = detect_wake_word(text)
    if wake_query:
        asyncio.create_task(_handle_agent_query_chat(incident, wake_query))

    return {"received": True, "handled": True, "incident_id": incident.id}