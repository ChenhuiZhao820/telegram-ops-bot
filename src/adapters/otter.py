"""Otter.ai adapter — MCP client, read-only.

Connects to Otter's remote MCP server, lists its tools at runtime, and
re-exposes only search/retrieval tools (prefixed `otter_`, all risk=read).
Tool names are NOT hardcoded beyond the filter keywords, because Otter's MCP
schema is not fully documented publicly.
"""

import json
import logging
import os
from contextlib import asynccontextmanager

import httpx

from src.adapters.base import Tool

logger = logging.getLogger(__name__)

TRANSCRIPT_TRUNCATE_CHARS = 8000
# A tool is exposed only if its name contains one of these and none of the deny words.
ALLOW_WORDS = ("search", "get", "list", "find", "fetch", "transcript", "summary", "action")
DENY_WORDS = ("create", "update", "delete", "write", "add", "remove", "set", "edit",
              "share", "export", "upload", "post", "send")

_expired = False  # set when auth fails and cannot be refreshed


def _mcp_url() -> str:
    return os.environ.get("OTTER_MCP_URL", "https://mcp.otter.ai/mcp")


def _load_tokens() -> tuple[str, str]:
    """Tokens live in the database because Otter rotates the refresh token on
    every use and env vars can't be updated from a running process. The env
    vars act as the seed: if OTTER_REFRESH_TOKEN differs from the seed that
    created the DB row, a human re-authed, so the row is reseeded from env."""
    from src.queue.models import OtterToken, db_session

    env_access = os.environ["OTTER_ACCESS_TOKEN"]
    env_refresh = os.environ.get("OTTER_REFRESH_TOKEN", "")
    with db_session() as db:
        row = db.get(OtterToken, 1)
        if row is None or row.seed_refresh_token != env_refresh:
            row = row or OtterToken(id=1)
            row.access_token = env_access
            row.refresh_token = env_refresh
            row.seed_refresh_token = env_refresh
            db.add(row)
            db.commit()
            logger.info("Seeded Otter tokens from environment")
        return row.access_token, row.refresh_token


def _save_tokens(access: str, refresh: str) -> None:
    from src.queue.models import OtterToken, db_session

    with db_session() as db:
        row = db.get(OtterToken, 1)
        row.access_token = access
        if refresh:
            row.refresh_token = refresh
        db.commit()
    logger.info("Persisted rotated Otter tokens to the database")


async def _refresh_token() -> bool:
    """Try an OAuth refresh. Returns True if a new access token was stored."""
    _, refresh = _load_tokens()
    token_url = os.environ.get("OTTER_TOKEN_URL")
    client_id = os.environ.get("OTTER_CLIENT_ID")
    if not (refresh and token_url and client_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(token_url, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            })
            resp.raise_for_status()
            data = resp.json()
        _save_tokens(data["access_token"], data.get("refresh_token", ""))
        return True
    except Exception:
        logger.warning("Otter token refresh failed", exc_info=True)
        return False


@asynccontextmanager
async def _mcp_session():
    # Imported lazily so a missing `mcp` package only disables this adapter.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    access, _ = _load_tokens()
    headers = {"Authorization": f"Bearer {access}"}
    async with streamablehttp_client(_mcp_url(), headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _is_read_tool(name: str) -> bool:
    lowered = name.lower()
    return any(w in lowered for w in ALLOW_WORDS) and not any(w in lowered for w in DENY_WORDS)


def _result_to_text(result) -> str:
    parts = []
    for block in result.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(json.dumps(getattr(block, "model_dump", dict)(), default=str))
    return "\n".join(parts)


def _make_handler(remote_name: str):
    async def handler(args: dict):
        global _expired
        if _expired:
            return {"error": "Otter connection expired, re-auth needed"}
        for attempt in range(2):
            try:
                async with _mcp_session() as session:
                    result = await session.call_tool(remote_name, args)
                text = _result_to_text(result)
                # Otter's generic fetch tool returns full transcripts; anything
                # oversized gets cut so a 90-minute meeting doesn't blow the
                # context window. Summaries/action items are far below the cap.
                if len(text) > TRANSCRIPT_TRUNCATE_CHARS:
                    text = text[:TRANSCRIPT_TRUNCATE_CHARS] + "\n\n[transcript truncated]"
                return text
            except Exception as exc:
                if attempt == 0 and _looks_like_auth_error(exc) and await _refresh_token():
                    continue
                if _looks_like_auth_error(exc):
                    _expired = True
                    return {"error": "Otter connection expired, re-auth needed"}
                raise
    return handler


def _looks_like_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_looks_like_auth_error(sub) for sub in exc.exceptions)
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text or "403" in text


async def load_tools() -> list[Tool]:
    if not os.environ.get("OTTER_ACCESS_TOKEN"):
        logger.warning("OTTER_ACCESS_TOKEN not set; Otter adapter stubbed (zero tools)")
        return []
    try:
        async with _mcp_session() as session:
            listed = await session.list_tools()
    except Exception as exc:
        if not (_looks_like_auth_error(exc) and await _refresh_token()):
            raise
        async with _mcp_session() as session:
            listed = await session.list_tools()
    tools: list[Tool] = []
    for remote in listed.tools:
        if not _is_read_tool(remote.name):
            logger.info("Skipping non-read Otter tool: %s", remote.name)
            continue
        exposed = remote.name if remote.name.startswith("otter_") else f"otter_{remote.name}"
        tools.append(Tool(
            name=exposed,
            description=(remote.description or remote.name)
            + " (Otter.ai meeting data, read-only.)",
            input_schema=remote.inputSchema or {"type": "object", "properties": {}},
            risk="read",
            handler=_make_handler(remote.name),
        ))
    return tools
