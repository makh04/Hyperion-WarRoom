"""Step 2 coverage: completed transcript windows pass through Prompt Guard."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import transcript_buffer  # noqa: E402
from app.routers import transcripts  # noqa: E402


class FakePromptGuard:
    calls = []

    async def classify(self, text: str) -> dict:
        self.calls.append(text)
        return {
            "model": "meta-llama/Llama-Prompt-Guard-2-86M",
            "label": "benign",
            "is_safe": True,
            "score": 0.99,
            "scores": {"benign": 0.99},
        }


async def main() -> None:
    original_buffer = transcripts.buffer
    original_guard = transcripts.analyze_completed_window
    original_step3 = transcripts.save_and_post_draft_summary
    original_draft_path = transcripts.load_current_state
    fake_guard = FakePromptGuard()

    async def fake_analysis(window):
        return {
            "window": window,
            "security": await fake_guard.classify("\n".join(item["message"] for item in window["messages"])),
            "incident_state": {
                "status": "active",
                "severity": "unknown",
                "summary": None,
                "signals": [],
                "participants": ["Alex"],
                "transcript_message_count": 1,
                "last_updated": 0,
            },
        }

    test_buffer = transcript_buffer.TranscriptBuffer(window_seconds=0)
    transcripts.buffer = test_buffer
    transcripts.analyze_completed_window = fake_analysis

    async def fake_step3(analysis):
        return None

    transcripts.save_and_post_draft_summary = fake_step3
    transcripts.load_current_state = lambda meeting_id=None: {}
    try:
        result = await transcripts.add_transcript_message(
            transcripts.TranscriptMessageRequest(speaker="Alex", message="Database is unavailable")
        )
        assert result["accepted"] is True
        assert result["completed_window"] is not None
        assert result["analysis"]["security"]["model"] == "meta-llama/Llama-Prompt-Guard-2-86M"
        assert fake_guard.calls == ["Database is unavailable"]
    finally:
        transcripts.buffer = original_buffer
        transcripts.analyze_completed_window = original_guard
        transcripts.save_and_post_draft_summary = original_step3
        transcripts.load_current_state = original_draft_path
    print("PROMPT GUARD STEP 2 PASSED")


if __name__ == "__main__":
    asyncio.run(main())