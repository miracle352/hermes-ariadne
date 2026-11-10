"""Differential replay: one cassette, two Hermes installs, identical scratch homes.

Comparing a replay with its recording mixes two things: what Hermes changed, and how the
replay machine differs from the recording machine. Running the *same* cassette on two
installs side by side cancels the machine out: both replays see the same scratch home, the
same stand-in provider, the same clock and the same skills, so any difference between
them is a change in Hermes itself.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import driver, normalize


@dataclass
class Side:
    label: str
    python: str
    result: driver.PlayResult | None = None
    version: str = ""
    live: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Comparison:
    cassette: str
    a: Side
    b: Side
    changes: list[dict[str, Any]]

    @property
    def verdict(self) -> str:
        return "same" if not self.changes else "changed"

    def to_json(self) -> dict[str, Any]:
        return {"cassette": self.cassette, "verdict": self.verdict,
                "a": {"label": self.a.label, "python": self.a.python, "version": self.a.version,
                      "replay_verdict": self.a.result.verdict if self.a.result else None},
                "b": {"label": self.b.label, "python": self.b.python, "version": self.b.version,
                      "replay_verdict": self.b.result.verdict if self.b.result else None},
                "changes": self.changes}


@functools.lru_cache(maxsize=16)
def hermes_version(python: str) -> str:
    import subprocess
    import tempfile
    from .util import env_for
    try:
        out = subprocess.run([python, "-m", "hermes_cli.main", "--version"], capture_output=True, text=True, timeout=60,
                             env=env_for(python), cwd=tempfile.gettempdir())
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line:
                return line[:120]
    except Exception:
        pass
    return "unknown"


def _load_live(result: driver.PlayResult) -> list[dict[str, Any]]:
    path = result.run_dir / "live.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compare(cassette: str | Path, python_a: str, python_b: str, *, label_a: str = "A", label_b: str = "B",
            opts: driver.PlayOptions | None = None) -> Comparison:
    base = opts or driver.PlayOptions()
    sides = [Side(label_a, python_a), Side(label_b, python_b)]

    def run(side: Side) -> None:
        o = driver.PlayOptions(**{**base.__dict__, "hermes_python": side.python})
        side.result = driver.play(cassette, o)
        side.version = hermes_version(side.python)
        side.live = _load_live(side.result)

    # The two installs replay at the same time: each is its own process in its own scratch home.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, sides))
    a, b = sides
    changes = diff_live(a.live, b.live)
    for side in sides:
        assert side.result is not None
        if side.result.errors:
            changes.append({"kind": "replay_error", "side": side.label, "errors": side.result.errors[:3]})
    if a.result and b.result:
        ra, rb = [_reply_shape(r) for r in a.result.replies], [_reply_shape(r) for r in b.result.replies]
        if ra != rb:
            changes.append({"kind": "final_replies", "a": ra, "b": rb})
    return Comparison(str(cassette), a, b, changes)


def _reply_shape(reply: str) -> str:
    """Structured command output (a manual compression's JSON report) carries token counts that
    follow from how long the request was; the request diff already shows that change."""
    import re
    text = reply if isinstance(reply, str) else json.dumps(reply)
    if text.lstrip().startswith(("{", "<session.")):  # e.g. "<session.compress: {...}"
        return re.sub(r"\d+", "#", text)
    return text


def diff_live(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    la = [e for e in a if e.get("kind") == "llm"]
    lb = [e for e in b if e.get("kind") == "llm"]
    if len(la) != len(lb):
        out.append({"kind": "model_call_count", "a": len(la), "b": len(lb)})
    for ea, eb in zip(la, lb):
        ra, rb = ea.get("request") or {}, eb.get("request") or {}
        ca, ea = normalize.split_environment(ra.get("system") or [])
        cb, eb = normalize.split_environment(rb.get("system") or [])
        ca, cb = normalize.settle_skills(ca, ea, cb, eb)
        diffs = normalize.diff({**ra, "system": ca}, {**rb, "system": cb})
        if diffs:
            out.append({"kind": "model_request", "n": ea.get("n"),
                        "sections": sorted({normalize.section_of(d["path"]) for d in diffs}), "diffs": diffs})
    ta = [(e.get("tool_name"), json.dumps(e.get("args"), sort_keys=True)) for e in a if e.get("kind") == "tool"]
    tb = [(e.get("tool_name"), json.dumps(e.get("args"), sort_keys=True)) for e in b if e.get("kind") == "tool"]
    # Tools the model asked for together run in parallel and finish in either order: compare
    # the calls each model call asked for, not the order they finished in.
    if sorted(ta) != sorted(tb):
        out.append({"kind": "tool_calls", "a": ta, "b": tb})
    return out


def render(c: Comparison, max_items: int = 10) -> str:
    lines = [f"Ariadne compare  ·  {Path(c.cassette).name}",
             f"A  {c.a.label:<10} {c.a.version}   (replay: {c.a.result.verdict if c.a.result else '-'})",
             f"B  {c.b.label:<10} {c.b.version}   (replay: {c.b.result.verdict if c.b.result else '-'})"]
    if not c.changes:
        lines.append("result     SAME — both versions built every request and tool call identically")
        return "\n".join(lines)
    lines.append(f"result     CHANGED — {len(c.changes)} difference(s) between A and B")
    from .report import _fmt_diff
    for ch in c.changes[:max_items]:
        k = ch["kind"]
        if k == "model_request":
            lines.append(f"\n  model call #{ch['n']} built differently in: {', '.join(ch['sections'])}")
            lines.extend(_fmt_diff(d) for d in ch["diffs"][:8])
        else:
            lines.append(f"\n  {k}: {json.dumps({x: y for x, y in ch.items() if x != 'kind'}, ensure_ascii=False)[:600]}")
    return "\n".join(lines)
