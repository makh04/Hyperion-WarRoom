from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Save Onboarding Setup
        onboarding_payload = {
            "service_name": "Test Auth Microservice",
            "service_summary": "Handles user login, OAuth2, and JWT session tokens.",
            "developers": [
                {"email": "backend-lead@example.com", "role": "Backend Lead"},
                {"email": "devops@example.com", "role": "DevOps Engineer"}
            ]
        }
        res_save = await client.post("/api/onboarding", json=onboarding_payload)
        assert res_save.status_code == 200, res_save.text
        data_save = res_save.json()
        assert data_save["status"] == "success"
        assert data_save["data"]["service_name"] == "Test Auth Microservice"

        # 2. Retrieve Saved Setup
        res_get = await client.get("/api/onboarding")
        assert res_get.status_code == 200
        assert res_get.json()["service_name"] == "Test Auth Microservice"
        assert len(res_get.json()["developers"]) == 2

        # 3. Test Live Request with Invalid Token
        res_unauth = await client.post("/api/live-request", headers={"X-API-Token": "invalid_token"})
        assert res_unauth.status_code == 401

        # 4. Test Live Request with Valid Token
        valid_token = settings.live_website_api_token
        live_payload = {
            "event": "auth_db_timeout",
            "error": "Connection pool exhausted to postgres DB",
            "impact": "Users unable to log in"
        }
        res_live = await client.post("/api/live-request", headers={"X-API-Token": valid_token}, json=live_payload)
        assert res_live.status_code == 200, res_live.text
        data_live = res_live.json()
        assert data_live["verified"] is True
        assert "incident_id" in data_live
        assert "meet_url" in data_live
        assert isinstance(data_live["invited_developers"], list)

        # 5. Serve onboarding.html
        res_html = await client.get("/onboarding.html")
        assert res_html.status_code == 200
        assert "<title>Hyperion Onboarding Dashboard</title>" in res_html.text

    print("ONBOARDING TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
