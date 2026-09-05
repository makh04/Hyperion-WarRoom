from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .. import state

router = APIRouter()


@router.get("/live/{incident_id}", response_class=HTMLResponse)
async def live_transcript_page(incident_id: str):
        """Small local viewer for the live transcript WebSocket."""
        return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Live transcript {incident_id}</title>
<style>
body {{ background:#111827; color:#e5e7eb; font:16px system-ui,sans-serif; margin:0; }}
main {{ max-width:900px; margin:0 auto; padding:24px; }}
h1 {{ font-size:22px; }}
#status {{ color:#93c5fd; margin-bottom:18px; }}
#transcript {{ white-space:pre-wrap; line-height:1.6; background:#030712; padding:18px; min-height:360px; border-radius:8px; }}
.live {{ color:#9ca3af; }}
.final {{ color:#f9fafb; }}
.speaker {{ font-weight:600; color:#93c5fd; }}
.error {{ color:#fca5a5; }}
</style></head>
<body><main><h1>Live transcript: {incident_id}</h1>
<div id="status">Connecting to backend...</div><div id="transcript"></div></main>
<script>
const status = document.getElementById('status');
const output = document.getElementById('transcript');
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const socket = new WebSocket(protocol + '//' + location.host + '/ws/dashboard/{incident_id}');
socket.onopen = () => status.textContent = 'Connected. Waiting for MeetingBaaS audio...';
socket.onclose = () => status.textContent = 'Disconnected from backend.';
socket.onerror = () => status.textContent = 'WebSocket connection failed.';
socket.onmessage = (message) => {{
    const event = JSON.parse(message.data);
    if (event.type === 'timeline.snapshot') {{
        output.textContent = event.entries.map(entry =>
            (entry.kind === 'transcript.user' ? 'FINAL | ' : '')
            + (entry.kind === 'meeting.chat' ? 'CHAT | ' : '')
            + (entry.speaker || 'Unknown speaker') + ': ' + entry.text
        ).join('\\n');
    }} else if (event.type === 'transcript.delta') {{
        const line = document.createElement('div');
        line.className = event.final ? 'final' : 'live';
        line.textContent = (event.final ? 'FINAL | ' : 'LIVE | ')
            + (event.speaker || 'Unknown speaker') + ': ' + event.text;
        output.appendChild(line);
        output.scrollTop = output.scrollHeight;
        status.textContent = event.final ? 'Receiving transcript.' : 'Receiving live audio transcription...';
    }} else if (event.type === 'chat.message') {{
        const line = document.createElement('div');
        line.className = 'final';
        line.textContent = 'CHAT | ' + (event.sender || 'Unknown sender') + ': ' + event.text;
        output.appendChild(line);
        output.scrollTop = output.scrollHeight;
        status.textContent = 'Receiving meeting chat.';
    }} else if (event.type === 'agent.error') {{
        const line = document.createElement('div');
        line.className = 'error'; line.textContent = 'ERROR: ' + event.message;
        output.appendChild(line);
    }}
}};
</script></body></html>""")


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
