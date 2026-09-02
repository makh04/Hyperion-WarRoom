from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from ..config import settings

# A few named services get realistic-looking baselines so a demo incident ("checkout-db is
# unhealthy") is reproducible; anything else gets plausible random numbers.
_SERVICE_BASELINE = {
    "checkout-db": {"cpu": 92, "ram": 81, "latency_ms": 480, "error_rate": 0.12},
    "api-gateway": {"cpu": 34, "ram": 40, "latency_ms": 90, "error_rate": 0.01},
    "auth-service": {"cpu": 28, "ram": 55, "latency_ms": 120, "error_rate": 0.02},
    "checkout-db-node-01": {"cpu": 95, "ram": 84, "latency_ms": 510, "error_rate": 0.14},
}


def _jitter(value: float, spread: float) -> float:
    return max(0.0, value + random.uniform(-spread, spread))


async def get_service_health(service_name: str) -> dict:
    await asyncio.sleep(0.15)  # simulate a fast metrics-API round trip
    key = service_name.strip().lower().replace(" ", "-")
    base = _SERVICE_BASELINE.get(
        key,
        {
            "cpu": random.randint(20, 60),
            "ram": random.randint(20, 60),
            "latency_ms": random.randint(50, 200),
            "error_rate": round(random.uniform(0.0, 0.02), 3),
        },
    )
    return {
        "service_name": service_name,
        "cpu_percent": round(_jitter(base["cpu"], 3), 1),
        "ram_percent": round(_jitter(base["ram"], 3), 1),
        "p95_latency_ms": round(_jitter(base["latency_ms"], 15)),
        "error_rate_percent": round(base["error_rate"] * 100, 2),
        "checked_at": time.time(),
        "source": "mock" if settings.use_mock_cloud else "live",
    }


async def fetch_recent_deployments(timeframe_minutes: int) -> dict:
    await asyncio.sleep(0.15)
    mock_deploys = [
        {"repo": "checkout-service", "commit": "a91f3c2", "author": "sam", "message": "bump connection pool size", "minutes_ago": 18},
        {"repo": "checkout-service", "commit": "7e02b19", "author": "alex", "message": "add retry backoff to db client", "minutes_ago": 42},
        {"repo": "infra", "commit": "c410aa5", "author": "priya", "message": "raise checkout-db max_connections", "minutes_ago": 61},
    ]
    in_window = [d for d in mock_deploys if d["minutes_ago"] <= timeframe_minutes]
    return {
        "timeframe_minutes": timeframe_minutes,
        "deployments": in_window,
        "checked_at": time.time(),
        "source": "mock" if settings.use_mock_cloud else "live",
    }


async def execute_infra_action(action_type: str, target_resource: str) -> dict:
    """Execute a (confirmed) destructive infra action. Mocked by default; flip
    USE_MOCK_CLOUD=false and fill in _execute_infra_action_live() for a real deployment."""
    if not settings.use_mock_cloud:
        return await _execute_infra_action_live(action_type, target_resource)

    await asyncio.sleep(1.2)  # simulate the action taking a moment
    return {
        "action_type": action_type,
        "target_resource": target_resource,
        "status": "success",
        "detail": f"{action_type} completed on {target_resource}",
        "executed_at": time.time(),
        "source": "mock",
    }


async def _execute_infra_action_live(action_type: str, target_resource: str) -> dict:
    """Extension point for real infrastructure calls. boto3 is imported lazily here (rather
    than at module load) so the default mock-only path stays free of that dependency, in
    keeping with the "fast and low resource" goal."""
    try:
        import boto3  # noqa: F401  (heavy; only needed on this path)
    except ImportError as exc:
        raise RuntimeError(
            "USE_MOCK_CLOUD=false but boto3 is not installed. `pip install boto3` and wire "
            "up the real AWS/Docker calls in _execute_infra_action_live()."
        ) from exc

    raise NotImplementedError(
        "Wire up the real infra call here, e.g. boto3 ec2.reboot_instances(...) or "
        "ecs.update_service(...), based on action_type and target_resource."
    )
