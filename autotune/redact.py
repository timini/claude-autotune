"""Best-effort secret redaction for captured text.

This catches common credential shapes only. It is not a guarantee that a
redacted string contains no secrets. Every pattern is anchored with a
lookbehind on its own character class and avoids nested quantifiers, so the
cost stays linear in the input length on adversarial text.
"""

from __future__ import annotations

import re

_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "bearer_token",
        re.compile(
            r"(?i)(\bauthorization[ \t]{0,20}:[ \t]{0,20}bearer[ \t]{1,20})"
            r"(?!\[REDACTED)([A-Za-z0-9._~+/=-]{1,4096})"
        ),
    ),
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})"),
    ),
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}")),
    ("api_key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
        ),
    ),
]

# A keyword, an optional key-like tail (`SECRET_ACCESS_KEY`, `SECRET_KEY`),
# a separator, then the value. Quotes may be JSON-escaped (`\"`) because tool
# inputs are captured as JSON text; a backslash never belongs to the value.
_CREDENTIAL = re.compile(
    r"(?i)((?:password|passwd|secret|token|api_key|apikey)(?:_(?:access_)?key)?"
    r"(?:\\?[\"'])?[ \t]{0,20}[:=][ \t]{0,20}(?:\\?[\"'])?)"
    r"(?!\[REDACTED)([^\s\"'\\,;&]{1,512})"
)

# scheme://user:password@host
_URL_USERINFO = re.compile(
    r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,30}://[^\s/:@\"'\\]{1,256}:)"
    r"(?!\[REDACTED)([^\s/@\"'\\]{1,256})(?=@)"
)

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _redact_pem(text: str) -> tuple[str, int]:
    if "PRIVATE KEY-----" not in text:
        return text, 0
    out: list[str] = []
    pos = 0
    count = 0
    while True:
        begin = _PEM_BEGIN.search(text, pos)
        if begin is None:
            break
        out.append(text[pos:begin.start()])
        out.append(_marker("private_key"))
        count += 1
        end = _PEM_END.search(text, begin.end())
        if end is None:
            # A truncated key is still a secret: drop everything after BEGIN.
            pos = len(text)
            break
        pos = end.end()
    out.append(text[pos:])
    return "".join(out), count


def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions). Best effort only."""
    if not text:
        return text, 0
    text, total = _redact_pem(text)
    for kind, pattern in _TOKEN_PATTERNS:
        if kind == "bearer_token":
            text, n = pattern.subn(lambda m: m.group(1) + _marker("bearer_token"), text)
        else:
            text, n = pattern.subn(_marker(kind), text)
        total += n
    text, n = _CREDENTIAL.subn(lambda m: m.group(1) + _marker("credential"), text)
    total += n
    text, n = _URL_USERINFO.subn(lambda m: m.group(1) + _marker("credential"), text)
    return text, total + n
