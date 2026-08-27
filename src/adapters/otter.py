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


async def _refresh_token() -> bool:
    """Try an OAuth refresh. Returns True if a new access token was stored.
    TODO(lynn): the exact token endpoint/client id come from scripts/otter_auth.py
    output; if absent we simply can't refresh."""
    refresh = os.environ.get("OTTER_REFRESH_TOKEN")
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
        os.environ["OTTER_ACCESS_TOKEN"] = data["access_token"]
        if data.get("refresh_token") and data["refresh_token"] != refresh:
            os.environ["OTTER_REFRESH_TOKEN"] = data["refresh_token"]
            _persist_rotated_refresh_token(data["refresh_token"])
        return True
    except Exception:
        logger.warning("Otter token refresh failed", exc_info=True)
        return False


def _persist_rotated_refresh_token(new_token: str) -> None:
    """If Otter rotates the refresh token, write it back to .env (local dev)
    so restarts keep working. On Render there is no .env; the env var must be
    updated in the dashboard, so log loudly instead."""
    if not os.path.exists(".env"):
        logger.warning("Otter rotated the refresh token; update OTTER_REFRESH_TOKEN "
                       "in the Render env vars or the next restart will fail auth.")
        return
    try:
        lines = open(".env", encoding="utf-8").read().splitlines()
        lines = [f"OTTER_REFRESH_TOKEN={new_token}"
                 if line.startswith("OTTER_REFRESH_TOKEN=") else line for line in lines]
        open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
        logger.info("Persisted rotated Otter refresh token to .env")
    except Exception:
        logger.warning("Could not persist rotated Otter refresh token", exc_info=True)


@asynccontextmanager
async def _mcp_session():
    # Imported lazily so a missing `mcp` package only disables this adapter.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {os.environ['OTTER_ACCESS_TOKEN']}"}
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
