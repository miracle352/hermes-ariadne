"""Lossless JSON encoding of provider responses and tool results.

Hermes hands execution middleware a provider response that is a tree of
``types.SimpleNamespace`` objects with pydantic models (``CompletionUsage``,
Anthropic ``Message`` blocks, ...) inside. Downstream code reads it with
``getattr``, so a faithful replay must rebuild the same *shapes*, not just the
same data. Every non-JSON node is tagged with how to rebuild it.

Tags (a dict with exactly one of these keys is a tagged node):
    "$ns"   -> types.SimpleNamespace(**decoded)
    "$pyd"  -> pydantic model: {"$pyd": "module:Qualname", "data": {...}}
    "$tuple"-> tuple
    "$bytes"-> bytes, base64
    "$repr" -> an object we could not encode; decoded back as a str and the
               cassette is marked lossy so replay can warn instead of lying.
Plain dicts whose keys start with "$" are escaped as {"$dict": {...}}.
"""

from __future__ import annotations

import base64
import importlib
from types import SimpleNamespace
from typing import Any

_TAGS = ("$ns", "$pyd", "$tuple", "$bytes", "$repr", "$dict")
_MAX_DEPTH = 64


class LossyEncoding(Exception):
    """Raised by :func:`decode` in strict mode when a ``$repr`` node is met."""


def encode(obj: Any, _depth: int = 0, _lossy: list | None = None) -> Any:
    """Encode ``obj`` into JSON-safe data. Appends a path note to ``_lossy`` for lossy nodes."""
    if _depth > _MAX_DEPTH:
        if _lossy is not None:
            _lossy.append("max-depth")
        return {"$repr": "<max depth>"}
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    nxt = _depth + 1
    if isinstance(obj, SimpleNamespace):
        return {"$ns": {k: encode(v, nxt, _lossy) for k, v in vars(obj).items()}}
    if isinstance(obj, dict):
        body = {str(k): encode(v, nxt, _lossy) for k, v in obj.items()}
        if any(k.startswith("$") for k in body):
            return {"$dict": body}
        return body
    if isinstance(obj, list):
        return [encode(v, nxt, _lossy) for v in obj]
    if isinstance(obj, tuple):
        return {"$tuple": [encode(v, nxt, _lossy) for v in obj]}
    if isinstance(obj, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(obj)).decode("ascii")}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        cls = type(obj)
        try:
            data = dump(mode="python", by_alias=False, exclude_unset=False)
        except TypeError:
            data = dump()
        # model_extra fields (provider-specific extras) are part of model_dump for extra="allow".
        return {"$pyd": f"{cls.__module__}:{cls.__qualname__}", "data": encode(data, nxt, _lossy)}
    if hasattr(obj, "__dict__") and not callable(obj):
        if _lossy is not None:
            _lossy.append(f"object:{type(obj).__qualname__}")
        return {"$ns": {k: encode(v, nxt, _lossy) for k, v in vars(obj).items() if not k.startswith("_")}}
    if _lossy is not None:
        _lossy.append(f"repr:{type(obj).__qualname__}")
    return {"$repr": repr(obj)}


def _import(path: str) -> Any:
    module, _, qual = path.partition(":")
    target: Any = importlib.import_module(module)
    for part in qual.split("."):
        target = getattr(target, part)
    return target


def decode(data: Any, *, strict: bool = False) -> Any:
    """Rebuild the object tree produced by :func:`encode`."""
    if isinstance(data, list):
        return [decode(v, strict=strict) for v in data]
    if not isinstance(data, dict):
        return data
    if len(data) == 1 or (len(data) == 2 and "$pyd" in data):
        if "$ns" in data:
            return SimpleNamespace(**{k: decode(v, strict=strict) for k, v in data["$ns"].items()})
        if "$tuple" in data:
            return tuple(decode(v, strict=strict) for v in data["$tuple"])
        if "$bytes" in data:
            return base64.b64decode(data["$bytes"])
        if "$dict" in data:
            return {k: decode(v, strict=strict) for k, v in data["$dict"].items()}
        if "$repr" in data:
            if strict:
                raise LossyEncoding(data["$repr"])
            return data["$repr"]
        if "$pyd" in data:
            fields = decode(data.get("data"), strict=strict)
            try:
                cls = _import(data["$pyd"])
                return cls.model_validate(fields)
            except Exception:
                # The model class moved or is unavailable on this Hermes: fall back to an
                # attribute tree, which is what downstream getattr-based readers need.
                return _to_ns(fields)
    return {k: decode(v, strict=strict) for k, v in data.items()}


def _to_ns(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_ns(v) for v in value]
    return value


def encode_checked(obj: Any) -> tuple[Any, list[str]]:
    """Encode and report lossy paths (empty list = lossless)."""
    lossy: list[str] = []
    return encode(obj, _lossy=lossy), lossy
