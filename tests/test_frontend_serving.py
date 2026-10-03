from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from app.main import app  # noqa: E402


async def _check_frontend_pages() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        home = await client.get("/")
        dashboard = await client.get("/dashboard.html")
        onboarding = await client.get("/onboarding.html")

    assert home.status_code == 200
    assert "<title>Hyperion WarRoom</title>" in home.text
    assert dashboard.status_code == 200
    assert onboarding.status_code == 200
    assert "Hyperion Onboarding Dashboard" in onboarding.text


def test_frontend_pages_are_served() -> None:
    asyncio.run(_check_frontend_pages())


if __name__ == "__main__":
    test_frontend_pages_are_served()