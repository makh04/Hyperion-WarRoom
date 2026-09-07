from __future__ import annotations

import logging
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _google_scopes() -> tuple[str, ...]:
    calendar_scope = "https://www.googleapis.com/auth/calendar.app.created"
    return (calendar_scope,)


@dataclass(frozen=True)
class Settings:
    # --- AssemblyAI ---
    assemblyai_api_key: str = os.getenv("ASSEMBLYAI_API_KEY", "")
    assemblyai_streaming_model: str = os.getenv("ASSEMBLYAI_STREAMING_MODEL", "universal-3-5-pro")
    streaming_sample_rate: int = int(os.getenv("STREAMING_SAMPLE_RATE", "24000"))
    # Optional: point at an agent you already created yourself (skips auto-provisioning).
    assemblyai_agent_id: str = os.getenv("ASSEMBLYAI_AGENT_ID", "")
    agent_voice: str = os.getenv("AGENT_VOICE", "anna")

    # --- Reasoning LLM (Groq) ---
    # AssemblyAI's Voice Agent API can call any OpenAI-compatible chat-completions endpoint
    # as the agent's reasoning LLM instead of its own managed model. Groq exposes exactly
    # that at https://api.groq.com/openai/v1 and is fast + cheap, which is why it's the
    # default reasoning brain here. Leave GROQ_API_KEY unset to fall back to AssemblyAI's
    # managed model with zero code changes.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    final_report_api_key: str = os.getenv("FINAL_REPORT_API_KEY", os.getenv("GROQ_API_KEY", ""))
    final_report_base_url: str = os.getenv("FINAL_REPORT_BASE_URL", os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    final_report_model: str = os.getenv("FINAL_REPORT_MODEL", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))

    # --- Transcript security classification ---
    prompt_guard_model: str = os.getenv(
        "PROMPT_GUARD_MODEL", "meta-llama/Llama-Prompt-Guard-2-86M"
    )

    # --- Cloud infra tools ---
    use_mock_cloud: bool = _bool_env("USE_MOCK_CLOUD", True)

    # --- Google OAuth ---
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/callback")
    google_scopes: tuple = _google_scopes()

    # --- MeetingBaaS ---
    meeting_baas_api_key: str = os.getenv("MEETING_BAAS_KEY", "")
    meeting_baas_api_base_url: str = os.getenv("MEETING_BAAS_API_BASE_URL", "https://api.meetingbaas.com/v2")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

    # --- Server ---
    cors_origins: tuple = tuple(
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
    )
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    # --- Session Management ---
    secret_key: str = os.getenv("SECRET_KEY", "your-super-secret-key")

    @property
    def use_stored_agent(self) -> bool:
        """Whether a session binds to a stored AssemblyAI agent_id instead of configuring
        everything inline per-session. Required for a custom LLM like Groq, since
        `llm` is only settable on the Agents REST resource, not inline in session.update."""
        return bool(self.assemblyai_agent_id or self.groq_api_key)


settings = Settings()

_log = logging.getLogger("sentinelvoice.config")

if not settings.assemblyai_api_key:
    _log.warning(
        "ASSEMBLYAI_API_KEY is not set. Get one from https://www.assemblyai.com/dashboard/home "
        "and put it in a .env file before starting a live voice session."
    )

if not settings.groq_api_key and not settings.assemblyai_agent_id:
    _log.info(
        "GROQ_API_KEY is not set - the agent will use AssemblyAI's managed reasoning model. "
        "Set GROQ_API_KEY (from https://console.groq.com/keys) to use Groq instead."
    )
