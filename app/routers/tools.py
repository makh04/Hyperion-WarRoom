from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/tools", tags=["tools"])

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_STORE = Path("temp/tools.json")

# ---------------------------------------------------------------------------
# Internal model
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    id: str
    name: str
    url: str
    description: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body_template: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------

_tools: dict[str, Tool] = {}


def _load() -> None:
    """Load tools from disk; silently ignore if the file does not exist."""
    if not _STORE.exists():
        return
    try:
        raw: list[dict] = json.loads(_STORE.read_text(encoding="utf-8"))
        for item in raw:
            tool = Tool(**item)
            _tools[tool.id] = tool
    except Exception:
        pass


def _save() -> None:
    """Persist all tools to disk as a JSON list."""
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(t) for t in _tools.values()]
    _STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# Load on import
_load()

# ---------------------------------------------------------------------------
# Request / response schemas  (Pydantic)
# ---------------------------------------------------------------------------


class CreateToolRequest(BaseModel):
    name: str
    url: str
    description: str = ""
    method: str = "POST"
    headers: dict[str, str] = {}
    body_template: Optional[str] = None


class UpdateToolRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    method: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    body_template: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_tools() -> list[dict]:
    """Return all registered tools."""
    return [asdict(t) for t in _tools.values()]


@router.post("")
async def create_tool(body: CreateToolRequest) -> dict:
    """Create a new tool and persist it."""
    tool = Tool(
        id=uuid.uuid4().hex,
        name=body.name,
        description=body.description,
        url=body.url,
        method=body.method,
        headers=body.headers,
        body_template=body.body_template,
        created_at=time.time(),
    )
    _tools[tool.id] = tool
    _save()
    return asdict(tool)


@router.put("/{tool_id}")
async def update_tool(tool_id: str, body: UpdateToolRequest) -> dict:
    """Update an existing tool (partial update). 404 if not found."""
    tool = _tools.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    if body.name is not None:
        tool.name = body.name
    if body.url is not None:
        tool.url = body.url
    if body.description is not None:
        tool.description = body.description
    if body.method is not None:
        tool.method = body.method
    if body.headers is not None:
        tool.headers = body.headers
    if body.body_template is not None:
        tool.body_template = body.body_template

    _save()
    return asdict(tool)


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str) -> dict:
    """Delete a tool. 404 if not found."""
    if tool_id not in _tools:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")
    del _tools[tool_id]
    _save()
    return {"deleted": True, "id": tool_id}


@router.post("/{tool_id}/test")
async def test_tool(tool_id: str) -> dict:
    """Fire the tool's HTTP request and return the response details."""
    tool = _tools.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

    try:
        async with httpx.AsyncClient() as client:
            kwargs: dict = {
                "method": tool.method,
                "url": tool.url,
                "headers": tool.headers,
            }
            if tool.body_template is not None:
                kwargs["content"] = tool.body_template.encode()

            response = await client.request(**kwargs)
            return {
                "status_code": response.status_code,
                "response_text": response.text,
                "ok": response.is_success,
            }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_all_tools() -> list[dict]:
    """Public helper used by agent_dispatch to read current tools."""
    return [asdict(t) if hasattr(t, "__dataclass_fields__") else t for t in _tools.values()]

