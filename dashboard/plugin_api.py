"""Ariadne backend for the Desktop app, mounted at /api/plugins/ariadne/.

Reads cassettes, runs and contract reports from the plugin's data folder and starts
replays and forks as background jobs. Progress is pushed to the desktop half as
plugin.ariadne.* events; every route also works by polling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

PKG_DIR = Path(__file__).resolve().parent.parent


def _pkg():
    """The Ariadne package: the copy Hermes already loaded, or this folder imported once."""
    for name, mod in list(sys.modules.items()):
        if name.startswith("hermes_plugins.") and getattr(mod, "__file__", None) and \
                Path(mod.__file__).resolve() == PKG_DIR / "__init__.py":
            return mod
    name = "ariadne_dashboard_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PKG_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PKG_DIR)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _mod(sub: str):
    base = _pkg().__name__
    return importlib.import_module(f"{base}.{sub}")


def _broadcast(event: str, payload: dict[str, Any]) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except ImportError:
        return
    broadcast_plugin_event("ariadne", event, payload)


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:] if limit else rows


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


# ----------------------------------------------------------------------------- reads

def _summary(path: Path) -> dict[str, Any] | None:
    cas = _mod("cassette")
    try:
        tape = cas.load(path)
    except cas.CassetteError:
        return None
    h = tape.header
    turns = tape.turns()
    first = _text(turns[0].get("user_message")) if turns else ""
    return {
        "id": path.name.removesuffix(cas.SUFFIX), "tape_id": tape.id, "created": h.get("created"), "session_id": h.get("session_id"),
        "model": h.get("model"), "provider": h.get("provider"), "platform": h.get("platform"),
        "turns": len(turns), "model_calls": len(tape.of("llm")), "tools": len(tape.of("tool")),
        "subagents": len(tape.of("subagent")), "compactions": len(tape.of("compaction")),
        "first_message": first[:200], "bytes": path.stat().st_size,
    }


def _cassette_path(cid: str) -> Path:
    rt, cas = _mod("runtime"), _mod("cassette")
    if cid in ("latest", "last"):
        newest = next(iter(cas.iter_dir(rt.cassette_dir())), None)
        if newest is None:
            raise HTTPException(404, "no cassettes yet")
        return newest
    path = rt.cassette_dir() / f"{Path(cid).name}{cas.SUFFIX}"
    if not path.is_file():
        raise HTTPException(404, f"no cassette {cid}")
    return path


@router.get("/status")
async def status():
    rt, cas = _mod("runtime"), _mod("cassette")
    mode = rt.Control().get().get("mode")
    cassettes = list(cas.iter_dir(rt.cassette_dir()))
    return {
        "recording": mode == "record", "mode": mode, "cassettes": len(cassettes),
        "runs": len(list((rt.data_dir() / "runs").glob("*/summary.json"))),
        "contract_violations": len(_read_jsonl(rt.data_dir() / "contracts.jsonl")),
        "data_dir": str(rt.data_dir()), "version": rt._plugin_version(),
        "jobs": [j.public() for j in JOBS.values() if j.state == "running"],
    }


@router.post("/record")
async def record(body: dict):
    rt = _mod("runtime")
    path = rt.data_dir() / rt.CONTROL_FILE
    on = bool(body.get("on"))
    if on:
        path.write_text(json.dumps({"mode": "record"}), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    _broadcast("record.changed", {"recording": on})
    return {"recording": on}


@router.get("/cassettes")
async def cassettes():
    rt, cas = _mod("runtime"), _mod("cassette")
    return {"cassettes": [s for s in (_summary(p) for p in cas.iter_dir(rt.cassette_dir())) if s]}


@router.get("/sessions")
async def sessions():
    """session id -> newest cassette id, for the badges on the session list."""
    rt, cas = _mod("runtime"), _mod("cassette")
    out: dict[str, str] = {}
    for p in cas.iter_dir(rt.cassette_dir()):
        try:
            sid = json.loads(p.open(encoding="utf-8").readline()).get("session_id")
        except (OSError, ValueError):
            continue
        if sid and sid not in out:
            out[sid] = p.name.removesuffix(cas.SUFFIX)
    return {"sessions": out}


@router.get("/cassettes/{cid}")
async def cassette(cid: str):
    cas = _mod("cassette")
    path = _cassette_path(cid)
    tape = cas.load(path)
    timeline = []
    for e in tape.events:
        k = e.get("kind")
        item: dict[str, Any] = {"seq": e.get("seq"), "kind": k, "t": e.get("t")}
        if k == "turn":
            item["text"] = _text(e.get("user_message"))[:400]
            item["subagent"] = bool(e.get("subagent"))
        elif k == "llm":
            item["call"] = e.get("api_call_count")
            item["elapsed_ms"] = e.get("elapsed_ms")
            item["model"] = e.get("model")
        elif k == "tool":
            item["tool"] = e.get("tool_name")
            item["args"] = _text(e.get("args"))[:200]
        elif k == "aux":
            item["task"] = e.get("aux_task")
        elif k == "compaction":
            item["reason"] = e.get("reason")
        elif k == "subagent":
            item["child"] = e.get("child_session_id")
        timeline.append(item)
    header = {k: v for k, v in tape.header.items() if k != "config_snapshot"}
    return {"summary": _summary(path), "header": header, "timeline": timeline}


def _runs_in(folder: str) -> list[dict[str, Any]]:
    rt = _mod("runtime")
    out = []
    for d in sorted((rt.data_dir() / folder).glob("*"), reverse=True):
        summary = d / "summary.json"
        info: dict[str, Any] = {"id": d.name, "kind": folder.rstrip("s")}
        if summary.is_file():
            try:
                info.update(json.loads(summary.read_text(encoding="utf-8")))
            except ValueError:
                pass
        elif not (d / "report.jsonl").is_file():
            continue
        info["id"] = d.name
        out.append(info)
    return out


@router.get("/runs")
async def runs():
    return {"runs": _runs_in("runs") + _runs_in("forks")}


@router.get("/runs/{rid}")
async def run(rid: str):
    rt = _mod("runtime")
    for folder in ("runs", "forks"):
        d = rt.data_dir() / folder / Path(rid).name
        if d.is_dir():
            summary = d / "summary.json"
            return {
                "id": d.name, "kind": folder.rstrip("s"),
                "summary": json.loads(summary.read_text(encoding="utf-8")) if summary.is_file() else None,
                "events": _read_jsonl(d / "report.jsonl", limit=2000),
            }
    raise HTTPException(404, f"no run {rid}")


@router.get("/contracts")
async def contracts():
    rt = _mod("runtime")
    rows = _read_jsonl(rt.data_dir() / "contracts.jsonl", limit=500)
    promises = _mod("contracts").PROMISES
    return {"violations": rows, "promises": promises}


# ----------------------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str, cassette: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.cassette = kind, cassette
        self.state, self.started, self.finished = "running", time.time(), None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.run_dir: Path | None = None
        self.seen = 0

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "cassette": self.cassette, "state": self.state,
                "started": self.started, "finished": self.finished, "result": self.result,
                "error": self.error, "run": self.run_dir.name if self.run_dir else None}


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def _newest_dir(folder: Path, prefix: str, after: float) -> Path | None:
    found = [d for d in folder.glob(f"{prefix}--*") if d.is_dir() and d.stat().st_mtime >= after - 1]
    return max(found, key=lambda d: d.stat().st_mtime) if found else None


def _tail(job: Job, folder: Path, prefix: str, done: threading.Event) -> None:
    """Forward new report lines as plugin.ariadne.run.event while the job runs."""
    while not done.wait(0.4):
        if job.run_dir is None:
            job.run_dir = _newest_dir(folder, prefix, job.started)
            if job.run_dir is None:
                continue
            _broadcast("run.started", job.public())
        rows = _read_jsonl(job.run_dir / "report.jsonl")
        for row in rows[job.seen:]:
            _broadcast("run.event", {"job": job.id, "event": row})
        job.seen = len(rows)


def _start(kind: str, cid: str, work) -> dict[str, Any]:
    path = _cassette_path(cid)
    rt = _mod("runtime")
    cas = _mod("cassette")
    job = Job(kind, path.name.removesuffix(cas.SUFFIX))
    prefix = cas.load(path).id  # run folders are named after the recorded id
    with _LOCK:
        if any(j.state == "running" for j in JOBS.values()):
            raise HTTPException(409, "a replay is already running")
        JOBS[job.id] = job
    folder = rt.data_dir() / ("runs" if kind == "play" else "forks")
    folder.mkdir(parents=True, exist_ok=True)
    done = threading.Event()

    def body() -> None:
        tail = threading.Thread(target=_tail, args=(job, folder, prefix, done), daemon=True)
        tail.start()
        try:
            job.result = work(path)
            job.state = "done"
        except Exception as exc:  # reported to the UI, not raised into the gateway
            job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"
        finally:
            job.finished = time.time()
            done.set()
            tail.join(timeout=2)
            if job.run_dir is None:
                job.run_dir = _newest_dir(folder, prefix, job.started)
            _broadcast("run.finished", job.public())

    threading.Thread(target=body, name=f"ariadne-{kind}-{job.id}", daemon=True).start()
    return job.public()


@router.post("/play")
async def play(body: dict):
    driver, rt = _mod("driver"), _mod("runtime")

    def work(path: Path) -> dict[str, Any]:
        result = driver.play(path, driver.PlayOptions(
            hermes_python=sys.executable, quiet=True,
            clone_home=None if body.get("no_clone") else rt.hermes_home()))
        return json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))

    return _start("play", str(body.get("cassette") or "latest"), work)


@router.post("/fork")
async def fork(body: dict):
    forker, rt = _mod("fork"), _mod("runtime")
    at_turn = body.get("at_turn")
    prompt = body.get("prompt") or None

    def work(path: Path) -> dict[str, Any]:
        result = forker.fork(path, at_turn=int(at_turn) if at_turn is not None else None, prompt=prompt,
                             home=rt.hermes_home(), hermes_python=sys.executable)
        return result.to_json()

    return _start("fork", str(body.get("cassette") or "latest"), work)


@router.get("/jobs/{jid}")
async def job(jid: str):
    j = JOBS.get(jid)
    if j is None:
        raise HTTPException(404, f"no job {jid}")
    return j.public()
