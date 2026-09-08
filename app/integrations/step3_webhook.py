from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

import httpx


DRAFT_SUMMARY_PATH = Path("temp") / "draft_summary.json"
LIVE_WEBHOOK_URL = "http://localhost:8000/live/inc_74e2d8af93"
LIVE_WEBHOOK_BASE_URL = "http://localhost:8000"
_DRAFT_LOCK = asyncio.Lock()


def _window_messages(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    window = analysis.get("window", {})
    return list(window.get("messages", window.get("conversation", [])))


def _segment_from_analysis(analysis: dict[str, Any], segment_id: int) -> dict[str, Any]:
    window = analysis.get("window", {})
    return {
        "segment_id": segment_id,
        "start_time": window.get("start_time", window.get("started_at")),
        "end_time": window.get("end_time"),
        "conversation": _window_messages(analysis),
        "analysis": {key: value for key, value in analysis.items() if key != "window"},
    }


def _load_history(meeting_id: str | None) -> dict[str, Any]:
    if not DRAFT_SUMMARY_PATH.exists():
        return {"meeting_id": meeting_id, "segments": []}
    try:
        value = json.loads(DRAFT_SUMMARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Step 3 draft state could not be read") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Step 3 draft state is not a JSON object")
    segments = value.get("segments")
    if isinstance(segments, list):
        value["meeting_id"] = value.get("meeting_id") or meeting_id
        return value
    legacy_segment = None
    if isinstance(value.get("window"), dict):
        legacy_segment = _segment_from_analysis(value, 1)
    return {
        "meeting_id": value.get("meeting_id") or meeting_id,
        "segments": [legacy_segment] if legacy_segment else [],
    }


def load_current_state(meeting_id: str | None = None) -> dict[str, Any]:
    return _load_history(meeting_id)


async def save_and_post_draft_summary(
    incident_json: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    """Append one completed Step 2 window and deliver the complete history."""
    async with _DRAFT_LOCK:
        history = _load_history(meeting_id)
        next_segment_id = incident_json.get("window", {}).get("segment_id") or len(history["segments"]) + 1
        segment = _segment_from_analysis(incident_json, next_segment_id)
        history["segments"].append(segment)
        accumulated_state = {
            key: value
            for key, value in incident_json.items()
            if key not in {"window", "meeting_id", "segments"}
        }
        history.update(accumulated_state)
        history["meeting_id"] = history.get("meeting_id") or meeting_id
        history["segments"] = history.get("segments", [])
        DRAFT_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DRAFT_SUMMARY_PATH.write_text(
            json.dumps(history, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20)
    webhook_url = (
        f"{LIVE_WEBHOOK_BASE_URL}/live/{history['meeting_id']}"
        if history.get("meeting_id")
        else LIVE_WEBHOOK_URL
    )
    try:
        response = await http_client.post(webhook_url, json=history)
    except httpx.HTTPError as exc:
        raise RuntimeError("Step 3 webhook request failed") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    response.raise_for_status()
    return {
        "saved_path": str(DRAFT_SUMMARY_PATH),
        "webhook_url": webhook_url,
        "status_code": response.status_code,
        "history": history,
    }