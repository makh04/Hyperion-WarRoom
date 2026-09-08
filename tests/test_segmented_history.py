"""Focused coverage for segmented transcript history and live ingestion."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import incident_state, transcript_buffer  # noqa: E402
from app.integrations import step3_webhook  # noqa: E402
from app.routers import meeting_stream  # noqa: E402
from app.routers.transcripts import process_completed_window  # noqa: E402
from app import state  # noqa: E402


class FakePromptGuard:
    async def classify(self, text: str) -> dict:
        return {"model": "test", "label": "benign", "is_safe": True}


async def main() -> None:
    original_guard = incident_state.prompt_guard
    incident_state.prompt_guard = FakePromptGuard()
    try:
        test_buffer = transcript_buffer.TranscriptBuffer(window_seconds=0)
        first = await test_buffer.add("Alex", "Database is unavailable")
        second = await test_buffer.add("Sam", "Rollback is approved")
        assert first["completed_window"]["segment_id"] == 1
        assert second["completed_window"]["segment_id"] == 2
        assert first["completed_window"]["start_time"] <= first["completed_window"]["end_time"]
        assert second["completed_window"]["start_time"] <= second["completed_window"]["end_time"]

        analysis = await incident_state.analyze_completed_window(first["completed_window"])
        for key in (
            "active_topics",
            "person_assignments",
            "suggested_actions",
            "key_decisions",
            "active_blockers",
        ):
            assert key in analysis

        original_path = step3_webhook.DRAFT_SUMMARY_PATH
        step3_webhook.DRAFT_SUMMARY_PATH = Path("temp") / "segmented_history_test.json"
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"received": True})

        try:
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                await process_completed_window(first["completed_window"], "inc_segmented", client)
                persisted = json.loads(
                    step3_webhook.DRAFT_SUMMARY_PATH.read_text(encoding="utf-8")
                )
                persisted["active_topics"] = ["database availability"]
                step3_webhook.DRAFT_SUMMARY_PATH.write_text(
                    json.dumps(persisted), encoding="utf-8"
                )
                await process_completed_window(second["completed_window"], "inc_segmented", client)
                third = await test_buffer.add("Taylor", "The rollback is complete")
                await process_completed_window(third["completed_window"], "inc_segmented", client)
            saved = json.loads(step3_webhook.DRAFT_SUMMARY_PATH.read_text(encoding="utf-8"))
            assert saved["meeting_id"] == "inc_segmented"
            assert [segment["segment_id"] for segment in saved["segments"]] == [1, 2, 3]
            assert saved["segments"][0]["conversation"][0]["message"] == "Database is unavailable"
            assert saved["incident_state"]["transcript_message_count"] == 3
            assert saved["active_topics"] == ["database availability"]
            assert len(requests) == 3
            assert [segment["segment_id"] for segment in requests[-1]["segments"]] == [1, 2, 3]
        finally:
            step3_webhook.DRAFT_SUMMARY_PATH.unlink(missing_ok=True)
            step3_webhook.DRAFT_SUMMARY_PATH = original_path

        original_buffer = meeting_stream.buffer
        original_processor = meeting_stream.process_completed_window
        live_buffer = transcript_buffer.TranscriptBuffer(window_seconds=0)
        completed = []

        async def capture_completed(window, meeting_id):
            completed.append((window, meeting_id))

        meeting_stream.buffer = live_buffer
        meeting_stream.process_completed_window = capture_completed
        try:
            incident = await state.store.create("live segmented test")
            await meeting_stream._handle_transcript(incident, {
                "type": "Turn",
                "transcript": "The database is unavailable",
                "speaker_name": "Alex",
                "end_of_turn": True,
            })
            assert completed[0][0]["messages"] == [{
                "speaker": "Alex",
                "message": "The database is unavailable",
                "received_at": completed[0][0]["messages"][0]["received_at"],
            }]
            assert completed[0][1] == incident.id
        finally:
            meeting_stream.buffer = original_buffer
            meeting_stream.process_completed_window = original_processor
    finally:
        incident_state.prompt_guard = original_guard
    print("SEGMENTED HISTORY PASSED")


if __name__ == "__main__":
    asyncio.run(main())