from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import state

router = APIRouter()


@router.websocket("/ws/dashboard/{incident_id}")
async def dashboard_socket(websocket: WebSocket, incident_id: str):
    """The Next.js dashboard connects here for a live push feed. On connect it gets a
    snapshot of everything so far; after that, new timeline entries, tool results, and
    pending-action lifecycle events arrive as they happen. This socket is push-only -
    inbound messages are ignored (send REST calls to /incidents/... to act)."""
    incident = state.store.get(incident_id)
    if incident is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    incident.dashboard_sockets.add(websocket)
    try:
        await websocket.send_json({
            "type": "timeline.snapshot",
            "entries": [state.entry_to_dict(e) for e in incident.timeline],
            "pending_actions": [state.action_to_dict(a) for a in incident.pending_actions.values()],
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        incident.dashboard_sockets.discard(websocket)
