"""Fork: replay a cassette up to model call N, then let a live model take over.

Calls 1..N-1 are served from the cassette and must rebuild byte-identical requests; that is
the proof the live model starts from exactly the recorded state. From call N the real model
answers. Tools stay frozen by default: a call identical to a recorded one gets the recorded
result, anything new is refused without running. `live_tools=True` lets tools run for real
(with Hermes's normal approvals).

The fork runs in the user's own Hermes home so Hermes uses its own provider credentials
(Ariadne never reads them); a private control file passed through the environment keeps the
fork's replay mode away from any other Hermes process on the same profile.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cassette as cas
from . import driver, runtime


@dataclass
class ForkResult:
    run_dir: Path
    at: int
    prefix_calls: int
    prefix_identical: bool
    live_calls: int
    replies: list[str]
    errors: list[str]
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"run_dir": str(self.run_dir), "at": self.at, "prefix_calls": self.prefix_calls,
                "prefix_identical": self.prefix_identical, "live_calls": self.live_calls,
                "replies": self.replies, "errors": self.errors,
                "frozen_tools": sum(1 for e in self.events if e.get("type") == "fork_tool_frozen"),
                "refused_tools": sum(1 for e in self.events if e.get("type") == "fork_tool_refused")}


def plan(tape: cas.Cassette, at_call: int | None, at_turn: int | None, prompt: str | None,
         continue_turns: bool) -> tuple[int, list[Any]]:
    """Return (fork_at model-call index, driver actions)."""
    actions = tape.actions()
    # Which action (user turn) each recorded model call belongs to.
    owner: list[int] = []
    user_turns = {id(t) for t in tape.turns()}
    seen = -1
    for e in tape.events:
        if e.get("kind") == "turn" and id(e) in user_turns:
            seen += 1
        elif e.get("kind") == "compaction" and str(e.get("reason") or "").lower() in cas.MANUAL_COMPACTION_REASONS:
            seen += 1
        elif e.get("kind") == "llm":
            owner.append(max(seen, 0))
    if at_turn is not None:
        k = at_turn - 1
        if not 0 <= k < len(actions):
            raise ValueError(f"--at-turn must be 1..{len(actions)}")
        fork_at = sum(1 for o in owner if o < k) + 1
        head = actions[:k] + [prompt if prompt is not None else actions[k]]
        return fork_at, head + (actions[k + 1:] if continue_turns else [])
    if at_call is None or not 1 <= at_call <= max(len(owner), 1):
        raise ValueError(f"--at must be 1..{len(owner)} (model calls in this cassette)")
    k = owner[at_call - 1] if owner else 0
    head = actions[:k + 1]
    if prompt is not None:
        head.append(prompt)
    return at_call, head + (actions[k + 1:] if continue_turns else [])


def fork(cassette_path: str | Path, *, at_call: int | None = None, at_turn: int | None = None,
         prompt: str | None = None, live_tools: bool = False, continue_turns: bool = False,
         home: str | None = None, hermes_python: str | None = None, turn_timeout: float = 300.0) -> ForkResult:
    tape = cas.load(cassette_path)
    fork_at, actions = plan(tape, at_call, at_turn, prompt, continue_turns)
    run_dir = runtime.data_dir() / "forks" / f"{tape.id}--at{fork_at}--{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    report = run_dir / "report.jsonl"
    report.touch()
    control = {"mode": "fork", "cassette": str(Path(cassette_path).resolve()), "report": str(report),
               "live_path": str(run_dir / "live.jsonl"), "run_id": run_dir.name, "fork_at": fork_at,
               "live_tools": live_tools}
    control_path = run_dir / "control.json"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    env = dict(os.environ, HERMES_ARIADNE_CONTROL=str(control_path), PYTHONUNBUFFERED="1")
    if home:
        env["HERMES_HOME"] = home
    if (tape.header.get("platform") or "") == "desktop":
        env["HERMES_DESKTOP"] = "1"
    cwd = tape.header.get("cwd") if tape.header.get("cwd") and Path(tape.header["cwd"]).is_dir() else str(run_dir)
    replies, errors, _, tail = driver.drive_session(hermes_python or driver.PlayOptions().hermes_python, env, cwd,
                                                    actions, turn_timeout, drain=None)
    if errors and tail:
        errors.append("hermes stderr (tail):\n" + "\n".join(tail[-12:]))
    events = [json.loads(l) for l in report.read_text(encoding="utf-8").splitlines() if l.strip()]
    prefix = [e for e in events if e.get("type") in ("llm_match", "llm_drift") and int(e.get("n") or 0) < fork_at]
    live = [e for e in events if e.get("type") == "fork_live_call"]
    res = ForkResult(run_dir, fork_at, len(prefix),
                     len(prefix) == fork_at - 1 and all(e.get("type") == "llm_match" for e in prefix),
                     len(live), replies, errors, events)
    (run_dir / "fork.json").write_text(json.dumps(res.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    return res


def render(r: ForkResult) -> str:
    d = r.to_json()
    lines = [f"Ariadne fork  ·  {r.run_dir.name}",
             f"prefix    model calls 1..{r.at - 1}: {'IDENTICAL to the recording' if r.prefix_identical else 'NOT identical, see report'}"
             f" ({r.prefix_calls} served from the cassette)",
             f"live      {r.live_calls} model call(s) answered by the live model from call {r.at}",
             f"tools     {d['frozen_tools']} served from the recording · {d['refused_tools']} new call(s) refused (world frozen)"]
    for i, reply in enumerate(r.replies, 1):
        lines.append(f"\n  turn {i}: {reply[:600]}")
    if r.errors:
        lines.append("\nerrors:")
        lines.extend(f"  {e}" for e in r.errors)
    lines.append(f"\nreport    {r.run_dir / 'report.jsonl'}")
    return "\n".join(lines)
