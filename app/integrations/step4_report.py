from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .. import state
from ..config import settings
from ..transcript_buffer import TranscriptBuffer


DRAFT_SUMMARY_PATH = Path("temp") / "draft_summary.json"
FINAL_REPORT_DIRECTORY = Path("temp") / "final_incident_reports"


class FinalReportError(RuntimeError):
    pass


def _read_latest_state() -> dict[str, Any]:
    if not DRAFT_SUMMARY_PATH.exists():
        return {}
    try:
        value = json.loads(DRAFT_SUMMARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalReportError("Step 3 draft state could not be read") from exc
    if not isinstance(value, dict):
        raise FinalReportError("Step 3 draft state is not a JSON object")
    return value


def _event_transcript(event: dict[str, Any]) -> Any:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("transcript", "transcript_log", "transcript_data", "log", "meeting_log"):
        if data.get(key) is not None:
            return data[key]
    return None


async def collect_final_report_input(
    incident: state.Incident,
    event: dict[str, Any],
    transcript_buffer: TranscriptBuffer | None = None,
) -> dict[str, Any]:
    buffer_snapshot = await transcript_buffer.current() if transcript_buffer else None
    return {
        "incident": {
            "id": incident.id,
            "name": incident.name,
            "status": incident.status,
            "meeting_baas_bot_id": incident.meeting_baas_bot_id,
        },
        "meeting_baas_end_event": event,
        "meeting_transcript_or_log": _event_transcript(event),
        "full_local_timeline": [state.entry_to_dict(entry) for entry in incident.timeline],
        "current_transcript_buffer": buffer_snapshot,
        "latest_step3_json_state": _read_latest_state(),
    }


def _report_prompt(source: dict[str, Any]) -> str:
    return (
        "Create a normal human-readable SRE incident report from the JSON source below. "
        "Return plain text only, not JSON, markdown code fences, or a JSON object. Explain, "
        "where available: what happened, the main issues and topics, who worked on what, "
        "important decisions, suggested next steps, blockers, and the outcome or status. "
        "Do not invent facts; say when evidence is missing.\n\n"
        f"SOURCE JSON:\n{json.dumps(source, indent=2, sort_keys=True)}"
    )


def _parse_report(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        raise FinalReportError("final report model returned non-text content")
    return content.strip()


async def synthesize_final_report(
    source: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not settings.final_report_api_key:
        raise FinalReportError(
            "final report provider is not configured; set FINAL_REPORT_API_KEY or GROQ_API_KEY"
        )
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=60)
    try:
        response = await http_client.post(
            f"{settings.final_report_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.final_report_api_key}"},
            json={
                "model": settings.final_report_model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a meticulous incident commander writing factual post-incident reports.",
                    },
                    {"role": "user", "content": _report_prompt(source)},
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return _parse_report(content)
    except httpx.HTTPError as exc:
        raise FinalReportError("final report provider request failed") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FinalReportError("final report provider returned an invalid response") from exc
    finally:
        if owns_client:
            await http_client.aclose()


async def finalize_incident(
    incident: state.Incident,
    event: dict[str, Any],
    transcript_buffer: TranscriptBuffer | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    source = await collect_final_report_input(incident, event, transcript_buffer)
    report = await synthesize_final_report(source, client)
    document = {
        "incident_id": incident.id,
        "model": settings.final_report_model,
        "provider": settings.final_report_base_url,
        "report": report,
        "source": source,
    }
    path = FINAL_REPORT_DIRECTORY / f"{incident.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path = FINAL_REPORT_DIRECTORY / f"{incident.id}.txt"
    summary_path.write_text(report + "\n", encoding="utf-8")
    return {
        "saved_path": str(path),
        "summary_path": str(summary_path),
        "report": report,
        "model": settings.final_report_model,
    }