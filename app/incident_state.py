from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from typing import Any

import httpx

from .config import settings


logger = logging.getLogger("sentinelvoice.reasoning_model")


_SEVERITY_PATTERNS = (
    ("critical", re.compile(r"\b(outage|data loss|critical)\b", re.IGNORECASE)),
    ("high", re.compile(r"\b(down|unavailable|security breach|incident)\b", re.IGNORECASE)),
)


class ReasoningModelError(RuntimeError):
    def __init__(self, message: str, category: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.details = details or {}


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
    }


async def analyze_completed_window(window: dict[str, Any]) -> dict[str, Any]:
    return await update_incident_state({}, window)


def _summary_prompt(previous_state: dict[str, Any], window: dict[str, Any]) -> str:
    return (
        "You maintain an ongoing SRE incident meeting summary. Phase A has no previous state; "
        "later phases must update the previous JSON using the new conversation. Return only a "
        "valid JSON object with exactly these keys: summary, active_topics, person_assignments, "
        "suggested_actions, key_decisions, etc. Preserve useful previous facts, "
        "append new facts without duplicates, and do not invent facts. Every value except "
        "summary must be an array. summary must be a concise plain-text summary.\n\n"
        f"PREVIOUS SUMMARY JSON:\n{json.dumps(previous_state, indent=2, sort_keys=True)}\n\n"
        f"NEW RAW CONVERSATION:\n{json.dumps(window, indent=2, sort_keys=True)}"
    )


def _parse_model_summary(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ReasoningModelError(
            "Groq returned non-text summary content", "invalid_response"
        )
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ReasoningModelError(
            "Groq returned invalid summary JSON", "invalid_response"
        ) from exc
    keys = (
        "summary",
        "active_topics",
        "person_assignments",
        "suggested_actions",
        "key_decisions",
    )
    if not isinstance(value, dict) or any(key not in value for key in keys):
        raise ReasoningModelError(
            "Groq returned incomplete summary JSON", "invalid_response"
        )
    if not isinstance(value["summary"], str) or any(not isinstance(value[key], list) for key in keys[1:]):
        raise ReasoningModelError(
            "Groq returned invalid summary fields", "invalid_response"
        )
    return {key: value[key] for key in keys}


async def _reason_over_window(
    previous_state: dict[str, Any],
    window: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    
    if not settings.final_report_api_key:
        message = "Groq API key is missing: set GROQ_API_KEY or FINAL_REPORT_API_KEY"
        logger.error(message)
        raise ReasoningModelError(message, "missing_api_key")
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=60)
    try:
        response = await http_client.post(
            f"{settings.final_report_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.final_report_api_key}"},
            json={
                "model": settings.buffer_summary_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You maintain factual incident summaries."},
                    {"role": "user", "content": _summary_prompt(previous_state, window)},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _parse_model_summary(content)
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:500]
        logger.error(
            "Groq API error status=%s response=%s",
            exc.response.status_code,
            response_text,
        )
        raise ReasoningModelError(
            f"Groq API returned HTTP {exc.response.status_code}",
            "provider_http_error",
            {"status_code": exc.response.status_code, "response": response_text},
        ) from exc
    except httpx.RequestError as exc:
        logger.exception("Groq network request failed")
        raise ReasoningModelError(
            f"Could not connect to Groq: {exc}", "network_error"
        ) from exc
    except ReasoningModelError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.exception("Groq returned an unexpected response shape")
        raise ReasoningModelError(
            f"Groq returned an unexpected response: {exc}", "invalid_response"
        ) from exc
    finally:
        if owns_client:
            await http_client.aclose()


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
    current_state = _state_from_window(window)
    model_state = await _reason_over_window(previous_state, window)

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
        "security": {"enabled": False, "reason": "Prompt Guard disabled"},
        "incident_state": merged_incident_state,
        **model_state,
        "window": window,
    })
    return result