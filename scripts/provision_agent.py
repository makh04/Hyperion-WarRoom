"""Stand-alone utility: provision (or reuse) the AssemblyAI stored agent and print its id.

Useful for confirming your ASSEMBLYAI_API_KEY / GROQ_API_KEY wiring works before starting
the full server, and for inspecting exactly what config gets sent.

Usage:
    python scripts/provision_agent.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.voice.provisioning import _agent_config_body, ensure_agent_id  # noqa: E402


async def main() -> None:
    if not settings.assemblyai_api_key:
        print("ASSEMBLYAI_API_KEY is not set - add it to your .env first.")
        return

    reasoning = f"Groq ({settings.groq_model})" if settings.groq_api_key else "AssemblyAI managed model"
    print(f"Reasoning LLM: {reasoning}\n")

    body = _agent_config_body()
    preview = json.loads(json.dumps(body))
    for entry in preview.get("llm", []):
        entry["api_key"] = "***"
    print("Agent config to send:")
    print(json.dumps(preview, indent=2))

    agent_id = await ensure_agent_id()
    print(f"\nagent_id = {agent_id}")
    print("Set ASSEMBLYAI_AGENT_ID in .env to pin the backend to this exact agent going forward.")


if __name__ == "__main__":
    asyncio.run(main())
