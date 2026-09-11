from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..integrations import step3_webhook, step4_report

router = APIRouter(prefix="/summaries", tags=["summaries"])


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "summary not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "summary could not be read") from exc


@router.get("/draft")
async def get_draft_summary():
    return _read_json(step3_webhook.DRAFT_SUMMARY_PATH)


@router.get("/final/{incident_id}")
async def get_final_summary(incident_id: str):
    json_path = step4_report.FINAL_REPORT_DIRECTORY / f"{incident_id}.json"
    if json_path.exists():
        return _read_json(json_path)

    text_path = step4_report.FINAL_REPORT_DIRECTORY / f"{incident_id}.txt"
    try:
        report = text_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(404, "summary not found") from exc
    except OSError as exc:
        raise HTTPException(500, "summary could not be read") from exc
    return {"incident_id": incident_id, "report": report}