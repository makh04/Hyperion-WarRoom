from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


DRAFT_SUMMARY_PATH = Path("temp") / "draft_summary.json"
LIVE_WEBHOOK_URL = "http://localhost:8000/live/inc_74e2d8af93"


async def save_and_post_draft_summary(
    incident_json: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Persist the Step 2 state and deliver the same JSON to the live webhook."""
    DRAFT_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRAFT_SUMMARY_PATH.write_text(
        json.dumps(incident_json, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20)
    try:
        response = await http_client.post(LIVE_WEBHOOK_URL, json=incident_json)
    except httpx.HTTPError as exc:
        raise RuntimeError("Step 3 webhook request failed") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    response.raise_for_status()
    return {
        "saved_path": str(DRAFT_SUMMARY_PATH),
        "webhook_url": LIVE_WEBHOOK_URL,
        "status_code": response.status_code,
    }