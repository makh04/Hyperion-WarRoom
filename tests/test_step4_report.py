"""Step 4 coverage for CALL_ENDED synthesis and local report persistence."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import state  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations import step4_report  # noqa: E402
from app.routers import meeting_webhooks  # noqa: E402


REPORT = (
    "The checkout database became unavailable, causing an outage. "
    "Alex owned follow-up while the team approved a rollback. "
    "Service recovered with no remaining blockers."
)


async def main() -> None:
    incident = await state.store.create("Step 4 test")
    incident.meeting_baas_bot_id = "bot-step4"
    state.record_timeline(incident, "transcript.user", "Database became unavailable", speaker="Alex")

    original_draft_path = step4_report.DRAFT_SUMMARY_PATH
    original_report_directory = step4_report.FINAL_REPORT_DIRECTORY
    original_key = settings.final_report_api_key
    original_url = settings.final_report_base_url
    original_model = settings.final_report_model
    original_finalizer = meeting_webhooks.finalize_incident
    draft_path = Path("temp") / "step4_test_draft.json"
    report_directory = Path("temp") / "step4_test_reports"
    draft_path.write_text(json.dumps({
        "meeting_id": incident.id,
        "incident_state": {"severity": "high"},
        "segments": [{
            "segment_id": 1,
            "start_time": 1.0,
            "end_time": 181.0,
            "conversation": [{"speaker": "Alex", "message": "Database became unavailable"}],
        }, {
            "segment_id": 2,
            "start_time": 181.0,
            "end_time": 361.0,
            "conversation": [{"speaker": "Sam", "message": "Rollback approved"}],
        }],
    }), encoding="utf-8")
    step4_report.DRAFT_SUMMARY_PATH = draft_path
    step4_report.FINAL_REPORT_DIRECTORY = report_directory
    object.__setattr__(settings, "final_report_api_key", "test-key")
    object.__setattr__(settings, "final_report_base_url", "https://provider.test/v1")
    object.__setattr__(settings, "final_report_model", "configured-120b")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": REPORT}}]},
        )

    try:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            source = await step4_report.collect_final_report_input(
                incident,
                {"event": "CALL_ENDED", "data": {"bot_id": "bot-step4", "transcript": "full log"}},
            )
            result = await step4_report.finalize_incident(
                incident,
                {"event": "CALL_ENDED", "data": {"bot_id": "bot-step4", "transcript": "full log"}},
                client=client,
            )
        assert source["latest_step3_json_state"]["incident_state"]["severity"] == "high"
        assert source["full_local_timeline"][0]["text"] == "Database became unavailable"
        assert len(requests) == 1
        assert json.loads(requests[0].content)["model"] == "configured-120b"
        assert '"segment_id": 2' in json.loads(requests[0].content)["messages"][1]["content"]
        saved = json.loads(Path(result["saved_path"]).read_text(encoding="utf-8"))
        assert saved["report"] == REPORT
        assert [segment["segment_id"] for segment in saved["source"]["latest_step3_json_state"]["segments"]] == [1, 2]
        assert isinstance(saved["report"], str)
        assert Path(result["summary_path"]).read_text(encoding="utf-8").strip() == REPORT

        async def fake_finalize(*args, **kwargs):
            return {"saved_path": "temp/step4_test_reports/inc.json", "report": REPORT, "model": "configured-120b"}

        meeting_webhooks.finalize_incident = fake_finalize
        webhook_result = await meeting_webhooks.meeting_baas_webhook({
            "event": "CALL_ENDED",
            "data": {"bot_id": "bot-step4", "transcript": "full log"},
        })
        assert webhook_result["event"] == "CALL_ENDED"
        assert incident.status == "resolved"
        assert webhook_result["report"] == REPORT
    finally:
        step4_report.DRAFT_SUMMARY_PATH = original_draft_path
        step4_report.FINAL_REPORT_DIRECTORY = original_report_directory
        object.__setattr__(settings, "final_report_api_key", original_key)
        object.__setattr__(settings, "final_report_base_url", original_url)
        object.__setattr__(settings, "final_report_model", original_model)
        meeting_webhooks.finalize_incident = original_finalizer
        draft_path.unlink(missing_ok=True)
        for path in report_directory.glob("*"):
            path.unlink()
        report_directory.rmdir() if report_directory.exists() else None
    print("STEP 4 REPORT PASSED")


if __name__ == "__main__":
    asyncio.run(main())