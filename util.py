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
