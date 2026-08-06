from __future__ import annotations

from app.services.privacy import contains_sensitive_hint, redact_sensitive_text


def test_redacts_common_authentication_secrets() -> None:
    text = "password=hunter2 token:abc123 Cookie=session-value Authorization:BearerValue sk-abcdefghijklmnop"
    redacted = redact_sensitive_text(text)
    assert "hunter2" not in redacted
    assert "abc123" not in redacted
    assert "session-value" not in redacted
    assert "BearerValue" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    assert redacted.count("[REDACTED]") >= 5


def test_sensitive_hint_detection() -> None:
    assert contains_sensitive_hint("我的银行卡和密码如下") is True
    assert contains_sensitive_hint("这是一个普通的产品规划问题") is False

