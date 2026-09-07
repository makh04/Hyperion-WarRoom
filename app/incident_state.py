from __future__ import annotations

import re
import time
from typing import Any

from .security.prompt_guard import prompt_guard


_SEVERITY_PATTERNS = (
    ("critical", re.compile(r"\b(outage|data loss|critical)\b", re.IGNORECASE)),
    ("high", re.compile(r"\b(down|unavailable|security breach|incident)\b", re.IGNORECASE)),
)


def _state_from_window(window: dict[str, Any]) -> dict[str, Any]:
    messages = window.get("messages", [])
    text = " ".join(item.get("message", "") for item in messages)
    severity = "unknown"
    for candidate, pattern in _SEVERITY_PATTERNS:
        if pattern.search(text):
            severity = candidate
            break
    participants = sorted({item.get("speaker", "") for item in messages if item.get("speaker")})
    return {
        "status": "active",
        "severity": severity,
        "summary": None,
        "signals": [],
        "participants": participants,
        "transcript_message_count": len(messages),
        "last_updated": time.time(),
    }


async def analyze_completed_window(window: dict[str, Any]) -> dict[str, Any]:
    text = "\n".join(
        f"{item.get('speaker', 'unknown')}: {item.get('message', '')}"
        for item in window.get("messages", [])
    )
    security = await prompt_guard.classify(text)
    state = _state_from_window(window)
    if not security["is_safe"]:
        state["signals"].append("unsafe_transcript_input")
    return {"window": window, "security": security, "incident_state": state}