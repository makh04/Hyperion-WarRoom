from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from ..integrations import step3_webhook, step4_report


router = APIRouter(prefix="/summaries", tags=["summaries"])
_INCIDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _read_json(path: Path, missing_message: str) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(404, missing_message)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "summary JSON could not be read") from exc
    if not isinstance(value, dict):
        raise HTTPException(500, "summary JSON must be an object")
    return value


@router.get("/draft")
async def get_draft_summary() -> dict[str, Any]:
    return _read_json(step3_webhook.DRAFT_SUMMARY_PATH, "draft summary is not available")


@router.get("/final/{incident_id}")
async def get_final_summary(incident_id: str) -> dict[str, Any]:
    if not _INCIDENT_ID_PATTERN.fullmatch(incident_id):
        raise HTTPException(400, "invalid incident id")
    return _read_json(
        step4_report.FINAL_REPORT_DIRECTORY / f"{incident_id}.json",
        "final summary is not available",
    )