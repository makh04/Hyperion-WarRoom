from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import state
from ..tools import dispatcher

router = APIRouter(prefix="/incidents", tags=["incidents"])


class CreateIncidentRequest(BaseModel):
    name: str
    meet_url: Optional[str] = None


class ApproveActionResponse(BaseModel):
    action_id: str
    status: str
    result: Optional[dict] = None


@router.post("")
async def create_incident(body: CreateIncidentRequest):
    incident = await state.store.create(body.name, body.meet_url)
    return {
        "incident_id": incident.id,
        "name": incident.name,
        "status": incident.status,
        "created_at": incident.created_at,
    }


@router.get("")
async def list_incidents():
    return [
        {"incident_id": i.id, "name": i.name, "status": i.status, "created_at": i.created_at}
        for i in state.store.list()
    ]


@router.get("/{incident_id}")
async def get_incident(incident_id: str):
    incident = state.store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return {
        "incident_id": incident.id,
        "name": incident.name,
        "status": incident.status,
        "created_at": incident.created_at,
        "meet_url": incident.meet_url,
        "session_ready": bool(incident.agent_session and incident.agent_session.ready),
    }


@router.get("/{incident_id}/timeline")
async def get_timeline(incident_id: str):
    incident = state.store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return [state.entry_to_dict(e) for e in incident.timeline]


@router.get("/{incident_id}/pending-actions")
async def get_pending_actions(incident_id: str):
    incident = state.store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return [state.action_to_dict(a) for a in incident.pending_actions.values()]


@router.post("/{incident_id}/pending-actions/{action_id}/approve", response_model=ApproveActionResponse)
async def approve_pending_action(incident_id: str, action_id: str):
    try:
        result = await dispatcher.approve_and_execute(incident_id, action_id)
    except dispatcher.ToolError as exc:
        raise HTTPException(400, str(exc))
    incident = state.store.get(incident_id)
    action = incident.pending_actions[action_id]
    return ApproveActionResponse(action_id=action_id, status=action.status.value, result=result)


@router.post("/{incident_id}/resolve")
async def resolve_incident(incident_id: str):
    incident = state.store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    incident.status = "resolved"
    return {"incident_id": incident_id, "status": "resolved"}
