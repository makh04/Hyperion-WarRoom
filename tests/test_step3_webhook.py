"""Step 3 coverage for local persistence and the live webhook POST."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.integrations import step3_webhook  # noqa: E402
from app.main import app  # noqa: E402


async def main() -> None:
    payload = {
        "window": {
            "start_time": 100.0,
            "end_time": 280.0,
            "messages": [{"speaker": "Alex", "message": "Database is unavailable"}],
        },
        "security": {"model": "meta-llama/Llama-Prompt-Guard-2-86M", "label": "benign"},
        "incident_state": {"status": "active", "severity": "high"},
    }

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"received": True})

    original_path = step3_webhook.DRAFT_SUMMARY_PATH
    step3_webhook.DRAFT_SUMMARY_PATH = Path("temp") / "step3_webhook_test.json"
    try:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await step3_webhook.save_and_post_draft_summary(payload, client, "inc_test")

        saved = json.loads(step3_webhook.DRAFT_SUMMARY_PATH.read_text(encoding="utf-8"))
        assert saved["meeting_id"] == "inc_test"
        assert saved["segments"][0]["segment_id"] == 1
        assert saved["segments"][0]["start_time"] == 100.0
        assert saved["segments"][0]["conversation"] == payload["window"]["messages"]
        assert len(requests) == 1
        assert str(requests[0].url) == "http://localhost:8000/live/inc_test"
        assert json.loads(requests[0].content) == saved
        assert result["status_code"] == 200
        assert Path(result["saved_path"]).exists()
    finally:
        step3_webhook.DRAFT_SUMMARY_PATH.unlink(missing_ok=True)
        step3_webhook.DRAFT_SUMMARY_PATH = original_path

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        response = await client.post(step3_webhook.LIVE_WEBHOOK_URL, json=payload)
    assert response.status_code == 200
    assert response.json()["received"] is True
    print("STEP 3 WEBHOOK PASSED")


if __name__ == "__main__":
    asyncio.run(main())