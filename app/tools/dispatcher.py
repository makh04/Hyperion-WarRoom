from __future__ import annotations

import time
import uuid
from typing import Any

from .. import state
from . import infra


class ToolError(Exception):
    """Raised when a tool call can't be fulfilled. The message is relayed back to the LLM
    as the tool result, so keep it short and speakable - the agent may say it out loud."""


async def execute_tool(incident_id: str, name: str, arguments: dict) -> dict:
    incident = state.store.get(incident_id)
    if incident is None:
        raise ToolError(f"Unknown incident_id {incident_id}.")

    speaker = state.attribute_speaker(incident)

    if name == "get_service_health":
        result = await infra.get_service_health(arguments["service_name"])
        state.record_timeline(
            incident, "tool.call", f"Checked health of {arguments['service_name']}",
            speaker=speaker, meta={"tool": name, "result": result},
        )
        await state.broadcast(incident, {"type": "tool.result", "tool": name, "result": result})
        return result

    if name == "fetch_recent_deployments":
        result = await infra.fetch_recent_deployments(int(arguments["timeframe_minutes"]))
        state.record_timeline(
            incident, "tool.call", f"Fetched deployments from last {arguments['timeframe_minutes']} min",
            speaker=speaker, meta={"tool": name, "result": result},
        )
        await state.broadcast(incident, {"type": "tool.result", "tool": name, "result": result})
        return result

    if name == "log_incident_decision":
        summary = arguments["summary"]
        assigned_to = arguments.get("assigned_to", "unassigned")
        entry = state.record_timeline(incident, "decision", summary, speaker=speaker, meta={"assigned_to": assigned_to})
        await state.broadcast(incident, {"type": "timeline.entry", "entry": state.entry_to_dict(entry)})
        return {"logged": True, "entry_id": entry.id}

    if name == "request_action_confirmation":
        action_id = f"ACT-{uuid.uuid4().hex[:6].upper()}"
        phrase = f"Confirm action {action_id}"
        pending = state.PendingAction(
            id=action_id,
            incident_id=incident_id,
            action_type=arguments["action_type"],
            target_resource=arguments["target_resource"],
            confirmation_phrase=phrase,
        )
        incident.pending_actions[action_id] = pending
        state.record_timeline(
            incident, "tool.call",
            f"Requested confirmation to {arguments['action_type']} {arguments['target_resource']}",
            speaker=speaker, meta={"tool": name, "pending_action_id": action_id},
        )
        await state.broadcast(incident, {"type": "pending_action.created", "action": state.action_to_dict(pending)})
        return {
            "pending_action_id": action_id,
            "confirmation_phrase": phrase,
            "message": f"Say '{phrase}' or approve it on the dashboard to proceed.",
        }

    if name == "execute_confirmed_action":
        return await _execute_confirmed_action(incident, speaker, arguments)

    raise ToolError(f"Unknown tool '{name}'.")


async def _execute_confirmed_action(incident: state.Incident, speaker, arguments: dict) -> dict:
    action_id = arguments["pending_action_id"]
    phrase = (arguments.get("confirmation_phrase") or "").strip().lower()

    pending = incident.pending_actions.get(action_id)
    if pending is None:
        raise ToolError(f"No pending action with id {action_id}.")
    if pending.status == state.ActionStatus.EXECUTED:
        raise ToolError(f"Action {action_id} was already executed.")

    confirmed = (
        pending.status == state.ActionStatus.APPROVED
        or phrase == pending.confirmation_phrase.strip().lower()
    )
    if not confirmed:
        raise ToolError(
            f"Action {action_id} is not confirmed yet. Ask the human to say "
            f"'{pending.confirmation_phrase}' or approve it on the dashboard first."
        )

    pending.status = state.ActionStatus.EXECUTED
    result = await infra.execute_infra_action(pending.action_type, pending.target_resource)
    pending.result = result
    pending.resolved_at = time.time()
    state.record_timeline(
        incident, "tool.result",
        f"Executed {pending.action_type} on {pending.target_resource}",
        speaker=speaker, meta={"action_id": action_id, "result": result},
    )
    await state.broadcast(incident, {"type": "pending_action.executed", "action": state.action_to_dict(pending)})
    return result


async def approve_and_execute(incident_id: str, action_id: str) -> dict:
    """Dashboard-driven alternative to speaking the confirmation phrase: a human clicks
    Approve in the UI and the action runs immediately, without going back through the
    voice agent."""
    incident = state.store.get(incident_id)
    if incident is None:
        raise ToolError(f"Unknown incident_id {incident_id}.")

    pending = incident.pending_actions.get(action_id)
    if pending is None:
        raise ToolError(f"No pending action with id {action_id}.")
    if pending.status == state.ActionStatus.EXECUTED:
        raise ToolError(f"Action {action_id} was already executed.")

    pending.status = state.ActionStatus.APPROVED
    result = await infra.execute_infra_action(pending.action_type, pending.target_resource)
    pending.status = state.ActionStatus.EXECUTED
    pending.result = result
    pending.resolved_at = time.time()
    state.record_timeline(
        incident, "tool.result",
        f"Dashboard-approved execution of {pending.action_type} on {pending.target_resource}",
        speaker="dashboard", meta={"action_id": action_id, "result": result},
    )
    await state.broadcast(incident, {"type": "pending_action.executed", "action": state.action_to_dict(pending)})
    return result
