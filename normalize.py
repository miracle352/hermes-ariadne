"""Canonical request forms and structured diffs (pure code).

Ariadne compares what Hermes *would send* to the model during replay with what
it sent during recording. Before comparing, both sides are scrubbed and a small
set of known-volatile values is masked, so a difference means Hermes really
built a different request.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any, Iterable

from . import redact

_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<datetime>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{8}_\d{6}(?:_[0-9a-f]{4,})?\b"), "<session-stamp>"),
    # runtime-minted ids Hermes puts in tool results (delegations, background processes, jobs)
    (re.compile(r"\b(deleg|proc|job|bg|sub|subagent|run)_[0-9a-f]{6,}\b"), r"<\1-id>"),
    (re.compile(r"\bsa-\d+-[0-9a-f]{6,}\b"), "<subagent-id>"),
    # wall-clock durations Hermes reports back to the model ("Total duration: 0.44s", "(0s ago)")
    (re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:ms|s)\b"), "<duration>"),
]


def path_variants(path: str, user_home: str | None = None) -> set[str]:
    """Every spelling Hermes may use for one path: native, forward-slash, and ``~``-relative."""
    if not path:
        return set()
    out = {path, path.replace("\\", "/")}
    if user_home:
        for h in {user_home, user_home.replace("\\", "/")}:
            for p in list(out):
                if p.lower().startswith(h.lower()) and len(p) > len(h):
                    rest = p[len(h):].lstrip("\\/")
                    out.add("~/" + rest.replace("\\", "/"))
                    out.add("~\\" + rest)
    return {p for p in out if len(p) > 3}


def guess_user_home(path: str) -> str | None:
    m = re.match(r"^([A-Za-z]:[\\/]Users[\\/][^\\/]+|/home/[^/]+|/Users/[^/]+|/root)", path or "")
    return m.group(1) if m else None


class Masker:
    """Applies volatile-value masks plus path rewrites (e.g. two different HERMES_HOME roots)."""

    def __init__(self, path_aliases: dict[str, str] | None = None, extra: Iterable[tuple[str, str]] = (),
                 user_homes: Iterable[str] = ()):
        expanded: dict[str, str] = {}
        homes = [h for h in user_homes if h]
        for src, alias in (path_aliases or {}).items():
            uh = next((h for h in homes if src.lower().startswith(h.lower()) or src.replace("\\", "/").lower().startswith(h.replace("\\", "/").lower())), None)
            for v in path_variants(src, uh):
                expanded.setdefault(v, alias)
        self.path_aliases = sorted(expanded.items(), key=lambda kv: -len(kv[0]))
        self.extra = [(re.compile(p), r) for p, r in extra]

    def text(self, s: str) -> str:
        for src, alias in self.path_aliases:
            if src:
                s = s.replace(src, alias)
        for pattern, repl in _MASKS:
            s = pattern.sub(repl, s)
        for pattern, repl in self.extra:
            s = pattern.sub(repl, s)
        return s

    def tree(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.tree(x) for x in v]
        if isinstance(v, dict):
            return {k: self.tree(x) for k, x in v.items()}
        return v


def _maybe_json(s: Any) -> Any:
    if isinstance(s, str) and s[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            return s
    return s


def _jsonable(v: Any) -> Any:
    """Requests can hold non-JSON members (clients, callbacks); keep only data."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items() if not callable(x)}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    dump = getattr(v, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump())
        except Exception:
            pass
    return f"<{type(v).__name__}>"


# Request keys that are transport plumbing, never model-facing content.
_PLUMBING = {"extra_headers", "timeout", "stream_options", "extra_query", "idempotency_key"}


def canonical_request(request: Any) -> dict[str, Any]:
    """Split a provider request into comparable sections."""
    req = _jsonable(request) if not isinstance(request, dict) else _jsonable(dict(request))
    messages = req.get("messages")
    if messages is None and isinstance(req.get("input"), list):  # Responses API
        messages = req.get("input")
    system: list[Any] = []
    convo: list[Any] = []
    if isinstance(req.get("system"), (str, list)):  # Anthropic Messages
        system.append(req["system"])
    for m in messages or []:
        if isinstance(m, dict):
            m = dict(m)
            if isinstance(m.get("tool_calls"), list):
                m["tool_calls"] = [
                    {**tc, "function": {**tc.get("function", {}), "arguments": _maybe_json(tc.get("function", {}).get("arguments"))}}
                    if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else tc
                    for tc in m["tool_calls"]
                ]
            if m.get("role") in ("system", "developer"):
                system.append(m.get("content"))
                continue
        convo.append(m)
    tools: dict[str, Any] = {}
    for t in req.get("tools") or []:
        if isinstance(t, dict):
            name = (t.get("function") or {}).get("name") or t.get("name") or json.dumps(t, sort_keys=True)[:40]
            tools[name] = t
    params = {k: v for k, v in req.items()
              if k not in ("messages", "input", "tools", "system", "model", "stream") and k not in _PLUMBING}
    return {"model": req.get("model"), "system": system, "messages": convo, "tools": tools, "params": params}


def prepare(request: Any, masker: Masker, extra_redact=None) -> dict[str, Any]:
    return masker.tree(redact.scrub(canonical_request(request), extra_redact))


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _short(v: Any, n: int = 400) -> Any:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def diff(recorded: Any, live: Any, path: str = "", limit: int = 40) -> list[dict[str, Any]]:
    """Structured differences between two JSON trees (bounded)."""
    out: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, p: str) -> None:
        if len(out) >= limit or a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b), key=str):
                if k not in a:
                    out.append({"path": f"{p}.{k}", "change": "added", "live": _short(b[k])})
                elif k not in b:
                    out.append({"path": f"{p}.{k}", "change": "removed", "recorded": _short(a[k])})
                else:
                    walk(a[k], b[k], f"{p}.{k}")
            return
        if isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                if i >= len(a):
                    out.append({"path": f"{p}[{i}]", "change": "added", "live": _short(b[i])})
                elif i >= len(b):
                    out.append({"path": f"{p}[{i}]", "change": "removed", "recorded": _short(a[i])})
                else:
                    walk(a[i], b[i], f"{p}[{i}]")
            return
        entry: dict[str, Any] = {"path": p or ".", "change": "changed"}
        if isinstance(a, str) and isinstance(b, str) and (len(a) > 120 or len(b) > 120):
            entry["text_diff"] = [
                line for line in difflib.unified_diff(a.splitlines(), b.splitlines(), "recorded", "live", lineterm="", n=1)
            ][:60]
        else:
            entry["recorded"], entry["live"] = _short(a), _short(b)
        out.append(entry)

    walk(recorded, live, path)
    return out


def section_of(path: str) -> str:
    return path.lstrip(".").split(".")[0].split("[")[0] or "request"


# ---------------------------------------------------------------- environment split
# Parts of the system prompt that describe the machine or the install rather than how Hermes
# builds a request. They are compared separately and reported as environment, never as drift.
_SKILLS_BLOCK = re.compile(r"<available_skills>[\s\S]*?</available_skills>")
_ENV_LINES = [
    re.compile(r"^Python toolchain:.*$", re.M),
    re.compile(r"^Node toolchain:.*$", re.M),
    re.compile(r"^OS:.*$", re.M),
    re.compile(r"^Conversation started:.*$", re.M),   # the date: a replay on another day must still match
    re.compile(r"^Host:.*$", re.M),
    re.compile(r"^User home directory:.*$", re.M),
]
_PROVIDER_LINE = re.compile(r"^Provider: .*$", re.M)


def split_environment(system: list[Any], provider_substituted: bool = False) -> tuple[list[Any], dict[str, Any]]:
    core: list[Any] = []
    env: dict[str, Any] = {"skills": [], "lines": []}
    for part in system or []:
        if not isinstance(part, str):
            core.append(part)
            continue
        for block in _SKILLS_BLOCK.findall(part):
            env["skills"].extend(sorted(line.strip() for line in block.splitlines()
                                        if line.strip().startswith("- ")))
        text = _SKILLS_BLOCK.sub("<available_skills/>", part)
        for pattern in _ENV_LINES + ([_PROVIDER_LINE] if provider_substituted else []):
            env["lines"].extend(pattern.findall(text))
            text = pattern.sub("<environment/>", text)
        core.append(text)
    return core, env


def environment_delta(rec_env: dict[str, Any], live_env: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rs, ls = set(rec_env.get("skills") or []), set(live_env.get("skills") or [])
    if rs != ls:
        out["skills_added"] = sorted(ls - rs)
        out["skills_removed"] = sorted(rs - ls)
    rl, ll = rec_env.get("lines") or [], live_env.get("lines") or []
    if rl != ll:
        out["lines"] = {"recorded": rl, "live": ll}
    return out


# Hermes appends a one-time note to an install's very first user message. Whether it appears
# depends on the install's history, not on how Hermes builds requests.
_FIRST_CONTACT = re.compile(r"\s*\[System note: This is the user's very first message ever\.[\s\S]*?\]\s*$")


def strip_first_contact(messages: list[Any]) -> tuple[list[Any], bool]:
    found = False
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            new = _FIRST_CONTACT.sub("", m["content"])
            if new != m["content"]:
                found = True
                m = {**m, "content": new}
        out.append(m)
    return out, found
