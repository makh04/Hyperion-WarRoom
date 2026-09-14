from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from requests_oauthlib import OAuth2Session
from uuid import uuid4

from ..config import settings

router = APIRouter(prefix="/api/google", tags=["google-meet"])

TEMP_AUTH_DIR = Path("temp_auth")
TEMP_AUTH_DIR.mkdir(exist_ok=True)
_oauth_flows: dict[str, dict] = {}

if settings.google_redirect_uri.startswith(("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def _credentials_path(user_id: str = "default") -> Path:
    return TEMP_AUTH_DIR / f"{user_id}_credentials.json"


def _save_credentials(credentials: Credentials, user_id: str = "default") -> None:
    _credentials_path(user_id).write_text(credentials.to_json(), encoding="utf-8")


def _load_credentials(user_id: str = "default") -> Optional[Credentials]:
    credentials_path = _credentials_path(user_id)
    if not credentials_path.exists():
        return None
    return Credentials.from_authorized_user_file(
        str(credentials_path), scopes=list(settings.google_scopes)
    )


def create_open_meet_space(
    user_id: str = "default",
    summary: str = "Hyperion WarRoom Incident",
    attendees: list[str] | None = None,
) -> dict:
    credentials = _load_credentials(user_id)
    if not credentials:
        raise PermissionError("Not authenticated. Visit /api/google/login first.")

    try:
        service = build("calendar", "v3", credentials=credentials)
        calendar = service.calendars().insert(
            body={"summary": "Hyperion WarRoom"}
        ).execute()
        calendar_id = calendar["id"]

        start = datetime.now(timezone.utc).replace(microsecond=0)
        
        # Configuring conferenceData with parameters/access type to be open if supported
        event = service.events().insert(
            calendarId=calendar_id,
            conferenceDataVersion=1,
            sendUpdates="all",
            body={
                "summary": summary,
                "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                "end": {
                    "dateTime": (start + timedelta(hours=1)).isoformat(),
                    "timeZone": "UTC",
                },
                "attendees": [{"email": email} for email in (attendees or [])],
                "conferenceData": {
                    "createRequest": {
                        "requestId": f"war-room-{uuid4().hex}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                        "parameters": {
                            "conferenceAccess": "OPEN"
                        }
                    }
                },
            },
        ).execute()
    except HttpError as exc:
        detail = getattr(exc, "_get_reason", lambda: str(exc))()
        raise RuntimeError(f"Google Calendar meeting creation failed: {detail}") from exc

    meeting_uri = next(
        (
            entry.get("uri")
            for entry in event.get("conferenceData", {}).get("entryPoints", [])
            if entry.get("entryPointType") == "video"
        ),
        None,
    )
    if not meeting_uri:
        raise RuntimeError("Google Calendar did not return a Meet link")

    return {
        "name": event.get("id"),
        "meetingUri": meeting_uri,
        "config": {"accessType": "OPEN"},
        "calendarId": calendar_id,
    }


class CreateMeetRequest(BaseModel):
    summary: str
    description: Optional[str] = None
    start_time: str
    end_time: str
    attendees: list[str]
    timezone: str = "UTC"
    access_type: str = "OPEN"  # Added option to control access type


@router.get("/login")
async def google_login(request: Request):
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": [settings.google_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    scopes = list(settings.google_scopes)
    
    flow = Flow.from_client_config(
        client_config,
        scopes=scopes,
        redirect_uri=settings.google_redirect_uri,
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    
    oauth_flows = dict(request.session.get("google_oauth_flows", {}))
    oauth_flows[state] = {
        "scopes": scopes,
        "redirect_uri": settings.google_redirect_uri,
        "code_verifier": flow.code_verifier,
    }
    request.session["google_oauth_flows"] = oauth_flows
    _oauth_flows[state] = oauth_flows[state]
    
    return RedirectResponse(url=authorization_url)


@router.get("/status")
async def google_auth_status():
    credentials = _load_credentials()
    if credentials is None:
        return {"logged_in": False}

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(GoogleAuthRequest())
            _save_credentials(credentials)
        except Exception:
            return {"logged_in": False}

    return {"logged_in": bool(credentials.valid)}


@router.get("/callback")
async def google_callback(request: Request):
    returned_state = request.query_params.get("state")
    session_flows = request.session.get("google_oauth_flows", {})
    flow_state = _oauth_flows.pop(returned_state, None) if returned_state else None
    if flow_state is None and returned_state:
        flow_state = session_flows.get(returned_state)
    if flow_state is None:
        raise HTTPException(400, "OAuth flow state not found in session. Please restart the login process.")

    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": [settings.google_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    oauth2_session = OAuth2Session(
        client_id=client_config["web"]["client_id"],
        scope=flow_state["scopes"],
        redirect_uri=flow_state["redirect_uri"],
        state=returned_state,
    )
    
    flow = Flow(
        oauth2session=oauth2_session,
        client_type="web",
        client_config=client_config,
        redirect_uri=flow_state["redirect_uri"],
        code_verifier=flow_state["code_verifier"],
        autogenerate_code_verifier=False,
    )
    
    flow.fetch_token(authorization_response=str(request.url))
    credentials = flow.credentials
    _save_credentials(credentials)

    remaining_flows = dict(session_flows)
    remaining_flows.pop(returned_state, None)
    if remaining_flows:
        request.session["google_oauth_flows"] = remaining_flows
    else:
        del request.session["google_oauth_flows"]

    return {"message": "Authentication successful. Tokens saved."}


@router.post("/create-meet")
async def create_meet(body: CreateMeetRequest, user_id: str = Query("default")):
    credentials = _load_credentials(user_id)
    if not credentials:
        raise HTTPException(401, "Not authenticated. Visit /api/google/login first.")

    service = build("calendar", "v3", credentials=credentials)

    new_calendar = service.calendars().insert(body={"summary": "Hyperion WarRoom"}).execute()
    calendar_id = new_calendar["id"]

    event = {
        "summary": body.summary,
        "description": body.description,
        "start": {"dateTime": body.start_time, "timeZone": body.timezone},
        "end": {"dateTime": body.end_time, "timeZone": body.timezone},
        "attendees": [{"email": email} for email in body.attendees],
        "conferenceData": {
            "createRequest": {
                "requestId": f"meet-{body.start_time}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "parameters": {
                    "conferenceAccess": body.access_type  # Allows setting access dynamically (e.g., "OPEN")
                }
            }
        },
    }

    created_event = (
        service.events()
        .insert(
            calendarId=calendar_id,
            body=event,
            conferenceDataVersion=1,
            sendUpdates="all",
        )
        .execute()
    )

    meet_link = None
    if created_event.get("conferenceData", {}).get("entryPoints"):
        for entry in created_event["conferenceData"]["entryPoints"]:
            if entry.get("entryPointType") == "video":
                meet_link = entry.get("uri")
                break

    return {
        "event_id": created_event.get("id"),
        "meet_link": meet_link,
        "html_link": created_event.get("htmlLink"),
        "calendar_id": calendar_id,
        "status": created_event.get("status"),
        "access_type": body.access_type,
    }