from __future__ import annotations

import copy

AGENT_SYSTEM_PROMPT = """You are Hypernion Agent, a calm, concise SRE incident co-pilot sitting in \
on a live engineering war-room call. Multiple engineers may be talking over each other - most of \
that is not addressed to you. Stay SILENT and do not reply to normal engineer-to-engineer \
conversation. Only speak or act when an engineer explicitly addresses you. When you do, use only \
the custom HTTP tools that the team has added to the live tool registry. Do not invent tools or \
pretend built-in actions exist.

Keep every spoken reply under two sentences. This is a stressful incident call - be fast, calm, \
and precise, never chatty."""


# registered via the /api/tools registry. Those live tools are the ones that should be available
# to the agent at runtime.
_TOOL_SCHEMAS: list = []


def build_agent_tools() -> list:
    """Return the stored-agent tool set. This app intentionally exposes no built-in tool schema."""
    return copy.deepcopy(_TOOL_SCHEMAS)


def build_inline_tools() -> list:
    """Return the inline tool set. This app intentionally exposes no built-in tool schema."""
    tools = copy.deepcopy(_TOOL_SCHEMAS)
    for tool in tools:
        tool["type"] = "function"
    return tools
