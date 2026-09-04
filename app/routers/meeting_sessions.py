from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import state
from ..config import settings
from ..integrations.meeting_baas import MeetingBaaSError, MeetingBaaSClient
from .google_meet import create_open_meet_space

router = APIRouter(prefix="/api/meeting-sessions", tags=["meeting-sessions"])


class CreateMeetingSessionRequest(BaseModel):
    name: str


class StartBotRequest(BaseModel):
    bot_name: str = "SentinelVoice"


@router.post("")
async def create_meeting_session(
    body: CreateMeetingSessionRequest,
    user_id: str = Query("default"),
):
    try:
        space = await run_in_threadpool(create_open_meet_space, user_id, body.name)
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
