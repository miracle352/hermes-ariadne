"""Human-readable replay reports (plain text; the Desktop UI comes later)."""

from __future__ import annotations

import json
from typing import Any

_VERDICT = {
    "identical": "IDENTICAL — Hermes rebuilt every request exactly as recorded",
    "drift": "DRIFT — Hermes behaved differently from the recording",
    "leak": "LEAK — a model call tried to leave the replay",
    "error": "ERROR — the replay could not run",
}


def render(result: Any, max_diffs: int = 12) -> str:
    ev = result.events
    c = result.count
    lines = [
        f"Ariadne replay  ·  {result.run_dir.name}",
        f"verdict   {_VERDICT.get(result.verdict, result.verdict)}",
        f"turns     {result.turns_sent} sent",
        f"model     {c('llm_match')} identical  ·  {c('llm_drift')} drifted  ·  {c('extra_llm_call')} extra",
        f"tools     {c('tool_match')} identical  ·  {c('tool_drift')} drifted  ·  {c('off_script_tool')} off-script",
        f"network   {c('aux_served')} auxiliary calls served locally  ·  {c('leak')} leaks",
    ]
    shown = 0
    for e in ev:
        t = e.get("type")
        if t not in ("llm_drift", "tool_drift", "off_script_tool", "extra_llm_call", "missing_llm_calls", "leak"):
            continue
        if shown >= max_diffs:
            lines.append("  … more in report.jsonl")
            break
        shown += 1
        if t == "llm_drift":
            lines.append(f"\n  ✗ model call #{e.get('n')} changed in: {', '.join(e.get('sections') or [])}")
            for d in (e.get("diffs") or [])[:6]:
                lines.append(_fmt_diff(d))
        elif t == "tool_drift":
            lines.append(f"\n  ✗ tool {e.get('tool_name')} ({e.get('tool_call_id')}) arguments changed")
            for d in (e.get("diffs") or [])[:4]:
                lines.append(_fmt_diff(d))
        else:
            lines.append(f"\n  ✗ {t}: {json.dumps({k: v for k, v in e.items() if k not in ('type', 't')}, ensure_ascii=False)[:300]}")
    if result.errors:
        lines.append("\nerrors:")
        lines.extend(f"  {err}" for err in result.errors)
    lines.append(f"\nreport    {result.report_path}")
    return "\n".join(lines)


def _fmt_diff(d: dict[str, Any]) -> str:
    if d.get("text_diff"):
        body = "\n".join(f"        {line}" for line in d["text_diff"][:14])
        return f"      {d['path']}:\n{body}"
    if d.get("change") == "added":
        return f"      + {d['path']} = {d.get('live')}"
    if d.get("change") == "removed":
        return f"      - {d['path']} (was {d.get('recorded')})"
    return f"      ~ {d['path']}: {d.get('recorded')}  →  {d.get('live')}"
