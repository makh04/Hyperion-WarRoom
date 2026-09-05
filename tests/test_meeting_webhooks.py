"""Dependency-light tests for Meeting BaaS webhook events."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state  # noqa: E402
from app.routers.meeting_webhooks import meeting_baas_webhook  # noqa: E402


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


async def main() -> None:
    incident = await state.store.create("chat test")
    incident.meeting_baas_bot_id = "bot-chat"
    socket = FakeSocket()
    incident.dashboard_sockets.add(socket)

    result = await meeting_baas_webhook({
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