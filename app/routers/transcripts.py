from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..incident_state import analyze_completed_window, update_incident_state
from ..integrations.step3_webhook import load_current_state, save_and_post_draft_summary
from ..transcript_buffer import buffer

router = APIRouter(prefix="/transcripts", tags=["transcripts"])


class TranscriptMessageRequest(BaseModel):
    speaker: str = Field(min_length=1)
    message: str = Field(min_length=1)
    meeting_id: str | None = None


async def process_completed_window(window: dict, meeting_id: str | None = None, client=None) -> dict:
    previous_state = load_current_state(meeting_id)
    if previous_state.get("segments"):
        analysis = await update_incident_state(previous_state, window, meeting_id)
    else:
        analysis = await analyze_completed_window(window)
        analysis["meeting_id"] = meeting_id
    if meeting_id is None and client is None:
        step3 = await save_and_post_draft_summary(analysis)
    else:
        step3 = await save_and_post_draft_summary(analysis, client=client, meeting_id=meeting_id)
    return {"analysis": analysis, "step3": step3}


@router.post("")
async def add_transcript_message(body: TranscriptMessageRequest):
    result = await buffer.add(body.speaker.strip(), body.message.strip())
    if result["completed_window"] is not None:
        result.update(await process_completed_window(result["completed_window"], body.meeting_id))
    return result


@router.get("")
async def get_transcript_buffer():
    return await buffer.current()