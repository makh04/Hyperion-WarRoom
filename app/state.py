from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# How long a speaker.active event from the bot stays "current" for the purpose of
# attributing a transcript line to a speaker. Best-effort only.
SPEAKER_STALENESS_SECONDS = 12.0


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTED = "executed"


@dataclass
class PendingAction:
    id: str
    incident_id: str
    action_type: str
    target_resource: str
    confirmation_phrase: str
    status: ActionStatus = ActionStatus.PENDING
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    result: Optional[dict] = None


@dataclass
class TimelineEntry:
    id: str
    incident_id: str
    ts: float
    kind: str  # transcript.user | transcript.agent | tool.call | tool.result | decision | system
    speaker: Optional[str]
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class SpeakerWindow:
    name: str
    started_at: float


@dataclass
class Incident:
    id: str
    name: str
    meet_url: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    status: str = "active"  # active | resolved
    timeline: list = field(default_factory=list)
    pending_actions: dict = field(default_factory=dict)  # action_id -> PendingAction
    recent_speaker: Optional[SpeakerWindow] = None
    agent_session: Any = None  # set by voice.bridge; typed loosely to avoid a circular import
    meet_space_name: Optional[str] = None
    meeting_baas_bot_id: Optional[str] = None
    meeting_baas_status: Optional[str] = None
    transcription_session: Any = None
    dashboard_sockets: set = field(default_factory=set)


class IncidentStore:
    def __init__(self) -> None:
        self._incidents: dict = {}
        self._lock = asyncio.Lock()

    async def create(self, name: str, meet_url: Optional[str] = None) -> Incident:
        async with self._lock:
            incident_id = f"inc_{uuid.uuid4().hex[:10]}"
            incident = Incident(id=incident_id, name=name, meet_url=meet_url)
            self._incidents[incident_id] = incident
            return incident

    def get(self, incident_id: str) -> Optional[Incident]:
        return self._incidents.get(incident_id)

    def list(self) -> list:
        return list(self._incidents.values())


store = IncidentStore()


def record_timeline(
    incident: Incident,
    kind: str,
    text: str,
    speaker: Optional[str] = None,
    meta: Optional[dict] = None,
) -> TimelineEntry:
    entry = TimelineEntry(
        id=f"tl_{uuid.uuid4().hex[:8]}",
        incident_id=incident.id,
        ts=time.time(),
        kind=kind,
        speaker=speaker,
        text=text,
        meta=meta or {},
    )
    incident.timeline.append(entry)
    return entry


def set_active_speaker(incident: Incident, name: str) -> None:
    incident.recent_speaker = SpeakerWindow(name=name, started_at=time.time())


def attribute_speaker(incident: Incident) -> Optional[str]:
    """Best-effort: attach whichever speaker the bot last reported as active, as long as
    that report is recent. Google Meet audio is multi-speaker, and AssemblyAI's turn
    detection has no notion of "who" - this is our only source of speaker attribution."""
    sw = incident.recent_speaker
    if sw and (time.time() - sw.started_at) <= SPEAKER_STALENESS_SECONDS:
        return sw.name
    return None


async def broadcast(incident: Incident, payload: dict) -> None:
    """Push a JSON payload to every dashboard WebSocket connected for this incident."""
    dead = []
    for ws in list(incident.dashboard_sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        incident.dashboard_sockets.discard(ws)


def entry_to_dict(entry: TimelineEntry) -> dict:
    return {
        "id": entry.id,
        "ts": entry.ts,
        "kind": entry.kind,
        "speaker": entry.speaker,
        "text": entry.text,
        "meta": entry.meta,
    }


def action_to_dict(action: PendingAction) -> dict:
    return {
        "id": action.id,
        "action_type": action.action_type,
        "target_resource": action.target_resource,
        "status": action.status.value,
        "confirmation_phrase": action.confirmation_phrase,
        "created_at": action.created_at,
        "resolved_at": action.resolved_at,
        "result": action.result,
    }
