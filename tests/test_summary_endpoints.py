"""Coverage for reading the persisted draft and final summary JSON files."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.integrations import step3_webhook, step4_report  # noqa: E402
from app.main import app  # noqa: E402


async def main() -> None:
    original_draft_path = step3_webhook.DRAFT_SUMMARY_PATH
    original_report_directory = step4_report.FINAL_REPORT_DIRECTORY
    draft_path = Path("temp") / "summary_endpoint_draft.json"
    report_directory = Path("temp") / "summary_endpoint_reports"
    draft_path.write_text(json.dumps({"segments": [{"segment_id": 1}]}), encoding="utf-8")
    report_directory.mkdir(parents=True, exist_ok=True)
    (report_directory / "inc_endpoint.json").write_text(
        json.dumps({"incident_id": "inc_endpoint", "report": "Resolved."}),
        encoding="utf-8",
    )
    step3_webhook.DRAFT_SUMMARY_PATH = draft_path
    step4_report.FINAL_REPORT_DIRECTORY = report_directory

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            draft_response = await client.get("/summaries/draft")
            final_response = await client.get("/summaries/final/inc_endpoint")
            missing_response = await client.get("/summaries/final/inc_missing")

        assert draft_response.status_code == 200
        assert draft_response.json()["segments"][0]["segment_id"] == 1
        assert final_response.status_code == 200
        assert final_response.json()["report"] == "Resolved."
        assert missing_response.status_code == 404
    finally:
        step3_webhook.DRAFT_SUMMARY_PATH = original_draft_path
        step4_report.FINAL_REPORT_DIRECTORY = original_report_directory
        draft_path.unlink(missing_ok=True)
        for path in report_directory.glob("*"):
            path.unlink()
        report_directory.rmdir() if report_directory.exists() else None

    print("SUMMARY ENDPOINTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())