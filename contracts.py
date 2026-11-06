"""Hook-contract checks: Hermes's documented hook promises, verified on every replay.

Ariadne already runs inside Hermes during a replay, so it can observe the public hooks
and check that Hermes keeps the promises its own docs make (website/docs/user-guide/
features/hooks.md and developer-guide/observer-hooks.md). Each violation is reported with
the hook, the ids involved and the documented promise, so it can be filed as an issue.

Observation only: every callback returns None and never changes Hermes's behaviour.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

PROMISES: dict[str, str] = {
    "pre_llm_call_once": "pre_llm_call fires once per turn (hooks.md: 'Fires once per turn, before the tool-calling loop')",
    "post_llm_call_once": "post_llm_call fires at most once per turn (hooks.md: 'Fires once per turn')",
    "post_llm_call_success": "post_llm_call fires on successful turns and not on interrupted ones (hooks.md)",
    "pre_tool_once": "pre_tool_call fires once per tool call (hooks.md: 'Fires once per tool call')",
    "post_tool_once": "post_tool_call fires once per tool call (hooks.md: 'Fires once per tool call')",
    "post_tool_after_pre": "post_tool_call follows the pre_tool_call of the same tool call",
    "api_pairs": "every pre_api_request ends with post_api_request or api_request_error (observer-hooks.md)",
    "aux_not_api": "auxiliary calls do not fire pre_api_request/post_api_request (hooks.md: auxiliary calls 'do not fire')",
    "session_end_per_turn": "on_session_end fires at the end of every run_conversation, once per turn (hooks.md: 'Always fires')",
    "subagent_pairs": "subagent_start and subagent_stop fire once per child agent (hooks.md)",
    "schema_version": "every hook payload carries telemetry_schema_version (observer-hooks.md)",
    "turn_order": "pre_llm_call fires before the turn's first pre_api_request (hooks.md: 'before the main while loop')",
}

HOOKS = ("pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call", "pre_api_request",
         "post_api_request", "api_request_error", "on_session_end", "subagent_start", "subagent_stop",
         "pre_auxiliary_call", "post_auxiliary_call")


# Hooks Hermes fires inside the real tool dispatcher. A replay serves tool results from the
# cassette and never enters the dispatcher, so these checks only run while recording.
DISPATCHER_RULES = frozenset({"post_tool_after_pre", "pre_tool_once"})


class ContractChecker:
    def __init__(self, emit: Callable[[str, dict[str, Any]], None], mode: Callable[[], str] = lambda: "record"):
        self.emit = emit
        self.mode = mode
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.turn_events: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self.api_open: dict[str, str] = {}
        self.children_started: dict[str, int] = defaultdict(int)
        self.children_stopped: dict[str, int] = defaultdict(int)
        self.ended_turns: dict[str, int] = defaultdict(int)
        self.reported: set[tuple] = set()
        self.checked_turns = 0

    # -------------------------------------------------------------- collection
    def hook(self, name: str) -> Callable[..., None]:
        def cb(**kw: Any) -> None:
            try:
                self._observe(name, kw)
            except Exception:
                pass
            return None
        cb.__name__ = f"ariadne_contract_{name}"
        return cb

    def _violation(self, rule: str, **ids: Any) -> None:
        if rule in DISPATCHER_RULES and self.mode() in ("replay", "fork"):
            return
        key = (rule, tuple(sorted((k, str(v)) for k, v in ids.items())))
        if key in self.reported:
            return
        self.reported.add(key)
        self.emit("contract_violation", {"rule": rule, "promise": PROMISES[rule], **ids})

    def _observe(self, name: str, kw: dict[str, Any]) -> None:
        with self.lock:
            if "telemetry_schema_version" not in kw:
                self._violation("schema_version", hook=name)
            turn = str(kw.get("turn_id") or "")
            rid = str(kw.get("api_request_id") or "")
            if name in ("pre_api_request", "post_api_request", "api_request_error") and rid.startswith("aux-"):
                self._violation("aux_not_api", hook=name, api_request_id=rid)
            if name == "pre_api_request":
                self.api_open[rid] = turn
            elif name in ("post_api_request", "api_request_error"):
                self.api_open.pop(rid, None)
            elif name == "subagent_start":
                self.children_started[str(kw.get("child_session_id") or kw.get("child_subagent_id") or "")] += 1
            elif name == "subagent_stop":
                self.children_stopped[str(kw.get("child_session_id") or kw.get("child_subagent_id") or "")] += 1
            if turn and name not in ("pre_auxiliary_call", "post_auxiliary_call", "subagent_start", "subagent_stop"):
                self.turn_events[turn].append((name, kw))
            if name == "on_session_end" and turn:
                self.ended_turns[turn] += 1
                if self.ended_turns[turn] > 1:
                    self._violation("session_end_per_turn", turn_id=turn, count=self.ended_turns[turn])
                self._check_turn(turn, kw)

    # -------------------------------------------------------------- checks
    def _check_turn(self, turn: str, end: dict[str, Any]) -> None:
        events = self.turn_events.get(turn, [])
        self.checked_turns += 1
        names = [n for n, _ in events]
        pre_llm = names.count("pre_llm_call")
        if pre_llm > 1:
            self._violation("pre_llm_call_once", turn_id=turn, count=pre_llm)
        post_llm = names.count("post_llm_call")
        if post_llm > 1:
            self._violation("post_llm_call_once", turn_id=turn, count=post_llm)
        completed = bool(end.get("completed")) and not end.get("interrupted") and not end.get("failed")
        if pre_llm and completed and post_llm == 0:
            self._violation("post_llm_call_success", turn_id=turn, expected="fired", observed="missing",
                            turn_exit_reason=end.get("turn_exit_reason"))
        if end.get("interrupted") and post_llm:
            self._violation("post_llm_call_success", turn_id=turn, expected="not fired", observed="fired")
        if "pre_api_request" in names and "pre_llm_call" in names and names.index("pre_api_request") < names.index("pre_llm_call"):
            self._violation("turn_order", turn_id=turn)
        pre_tools: dict[str, int] = defaultdict(int)
        post_tools: dict[str, int] = defaultdict(int)
        for n, kw in events:
            cid = str(kw.get("tool_call_id") or "")
            if not cid:
                continue
            if n == "pre_tool_call":
                pre_tools[cid] += 1
            elif n == "post_tool_call":
                post_tools[cid] += 1
                if pre_tools[cid] == 0:
                    self._violation("post_tool_after_pre", turn_id=turn, tool_call_id=cid, tool_name=kw.get("tool_name"))
        for cid, c in pre_tools.items():
            if c > 1:
                self._violation("pre_tool_once", turn_id=turn, tool_call_id=cid, count=c)
        for cid, c in post_tools.items():
            if c > 1:
                self._violation("post_tool_once", turn_id=turn, tool_call_id=cid, count=c)
        for rid, t in list(self.api_open.items()):
            if t == turn:
                self._violation("api_pairs", turn_id=turn, api_request_id=rid)
                self.api_open.pop(rid, None)

    def finish(self) -> None:
        with self.lock:
            for child, n in self.children_started.items():
                stopped = self.children_stopped.get(child, 0)
                if n != 1 or stopped != 1:
                    self._violation("subagent_pairs", child=child, started=n, stopped=stopped)
            for child, n in self.children_stopped.items():
                if child not in self.children_started:
                    self._violation("subagent_pairs", child=child, started=0, stopped=n)
            self.emit("contracts_checked", {"turns": self.checked_turns, "rules": len(PROMISES)})
