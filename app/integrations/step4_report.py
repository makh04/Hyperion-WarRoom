from __future__ import annotations

import json
import html
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from .. import state
from ..config import settings
from ..transcript_buffer import TranscriptBuffer


logger = logging.getLogger("sentinelvoice.reasoning_model")


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


def load_saved_final_report(incident_id: str) -> dict[str, Any] | None:
    path = FINAL_REPORT_DIRECTORY / f"{incident_id}.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalReportError("saved final report could not be read") from exc
    if not isinstance(document, dict) or not isinstance(document.get("report"), str):
        raise FinalReportError("saved final report is invalid")
    return {
        "saved_path": str(path),
        "summary_path": str(FINAL_REPORT_DIRECTORY / f"{incident_id}.txt"),
        "pdf_path": str(FINAL_REPORT_DIRECTORY / f"{incident_id}.pdf"),
        "report": document["report"],
        "model": document.get("model", settings.final_report_model),
    }


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
        "Create a readable SRE incident summary from the latest JSON summary in the source below. "
        "Return Markdown (not JSON and do not use markdown code fences). Use a clear title, "
        "bold section labels, and concise bullet lists where useful. Explain, "
        "where available: what happened, the main issues and topics, who worked on what, "
        "important decisions, suggested next steps, blockers, and the outcome or status. "
        "Do not invent facts; say when evidence is missing.\n\n"
        f"SOURCE JSON:\n{json.dumps(source, indent=2, sort_keys=True)}"
    )


def _parse_report(content: Any) -> str:
    if not isinstance(content, str) or not content.strip():
        raise FinalReportError("final report model returned non-text content")
    return content.strip()


def _markdown_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', escaped)
    return escaped


def _write_report_pdf(report: str, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=19, leading=24, alignment=TA_CENTER, textColor=colors.HexColor("#18324b"),
        spaceAfter=18,
    ))
    for level, size in ((2, 15), (3, 12), (4, 11), (5, 10), (6, 10)):
        styles.add(ParagraphStyle(
            name=f"ReportHeading{level}", parent=styles["Heading2"],
            fontName="Helvetica-Bold", fontSize=size, leading=size + 4,
            textColor=colors.HexColor("#18324b"), spaceBefore=10, spaceAfter=5,
        ))
    body_style = ParagraphStyle(
        name="ReportBody", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=10, leading=15, spaceAfter=7,
    )
    bullet_style = ParagraphStyle(
        name="ReportBullet", parent=body_style, leftIndent=18, firstLineIndent=-10,
    )

    content = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            text = " ".join(line.strip() for line in paragraph_lines)
            content.append(Paragraph(_markdown_inline(text), body_style))
            paragraph_lines.clear()

    for raw_line in report.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            style = styles["ReportTitle"] if level == 1 else styles[f"ReportHeading{level}"]
            content.append(Paragraph(_markdown_inline(heading.group(2)), style))
            continue
        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(.+)$", line)
        if bullet:
            flush_paragraph()
            content.append(Paragraph("&#8226; " + _markdown_inline(bullet.group(1)), bullet_style))
            continue
        paragraph_lines.append(line)
    flush_paragraph()

    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(path), pagesize=letter, rightMargin=0.75 * inch, leftMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch, title="Incident Summary",
    ).build(content)


from groq import AsyncGroq


async def synthesize_final_report(
    source: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not settings.final_report_api_key:
        logger.error("Reasoning model failed: final report API key is not configured")
        raise FinalReportError(
            "final report provider is not configured; set FINAL_REPORT_API_KEY or GROQ_API_KEY"
        )
    raw_base_url = (settings.final_report_base_url or "").rstrip("/")
    if raw_base_url.endswith("/chat/completions"):
        raw_base_url = raw_base_url[:-len("/chat/completions")].rstrip("/")
    if raw_base_url.endswith("/openai/v1"):
        raw_base_url = raw_base_url[:-len("/openai/v1")].rstrip("/")

    groq_client = AsyncGroq(
        api_key=settings.final_report_api_key,
        base_url=raw_base_url if raw_base_url else None,
        http_client=client,
    )
    try:
        response = await groq_client.chat.completions.create(
            model=settings.final_report_model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You are a meticulous incident commander writing factual post-incident reports.",
                },
                {"role": "user", "content": _report_prompt(source)},
            ],
        )
        content = response.choices[0].message.content
        return _parse_report(content)
    except Exception as exc:
        logger.exception("Reasoning model failed during provider request")
        raise FinalReportError(f"final report provider request failed: {exc}") from exc



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
    pdf_path = FINAL_REPORT_DIRECTORY / f"{incident.id}.pdf"
    _write_report_pdf(report, pdf_path)
    return {
        "saved_path": str(path),
        "summary_path": str(summary_path),
        "pdf_path": str(pdf_path),
        "report": report,
        "model": settings.final_report_model,
    }