from src.gateway.auth import is_allowed


def test_allowed_user(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111, 222")
    assert is_allowed(111)
    assert is_allowed("222")


def test_rejected_user(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,222")
    assert not is_allowed(333)
    assert not is_allowed(None)
    assert not is_allowed("")


def test_empty_whitelist(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "")
    assert not is_allowed(111)
