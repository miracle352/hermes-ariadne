"""Small shared helpers (no Hermes imports)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import re as _re

# Credential-shaped key names: whole words, so "threshold_tokens" / "max_tokens" (settings) stay
# while "api_key", "token", "access_token", "client_secret", "password" go.
_SECRET_KEY = _re.compile(
    r"(^|[_\-.])(api[_\-]?key|key|secret|client[_\-]?secret|password|passwd|passphrase|token|"
    r"access[_\-]?token|refresh[_\-]?token|id[_\-]?token|bearer|credentials?|cookie|session[_\-]?key|private[_\-]?key)$",
    _re.I)


def is_secret_key(name: str) -> bool:
    return bool(_SECRET_KEY.search(name))


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
    except Exception:
        try:
            from ruamel.yaml import YAML  # type: ignore  (Hermes ships ruamel)
            data = YAML(typ="safe").load(text)
        except Exception:
            try:
                data = json.loads(text)
            except Exception:
                data = {}
    return data if isinstance(data, dict) else {}


def strip_secrets(v: Any) -> Any:
    """Drop every mapping key that looks like a credential, recursively."""
    if isinstance(v, dict):
        return {k: strip_secrets(x) for k, x in v.items() if not (isinstance(k, str) and is_secret_key(k))}
    if isinstance(v, list):
        return [strip_secrets(x) for x in v]
    return v


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# Install/progress state that Hermes writes into config.yaml as it runs (not settings). A replay
# must start from the state the recording started from, so these never travel with a cassette.
RUNTIME_STATE_KEYS = ("onboarding", "_config_version")


def settings_only(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (cfg or {}).items() if k not in RUNTIME_STATE_KEYS}


# ---------------------------------------------------------------- replay config allowlist
# A replay builds its config from a cassette (or from the profile it clones). A cassette can
# come from anyone, so only settings that shape what Hermes sends and that cannot run commands,
# start servers or reach an endpoint are carried over. Everything else (hooks, MCP servers,
# terminal, auxiliary endpoints, shell-in-skills ...) is dropped unless the caller trusts the
# source explicitly (--trust-config).
REPLAY_KEYS: dict[str, Any] = {
    "toolsets": True,
    "agent": True,
    "compression": True,
    "prompt_caching": True,
    "tool_output": True,
    "tool_loop_guardrails": True,
    "context": True,
    "context_file_max_chars": True,
    "file_read_max_chars": True,
    "display": {"personality", "compact"},
    "personalities": True,
    "timezone": True,
    "platform_hints": True,
    "privacy": True,
    "vision": True,
    "memory": {"memory_enabled", "user_profile_enabled", "memory_char_limit", "user_char_limit", "nudge_interval"},
    "skills": {"create_dir", "auto_load", "guard_agent_created", "tier1_advisory", "write_approval"},
    "tools": {"tool_search"},
    "delegation": {"max_iterations", "max_summary_chars", "inherit_mcp_toolsets", "compression_threshold_tokens",
                   "child_timeout_seconds"},
    "goals": True,
}


def replay_settings(cfg: dict[str, Any], trust: bool = False) -> tuple[dict[str, Any], list[str]]:
    """The part of ``cfg`` a replay may use, and the top-level keys it left out."""
    if trust:
        return dict(cfg), []
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in cfg.items():
        rule = REPLAY_KEYS.get(key)
        if rule is True:
            kept[key] = value
        elif isinstance(rule, set) and isinstance(value, dict):
            sub = {k: v for k, v in value.items() if k in rule}
            if sub:
                kept[key] = sub
            if len(sub) != len(value):
                dropped.append(key + ".*")
        elif key not in ("model", "plugins"):
            dropped.append(key)
    return kept, sorted(dropped)
