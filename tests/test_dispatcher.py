"""Dependency-free smoke test for the core incident/tool-dispatch logic.

Only touches app.state / app.tools.* / app.config - no fastapi, websockets, or httpx
required - so it runs anywhere with just the stdlib, including CI without deps installed.

Usage:
    python3 tests/test_dispatcher.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state  # noqa: E402
from app.tools import dispatcher  # noqa: E402


async def main() -> None:
    incident = await state.store.create("Checkout latency spike")

    # 1. Read-only tool
    health = await dispatcher.execute_tool(incident.id, "get_service_health", {"service_name": "checkout-db"})
    assert "cpu_percent" in health
    print("get_service_health ->", health)

    # 2. Decision logging
    log_result = await dispatcher.execute_tool(
        incident.id, "log_incident_decision",
        {"summary": "Roll back last deploy", "assigned_to": "sam"},
    )
    assert log_result["logged"] is True
    assert any(e.kind == "decision" for e in incident.timeline)
    print("log_incident_decision ->", log_result)

    # 3. Two-phase confirmation: request
    req = await dispatcher.execute_tool(
        incident.id, "request_action_confirmation",
        {"action_type": "restart", "target_resource": "checkout-db-node-01"},
    )
    action_id = req["pending_action_id"]
    phrase = req["confirmation_phrase"]
    assert incident.pending_actions[action_id].status == state.ActionStatus.PENDING
    print("request_action_confirmation ->", req)

    # 4. Wrong phrase must be rejected
    try:
        await dispatcher.execute_tool(
            incident.id, "execute_confirmed_action",
            {"pending_action_id": action_id, "confirmation_phrase": "nope"},
        )
        raise AssertionError("expected ToolError for wrong confirmation phrase")
    except dispatcher.ToolError as exc:
        print("correctly rejected wrong phrase ->", exc)

    # 5. Correct phrase executes
    exec_result = await dispatcher.execute_tool(
        incident.id, "execute_confirmed_action",
        {"pending_action_id": action_id, "confirmation_phrase": phrase},
    )
    assert exec_result["status"] == "success"
    assert incident.pending_actions[action_id].status == state.ActionStatus.EXECUTED
    print("execute_confirmed_action ->", exec_result)

    # 6. Double-execution must be rejected
    try:
        await dispatcher.execute_tool(
            incident.id, "execute_confirmed_action",
            {"pending_action_id": action_id, "confirmation_phrase": phrase},
        )
        raise AssertionError("expected ToolError for double execution")
    except dispatcher.ToolError as exc:
        print("correctly rejected double-execution ->", exc)

    # 7. Dashboard-approve path on a second action (no spoken phrase at all)
    req2 = await dispatcher.execute_tool(
        incident.id, "request_action_confirmation",
        {"action_type": "scale_up", "target_resource": "api-gateway"},
    )
    action_id_2 = req2["pending_action_id"]
    dash_result = await dispatcher.approve_and_execute(incident.id, action_id_2)
    assert dash_result["status"] == "success"
    assert incident.pending_actions[action_id_2].status == state.ActionStatus.EXECUTED
    print("approve_and_execute (dashboard path) ->", dash_result)

    # 8. Unknown tool / unknown incident are rejected cleanly, not with a raw exception
    try:
        await dispatcher.execute_tool(incident.id, "not_a_real_tool", {})
        raise AssertionError("expected ToolError for unknown tool")
    except dispatcher.ToolError:
        pass

    print(
        f"\nTimeline has {len(incident.timeline)} entries; "
        f"{len(incident.pending_actions)} pending actions tracked."
    )
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
