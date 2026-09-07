from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..incident_state import analyze_completed_window
from ..integrations.step3_webhook import save_and_post_draft_summary
from ..transcript_buffer import buffer

router = APIRouter(prefix="/transcripts", tags=["transcripts"])


class TranscriptMessageRequest(BaseModel):
    speaker: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.post("")
async def add_transcript_message(body: TranscriptMessageRequest):
    result = await buffer.add(body.speaker.strip(), body.message.strip())
    if result["completed_window"] is not None:
        result["analysis"] = await analyze_completed_window(result["completed_window"])
        result["step3"] = await save_and_post_draft_summary(result["analysis"])
    return result


@router.get("")
async def get_transcript_buffer():
    return await buffer.current()