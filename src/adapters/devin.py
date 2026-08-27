"""Devin REST adapter. Three tools, fire-and-forget sessions.

Recent sessions are tracked in the local devin_sessions table so a founder can
ask "how's Devin doing?" without quoting a session ID.
"""

import logging
import os

import httpx
from sqlalchemy import select

from src.adapters.base import Tool, current_user_id
from src.queue.models import DevinSession, db_session

logger = logging.getLogger(__name__)

API = "https://api.devin.ai/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}"}


async def _request(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as client:
        resp = await client.request(method, f"{API}{path}", **kwargs)
        if resp.status_code >= 500:
            resp = await client.request(method, f"{API}{path}", **kwargs)
            resp.raise_for_status()
        if 400 <= resp.status_code < 500:
            return {"error": resp.status_code, "body": resp.text}
        return resp.json() if resp.text else {}


async def create_devin_session(args: dict):
    result = await _request("POST", "/sessions", json={"prompt": args["task_description"]})
    if isinstance(result, dict) and result.get("error"):
        return result
    session_id = result.get("session_id")
    if session_id:
        with db_session() as db:
            db.add(DevinSession(session_id=session_id,
                                task_description=args["task_description"],
                                created_by=current_user_id.get()))
            db.commit()
    return {"session_id": session_id, "url": result.get("url")}


async def get_devin_session(args: dict):
    with db_session() as db:
        recent = db.scalars(select(DevinSession)
                            .order_by(DevinSession.created_at.desc()).limit(10)).all()
    recent_list = [{"session_id": s.session_id, "task": s.task_description,
                    "created_at": s.created_at.isoformat()} for s in recent]
    if args.get("list_recent"):
        return {"recent_sessions": recent_list}
    session_id = args.get("session_id") or (recent_list[0]["session_id"] if recent_list else None)
    if not session_id:
        return {"error": "No Devin sessions found. Start one with create_devin_session."}
    status = await _request("GET", f"/sessions/{session_id}")
    if isinstance(status, dict) and not status.get("error"):
        status = {"session_id": session_id,
                  "status": status.get("status_enum") or status.get("status"),
                  "title": status.get("title"),
                  "structured_output": status.get("structured_output"),
                  "url": f"https://app.devin.ai/sessions/{session_id.removeprefix('devin-')}"}
    return status


async def send_message_to_devin(args: dict):
    result = await _request("POST", f"/sessions/{args['session_id']}/message",
                            json={"message": args["message"]})
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"sent": True, "session_id": args["session_id"]}


async def load_tools() -> list[Tool]:
    if not os.environ.get("DEVIN_API_KEY"):
        raise ValueError("DEVIN_API_KEY not set")
    return [
        Tool(
            name="create_devin_session",
            description=("Start a new Devin (coding agent) session with a task description. "
                         "Returns the session ID and a web link immediately — do NOT wait or "
                         "poll for completion. Sessions cost real money (ACUs), so the task "
                         "description must be clear and complete in one session."),
            input_schema={"type": "object",
                          "properties": {"task_description": {"type": "string"}},
                          "required": ["task_description"]},
            risk="write", handler=create_devin_session),
        Tool(
            name="get_devin_session",
            description=("Get the status and latest output of a Devin session. If session_id "
                         "is omitted, returns the most recent session's status. Pass "
                         "list_recent=true to list recent sessions instead."),
            input_schema={"type": "object",
                          "properties": {"session_id": {"type": "string"},
                                         "list_recent": {"type": "boolean"}}},
            risk="read", handler=get_devin_session),
        Tool(
            name="send_message_to_devin",
            description="Send a follow-up instruction to a running Devin session.",
            input_schema={"type": "object",
                          "properties": {"session_id": {"type": "string"},
                                         "message": {"type": "string"}},
                          "required": ["session_id", "message"]},
            risk="write", handler=send_message_to_devin),
    ]
