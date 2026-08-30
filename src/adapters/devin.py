"""Devin REST adapter. Three tools, fire-and-forget sessions.

Recent sessions are tracked in the local devin_sessions table so a founder can
ask "how's Devin doing?" without quoting a session ID.
"""

import logging
import os

import httpx
from sqlalchemy import select

from src.adapters.base import Tool, current_chat_id, current_user_id
from src.queue.models import DevinSession, db_session, utcnow

logger = logging.getLogger(__name__)

# v3 API: service-user keys (cog_...) scoped to an organization.
API = "https://api.devin.ai/v3"


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}"}


def _org_path(path: str) -> str:
    return f"/organizations/{os.environ['DEVIN_ORG_ID']}{path}"


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
    # bypass_approval: the founders' approval gate lives in Telegram (the
    # confirm button before this tool runs); Devin's own safe-mode approval
    # prompts would otherwise stall sessions with nobody there to click Allow.
    result = await _request("POST", _org_path("/sessions"),
                            json={"prompt": args["task_description"],
                                  "bypass_approval": True})
    if isinstance(result, dict) and result.get("error"):
        return result
    session_id = result.get("session_id")
    if session_id:
        with db_session() as db:
            db.add(DevinSession(session_id=session_id,
                                task_description=args["task_description"],
                                created_by=current_user_id.get(),
                                chat_id=current_chat_id.get() or current_user_id.get()))
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
    status = await _request("GET", _org_path(f"/sessions/{session_id}"))
    if isinstance(status, dict) and not status.get("error"):
        status = {"session_id": session_id,
                  "status": status.get("status"),
                  "status_detail": status.get("status_detail"),
                  "title": status.get("title"),
                  "structured_output": status.get("structured_output"),
                  "pull_requests": status.get("pull_requests"),
                  "url": status.get("url")
                  or f"https://app.devin.ai/sessions/{session_id.removeprefix('devin-')}"}
    return status


async def archive_devin_session(args: dict):
    result = await _request("POST", _org_path(f"/sessions/{args['session_id']}/archive"))
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"archived": True, "session_id": args["session_id"]}


async def send_message_to_devin(args: dict):
    result = await _request("POST", _org_path(f"/sessions/{args['session_id']}/messages"),
                            json={"message": args["message"]})
    if isinstance(result, dict) and result.get("error"):
        return result
    return {"sent": True, "session_id": args["session_id"]}


TERMINAL = ("exit", "error")
WATCH_WINDOW_DAYS = 7


def attention_message(status: str, detail: str, task: str, url: str) -> str | None:
    """Human message for a state that needs the founder's awareness, or None
    if the state is routine (e.g. actively working)."""
    label = f"*Devin update* ({task[:80]}):\n"
    if detail == "waiting_for_user":
        return label + f"Devin has a question and is waiting for your input. Ask me \"how's Devin doing\" for details, or tell me what to answer.\n{url}"
    if detail == "waiting_for_approval":
        return label + f"Devin is waiting for an action approval.\n{url}"
    if detail == "finished" or status == "exit":
        return label + f"Devin finished the task. Ask me for the results.\n{url}"
    if status == "error" or detail == "error":
        return label + f"Devin hit an error and stopped.\n{url}"
    if status == "suspended" and detail not in ("user_request",):
        reason = (detail or "unknown reason").replace("_", " ")
        return label + f"Devin was suspended ({reason}) and is NOT making progress. It may need repo access, credits, or a nudge.\n{url}"
    return None


async def watch_sessions() -> list[tuple[str, str]]:
    """Polls recent sessions; returns (chat_id, message) notifications for any
    session whose state changed to something the founder should know about."""
    from datetime import timedelta

    notifications: list[tuple[str, str]] = []
    with db_session() as db:
        rows = db.scalars(select(DevinSession).where(
            DevinSession.created_at >= utcnow() - timedelta(days=WATCH_WINDOW_DAYS))).all()
        for row in rows:
            prev = row.last_status or ""
            if prev.split("/")[0] in TERMINAL or prev.endswith("/finished"):
                continue  # already reported a terminal state, stop polling it
            data = await _request("GET", _org_path(f"/sessions/{row.session_id}"))
            if not isinstance(data, dict) or data.get("error"):
                continue
            status, detail = data.get("status", ""), data.get("status_detail") or ""
            state = f"{status}/{detail}"
            if state == prev:
                continue
            row.last_status = state
            # Routine states (working etc.) yield None, so even the first
            # observation after creation notifies only when action is needed.
            msg = attention_message(status, detail, row.task_description,
                                    data.get("url") or "")
            if msg and row.chat_id:
                notifications.append((row.chat_id, msg))
        db.commit()
    return notifications


async def load_tools() -> list[Tool]:
    if not os.environ.get("DEVIN_API_KEY"):
        raise ValueError("DEVIN_API_KEY not set")
    if not os.environ.get("DEVIN_ORG_ID"):
        raise ValueError("DEVIN_ORG_ID not set (org-... ID required by the v3 API)")
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
                         "list_recent=true to list recent sessions instead. If status_detail "
                         "is 'waiting_for_user', Devin has a question — relay it to the founder "
                         "and forward their answer with send_message_to_devin."),
            input_schema={"type": "object",
                          "properties": {"session_id": {"type": "string"},
                                         "list_recent": {"type": "boolean"}}},
            risk="read", handler=get_devin_session),
        Tool(
            name="send_message_to_devin",
            description=("Send a follow-up instruction or an answer to Devin's question in a "
                         "session. Automatically resumes the session if it was suspended."),
            input_schema={"type": "object",
                          "properties": {"session_id": {"type": "string"},
                                         "message": {"type": "string"}},
                          "required": ["session_id", "message"]},
            risk="write", handler=send_message_to_devin),
        Tool(
            name="archive_devin_session",
            description=("Close (archive) a Devin session that is finished or no longer "
                         "needed, so it stops consuming attention and resources."),
            input_schema={"type": "object",
                          "properties": {"session_id": {"type": "string"}},
                          "required": ["session_id"]},
            risk="write", handler=archive_devin_session),
    ]
