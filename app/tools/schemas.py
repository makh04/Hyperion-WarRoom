from __future__ import annotations

import copy

AGENT_SYSTEM_PROMPT = """You are SentinelVoice, a calm, concise SRE incident co-pilot sitting in \
on a live engineering war-room call. Multiple engineers may be talking over each other - most of \
that is not addressed to you. Stay SILENT and do not reply to normal engineer-to-engineer \
conversation. Only speak or act when an engineer explicitly addresses you, typically by saying \
"Agent" or "SentinelVoice" first, e.g. "Agent, check checkout-db CPU load."

When addressed:
- For read-only questions (service health, recent deployments), just call the matching tool and \
answer briefly and factually. No filler, no small talk.
- For destructive actions (restart, scale, rollback, deploy), you MUST call \
request_action_confirmation first, read back the confirmation phrase, and WAIT. Only call \
execute_confirmed_action after a human has spoken that exact phrase back to you, or you are told \
it was approved on the dashboard. Never skip this step, even if asked to hurry.
- When the team states a decision, conclusion, or action item out loud, call \
log_incident_decision to record it, then acknowledge in a few words.

Keep every spoken reply under two sentences. This is a stressful incident call - be fast, calm, \
and precise, never chatty."""


# Tool definitions in the shape AssemblyAI's Agents REST API expects
# (POST/PUT https://agents.assemblyai.com/v1/agents). No "type" field here - that's only
# required for *inline* session.update tool definitions; see build_inline_tools() below.
_TOOL_SCHEMAS: list = [
    {
        "name": "get_service_health",
        "description": (
            "Look up current CPU, memory, latency, and error-rate metrics for a named backend "
            "service or infrastructure node. Use this whenever an engineer asks about the "
            "health, load, or status of a service."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": (
                        "The name of the service or node to check, e.g. 'checkout-db', "
                        "'api-gateway', 'auth-service'."
                    ),
                }
            },
            "required": ["service_name"],
        },
        "execution_mode": "interactive",
        "timeout_seconds": 15,
    },
    {
        "name": "fetch_recent_deployments",
        "description": (
            "Fetch code commits and deployments pushed within the given time window, to help "
            "correlate a recent deploy with the start of an incident."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeframe_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to look for deployments, e.g. 30, 60, 120.",
                }
            },
            "required": ["timeframe_minutes"],
        },
        "execution_mode": "interactive",
        "timeout_seconds": 20,
    },
    {
        "name": "log_incident_decision",
        "description": (
            "Record a decision, action item, or key fact into the live incident timeline / "
            "post-mortem log. Use this whenever the team agrees on something, assigns an owner, "
            "or reaches a conclusion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A short, clear summary of the decision or fact to log.",
                },
                "assigned_to": {
                    "type": "string",
                    "description": (
                        "The name of the engineer responsible for this action item, if any. "
                        "Use 'unassigned' if none was given."
                    ),
                },
            },
            "required": ["summary", "assigned_to"],
        },
        "execution_mode": "interactive",
        "timeout_seconds": 10,
    },
    {
        "name": "request_action_confirmation",
        "description": (
            "Register a request to perform a potentially destructive infrastructure action "
            "(restart, scale, rollback, deploy). This does NOT execute the action - it only "
            "registers it and returns a confirmation phrase that a human must speak back, or "
            "approve on the live dashboard, before execute_confirmed_action can run. Always call "
            "this before execute_confirmed_action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "description": "The kind of action requested.",
                    "enum": ["restart", "scale_up", "scale_down", "rollback", "deploy"],
                },
                "target_resource": {
                    "type": "string",
                    "description": "The resource this action targets, e.g. 'checkout-db-node-01'.",
                },
            },
            "required": ["action_type", "target_resource"],
        },
        "execution_mode": "interactive",
        "timeout_seconds": 15,
    },
    {
        "name": "execute_confirmed_action",
        "description": (
            "Execute a previously-registered infrastructure action after it has been confirmed "
            "by a human, either by speaking the confirmation phrase or approving it on the live "
            "dashboard. Refuses to run if the action has not been confirmed yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pending_action_id": {
                    "type": "string",
                    "description": "The pending_action_id returned by request_action_confirmation.",
                },
                "confirmation_phrase": {
                    "type": "string",
                    "description": (
                        "The exact confirmation phrase spoken by the human, e.g. "
                        "'Confirm action ACT-102'. Leave empty if approval was given via the "
                        "dashboard instead."
                    ),
                },
            },
            "required": ["pending_action_id"],
        },
        # "hold": the agent stays silent while this runs (mock action takes ~1s) instead of
        # narrating a filler line, then delivers the result directly once tool.result lands.
        "execution_mode": "hold",
        "timeout_seconds": 45,
    },
]


def build_agent_tools() -> list:
    """Tool definitions shaped for POST/PUT https://agents.assemblyai.com/v1/agents."""
    return copy.deepcopy(_TOOL_SCHEMAS)


def build_inline_tools() -> list:
    """Tool definitions shaped for an inline `session.update` (no stored agent). Inline tools
    require an explicit "type": "function" that the stored-agent shape doesn't use."""
    tools = copy.deepcopy(_TOOL_SCHEMAS)
    for tool in tools:
        tool["type"] = "function"
    return tools
