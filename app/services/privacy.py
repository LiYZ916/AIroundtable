from __future__ import annotations

import re


SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_-]?key|token|cookie|password|passwd|密码)\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(bearer\s+)([a-z0-9._~+\-/]+=*)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
]


def redact_sensitive_text(text: str) -> str:
    result = text
    for pattern in SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def contains_sensitive_hint(text: str) -> bool:
    keywords = (
        "password",
        "passwd",
        "api key",
        "api_key",
        "token",
        "cookie",
        "身份证",
        "银行卡",
        "密码",
        "访问密钥",
    )
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)

