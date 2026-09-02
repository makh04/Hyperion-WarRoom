from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings
from ..tools.schemas import AGENT_SYSTEM_PROMPT, build_agent_tools

logger = logging.getLogger("sentinelvoice.provisioning")

AGENTS_URL = "https://agents.assemblyai.com/v1/agents"

# Cache file lives next to the project root, not inside app/, so it survives package moves
# and is easy to .gitignore.
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / ".assemblyai_agent_cache.json"

_lock = asyncio.Lock()
_cached_agent_id: Optional[str] = None


def _agent_config_body() -> dict:
    """The full config AssemblyAI needs to run SentinelVoice as a stored agent: persona,
    voice, tools, turn-detection tuning, and (if configured) Groq as the reasoning LLM."""
    body: dict = {
        "name": "SentinelVoice Incident Co-Pilot",
        "system_prompt": AGENT_SYSTEM_PROMPT,
        "voice": {"voice_id": settings.agent_voice},
        "tools": build_agent_tools(),
        "input": {
            "format": {"encoding": "audio/pcm", "sample_rate": 24000},
            "turn_detection": {
                # War-room audio is noisy and multi-speaker, so we bias towards requiring a
                # bit more silence/confidence before deciding a "turn" ended, rather than the
                # tighter defaults tuned for 1:1 calls.
                "vad_threshold": 0.55,
                "min_silence": 700,
                "max_silence": 4000,
                "interrupt_response": True,
            },
        },
        "output": {
            "voice": settings.agent_voice,
            "format": {"encoding": "audio/pcm", "sample_rate": 24000},
            "volume": 100,
        },
    }
    if settings.groq_api_key:
        body["llm"] = [
            {
                "base_url": settings.groq_base_url,
                "model": settings.groq_model,
                "api_key": settings.groq_api_key,
            }
        ]
    return body


def _config_hash(body: dict) -> str:
    """Hash the config (minus the secret) so we only hit the Agents API when something
    actually changed, instead of re-provisioning on every restart."""
    hashable = json.loads(json.dumps(body))
    for entry in hashable.get("llm", []):
        entry.pop("api_key", None)
    return hashlib.sha256(json.dumps(hashable, sort_keys=True).encode()).hexdigest()


def _load_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(agent_id: str, config_hash: str) -> None:
    try:
        CACHE_PATH.write_text(json.dumps({"agent_id": agent_id, "config_hash": config_hash}))
    except OSError:
        logger.warning("Could not write agent cache file at %s", CACHE_PATH)


async def ensure_agent_id() -> str:
    """Return an AssemblyAI stored agent_id configured with our tools and, if GROQ_API_KEY is
    set, Groq as the reasoning LLM (per AssemblyAI's "connect your own LLM" mechanism, which
    only exists at the Agents-resource level, not inline in session.update).

    Reuses a cached agent across restarts and only calls the Agents REST API when the
    configuration actually changed, so redeploys stay cheap and you don't accumulate orphaned
    agents in the AssemblyAI dashboard.
    """
    global _cached_agent_id

    if settings.assemblyai_agent_id:
        return settings.assemblyai_agent_id

    if _cached_agent_id:
        return _cached_agent_id

    async with _lock:
        if _cached_agent_id:  # another task may have won the race while we waited on the lock
            return _cached_agent_id

        body = _agent_config_body()
        config_hash = _config_hash(body)
        cache = _load_cache()
        headers = {
            "Authorization": f"Bearer {settings.assemblyai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            if cache and cache.get("config_hash") == config_hash and cache.get("agent_id"):
                _cached_agent_id = cache["agent_id"]
                logger.info("Reusing cached AssemblyAI agent %s (config unchanged)", _cached_agent_id)
                return _cached_agent_id

            if cache and cache.get("agent_id"):
                agent_id = cache["agent_id"]
                resp = await client.put(f"{AGENTS_URL}/{agent_id}", headers=headers, json=body)
                if resp.status_code == 200:
                    logger.info("Updated existing AssemblyAI agent %s (config changed)", agent_id)
                    _cached_agent_id = agent_id
                    _save_cache(agent_id, config_hash)
                    return agent_id
                logger.warning(
                    "Failed to update cached agent %s (HTTP %s); creating a new one instead",
                    agent_id, resp.status_code,
                )

            resp = await client.post(AGENTS_URL, headers=headers, json=body)
            resp.raise_for_status()
            agent_id = resp.json()["id"]
            logger.info(
                "Created AssemblyAI agent %s (llm=%s)",
                agent_id, "groq:" + settings.groq_model if settings.groq_api_key else "managed",
            )
            _cached_agent_id = agent_id
            _save_cache(agent_id, config_hash)
            return agent_id
