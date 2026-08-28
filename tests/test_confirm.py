import pytest

import src.orchestrator.confirm as confirm_mod
from src.orchestrator.confirm import (classify_confirmation_reply, describe_pending,
                                      describe_pending_action)


def test_create_record_description():
    text = describe_pending_action("create_record", {
        "base": "Basanite XRM", "table": "Contacts",
        "fields": {"Name": "Jane Doe", "Company": "Acme", "Title": "VP Eng"}})
    assert "Basanite XRM" in text and "Contacts" in text
    assert "Jane Doe" in text and text.endswith("Confirm?")


def test_devin_session_mentions_credits():
    text = describe_pending_action("create_devin_session",
                                   {"task_description": "Fix the flaky signup test"})
    assert "credits" in text and "Fix the flaky signup test" in text


def test_unknown_write_tool_falls_back():
    text = describe_pending_action("some_future_tool", {"x": 1})
    assert "some_future_tool" in text and text.endswith("Confirm?")


def test_close_session_description():
    text = describe_pending_action("close_session", {})
    assert "Close this session" in text and "context" in text


def test_describe_pending_continue():
    assert "Continue" in describe_pending({"continue": True})
    assert "Contacts" in describe_pending(
        {"name": "create_record", "input": {"base": "Basanite XRM", "table": "Contacts", "fields": {}}})


class _ScriptedClient:
    def __init__(self, reply):
        self.messages = self
        self._reply = reply

    async def create(self, **kw):
        if isinstance(self._reply, Exception):
            raise self._reply
        block = type("B", (), {"type": "text", "text": self._reply})()
        return type("R", (), {"content": [block]})()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,expected", [
    ("confirm", "confirm"), ("Cancel", "cancel"), ("banana", "unrelated")])
async def test_classify_verdicts(monkeypatch, reply, expected):
    monkeypatch.setattr(confirm_mod, "AsyncAnthropic", lambda: _ScriptedClient(reply))
    assert await classify_confirmation_reply("Create record. Confirm?", "yes") == expected


@pytest.mark.asyncio
async def test_classify_defaults_to_unrelated_on_error(monkeypatch):
    monkeypatch.setattr(confirm_mod, "AsyncAnthropic", lambda: _ScriptedClient(RuntimeError("boom")))
    assert await classify_confirmation_reply("Create record. Confirm?", "yes") == "unrelated"
