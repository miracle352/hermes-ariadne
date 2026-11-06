"""Replay driver: runs a cassette through a real Hermes in a throwaway home.

Hermes is driven over its documented TUI-gateway JSON-RPC protocol on stdio
(``python -m tui_gateway.entry``), the same protocol the TUI and Desktop use.
That gives Ariadne three guarantees:

* every approval prompt is answered **deny** by the driver — a replay never
  approves anything (catalog rule 12);
* the replayed Hermes runs with ``HERMES_HOME`` pointed at a scratch directory,
  so the user's sessions, memory and config are never touched;
* outbound proxies are pointed at a closed loopback port and the model
  endpoint at Ariadne's loopback stub, so a replay cannot reach the network.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cassette as cas
from . import runtime
from .stub import Stub

PLUGIN_DIR = Path(__file__).resolve().parent
DEAD_PROXY = "http://127.0.0.1:9"  # discard port: connections are refused
FIRST_CONTACT_MARK = "This is the user's very first message ever"


@dataclass
class PlayOptions:
    hermes_python: str = sys.executable
    keep: bool = False
    turn_timeout: float = 300.0
    fork_at: int | None = None
    fork_prompt: str | None = None
    live_tools: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)
    quiet: bool = False
    provider: str | None = None        # None = the provider the cassette was recorded on
    surface: str | None = None         # None = recorded platform (desktop / tui)
    clone_home: str | None = None      # copy non-secret context (config, SOUL.md, memories, skills) from this home


@dataclass
class PlayResult:
    run_dir: Path
    report_path: Path
    events: list[dict[str, Any]]
    turns_sent: int
    replies: list[str]
    errors: list[str]

    def count(self, type_: str) -> int:
        return sum(1 for e in self.events if e.get("type") == type_)

    @property
    def verdict(self) -> str:
        if self.errors and not self.count("llm_match") and not self.count("llm_drift"):
            return "error"
        if self.count("leak"):
            return "leak"
        if any(self.count(t) for t in ("llm_drift", "tool_drift", "off_script_tool", "extra_llm_call")):
            return "drift"
        if self.count("missing_llm_calls"):
            return "drift"
        return "identical"


class RpcClient:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: str):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=cwd, text=True, bufsize=1, encoding="utf-8", errors="replace")
        self.q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_tail: list[str] = []
        self.next_id = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                continue
        self.q.put({"__eof__": True})

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-40:]

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        with self.lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> int:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# Server->client request methods we answer. Anything that would need a human is refused.
_DENY = {"approval": {"choice": "deny"}, "clarify": {}, "sudo": {"value": ""}, "secret": {"value": ""},
         "vault.code": {"value": ""}, "vault.unlock_prompt": {"value": ""}}


def recorded_provider(tape: cas.Cassette) -> str:
    first = next(iter(tape.of("llm")), {})
    return tape.header.get("provider") or first.get("provider") or "custom"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


_SECRETISH = ("key", "token", "secret", "password", "passwd", "credential", "auth")


def _strip_secrets(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip_secrets(x) for k, x in v.items()
                if not (isinstance(k, str) and any(s in k.lower() for s in _SECRETISH))}
    if isinstance(v, list):
        return [_strip_secrets(x) for x in v]
    return v


# Context Hermes folds into the system prompt. Credentials (.env, auth.json, ...) are never copied.
CONTEXT_FILES = ("SOUL.md", "AGENTS.md")
CONTEXT_DIRS = ("memories", "skills")


def clone_context(src: Path, dst: Path) -> None:
    """Copy the non-secret context that shapes the system prompt from ``src`` into the scratch home."""
    for name in CONTEXT_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
    for name in CONTEXT_DIRS:
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".env", "*.key"))
    cfg = _strip_secrets(_read_yaml(src / "config.yaml"))
    if cfg:
        (dst / "config.yaml").write_text(json.dumps(cfg, indent=2, default=str), encoding="utf-8")


def _scratch_config(tape: cas.Cassette, stub_port: int, provider: str | None = None, existing: Path | None = None) -> str:
    h = tape.header
    model = h.get("model") or "ariadne-replay"
    provider = provider or "custom"
    snap = _read_yaml(existing) if existing else {}
    if not snap and isinstance(h.get("config_snapshot"), dict):
        snap = h["config_snapshot"]
    cfg: dict[str, Any] = {k: v for k, v in snap.items() if k not in ("model", "providers", "custom_providers", "fallback_providers", "fallback_model")}
    cfg["model"] = {"provider": provider, "model": model, "base_url": f"http://127.0.0.1:{stub_port}/v1",
                    "api_key": "ariadne-replay-offline"}
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    enabled = [p for p in (plugins.get("enabled") or []) if isinstance(p, str)]
    cfg["plugins"] = {**plugins, "enabled": sorted(set(enabled) | {"ariadne"}), "disabled": []}
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(cfg, sort_keys=False)
    except Exception:
        return json.dumps(cfg, indent=2)  # JSON is valid YAML


def _child_env(scratch: Path, opts: PlayOptions) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET")) and k.upper() not in ("HERMES_PROFILE",)}
    env.update({
        "HERMES_HOME": str(scratch),
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
        "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY, "all_proxy": DEAD_PROXY,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "HERMES_ARIADNE_REPLAY": "1",
        "PYTHONUNBUFFERED": "1",
    })
    if opts.fork_at and opts.live_tools is False:
        pass
    env.update(opts.extra_env)
    return env


def _install_into(scratch: Path, opts: PlayOptions, env: dict[str, str]) -> None:
    plugins = scratch / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    target = plugins / "ariadne"
    if not target.exists():
        try:
            target.symlink_to(PLUGIN_DIR, target_is_directory=True)
        except OSError:  # Windows without symlink rights
            shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "*.pyc"))


def play(cassette_path: str | Path, opts: PlayOptions | None = None) -> PlayResult:
    opts = opts or PlayOptions()
    tape = cas.load(cassette_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = runtime.data_dir() / "runs" / f"{tape.id}--{stamp}"
    scratch = run_dir / "home"
    scratch.mkdir(parents=True, exist_ok=True)
    report = run_dir / "report.jsonl"
    report.touch()

    def emit(type_: str, data: dict[str, Any]) -> None:
        with open(report, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": type_, "t": round(time.time(), 3), **data}) + "\n")

    stub = Stub([e.get("response_text") or "" for e in tape.of("aux")], emit, model=tape.header.get("model") or "ariadne-replay")
    port = stub.start()
    recorded = recorded_provider(tape)
    # Signed-in providers (nous, codex, ...) cannot start without the user's credentials, which a
    # replay never reads. Replays run on a local stand-in provider unless one is forced; the
    # provider-specific request extras are then reported apart from Hermes drift.
    provider = opts.provider or "custom"
    if opts.clone_home:
        clone_context(Path(opts.clone_home), scratch)
    (scratch / "config.yaml").write_text(_scratch_config(tape, port, provider, scratch / "config.yaml"), encoding="utf-8")
    env = _child_env(scratch, opts)
    surface = opts.surface or tape.header.get("platform") or "tui"
    if surface == "desktop":
        env["HERMES_DESKTOP"] = "1"
    else:
        env.pop("HERMES_DESKTOP", None)
    _install_into(scratch, opts, env)

    ctl_dir = scratch / "plugin-data" / "ariadne"
    ctl_dir.mkdir(parents=True, exist_ok=True)
    first = next(iter(tape.of("llm")), {})
    first_msgs = (first.get("request") or {}).get("messages") or []
    recorded_first_contact = bool(first_msgs) and FIRST_CONTACT_MARK in json.dumps(first_msgs[:1], ensure_ascii=False)
    if not recorded_first_contact:
        # The recording came from an install with history; a scratch home has none, and Hermes would
        # add its first-contact note. One offline warm-up turn makes the install "not new" first.
        (ctl_dir / runtime.CONTROL_FILE).write_text(json.dumps({"mode": "warmup"}), encoding="utf-8")
        stub.armed = False
        drive_session(opts.hermes_python, env, str(run_dir), ["ariadne warm-up"], opts.turn_timeout)
        stub.armed = True

    control = {"mode": "fork" if opts.fork_at else "replay", "cassette": str(Path(cassette_path).resolve()),
               "report": str(report), "run_id": stamp, "fork_at": opts.fork_at, "live_tools": opts.live_tools,
               "provider_substituted": {"recorded": recorded, "replay": provider} if recorded != provider else None,
               "soft_sections": ["params"] if recorded != provider else []}
    ctl_dir = scratch / "plugin-data" / "ariadne"
    ctl_dir.mkdir(parents=True, exist_ok=True)
    (ctl_dir / runtime.CONTROL_FILE).write_text(json.dumps(control), encoding="utf-8")

    cwd = tape.header.get("cwd") if tape.header.get("cwd") and Path(tape.header["cwd"]).is_dir() else str(run_dir)
    turns = [t.get("user_message") for t in tape.turns()]
    turns = [t if isinstance(t, str) else json.dumps(t) for t in turns if t is not None]
    if opts.fork_prompt:
        turns.append(opts.fork_prompt)
    try:
        replies, errors, sent, stderr_tail = drive_session(opts.hermes_python, env, cwd, turns, opts.turn_timeout)
    finally:
        stub.stop()
    if errors and stderr_tail:
        errors.append("hermes stderr (tail):\n" + "\n".join(stderr_tail[-15:]))

    events = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines() if line.strip()]
    served = sum(1 for e in events if e.get("type") in ("llm_match", "llm_drift"))
    expected = len(tape.of("llm"))
    if not opts.fork_at and served < expected:
        missing = {"type": "missing_llm_calls", "served": served, "expected": expected,
                   "note": "Hermes made fewer model calls than the recording"}
        events.append(missing)
        emit("missing_llm_calls", {k: v for k, v in missing.items() if k != "type"})
    result = PlayResult(run_dir, report, events, sent, replies, errors)
    (run_dir / "summary.json").write_text(json.dumps({
        "cassette": tape.id, "verdict": result.verdict, "turns_sent": sent, "turns_total": len(turns),
        "replies": replies, "errors": errors,
        "counts": {t: result.count(t) for t in sorted({e.get("type") for e in events})},
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    if not opts.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return result


def drive_session(hermes_python: str, env: dict[str, str], cwd: str, turns: list[str],
                  turn_timeout: float = 300.0) -> tuple[list[str], list[str], int, list[str]]:
    """Run ``turns`` through one Hermes session over the TUI-gateway protocol.

    Every approval or other human prompt is refused. Returns (replies, errors, turns_sent, stderr_tail).
    """
    replies: list[str] = []
    errors: list[str] = []
    client = RpcClient([hermes_python, "-m", "tui_gateway.entry"], env, cwd)
    sent = 0
    try:
        sid = _handshake(client, turn_timeout, errors)
        for text in turns if sid else []:
            reply = _run_turn(client, sid, text, turn_timeout, errors)
            sent += 1
            replies.append(reply if reply is not None else "")
            if reply is None:
                break
    finally:
        client.close()
    return replies, errors, sent, client.stderr_tail


def _wait(client: RpcClient, pred, timeout: float, errors: list[str], sid: str | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = client.q.get(timeout=0.5)
        except queue.Empty:
            if client.proc.poll() is not None:
                errors.append(f"hermes exited with code {client.proc.returncode}")
                return None
            continue
        if msg.get("__eof__"):
            errors.append("hermes closed its output")
            return None
        method = msg.get("method")
        if method and "id" in msg and method != "event":  # server -> client request
            answer = _DENY.get(method)
            if answer is None:
                client.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "ariadne replay: not supported"}})
            else:
                client.send({"jsonrpc": "2.0", "id": msg["id"], "result": answer})
            continue
        got = pred(msg)
        if got is not None:
            return got
    errors.append(f"timed out after {timeout:.0f}s")
    return None


def _handshake(client: RpcClient, timeout: float, errors: list[str]) -> str | None:
    ready = _wait(client, lambda m: True if (m.get("params") or {}).get("type") == "gateway.ready" else None, timeout, errors)
    if not ready:
        return None
    client.call("client.capabilities", {"server_requests": True})
    cid = client.call("session.create", {})

    def created(m: dict[str, Any]):
        if m.get("id") == cid:
            if "error" in m:
                errors.append(f"session.create failed: {m['error']}")
                return False
            return (m.get("result") or {}).get("session_id") or False
        return None

    sid = _wait(client, created, timeout, errors)
    return sid or None


def _run_turn(client: RpcClient, sid: str, text: str, timeout: float, errors: list[str]) -> str | None:
    rid = client.call("prompt.submit", {"session_id": sid, "text": text})

    def done(m: dict[str, Any]):
        if m.get("id") == rid and "error" in m:
            errors.append(f"prompt.submit failed: {m['error']}")
            return False
        p = m.get("params") or {}
        if p.get("session_id") == sid and p.get("type") == "message.complete":
            return (p.get("payload") or {}).get("text") or ""
        if p.get("session_id") == sid and p.get("type") in ("error", "turn.error", "message.error"):
            errors.append(f"turn error: {json.dumps(p.get('payload'))[:400]}")
            return False
        return None

    got = _wait(client, done, timeout, errors)
    return got if isinstance(got, str) else None
