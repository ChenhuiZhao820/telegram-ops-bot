import pytest

from src.adapters.sessions import load_tools
from src.orchestrator.loop import Completed
from src.queue.worker import _final_messages


@pytest.mark.asyncio
async def test_close_session_tool_is_write_gated():
    tools = await load_tools()
    assert [t.name for t in tools] == ["close_session"]
    assert tools[0].risk == "write"  # closing must go through the confirm gate
    result = await tools[0].handler({})
    assert result["closed"] is True


def test_closing_task_clears_context():
    closed = Completed(reply="Bye!", messages=[{"role": "user", "content": "hi"}],
                       tools_called=[{"tool": "close_session", "input": {}}])
    assert _final_messages(closed) == []


def test_normal_task_keeps_context():
    normal = Completed(reply="Done.", messages=[{"role": "user", "content": "hi"}],
                       tools_called=[{"tool": "create_record", "input": {}}])
    assert _final_messages(normal) == [{"role": "user", "content": "hi"}]
