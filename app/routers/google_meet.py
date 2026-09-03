from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from requests_oauthlib import OAuth2Session

from ..config import settings

router = APIRouter(prefix="/api/google", tags=["google-meet"])

TEMP_AUTH_DIR = Path("temp_auth")
TEMP_AUTH_DIR.mkdir(exist_ok=True)


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


class CreateMeetRequest(BaseModel):
    summary: str
    description: Optional[str] = None
    start_time: str
    end_time: str
    attendees: list[str]
    timezone: str = "UTC"


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
    
    # Keep concurrent login attempts separate so a second tab cannot overwrite the first state.
    oauth_flows = dict(request.session.get("google_oauth_flows", {}))
    oauth_flows[state] = {
        "scopes": scopes,
        "redirect_uri": settings.google_redirect_uri,
        "code_verifier": flow.code_verifier,
    }
    request.session["google_oauth_flows"] = oauth_flows
    
    return RedirectResponse(url=authorization_url)


@router.get("/callback")
async def google_callback(request: Request):
    returned_state = request.query_params.get("state")
    oauth_flows = request.session.get("google_oauth_flows", {})
    if not returned_state or returned_state not in oauth_flows:
        raise HTTPException(400, "OAuth flow state not found in session. Please restart the login process.")

    flow_state = oauth_flows[returned_state]
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": [settings.google_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    # Re-initialize the OAuth2Session with the original state token
    oauth2_session = OAuth2Session(
        client_id=client_config["web"]["client_id"],
        scope=flow_state["scopes"],
        redirect_uri=flow_state["redirect_uri"],
        state=returned_state,
    )
    
    # Construct Flow directly to inject the saved code_verifier without regenerating a new one
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

    # Clear this flow while leaving any other concurrent login attempt intact.
    remaining_flows = dict(oauth_flows)
    del remaining_flows[returned_state]
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

    # calendar.app.created scope requires creating your own calendar
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
    }