from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional
import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import state
from ..config import settings
from ..integrations.meeting_baas import MeetingBaaSClient, MeetingBaaSError
from ..transcript_buffer import buffer
from .google_meet import create_open_meet_space

logger = logging.getLogger("hyperion_warroom.onboarding")

router = APIRouter(tags=["onboarding"])

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)
ONBOARDING_FILE = TEMP_DIR / "onboarding_info.json"


class DeveloperInfo(BaseModel):
    email: str
    role: str
    name: Optional[str] = None


class OnboardingSetupRequest(BaseModel):
    service_name: str
    service_summary: str
    developers: List[DeveloperInfo] = Field(..., min_items=1)


def _load_onboarding_data() -> dict[str, Any]:
    if not ONBOARDING_FILE.exists():
        return {
            "service_name": "Default Service",
            "service_summary": "No service summary configured yet.",
            "developers": [],
        }
    try:
        return json.loads(ONBOARDING_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read onboarding file: %s", exc)
        return {
            "service_name": "Default Service",
            "service_summary": "Error reading saved service summary.",
            "developers": [],
        }


def _save_onboarding_data(data: dict[str, Any]) -> None:
    TEMP_DIR.mkdir(exist_ok=True)
    ONBOARDING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


@router.post("/api/onboarding")
async def save_onboarding_setup(body: OnboardingSetupRequest):
    data = {
        "service_name": body.service_name.strip(),
        "service_summary": body.service_summary.strip(),
        "developers": [dev.dict() for dev in body.developers],
    }
    _save_onboarding_data(data)
    logger.info("Onboarding information saved to %s", ONBOARDING_FILE)
    return {
        "status": "success",
        "message": "Onboarding setup saved to temp folder",
        "file_path": str(ONBOARDING_FILE),
        "data": data,
    }


@router.get("/api/onboarding")
async def get_onboarding_setup():
    return _load_onboarding_data()


async def _consult_ai_for_incident(
    service_info: dict[str, Any],
    live_request_body: dict[str, Any],
) -> dict[str, Any]:
    """Uses Groq AI (via GROQ_ONBOARDING_API) to analyze error, pick developers to invite,
    and formulate incident summary."""
    api_key = settings.groq_onboarding_api or settings.groq_api_key
    devs = service_info.get("developers", [])
    dev_emails = [d.get("email") for d in devs if d.get("email")]

    if not api_key:
        logger.info("GROQ_ONBOARDING_API key not set, using default developer selection")
        return {
            "incident_name": f"Incident: {service_info.get('service_name', 'Live Site Error')}",
            "invited_developers": dev_emails,
            "ai_reasoning": "Groq API key not configured. Invited all available developers from roster.",
            "severity": "HIGH",
        }

    try:
        from groq import AsyncGroq

        raw_base_url = (settings.groq_base_url or "").rstrip("/")
        if raw_base_url.endswith("/chat/completions"):
            raw_base_url = raw_base_url[:-len("/chat/completions")].rstrip("/")
        if raw_base_url.endswith("/openai/v1"):
            raw_base_url = raw_base_url[:-len("/openai/v1")].rstrip("/")

        client = AsyncGroq(
            api_key=api_key,
            base_url=raw_base_url if raw_base_url else None,
        )

        prompt = (
            "You are an automated SRE AI Commander.\n"
            "Analyze the incoming live website request/error details along with the service summary "
            "and developer roster to decide which developers should be invited to an emergency meeting.\n\n"
            f"SERVICE NAME: {service_info.get('service_name')}\n"
            f"SERVICE SUMMARY: {service_info.get('service_summary')}\n"
            f"DEVELOPER ROSTER: {json.dumps(devs, indent=2)}\n\n"
            f"LIVE WEBSITE EVENT/ERROR DETAILS:\n{json.dumps(live_request_body, indent=2)}\n\n"
            "Respond ONLY with valid JSON in the following format:\n"
            "{\n"
            '  "incident_name": "<short incident title>",\n'
            '  "invited_developers": ["<email1>", "<email2>"],\n'
            '  "ai_reasoning": "<explanation of why these developers were picked>",\n'
            '  "severity": "CRITICAL|HIGH|MEDIUM|LOW"\n'
            "}"
        )

        response = await client.chat.completions.create(
            model=settings.groq_model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": "You are a senior incident response triage AI."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        parsed = json.loads(content)
        if "invited_developers" not in parsed or not isinstance(parsed["invited_developers"], list):
            parsed["invited_developers"] = dev_emails
        return parsed
    except Exception as exc:
        logger.exception("AI evaluation failed, falling back to all developers")
        return {
            "incident_name": f"Incident: {service_info.get('service_name', 'Live Error')}",
            "invited_developers": dev_emails,
            "ai_reasoning": f"AI Triage fallback due to error: {exc}",
            "severity": "HIGH",
        }


@router.post("/api/live-request")
async def handle_live_website_request(
    request: Request,
    user_id: str = Query("default"),
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    # 1. Verify API Token from .env
    provided_token = x_api_token or token
    if not provided_token and authorization:
        if authorization.lower().startswith("bearer "):
            provided_token = authorization[7:].strip()
        else:
            provided_token = authorization.strip()

    expected_token = settings.live_website_api_token
    if not provided_token or provided_token != expected_token:
        logger.warning("Unauthorized live request attempt. Provided token: %s", provided_token)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing API token",
        )

    # 2. Parse live request body
    try:
        body = await request.json()
    except Exception:
        body = {"raw_text": (await request.body()).decode("utf-8", errors="ignore")}

    # 3. Read saved onboarding service summary & developer info from temp folder
    service_info = _load_onboarding_data()

    # 4. LLM reasoning: analyze error, decide title and pick needed developers
    ai_result = await _consult_ai_for_incident(service_info, body)
    invited_emails = ai_result.get("invited_developers", [])
    incident_name = ai_result.get("incident_name", "Live Website Incident")

    logger.info("AI selected incident name '%s' and attendees: %s", incident_name, invited_emails)

    # 5. Create Google Meet session (equivalent to POST /api/meeting-sessions)
    meet_url = None
    space_name = None
    access_type = "OPEN"
    try:
        space = await run_in_threadpool(
            create_open_meet_space, user_id, incident_name, invited_emails
        )
        meet_url = space.get("meetingUri")
        space_name = space.get("name")
        access_type = space.get("config", {}).get("accessType", "OPEN")
        logger.info("Google Meet created: %s", meet_url)
    except Exception as exc:
        logger.warning("Google Meet auto-creation failed (%s); generating fallback Meet link", exc)
        meet_url = f"https://meet.google.com/war-room-{uuid.uuid4().hex[:8]}"

    # 6. Store incident
    incident = await state.store.create(incident_name, meet_url)
    incident.meet_space_name = space_name

    state.record_timeline(
        incident=incident,
        kind="system",
        text=f"Incident auto-created. AI invited: {', '.join(invited_emails)}",
        meta={"ai_analysis": ai_result, "request_body": body},
    )

    # 7. Start the AI Bot into the meeting (equivalent to POST /api/meeting-sessions/{incident_id}/bot)
    bot_name = "Hyperion AI Agent"
    bot_data: dict[str, Any] = {}
    try:
        bot = await MeetingBaaSClient().create_audio_bot(
            incident.meet_url,
            incident.id,
            bot_name,
        )
        await buffer.reset()
        incident.meeting_baas_bot_id = bot.bot_id
        incident.meeting_baas_status = "queued"

        bot_data = {
            "incident_id": incident.id,
            "bot_id": bot.bot_id,
            "status": incident.meeting_baas_status,
            "stream_url": f"/ws/meeting-baas/{incident.id}",
            "callback_url": f"{settings.public_base_url.rstrip('/')}/ws/meeting-baas/{incident.id}",
            "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
            "reused": False,
        }
        logger.info("MeetingBaaS bot %s launched for incident %s", bot.bot_id, incident.id)
    except MeetingBaaSError as exc:
        logger.warning("MeetingBaaS bot launch failed: %s", exc)
        bot_data = {
            "incident_id": incident.id,
            "bot_id": None,
            "status": "failed",
            "error": str(exc),
            "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
        }
    except Exception as exc:
        logger.exception("Unexpected error launching bot: %s", exc)
        bot_data = {
            "incident_id": incident.id,
            "bot_id": None,
            "status": "failed",
            "error": str(exc),
            "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
        }

    return {
        "status": "success",
        "verified": True,
        "incident_id": incident.id,
        "name": incident.name,
        "meet_link": incident.meet_url,
        "space_name": incident.meet_space_name,
        "access_type": access_type,
        "invited_developers": invited_emails,
        "ai_analysis": ai_result,
        "bot": bot_data,
    }


@router.get("/onboarding.html")
@router.get("/onboarding")
async def serve_onboarding_html():
    path = Path("frontend/onboarding.html")
    if not path.exists():
        raise HTTPException(404, "onboarding.html not found")
    return FileResponse(str(path))
