"""Chat-session lifecycle adapter: one tool, close_session.

Chat context persists across messages until the founder permits closing the
session. close_session is risk=write so the confirmation gate notifies the
founder and waits for their Confirm before the context is cleared. The actual
clearing happens in the worker: when a completed task's tools include
close_session, its stored conversation is emptied, which is the boundary the
context-inheritance lookup respects.
"""

from src.adapters.base import Tool

CLOSE_SESSION = "close_session"


async def close_session(args: dict):
    return {"closed": True,
            "note": "Session will be cleared after this task completes. Say goodbye briefly."}


async def load_tools() -> list[Tool]:
    return [
        Tool(
            name=CLOSE_SESSION,
            description=("Close the current chat session and clear its context/memory. Call "
                         "this ONLY after the founder has explicitly agreed to end the session "
                         "(e.g. you asked 'anything else, or shall I close this session?' and "
                         "they said no / go ahead). Never call it unprompted."),
            input_schema={"type": "object", "properties": {}},
            risk="write", handler=close_session),
    ]
