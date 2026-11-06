"""Record and replay engines that live inside the Hermes process.

Both are wired through Hermes's public middleware and hooks only:

* ``llm_execution`` middleware  — record: wrap the real provider call.
                                  replay: return the recorded response, never call the provider.
* ``tool_request`` middleware   — replay only: neutralise the arguments every downstream
                                  step sees, so even a fail-open path cannot execute anything.
* ``tool_execution`` middleware — record: wrap the real tool. replay: return the recorded result.
* observer hooks                — user turns, auxiliary calls, compaction, subagents, approvals.

The mode comes from a control file in this profile's plugin-data directory
(written by ``hermes ariadne play``) or from the ``record`` plugin setting. A
file, not an environment variable, because ``hermes -z`` runs inside a
background gateway process that never sees the caller's environment.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from . import cassette as cas
from . import codec, normalize, redact

logger = logging.getLogger("ariadne")

NEUTRAL_ARGS = {"__ariadne_replay__": True}
# Agent-internal tools that only start more agent work inside Hermes. During replay they run
# for real so the subagent exists; everything that subagent does (model calls, tools) is still
# served from the cassette and neutralised like any other call.
INTERNAL_TOOLS = frozenset({"delegate_task"})
CONTROL_FILE = "control.json"


# --------------------------------------------------------------------------- paths

def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("ariadne"))
    except Exception:  # running outside Hermes (unit tests)
        root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        d = root / "plugin-data" / "ariadne"
        d.mkdir(parents=True, exist_ok=True)
        return d


def cassette_dir() -> Path:
    d = data_dir() / "cassettes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hermes_home() -> str:
    return str(data_dir().parent.parent)


def user_home() -> str:
    return str(Path.home())


def agent_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:
        return os.getcwd()


# --------------------------------------------------------------------------- control

class Control:
    """Reads control.json with an mtime cache so every call stays cheap."""

    def __init__(self) -> None:
        self._mtime: float | None = None
        self._data: dict[str, Any] = {}
        self.setting_record = False

    def get(self) -> dict[str, Any]:
        # A replay/fork child started by `hermes ariadne` gets its own control file through the
        # environment; other processes on the same profile (an open Desktop) never see it.
        override = os.environ.get("HERMES_ARIADNE_CONTROL")
        path = Path(override) if override else data_dir() / CONTROL_FILE
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._mtime, self._data = None, {}
            return {"mode": "record" if self.setting_record else "off"}
        if mtime != self._mtime:
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
            self._mtime = mtime
        data = dict(self._data)
        data.setdefault("mode", "record" if self.setting_record else "off")
        return data


CONTROL = Control()


# --------------------------------------------------------------------------- recorder

def _response_text(resp: Any) -> str | None:
    """Best-effort assistant text from a (sanitized) auxiliary response."""
    try:
        if isinstance(resp, dict):
            am = resp.get("assistant_message")
            if isinstance(am, dict) and isinstance(am.get("content"), str):
                return am["content"]
            ch = resp.get("choices") or []
            if ch:
                return ((ch[0] or {}).get("message") or {}).get("content")
            if isinstance(resp.get("content"), str):
                return resp["content"]
        ch = getattr(resp, "choices", None)
        if ch:
            return getattr(getattr(ch[0], "message", None), "content", None)
    except Exception:
        return None
    return None


class Recorder:
    def __init__(self, extra_redact: list[str] | None = None) -> None:
        self.lock = threading.Lock()
        self.writers: dict[str, cas.Writer] = {}
        self.alias: dict[str, str] = {}  # child/rotated session -> root session
        self.extra = redact.compile_extra(extra_redact)
        self.hermes_version: str | None = None

    def _root(self, session_id: str) -> str:
        seen = set()
        while session_id in self.alias and session_id not in seen:
            seen.add(session_id)
            session_id = self.alias[session_id]
        return session_id

    def _masker(self) -> normalize.Masker:
        return normalize.Masker({hermes_home(): "$HERMES_HOME", agent_cwd(): "$CWD"}, user_homes=[user_home()])

    def writer(self, session_id: str, ctx: dict[str, Any] | None = None) -> cas.Writer:
        ctx = ctx or {}
        root = self._root(session_id or "nosession")
        with self.lock:
            w = self.writers.get(root)
            if w is None:
                cid = cas.new_id(root)
                header = {
                    "id": cid,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session_id": root,
                    "hermes_home": hermes_home(),
                    "cwd": agent_cwd(),
                    "user_home": user_home(),
                    "platform": ctx.get("platform") or "",
                    "provider": ctx.get("provider") or "",
                    "model": ctx.get("model") or "",
                    "api_mode": ctx.get("api_mode") or "",
                    "base_url_host": _host(ctx.get("base_url")),
                    "recorder": {"ariadne": _plugin_version(), "pid": os.getpid()},
                    # Settings shape how Hermes builds requests (compression, toolsets, agent...);
                    # a replay on another machine reuses them. Credentials are stripped and the
                    # rest is scrubbed.
                    "config_snapshot": _config_snapshot(),
                }
                w = cas.Writer(cassette_dir() / f"{cid}{cas.SUFFIX}", header)
                self.writers[root] = w
            return w

    def link(self, child: str, parent: str) -> None:
        if child and parent and child != parent:
            with self.lock:
                self.alias[child] = parent

    # -- middleware ---------------------------------------------------------
    def llm_execution(self, **kw: Any) -> Any:
        next_call: Callable[[Any], Any] = kw["next_call"]
        request = kw["request"]
        started = time.monotonic()
        try:
            response = next_call(request)
        except Exception as exc:
            self._safe(lambda: self.writer(kw.get("session_id", ""), kw).append(
                "llm_error", api_call_count=kw.get("api_call_count"), turn_id=kw.get("turn_id"),
                task_id=kw.get("task_id"), error_type=type(exc).__name__, error=redact.scrub_text(str(exc))[:2000]))
            raise
        elapsed = int((time.monotonic() - started) * 1000)

        def save() -> None:
            prepared = normalize.prepare(request, self._masker(), self.extra)
            encoded, lossy = codec.encode_checked(response)
            self.writer(kw.get("session_id", ""), kw).append(
                "llm", session_id=kw.get("session_id"), task_id=kw.get("task_id"), turn_id=kw.get("turn_id"),
                api_call_count=kw.get("api_call_count"), api_mode=kw.get("api_mode"), model=kw.get("model"),
                provider=kw.get("provider"), elapsed_ms=elapsed,
                request=prepared, request_digest=normalize.digest(prepared),
                response=redact.scrub(encoded, self.extra), lossy=lossy)

        self._safe(save)
        return response

    def tool_execution(self, **kw: Any) -> Any:
        next_call: Callable[[Any], Any] = kw["next_call"]
        args = kw.get("args")
        try:
            result = next_call(args)
        except Exception as exc:
            self._safe(lambda: self.writer(kw.get("session_id", "")).append(
                "tool_error", tool_name=kw.get("tool_name"), tool_call_id=kw.get("tool_call_id"),
                error_type=type(exc).__name__, error=redact.scrub_text(str(exc))[:2000]))
            raise

        def save() -> None:
            encoded, lossy = codec.encode_checked(result)
            self.writer(kw.get("session_id", "")).append(
                "tool", session_id=kw.get("session_id"), task_id=kw.get("task_id"), turn_id=kw.get("turn_id"),
                tool_name=kw.get("tool_name"), tool_call_id=kw.get("tool_call_id"),
                args=redact.scrub(normalize._jsonable(kw.get("original_args", args)), self.extra),
                result=redact.scrub(encoded, self.extra), lossy=lossy)

        self._safe(save)
        return result

    # -- hooks ----------------------------------------------------------------
    def on_pre_llm_call(self, **kw: Any) -> None:
        sid = kw.get("session_id") or ""
        subagent = bool(sid) and self._root(sid) != sid
        self._safe(lambda: self.writer(sid, kw).append(
            "turn", session_id=kw.get("session_id"), turn_id=kw.get("turn_id"), subagent=subagent,
            is_first_turn=bool(kw.get("is_first_turn")),
            user_message=redact.scrub(normalize._jsonable(kw.get("user_message")), self.extra),
            platform=kw.get("platform")))
        return None

    def on_post_auxiliary_call(self, **kw: Any) -> None:
        def save() -> None:
            sid = kw.get("session_id") or ""
            if not sid and len(self.writers) == 1:
                sid = next(iter(self.writers))  # e.g. compression summaries carry no session id
            if not sid or self._root(sid) not in self.writers:
                return  # side traffic outside a recorded session
            self.writer(sid).append(
                "aux", aux_task=kw.get("aux_task"), api_request_id=kw.get("api_request_id"),
                response_text=redact.scrub_text(_response_text(kw.get("response")) or ""),
                error_type=kw.get("error_type"), streaming=bool(kw.get("streaming")))
        self._safe(save)

    def on_post_compaction(self, **kw: Any) -> None:
        old, new = kw.get("old_session_id"), kw.get("session_id")
        if old and new:
            self.link(new, old)
        self._safe(lambda: self.writer(new or old or "").append(
            "compaction", old_session_id=old, session_id=new, compaction_kind=kw.get("kind"), reason=kw.get("reason"),
            before_tokens=kw.get("before_tokens"), after_tokens=kw.get("after_tokens")))

    def on_subagent_start(self, **kw: Any) -> None:
        child = kw.get("child_session_id") or ""
        parent = kw.get("parent_session_id") or kw.get("session_id") or ""
        self.link(child, parent)
        if parent and self._root(parent) in self.writers:
            self._safe(lambda: self.writer(parent).append(
                "subagent", child_session_id=child, parent_session_id=parent,
                goal=redact.scrub_text(str(kw.get("goal") or ""))[:2000]))

    def on_approval(self, phase: str) -> Callable[..., None]:
        def hook(**kw: Any) -> None:
            sid = kw.get("session_id") or ""
            if sid and self._root(sid) in self.writers:
                self._safe(lambda: self.writer(sid).append(
                    "approval", phase=phase, tool_name=kw.get("tool_name"),
                    decision=kw.get("decision") or kw.get("response") or kw.get("choice")))
        return hook

    @staticmethod
    def _safe(fn: Callable[[], Any]) -> None:
        """Recording must never break the agent: any failure is logged and dropped."""
        try:
            fn()
        except Exception:
            logger.warning("ariadne: recording step failed", exc_info=True)


# --------------------------------------------------------------------------- replayer

class Replayer:
    """Serves a cassette back to Hermes and reports every difference."""

    def __init__(self, control: dict[str, Any]) -> None:
        self.control = control
        self.tape = cas.load(control["cassette"])
        self.report_path = Path(control["report"])
        self.lock = threading.RLock()  # emit() is called while the lock is held
        self.llm = self.tape.of("llm")
        self.llm_pos = 0
        # Parent and subagents run concurrently, so each agent gets its own queue. The recorded
        # root session is the cassette's; recorded children come from `subagent` events, in order.
        self.rec_root = self.tape.header.get("session_id") or (self.llm[0].get("session_id") if self.llm else "")
        self.rec_children = [e.get("child_session_id") for e in self.tape.of("subagent")]
        self.queues: dict[str, list[dict[str, Any]]] = {}
        for ev in self.llm:
            self.queues.setdefault(self._rec_key(ev.get("session_id") or ""), []).append(ev)
        self.qpos: dict[str, int] = {}
        self.live_root: str | None = None
        self.live_children: dict[str, str] = {}
        self.tools_by_id = {e["tool_call_id"]: e for e in self.tape.of("tool") if e.get("tool_call_id")}
        self.tools_in_order = self.tape.of("tool")
        self.tool_pos = 0
        self.used_tool_ids: set[str] = set()
        self.fork_at = control.get("fork_at")  # 1-based llm call index that goes live
        self.forked = False
        self.extra = redact.compile_extra(control.get("extra_redact"))
        h = self.tape.header
        rec_home = h.get("user_home") or normalize.guess_user_home(h.get("hermes_home", "")) or ""
        self.masker = normalize.Masker({
            h.get("hermes_home", ""): "$HERMES_HOME", h.get("cwd", ""): "$CWD",
            hermes_home(): "$HERMES_HOME", agent_cwd(): "$CWD",
        }, user_homes=[rec_home, user_home()])
        self.masker_recorded = self.masker
        self.emit("start", cassette=self.tape.id, llm_events=len(self.llm), tool_events=len(self.tools_in_order))

    def _rec_key(self, rec_sid: str) -> str:
        if rec_sid in self.rec_children:
            return f"child:{self.rec_children.index(rec_sid)}"
        return "root"

    def on_subagent_start(self, **kw: Any) -> None:
        child = kw.get("child_session_id") or ""
        with self.lock:
            if child and child not in self.live_children:
                self.live_children[child] = f"child:{len(self.live_children)}"

    def _live_key(self, live_sid: str) -> str:
        with self.lock:
            if live_sid in self.live_children:
                return self.live_children[live_sid]
            if self.live_root is None:
                self.live_root = live_sid
            return "root"

    def expected_llm(self) -> int:
        return len(self.llm)

    def _save_live(self, kind: str, **data: Any) -> None:
        """What this Hermes actually built, for differential comparison between two installs."""
        path = self.control.get("live_path")
        if not path:
            return
        with self.lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, **data}, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def emit(self, type_: str, **data: Any) -> None:
        with self.lock:
            with open(self.report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": type_, "t": round(time.time(), 3), **data},
                                    ensure_ascii=False, default=str) + "\n")

    # -- model ------------------------------------------------------------------
    def llm_execution(self, **kw: Any) -> Any:
        key = self._live_key(kw.get("session_id") or "")
        with self.lock:
            index = self.llm_pos
            self.llm_pos += 1
            qi = self.qpos.get(key, 0)
            self.qpos[key] = qi + 1
        n = index + 1
        queue = self.queues.get(key, [])
        live = normalize.prepare(kw["request"], self.masker, self.extra)
        self._save_live("llm", n=n, agent=key, request=live)
        if self.fork_at and n >= int(self.fork_at):
            self.forked = True
            self.emit("fork_live_call", n=n, request_digest=normalize.digest(live))
            response = kw["next_call"](kw["request"])
            encoded, _ = codec.encode_checked(response)
            self.emit("fork_response", n=n, response=redact.scrub(encoded, self.extra))
            return response
        if qi >= len(queue):
            self.emit("extra_llm_call", n=n, agent=key, request_digest=normalize.digest(live),
                      note="Hermes asked the model for more than the cassette holds")
            return synthetic_response(kw.get("api_mode") or "", "[ariadne] cassette exhausted: this call did not happen in the recording.")
        ev = queue[qi]
        # Re-mask the recorded side too: masks are idempotent, and a cassette written by an older
        # recorder may predate a mask.
        recorded = self.masker.tree(ev.get("request") or {})
        if normalize.digest(live) == ev.get("request_digest") or live == recorded:
            self.emit("llm_match", n=n, agent=key, seq=ev.get("seq"))
        else:
            soft = set(self.control.get("soft_sections") or [])
            substituted = bool(self.control.get("provider_substituted"))
            rec_core, rec_env = normalize.split_environment(recorded.get("system") or [], substituted)
            live_core, live_env = normalize.split_environment(live.get("system") or [], substituted)
            env_delta = normalize.environment_delta(rec_env, live_env)
            rec_msgs, rec_fc = normalize.strip_first_contact(recorded.get("messages") or [])
            live_msgs, live_fc = normalize.strip_first_contact(live.get("messages") or [])
            if rec_fc != live_fc:
                env_delta["first_contact_note"] = {"recorded": rec_fc, "live": live_fc}
            recorded = {**recorded, "messages": rec_msgs}
            live = {**live, "messages": live_msgs}
            if env_delta:
                self.emit("environment_diff", n=n, **env_delta)
            recorded = {**recorded, "system": rec_core}
            live = {**live, "system": live_core}
            diffs = normalize.diff(recorded, live)
            if self.control.get("provider_substituted"):
                # Provider-specific reasoning carriers ride on assistant messages; a stand-in
                # provider legitimately drops or reshapes them.
                carriers = [d for d in diffs if _is_carrier_path(d["path"])]
                if carriers:
                    diffs = [d for d in diffs if not _is_carrier_path(d["path"])]
                    self.emit("provider_standin_diff", n=n, diffs=carriers, provider=self.control.get("provider_substituted"))
            hard = [d for d in diffs if normalize.section_of(d["path"]) not in soft]
            softd = [d for d in diffs if normalize.section_of(d["path"]) in soft]
            if hard:
                self.emit("llm_drift", n=n, seq=ev.get("seq"),
                          sections=sorted({normalize.section_of(d["path"]) for d in hard}), diffs=hard)
            else:
                self.emit("llm_match", n=n, seq=ev.get("seq"), soft=True)
            if softd:
                self.emit("provider_standin_diff", n=n, diffs=softd, provider=self.control.get("provider_substituted"))
        if ev.get("lossy"):
            self.emit("lossy_response", n=n, paths=ev["lossy"])
        return codec.decode(ev["response"])

    # -- tools --------------------------------------------------------------------
    def tool_request(self, **kw: Any) -> dict[str, Any] | None:
        if self.forked and self.control.get("live_tools"):
            return None
        if kw.get("tool_name") in INTERNAL_TOOLS:
            return None
        return {"args": dict(NEUTRAL_ARGS), "source": "ariadne", "reason": "replay: tools never execute"}

    def tool_execution(self, **kw: Any) -> Any:
        tool_name = kw.get("tool_name")
        call_id = kw.get("tool_call_id")
        live_args = redact.scrub(normalize._jsonable(kw.get("original_args", kw.get("args"))), self.extra)
        self._save_live("tool", tool_name=tool_name, tool_call_id=call_id, args=self.masker.tree(live_args))
        if self.forked and self.control.get("live_tools"):
            self.emit("fork_live_tool", tool_name=tool_name, tool_call_id=call_id)
            return kw["next_call"](kw.get("original_args", kw.get("args")))
        if self.forked:
            return self._forked_tool(tool_name, call_id, live_args, kw)
        with self.lock:
            ev = self.tools_by_id.get(call_id) if call_id else None
            if ev is None and not call_id:
                while self.tool_pos < len(self.tools_in_order):
                    cand = self.tools_in_order[self.tool_pos]
                    self.tool_pos += 1
                    if cand.get("tool_call_id") not in self.used_tool_ids:
                        ev = cand
                        break
            if ev is not None and ev.get("tool_call_id"):
                self.used_tool_ids.add(ev["tool_call_id"])
        if tool_name in INTERNAL_TOOLS:
            result = kw["next_call"](kw.get("args"))
            self.emit("internal_tool_ran", tool_name=tool_name, tool_call_id=call_id, matched=ev is not None)
            return result
        if ev is None:
            self.emit("off_script_tool", tool_name=tool_name, tool_call_id=call_id, args=live_args,
                      note="the model asked for a tool call the recording never made")
            return json.dumps({"error": "ariadne: this tool call is not in the recording (off-script); nothing was executed"})
        drift = []
        if ev.get("tool_name") != tool_name:
            drift.append({"path": ".tool_name", "change": "changed", "recorded": ev.get("tool_name"), "live": tool_name})
        drift += normalize.diff(self.masker.tree(ev.get("args")), self.masker.tree(live_args), ".args")
        self.emit("tool_drift" if drift else "tool_match", tool_name=tool_name, tool_call_id=call_id,
                  seq=ev.get("seq"), diffs=drift)
        return codec.decode(ev["result"])

    def _forked_tool(self, tool_name: Any, call_id: Any, live_args: Any, kw: dict[str, Any]) -> Any:
        """After the fork point the live model mints new call ids. A call identical to an unused
        recorded one (same tool, same arguments) gets the recorded result; anything else is
        refused without running, so the world stays frozen."""
        if tool_name in INTERNAL_TOOLS:
            return kw["next_call"](kw.get("args"))
        want = (tool_name, json.dumps(self.masker.tree(live_args), sort_keys=True))
        with self.lock:
            for ev in self.tools_in_order:
                cid = ev.get("tool_call_id")
                if cid in self.used_tool_ids:
                    continue
                if (ev.get("tool_name"), json.dumps(self.masker.tree(ev.get("args")), sort_keys=True)) == want:
                    self.used_tool_ids.add(cid)
                    self.emit("fork_tool_frozen", tool_name=tool_name, tool_call_id=call_id, recorded_seq=ev.get("seq"))
                    return codec.decode(ev["result"])
        self.emit("fork_tool_refused", tool_name=tool_name, tool_call_id=call_id, args=live_args)
        return json.dumps({"error": "ariadne fork: this tool call is new after the fork point and the world is frozen; "
                                    "nothing was executed (rerun with --live-tools to allow real tools)"})

    def finish(self) -> None:
        self.emit("finish", llm_served=min(self.llm_pos, len(self.llm)), llm_total=len(self.llm),
                  tools_served=len(self.used_tool_ids), tools_total=len(self.tools_in_order))


_CARRIER_KEYS = ("reasoning_details", "reasoning_content", "reasoning", "extra_content", "thought_signature",
                 "encrypted_content", "signature", "thinking", "redacted_thinking")


def _is_carrier_path(path: str) -> bool:
    if not path.startswith(".messages"):
        return False
    tail = path.replace("[", ".").replace("]", "").split(".")
    return any(k in tail for k in _CARRIER_KEYS)


def synthetic_response(api_mode: str, text: str) -> Any:
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                            input_tokens=0, output_tokens=0,
                            prompt_tokens_details=None, completion_tokens_details=None)
    if api_mode == "anthropic_messages":
        return SimpleNamespace(id="ariadne-synthetic", type="message", role="assistant", model="ariadne",
                               content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               stop_sequence=None, usage=usage)
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None, reasoning_content=None, refusal=None)
    return SimpleNamespace(id="ariadne-synthetic", model="ariadne", provider=None, usage=usage,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


def _config_snapshot() -> dict[str, Any]:
    try:
        from .util import read_yaml, settings_only, strip_secrets
        cfg = settings_only(strip_secrets(read_yaml(Path(hermes_home()) / "config.yaml")))
        model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        # The replay runs on its own stand-in endpoint; keep the model *settings* (context_length,
        # reasoning, ...) because they change how Hermes builds requests.
        cfg["model"] = {k: v for k, v in model.items() if k not in ("provider", "base_url", "api_key", "api_mode")}
        return redact.scrub(cfg)
    except Exception:
        return {}


def _host(url: Any) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def _plugin_version() -> str:
    try:
        import yaml  # type: ignore
        meta = yaml.safe_load((Path(__file__).parent / "plugin.yaml").read_text())
        return str(meta.get("version"))
    except Exception:
        for line in (Path(__file__).parent / "plugin.yaml").read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"')
    return "unknown"
