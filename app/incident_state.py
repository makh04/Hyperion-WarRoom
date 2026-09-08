from __future__ import annotations

import re
import time
from copy import deepcopy
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
        "active_topics": [],
        "person_assignments": [],
        "suggested_actions": [],
        "key_decisions": [],
        "active_blockers": [],
    }


async def analyze_completed_window(window: dict[str, Any]) -> dict[str, Any]:
    return await update_incident_state({}, window)


def _merge_values(previous: Any, current: Any) -> Any:
    if not isinstance(previous, list) or not isinstance(current, list):
        return current if current is not None else previous
    merged = list(previous)
    for value in current:
        if value not in merged:
            merged.append(value)
    return merged


async def update_incident_state(
    previous_state: dict[str, Any],
    window: dict[str, Any],
    meeting_id: str | None = None,
) -> dict[str, Any]:
    """Update the accumulated incident state with one new transcript window."""
    text = "\n".join(
        f"{item.get('speaker', 'unknown')}: {item.get('message', '')}"
        for item in window.get("messages", [])
    )
    security = await prompt_guard.classify(text)
    current_state = _state_from_window(window)
    if not security["is_safe"]:
        current_state["signals"].append("unsafe_transcript_input")

    previous_incident_state = previous_state.get("incident_state", {})
    merged_incident_state = deepcopy(previous_incident_state)
    for key, value in current_state.items():
        if key in {"signals", "participants"}:
            merged_incident_state[key] = _merge_values(merged_incident_state.get(key, []), value)
        elif key == "transcript_message_count":
            merged_incident_state[key] = (
                merged_incident_state.get(key, 0) + value
                if previous_incident_state
                else value
            )
        else:
            merged_incident_state[key] = value

    result = deepcopy(previous_state)
    result.update({
        "meeting_id": meeting_id or previous_state.get("meeting_id"),
        "timestamp": time.time(),
        "security": security,
        "incident_state": merged_incident_state,
        "active_topics": _merge_values(previous_state.get("active_topics", []), current_state["active_topics"]),
        "person_assignments": _merge_values(
            previous_state.get("person_assignments", []), current_state["person_assignments"]
        ),
        "suggested_actions": _merge_values(
            previous_state.get("suggested_actions", []), current_state["suggested_actions"]
        ),
        "key_decisions": _merge_values(
            previous_state.get("key_decisions", []), current_state["key_decisions"]
        ),
        "active_blockers": _merge_values(
            previous_state.get("active_blockers", []), current_state["active_blockers"]
        ),
        "window": window,
    })
    return result