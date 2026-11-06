"""The /ariadne chat command (Desktop, TUI, CLI and messaging platforms)."""

from __future__ import annotations

import json
from typing import Any

from . import cassette as cas
from . import runtime

USAGE = ("/ariadne on | off | status | list | last | play [id] | contracts\n"
         "  on/off    start or stop recording new sessions on this profile\n"
         "  last      summary of the newest cassette\n"
         "  play      replay a cassette offline (default: the newest) and show the verdict\n"
         "  contracts hook-contract problems seen while recording")


def _set_recording(on: bool) -> str:
    path = runtime.data_dir() / runtime.CONTROL_FILE
    if on:
        path.write_text(json.dumps({"mode": "record"}), encoding="utf-8")
        return f"Recording on. New sessions are saved to {runtime.cassette_dir()}"
    path.unlink(missing_ok=True)
    return "Recording off."


def _summary(path) -> str:
    tape = cas.load(path)
    first = next((t.get("user_message") for t in tape.turns()), "") or ""
    first = first if isinstance(first, str) else json.dumps(first)
    return (f"{tape.id}: {len(tape.turns())} turns, {len(tape.of('llm'))} model calls, "
            f"{len(tape.of('tool'))} tool calls — “{first[:60]}”")


def handle(raw_args: str = "", **_: Any) -> str:
    words = (raw_args or "").split()
    sub = words[0].lower() if words else "status"
    try:
        if sub in ("on", "off"):
            return _set_recording(sub == "on")
        if sub == "status":
            mode = runtime.Control().get().get("mode")
            n = sum(1 for _ in cas.iter_dir(runtime.cassette_dir()))
            return f"Recording {'on' if mode == 'record' else 'off'} · {n} cassette(s) in {runtime.cassette_dir()}"
        if sub == "list":
            rows = [_summary(p) for p in list(cas.iter_dir(runtime.cassette_dir()))[:10]]
            return "\n".join(rows) or "No cassettes yet. Use /ariadne on."
        if sub == "last":
            newest = next(iter(cas.iter_dir(runtime.cassette_dir())), None)
            return _summary(newest) if newest else "No cassettes yet. Use /ariadne on."
        if sub == "contracts":
            path = runtime.data_dir() / "contracts.jsonl"
            if not path.is_file():
                return "No hook-contract problems seen while recording."
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()][-10:]
            return "\n".join(f"{r.get('rule')}: {r.get('promise')}" for r in rows)
        if sub == "play":
            from . import cli, driver, report
            ref = words[1] if len(words) > 1 else "latest"
            result = driver.play(cli.resolve(ref), driver.PlayOptions(turn_timeout=180, clone_home=runtime.hermes_home()))
            return report.render(result, max_diffs=4)
    except SystemExit as exc:  # cli.resolve reports a bad reference this way
        return str(exc)
    return USAGE
