from src.adapters.otter import _is_read_tool


def test_read_tools_allowed():
    assert _is_read_tool("search_conversations")
    assert _is_read_tool("get_transcript")
    assert _is_read_tool("get_summary")
    assert _is_read_tool("get_action_items")
    assert _is_read_tool("fetch")


def test_write_tools_denied():
    assert not _is_read_tool("create_note")
    assert not _is_read_tool("delete_conversation")
    assert not _is_read_tool("update_summary")
    assert not _is_read_tool("share_conversation")
