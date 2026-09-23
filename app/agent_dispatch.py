from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from groq import AsyncGroq

from .config import settings


logger = logging.getLogger("hyperion_warroom.agent_dispatch")

# ── Lazy import to avoid circular deps ──────────────────────────────────────
_WAKE_PATTERN = re.compile(
    r"(?:^|\s)hey\s+agent[,:]?\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)

AGENT_SYSTEM_PROMPT = """\
You are Hypernion, an SRE operations agent embedded in a Google Meet incident war room.
You have access to a set of HTTP tools that the team has registered (Vercel, GitHub, PagerDuty, etc.).
Your job is to take direct action when asked — restart servers, trigger deployments, query status,
page on-call engineers, or answer questions using the current incident summary as context.

Rules:
- Be concise. One or two sentences max unless asked for detail.
- If you call a tool, briefly confirm what you did and what the result was.
- If you cannot help or a tool is missing, say so clearly.
- Never make up tool responses. Only report what the API actually returned.
- Do not repeat the wake phrase.
"""


def detect_wake_word(text: str) -> str | None:
    """Return the query text after 'hey agent' if present, else None."""
    match = _WAKE_PATTERN.search(text.strip())
    if match:
        query = match.group(1).strip()
        if query:
            print(f"[WAKE WORD DETECTED] Text: '{text}' -> Query: '{query}'", flush=True)
            return query
    return None


def _build_openai_tools(tool_defs: list[dict]) -> list[dict]:
    """Convert saved tool registry entries into OpenAI-style function specs."""
    openai_tools = []
    for t in tool_defs:
        props: dict[str, Any] = {}
        desc_parts = [t.get("description") or t["name"]]
        desc_parts.append(f"HTTP {t.get('method','POST')} {t['url']}")
        if t.get("body_template"):
            desc_parts.append("Accepts a JSON body — pass it as the 'body' argument (JSON string).")
            props["body"] = {
                "type": "string",
                "description": "JSON string to send as the request body. Leave empty to use the saved template.",
            }

        fn_name = re.sub(r"[^a-zA-Z0-9_]", "_", t["name"])[:64]
        openai_tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": ". ".join(desc_parts),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [],
                },
                "_tool_id": t["id"],  # we'll strip this before sending to API
            },
        })
    return openai_tools


async def _call_tool(tool_def: dict, arguments: dict) -> str:
    """Fire the HTTP tool and return a short summary string."""
    method = tool_def.get("method", "POST").upper()
    url = tool_def["url"]
    headers = dict(tool_def.get("headers", {}))
    body_template = tool_def.get("body_template")

    if "body" in arguments and arguments["body"]:
        content = arguments["body"]
        if "content-type" not in {k.lower() for k in headers}:
            headers["Content-Type"] = "application/json"
    elif body_template:
        content = body_template
    else:
        content = None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            req = client.build_request(
                method,
                url,
                headers=headers,
                content=content.encode() if content else None,
            )
            resp = await client.send(req)
        status = resp.status_code
        body_text = resp.text[:1500]
        return f"HTTP {status}: {body_text}"
    except httpx.HTTPError as exc:
        return f"Request failed: {exc}"


# Agent log broadcast (populated by main.py after startup)
_agent_log_broadcast = None  # callable(payload: dict) -> Awaitable


def set_agent_log_broadcaster(fn) -> None:
    global _agent_log_broadcast
    _agent_log_broadcast = fn


async def _log(payload: dict) -> None:
    if _agent_log_broadcast:
        try:
            await _agent_log_broadcast(payload)
        except Exception:
            pass


async def dispatch_agent_query(query: str, incident: Any) -> str:
    """
    Run the SRE command agent:
      1. Build messages with system prompt + draft summary context + user query.
      2. Call Groq with all saved tools as function specs.
      3. Execute any tool calls (single round-trip).
      4. Return the final answer string.
    """
    from .routers.tools import get_all_tools  # late import

    if not settings.groq_api_key:
        return "Agent is not configured — GROQ_API_KEY is missing."

    tool_defs = get_all_tools()
    openai_tools = _build_openai_tools(tool_defs)

    # Map function name → tool_def for execution
    fn_to_tool: dict[str, dict] = {}
    clean_tools = []
    for spec, tdef in zip(openai_tools, tool_defs):
        fn_name = spec["function"]["name"]
        fn_to_tool[fn_name] = tdef
        # Strip our internal _tool_id before sending to API
        clean_spec = {
            "type": spec["type"],
            "function": {k: v for k, v in spec["function"].items() if k != "_tool_id"},
        }
        clean_tools.append(clean_spec)

    # Build context from draft summary AND recent timeline history
    context_parts = []
    draft = getattr(incident, "draft_summary", None)
    if draft:
        context_parts.append(f"Current incident summary (JSON):\n{json.dumps(draft, indent=2)}")

    timeline = getattr(incident, "timeline", [])
    if timeline:
        recent_entries = timeline[-20:]
        timeline_lines = [
            f"[{e.kind}] {e.speaker or 'Unknown'}: {e.text}" for e in recent_entries
        ]
        context_parts.append("Recent meeting transcript & chat history:\n" + "\n".join(timeline_lines))

    if not context_parts:
        summary_ctx = "\n\nNo prior summary or transcript available yet."
    else:
        summary_ctx = "\n\n" + "\n\n".join(context_parts)

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT + summary_ctx},
        {"role": "user", "content": query},
    ]

    print(f"[AGENT DISPATCH] Processing wake query for incident {incident.id}: '{query}'", flush=True)
    print(f"[AGENT DISPATCH] Context includes {len(timeline)} timeline entries, draft summary present: {bool(draft)}", flush=True)

    await _log({"type": "wake_word_triggered", "incident_id": incident.id, "query": query})

    raw_base_url = (settings.groq_base_url or "").rstrip("/")
    if raw_base_url.endswith("/chat/completions"):
        raw_base_url = raw_base_url[:-len("/chat/completions")].rstrip("/")
    if raw_base_url.endswith("/openai/v1"):
        raw_base_url = raw_base_url[:-len("/openai/v1")].rstrip("/")

    groq_client = AsyncGroq(
        api_key=settings.groq_api_key,
        base_url=raw_base_url if raw_base_url else None,
    )

    kwargs: dict[str, Any] = {
        "model": settings.groq_model,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.2,
    }
    if clean_tools:
        kwargs["tools"] = clean_tools
        kwargs["tool_choice"] = "auto"

    try:
        response = await groq_client.chat.completions.create(**kwargs)
    except Exception as exc:
        logger.exception("Groq call failed")
        return f"Agent error: {exc}"

    choice = response.choices[0]
    message = choice.message

    # ── Handle tool calls ─────────────────────────────────────────────────
    tool_calls = message.tool_calls or []
    if tool_calls:
        # Convert message object to dict for messages list
        assistant_msg = {"role": "assistant", "content": message.content}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        for tc in tool_calls:
            fn_name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            tool_def = fn_to_tool.get(fn_name)
            if tool_def:
                await _log({
                    "type": "tool_call",
                    "incident_id": incident.id,
                    "tool_name": fn_name,
                    "args": args,
                })
                tool_result = await _call_tool(tool_def, args)
            else:
                tool_result = f"Unknown tool: {fn_name}"

            await _log({
                "type": "tool_result",
                "incident_id": incident.id,
                "tool_name": fn_name,
                "result": tool_result[:500],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })

        # Second Groq call to get the final answer
        try:
            response2 = await groq_client.chat.completions.create(
                model=settings.groq_model,
                messages=messages,
                max_tokens=512,
                temperature=0.2,
            )
            answer = (response2.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.exception("Groq second-turn call failed")
            answer = f"Tool executed but agent follow-up failed: {exc}"
    else:
        answer = (message.content or "").strip() or "No response from agent."

    await _log({"type": "agent_reply", "incident_id": incident.id, "reply": answer})
    print(f"[AGENT DISPATCH] Final answer for incident {incident.id}: '{answer}'", flush=True)
    return answer

