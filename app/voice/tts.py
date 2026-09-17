from __future__ import annotations

import logging
from pathlib import Path
import tempfile

from ..routers.bot_audio_input import push_tts_audio

logger = logging.getLogger("hyperion_warroom.tts")


async def speak_in_meeting(incident_id: str, text: str) -> None:
    """
    Convert text to speech via edge-tts and push audio
    into the bot audio input channel so it plays in the meeting.
    """
    if not text.strip():
        return

    try:
        import edge_tts

        temp_path: Path | None = None
        try:
            import miniaudio

            with tempfile.NamedTemporaryFile(prefix="sentinelvoice-", suffix=".mp3", delete=False) as audio_file:
                temp_path = Path(audio_file.name)
                communicate = edge_tts.Communicate(
                    text,
                    voice="en-US-AvaNeural",
                )
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio":
                        audio_file.write(chunk.get("data", b""))

            decoded = miniaudio.decode(
                temp_path.read_bytes(),
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=24000,
            )
            audio_bytes = decoded.samples.tobytes()
            if audio_bytes:
                await push_tts_audio(incident_id, audio_bytes)
                logger.info("[%s] edge-tts PCM audio pushed (%d bytes)", incident_id, len(audio_bytes))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    except ImportError:
        logger.warning("[%s] edge-tts library is not installed yet — skipping TTS", incident_id)
    except Exception as exc:
        logger.exception("[%s] edge-tts request failed: %s", incident_id, exc)


