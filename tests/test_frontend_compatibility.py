from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state
from app.integrations import step3_webhook, step4_report
from app.main import app
from app.routers import google_meet, meeting_sessions


async def main():
    temporary_directory = Path("temp") / "frontend_compatibility_test"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    draft_path = temporary_directory / "draft.json"
    draft_path.write_text(json.dumps({"meeting_id": "inc-test", "segments": []}), encoding="utf-8")
    report_directory = temporary_directory / "reports"
    report_directory.mkdir()
    (report_directory / "inc-test.json").write_text(
        json.dumps({"incident_id": "inc-test", "report": "resolved"}), encoding="utf-8"
    )
    original_draft_path = step3_webhook.DRAFT_SUMMARY_PATH
    original_report_directory = step4_report.FINAL_REPORT_DIRECTORY
    original_loader = google_meet._load_credentials
    step3_webhook.DRAFT_SUMMARY_PATH = draft_path
    step4_report.FINAL_REPORT_DIRECTORY = report_directory
    google_meet._load_credentials = lambda user_id="default": object()
    try:
        incident = await state.store.create("Compatibility test", "https://meet.google.com/test")
        incident.meeting_baas_bot_id = "existing-bot"
        incident.meeting_baas_status = "queued"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            status = await client.get("/api/google/status")
            draft = await client.get("/summaries/draft")
            final = await client.get("/summaries/final/inc-test")
            bot = await client.post(f"/api/meeting-sessions/{incident.id}/bot")
            empty_bot = await client.post(f"/api/meeting-sessions/{incident.id}/bot", json={})

        assert status.status_code == 200
        assert status.json()["authenticated"] is True
        assert draft.json()["meeting_id"] == "inc-test"
        assert final.json()["report"] == "resolved"
        assert bot.status_code == 200
        assert bot.json()["reused"] is True
        assert empty_bot.status_code == 200
        assert empty_bot.json()["reused"] is True
        print("FRONTEND COMPATIBILITY PASSED")
    finally:
        step3_webhook.DRAFT_SUMMARY_PATH = original_draft_path
        step4_report.FINAL_REPORT_DIRECTORY = original_report_directory
        google_meet._load_credentials = original_loader
        for path in report_directory.glob("*"):
            path.unlink()
        report_directory.rmdir()
        draft_path.unlink()
        temporary_directory.rmdir()


if __name__ == "__main__":
    asyncio.run(main())