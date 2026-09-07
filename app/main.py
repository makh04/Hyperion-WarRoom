from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .routers import dashboard_ws, incidents, google_meet, meeting_sessions, meeting_stream, meeting_webhooks, transcripts
from .voice import bridge
from .voice.provisioning import ensure_agent_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hyperion_warroom.main")

app = FastAPI(
    title="Hyperion WarRoom",
    description=(
        "Real-time SRE incident co-pilot backend: bridges Google Meet audio to AssemblyAI's "
        "Voice Agent API (reasoning via Groq) and exposes incident/timeline/confirmation REST APIs."
    ),
    version="0.2.0",
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


@app.on_event("startup")
async def _provision_agent_on_startup() -> None:
    """Provision (or reuse) the AssemblyAI stored agent up front, so the first real
    incident call doesn't pay that latency. Best-effort: if this fails (no key yet, no
    network in dev, etc.) we just retry lazily on the first WebSocket connection."""
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
