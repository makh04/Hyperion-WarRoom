from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import state
from ..config import settings
from ..voice.streaming_transcriber import StreamingTranscriber

logger = logging.getLogger("sentinelvoice.meeting_stream")
router = APIRouter()


def _speaker_name(message: Any) -> str | None:
    if isinstance(message, dict):
        if message.get("isSpeaking") and message.get("name"):
            return str(message["name"])
        for key in ("speaker_name", "speakerName", "participant_name", "participantName", "speaker_label"):
            if message.get(key):
                return str(message[key])
        speaker = message.get("speaker")
        if isinstance(speaker, str) and speaker:
            return speaker
        return None
    if isinstance(message, list):
        for speaker in message:
            if isinstance(speaker, dict) and speaker.get("isSpeaking") and speaker.get("name"):
                return str(speaker["name"])
    return None


def _assemblyai_named_speaker(event: dict) -> str | None:
    for key in ("speaker_name", "speakerName", "participant_name", "participantName"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _resolve_speaker(incident: state.Incident, event: dict) -> tuple[str | None, str | None]:
    """Prefer actual participant names, then fall back to AssemblyAI labels."""
    named_speaker = _assemblyai_named_speaker(event)
    if named_speaker:
        return named_speaker, "assemblyai"
    meeting_baas_speaker = state.attribute_speaker(incident)
    if meeting_baas_speaker:
        return meeting_baas_speaker, "meeting_baas"
    event_speaker = _speaker_name(event)
    if event_speaker:
        return event_speaker, "assemblyai"
    return None, None


async def _handle_transcript(incident: state.Incident, event: dict) -> None:
    if event.get("type") != "Turn":
        if event.get("type") in {"Error", "error"}:
            print(
                f"[AssemblyAI error] {event.get('message', 'streaming transcription failed')}",
                flush=True,
            )
            await state.broadcast(incident, {
                "type": "agent.error",
                "code": event.get("code", "assemblyai_streaming_error"),
                "message": event.get("message", "AssemblyAI streaming transcription failed"),
            })
        return

    text = (event.get("transcript") or "").strip()
    if not text:
        return
    is_final = bool(event.get("end_of_turn"))
    print(
        f"[{incident.id}] {'FINAL' if is_final else 'LIVE'}: {text}",
        flush=True,
    )
    speaker, speaker_source = _resolve_speaker(incident, event)
    payload = {
        "type": "transcript.delta",
        "text": text,
        "final": is_final,
        "speaker": speaker,
        "speaker_source": speaker_source,
    }
    await state.broadcast(incident, payload)
    if not is_final:
        return

    entry = state.record_timeline(
        incident,
        "transcript.user",
        text,
        speaker=speaker,
        meta={
            "source": "meeting_baas",
            "speaker_source": speaker_source,
            "utterance_start": event.get("utteranceStart", event.get("utterance_start")),
            "utterance_end": event.get("utteranceEnd", event.get("utterance_end")),
            "confidence": event.get("confidence"),
        },
    )
    await state.broadcast(incident, {"type": "timeline.entry", "entry": state.entry_to_dict(entry)})


@router.websocket("/ws/meeting-baas/{incident_id}")
async def meeting_baas_stream(websocket: WebSocket, incident_id: str):
    incident = state.store.get(incident_id)
    if incident is None or not incident.meeting_baas_bot_id:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    transcriber = StreamingTranscriber(lambda event: _handle_transcript(incident, event))
    incident.transcription_session = transcriber
    handshake_seen = False
    try:
        await transcriber.connect()
        incident.meeting_baas_status = "in_call_recording"
        print(f"[{incident_id}] MeetingBaaS audio connected; live AssemblyAI transcription started", flush=True)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                audio = message["bytes"]
                if not handshake_seen:
                    logger.warning("Ignoring audio before MeetingBaaS handshake for %s", incident_id)
                    continue
                if len(audio) % 2:
                    logger.warning("Ignoring odd-length MeetingBaaS audio frame for %s", incident_id)
                    continue
                await transcriber.send_audio(audio)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed MeetingBaaS control message for %s", incident_id)
                continue
            if isinstance(control, dict) and "protocol_version" in control:
                if control.get("bot_id") and control["bot_id"] != incident.meeting_baas_bot_id:
                    await websocket.close(code=4403)
                    return
                sample_rate = control.get("sample_rate")
                if sample_rate and sample_rate != settings.streaming_sample_rate:
                    await websocket.close(code=4400)
                    return
                handshake_seen = True
                continue
            speaker = _speaker_name(control)
            if speaker:
                state.set_active_speaker(incident, speaker)
        if not handshake_seen:
            logger.warning("MeetingBaaS stream closed without a handshake for %s", incident_id)
    except WebSocketDisconnect:
        logger.info("MeetingBaaS bot disconnected for incident %s", incident_id)
    except Exception:
        logger.exception("MeetingBaaS stream failed for incident %s", incident_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        await transcriber.close()
        if incident.transcription_session is transcriber:
            incident.transcription_session = None
        incident.meeting_baas_status = "disconnected"
