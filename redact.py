"""Secret scrubbing for cassettes (pure code, no model).

A secret is replaced by a stable token ``«secret:<kind>:<hash8>»``: the same secret
always maps to the same token, so a recorded request and a replayed request still
compare equal after both are scrubbed, and nobody can recover the value.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# (kind, pattern). Ordered most-specific first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("openrouter", re.compile(r"\bsk-or-v1-[a-f0-9]{32,}")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("hf", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._\-~+/]{20,}=*")),
    # key=value / "key": "value" for secret-sounding names
    ("assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9])((?:api[_-]?key|secret|token|passw(?:or)?d|auth[_-]?token|access[_-]?key|client[_-]?secret)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{8,})")),
]

_TOKEN_RE = re.compile(r"«secret:[a-z0-9\-]+:[0-9a-f]{8}»")


def _token(kind: str, value: str) -> str:
    return f"«secret:{kind}:{hashlib.sha256(value.encode()).hexdigest()[:8]}»"


def scrub_text(text: str, extra: list[re.Pattern[str]] | None = None) -> str:
    if not text or not isinstance(text, str):
        return text
    for kind, pattern in _PATTERNS:
        if kind == "assignment":
            text = pattern.sub(lambda m: m.group(1) + (m.group(2) if _TOKEN_RE.fullmatch(m.group(2)) else _token(kind, m.group(2))), text)
        else:
            text = pattern.sub(lambda m, k=kind: _token(k, m.group(0)), text)
    for pattern in extra or []:
        text = pattern.sub(lambda m: _token("custom", m.group(0)), text)
    return text


def scrub(value: Any, extra: list[re.Pattern[str]] | None = None) -> Any:
    """Scrub every string inside a JSON-like tree (dict keys included)."""
    if isinstance(value, str):
        return scrub_text(value, extra)
    if isinstance(value, list):
        return [scrub(v, extra) for v in value]
    if isinstance(value, dict):
        return {scrub_text(k, extra) if isinstance(k, str) else k: scrub(v, extra) for k, v in value.items()}
    return value


def compile_extra(patterns: list[str] | None) -> list[re.Pattern[str]]:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out
