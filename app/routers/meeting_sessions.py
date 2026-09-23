import json
import logging
import re
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import state
from ..config import settings
from ..integrations.meeting_baas import MeetingBaaSError, MeetingBaaSClient
from ..transcript_buffer import buffer
from .google_meet import create_open_meet_space

logger = logging.getLogger("hyperion_warroom.meeting_sessions")

router = APIRouter(prefix="/api/meeting-sessions", tags=["meeting-sessions"])


class CreateMeetingSessionRequest(BaseModel):
    name: str
    attendees: list[str] = Field(default_factory=list)


class StartBotRequest(BaseModel):
    bot_name: str = "Hypernion_Agent"


class CustomMeetingSessionRequest(BaseModel):
    meet_link: Optional[str] = Field(None, alias="meeting_url", description="Google Meet or meeting link to join directly")
    api_code: Optional[str] = Field(None, alias="api_token", description="Secret API code, must be 'test_hackathon_2026'")
    api_key: Optional[str] = Field(None, description="Secret API code alias")
    code: Optional[str] = Field(None, description="Secret API code alias")
    name: Optional[str] = Field(default="Custom Meeting Session", description="Incident / meeting session name")
    bot_name: Optional[str] = Field(default="Hyperion AI Agent", description="Name of the bot to appear in meeting")


@router.post("")
async def create_meeting_session(
    body: CreateMeetingSessionRequest,
    user_id: str = Query("default"),
):
    try:
        space = await run_in_threadpool(
            create_open_meet_space, user_id, body.name, body.attendees
        )
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc

    incident = await state.store.create(body.name, space["meetingUri"])
    incident.meet_space_name = space.get("name")
    return {
        "incident_id": incident.id,
        "name": incident.name,
        "meet_link": space["meetingUri"],
        "space_name": incident.meet_space_name,
        "access_type": space["config"]["accessType"],
        "status": incident.status,
    }


@router.post("/custom")
@router.post("/hackathon")
@router.post("/direct")
async def create_custom_meeting_session(
    request: Request,
    body: Optional[CustomMeetingSessionRequest] = None,
    meet_link: Optional[str] = Query(None),
    meeting_url: Optional[str] = Query(None),
    api_code: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
    api_token: Optional[str] = Query(None),
    code: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    bot_name: Optional[str] = Query(None),
    user_id: str = Query("default"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_api_code: Optional[str] = Header(None, alias="X-API-Code"),
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    authorization: Optional[str] = Header(None),
):
    # Try parsing json from request body directly if body is empty or malformed
    raw_dict = {}
    raw_text = ""
    try:
        raw_bytes = await request.body()
        raw_text = raw_bytes.decode("utf-8", errors="ignore")
        if raw_text.strip():
            try:
                raw_dict = json.loads(raw_text)
            except Exception:
                pass
    except Exception:
        pass

    # 1. Validate API code / key from headers, body, or query
    header_key = (
        request.headers.get("x-api-key")
        or request.headers.get("x-api-code")
        or request.headers.get("x-api-token")
        or request.headers.get("api-key")
        or request.headers.get("api-code")
    )
    provided_code = (
        (body.api_code if body else None)
        or (body.api_key if body else None)
        or (body.code if body else None)
        or raw_dict.get("api_code")
        or raw_dict.get("api_key")
        or raw_dict.get("api_token")
        or raw_dict.get("code")
        or raw_dict.get("key")
        or api_code
        or api_key
        or api_token
        or code
        or key
        or token
        or x_api_key
        or x_api_code
        or x_api_token
        or header_key
    )
    if not provided_code and authorization:
        if authorization.lower().startswith("bearer "):
            provided_code = authorization[7:].strip()
        else:
            provided_code = authorization.strip()

    if provided_code != "test_hackathon_2026":
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing API code. Expected 'test_hackathon_2026'",
        )

    # 2. Validate explicit Meet link
    provided_meet_link = (
        (body.meet_link if body and body.meet_link else None)
        or raw_dict.get("meet_link")
        or raw_dict.get("meeting_url")
        or raw_dict.get("meet_url")
        or raw_dict.get("meeting_link")
        or raw_dict.get("link")
        or meet_link
        or meeting_url
    )

    # Regex fallback if JSON was escaped oddly in Windows cmd
    if not provided_meet_link and raw_text:
        match = re.search(r"https?://[^\s\"\'\}\],]+", raw_text)
        if match:
            provided_meet_link = match.group(0)

    if not provided_meet_link or not str(provided_meet_link).strip():
        raise HTTPException(
            status_code=400,
            detail="Missing required field: 'meet_link'",
        )

    provided_meet_link = str(provided_meet_link).strip()

    session_name = (
        name
        or (body.name if body and body.name else None)
        or raw_dict.get("name")
        or raw_dict.get("incident_name")
        or "Custom Meeting Session"
    )
    session_bot_name = (
        bot_name
        or (body.bot_name if body and body.bot_name else None)
        or raw_dict.get("bot_name")
        or "Hyperion AI Agent"
    )

    # 3. Create incident record in state store (Google Meet creation bypassed)
    incident = await state.store.create(session_name, provided_meet_link.strip())

    state.record_timeline(
        incident=incident,
        kind="system",
        text=f"Custom session initialized with meet link: {provided_meet_link.strip()}",
        meta={"meet_link": provided_meet_link.strip(), "bot_name": session_bot_name},
    )

    # 4. Create and launch MeetingBaaS Bot directly
    try:
        bot = await MeetingBaaSClient().create_audio_bot(
            incident.meet_url,
            incident.id,
            session_bot_name,
        )
    except MeetingBaaSError as exc:
        logger.error("MeetingBaaS error launching bot: %s", exc)
        raise HTTPException(502, f"Failed to launch MeetingBaaS bot: {exc}") from exc
    except Exception as exc:
        logger.exception("Unexpected error launching bot: %s", exc)
        raise HTTPException(500, f"Unexpected error launching bot: {exc}") from exc

    await buffer.reset()
    incident.meeting_baas_bot_id = bot.bot_id
    incident.meeting_baas_status = "queued"

    return {
        "status": "success",
        "incident_id": incident.id,
        "name": incident.name,
        "meet_link": incident.meet_url,
        "bot_id": bot.bot_id,
        "bot_status": incident.meeting_baas_status,
        "stream_url": f"/ws/meeting-baas/{incident.id}",
        "callback_url": f"{settings.public_base_url.rstrip('/')}/ws/meeting-baas/{incident.id}",
        "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
        "reused": False,
    }


@router.post("/{incident_id}/bot")
async def start_meeting_bot(incident_id: str, body: StartBotRequest):
    incident = state.store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    if not incident.meet_url:
        raise HTTPException(400, "incident has no meeting URL")
    if incident.meeting_baas_bot_id and incident.meeting_baas_status not in {"failed", "completed"}:
        return {
            "incident_id": incident.id,
            "bot_id": incident.meeting_baas_bot_id,
            "status": incident.meeting_baas_status or "queued",
            "callback_url": f"{settings.public_base_url.rstrip('/')}/ws/meeting-baas/{incident.id}",
            "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
            "reused": True,
        }

    try:
        bot = await MeetingBaaSClient().create_audio_bot(
            incident.meet_url,
            incident.id,
            body.bot_name,
        )
    except MeetingBaaSError as exc:
        raise HTTPException(502, str(exc)) from exc

    await buffer.reset()
    incident.meeting_baas_bot_id = bot.bot_id
    incident.meeting_baas_status = "queued"
    return {
        "incident_id": incident.id,
        "bot_id": bot.bot_id,
        "status": incident.meeting_baas_status,
        "stream_url": f"/ws/meeting-baas/{incident.id}",
        "callback_url": f"{settings.public_base_url.rstrip('/')}/ws/meeting-baas/{incident.id}",
        "viewer_url": f"http://localhost:{settings.port}/live/{incident.id}",
        "reused": False,
    }
