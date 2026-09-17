from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .routers import (
    dashboard_ws, incidents, google_meet, meeting_sessions,
    meeting_stream, meeting_webhooks, summaries, transcripts,
    tools, bot_audio_input,
)
from .voice import bridge
from .voice.provisioning import ensure_agent_id
from .agent_dispatch import set_agent_log_broadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hyperion_warroom.main")

app = FastAPI(
    title="Hyperion WarRoom",
    description=(
        "Real-time SRE incident co-pilot backend: bridges Google Meet audio to AssemblyAI's "
        "Voice Agent API (reasoning via Groq) and exposes incident/timeline/confirmation REST APIs."
    ),
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

app.include_router(incidents.router)
app.include_router(dashboard_ws.router)
app.include_router(bridge.router)
app.include_router(google_meet.router)
app.include_router(meeting_sessions.router)
app.include_router(meeting_stream.router)
app.include_router(meeting_webhooks.router)
app.include_router(transcripts.router)
app.include_router(summaries.router)
app.include_router(tools.router)
app.include_router(bot_audio_input.router)


@app.post("/")
async def root_webhook(event: dict[str, Any]):
    """Fallback route for MeetingBaaS webhooks hitting POST / directly."""
    return await meeting_webhooks.meeting_baas_webhook(event)


# ── Agent log WebSocket ───────────────────────────────────────────────────────
_agent_log_sockets: set[WebSocket] = set()


async def _broadcast_agent_log(payload: dict) -> None:
    dead = []
    for ws in list(_agent_log_sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _agent_log_sockets.discard(ws)


set_agent_log_broadcaster(_broadcast_agent_log)


@app.websocket("/ws/agent-log")
async def agent_log_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    _agent_log_sockets.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _agent_log_sockets.discard(websocket)


# ── Incidents list endpoint ───────────────────────────────────────────────────
from . import state as _state  # noqa: E402


@app.get("/api/incidents")
async def list_incidents() -> list[dict]:
    return [
        {
            "incident_id": inc.id,
            "name": inc.name,
            "status": inc.status,
            "meet_url": inc.meet_url,
            "bot_status": inc.meeting_baas_status,
            "bot_id": inc.meeting_baas_bot_id,
            "created_at": inc.created_at,
        }
        for inc in _state.store.list()
    ]


@app.on_event("startup")
async def _provision_agent_on_startup() -> None:
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/meeting-baas"
    logger.info("Server active - MeetingBaaS webhooks listening on: %s and %s/", webhook_url, settings.public_base_url.rstrip('/'))
    if not settings.assemblyai_api_key or not settings.use_stored_agent:
        return
    try:
        agent_id = await ensure_agent_id()
        logger.info(
            "Ready - using AssemblyAI agent_id=%s (llm=%s)",
            agent_id, "groq:" + settings.groq_model if settings.groq_api_key else "managed",
        )
    except Exception:
        logger.exception("Could not provision the AssemblyAI agent at startup; will retry on first call")



@app.get("/health")
async def health():
    return {
        "status": "ok",
        "assemblyai_key_configured": bool(settings.assemblyai_api_key),
        "reasoning_llm": f"groq:{settings.groq_model}" if settings.groq_api_key else "assemblyai-managed",
    }

