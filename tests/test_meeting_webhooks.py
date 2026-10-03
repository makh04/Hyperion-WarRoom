"""Dependency-light tests for Meeting BaaS webhook events."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state  # noqa: E402
from app.routers import meeting_webhooks  # noqa: E402


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


async def main() -> None:
    incident = await state.store.create("chat test")
    incident.meeting_baas_bot_id = "bot-chat"

    finalized = []

    async def fake_finalize(received_incident, event, transcript_buffer):
        finalized.append((received_incident.id, event["event"]))
        return {"saved_path": "temp/report.json", "report": "# Summary", "model": "test"}

    original_finalizer = meeting_webhooks.finalize_incident
    meeting_webhooks.finalize_incident = fake_finalize
    end_result = await meeting_webhooks.meeting_baas_webhook({
        "event": "bot.status_change",
        "data": {
            "bot_id": "bot-chat",
            "status": {"code": "call_ended", "created_at": "2026-09-04T12:00:00Z"},
        },
    })
    meeting_webhooks.finalize_incident = original_finalizer
    assert finalized == [(incident.id, "bot.status_change")]
    assert end_result["handled"] is True
    assert end_result["event"] == "CALL_ENDED"
    assert incident.status == "resolved"

    socket = FakeSocket()
    incident.dashboard_sockets.add(socket)

    result = await meeting_webhooks.meeting_baas_webhook({
        "event": "bot.chat_message",
        "data": {
            "bot_id": "bot-chat",
            "message_id": "msg-1",
            "sender_name": "Alex",
            "sender_id": 7,
            "text": "Deploy is complete",
            "sent_at": "2026-09-04T12:00:00Z",
        },
    })

    assert result == {"received": True, "handled": True, "incident_id": incident.id}
    assert incident.timeline[0].kind == "meeting.chat"
    assert incident.timeline[0].speaker == "Alex"
    assert incident.timeline[0].text == "Deploy is complete"
    assert socket.messages == [{
        "type": "chat.message",
        "sender": "Alex",
        "text": "Deploy is complete",
        "message_id": "msg-1",
        "sent_at": "2026-09-04T12:00:00Z",
    }]
    print("MEETING CHAT WEBHOOK PASSED")


if __name__ == "__main__":
    asyncio.run(main())