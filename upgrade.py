"""Safe to update? Replay your own recorded sessions on a newer Hermes before you switch.

The newer Hermes is installed into a throwaway folder under the plugin's data directory, from
the Hermes git repository and its own uv.lock, so your real install is never touched. Each
cassette is then replayed on both installs side by side (see compare.py), which cancels out
everything about your machine: what is left is what the new Hermes does differently with your
sessions. Replays are offline; only the install step uses the network (git and the package index).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import cassette as cas
from . import compare, driver, runtime

REPO = "https://github.com/NousResearch/hermes-agent.git"
Progress = Callable[[dict[str, Any]], None]


def _quiet(_: dict[str, Any]) -> None:
    pass


# ----------------------------------------------------------------------------- targets

def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 900) -> str:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(out.stderr or out.stdout).strip()[-600:]}")
    return out.stdout


_RELEASE = re.compile(r"refs/tags/(v(\d+)\.(\d+)\.(\d+))$")


def pick_release(ls_remote: str) -> str:
    """Newest plain release tag (no rc, canary or other suffix) in ``git ls-remote`` output."""
    best: tuple[int, int, int] | None = None
    tag = ""
    for line in ls_remote.splitlines():
        m = _RELEASE.search(line.strip())
        if m and int(m.group(2)) < 1000:  # Hermes also has date tags (v2026.9.24); releases are vX.Y.Z
            v = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if best is None or v > best:
                best, tag = v, m.group(1)
    if not tag:
        raise RuntimeError("could not find a Hermes release tag")
    return tag


def latest_release() -> str:
    return pick_release(_run(["git", "ls-remote", "--tags", "--refs", REPO, "v*"], timeout=60))


def find_uv() -> str | None:
    """uv on PATH, or the uv Hermes's own installer staged for this install."""
    found = shutil.which("uv")
    if found:
        return found
    probe = "from pm.install import uv_launcher; p = uv_launcher('uv'); print(p or '')"
    try:
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        path = out.stdout.strip()
        return path if path and Path(path).is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _rmtree(path: Path) -> None:
    """Delete a folder git wrote. On Windows git marks its object files read-only, which makes a
    plain rmtree fail, so clear the flag and retry each file."""
    import stat

    def again(func, target, _exc):
        for p in (os.path.dirname(target), target):  # the folder may be locked as well as the file
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        func(target)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=again)
        else:  # pragma: no cover
            shutil.rmtree(path, onerror=again)


def _safe(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ref)[:60]


def install(ref: str, progress: Progress = _quiet, max_age_hours: float = 6.0) -> tuple[Path, str]:
    """Install Hermes ``ref`` into a throwaway env; returns (python, commit)."""
    root = runtime.data_dir() / "hermes" / _safe(ref)
    src, venv, stamp = root / "src", root / "venv", root / "installed.json"
    py = _venv_python(venv)
    if stamp.is_file() and py.is_file():
        info = json.loads(stamp.read_text(encoding="utf-8"))
        moving = not _RELEASE.search("refs/tags/" + ref)
        stale = moving and ((src / ".git" / "shallow").exists() or time.time() - info.get("at", 0) > max_age_hours * 3600)
        if not stale:
            progress({"stage": "install", "note": f"using the {ref} already installed for checks"})
            return py, info.get("commit", "")
    uv = find_uv()
    if not uv:
        raise RuntimeError("uv was not found. Install uv (https://docs.astral.sh/uv/) or point --to at a Hermes python")
    _rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    progress({"stage": "install", "note": f"downloading Hermes {ref}"})
    # Releases carry their version; main and other refs work it out from the release tags with
    # `git describe`, so they need history and tags (commits only: --filter=tree:0), not depth 1.
    shallow = ["--depth", "1"] if _RELEASE.search("refs/tags/" + ref) else ["--filter=tree:0"]
    try:
        _run(["git", "clone", "--quiet", *shallow, "--branch", ref, REPO, str(src)])
    except RuntimeError:  # a commit sha, not a branch or tag
        _run(["git", "clone", "--quiet", "--filter=blob:none", REPO, str(src)])
        _run(["git", "checkout", "--quiet", ref], cwd=src)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=src).strip()
    progress({"stage": "install", "note": "installing its pinned dependencies"})
    req = root / "requirements.txt"
    _run([uv, "export", "--quiet", "--frozen", "--no-hashes", "--no-emit-project",
          "--extra", "anthropic", "-o", str(req)], cwd=src)
    _run([uv, "venv", "--quiet", str(venv), "--python", f"{sys.version_info.major}.{sys.version_info.minor}"])
    env = dict(os.environ, VIRTUAL_ENV=str(venv))
    for args in (["-r", str(req)], ["-e", str(src)]):
        out = subprocess.run([uv, "pip", "install", "--quiet", "--no-deps", "--python", str(py), *args],
                             capture_output=True, text=True, env={k: v for k, v in env.items() if k != "PYTHONPATH"}, timeout=1800)
        if out.returncode != 0:
            raise RuntimeError(f"installing Hermes {ref} failed: {(out.stderr or out.stdout).strip()[-600:]}")
    stamp.write_text(json.dumps({"ref": ref, "commit": commit, "at": time.time()}), encoding="utf-8")
    return py, commit


def resolve(to: str, progress: Progress = _quiet) -> tuple[str, str, str]:
    """``to`` -> (python, label, commit). ``release`` | ``main`` | a git ref | a python path."""
    p = Path(to).expanduser()
    if p.is_file():
        return str(p), p.parent.parent.name or str(p), ""
    if p.is_dir() and _venv_python(p).is_file():
        return str(_venv_python(p)), p.name, ""
    ref = latest_release() if to in ("release", "latest") else to
    py, commit = install(ref, progress)
    # Prove the throwaway interpreter really runs the Hermes we installed, not the one we run in.
    from .util import hermes_source
    src = runtime.data_dir() / "hermes" / _safe(ref) / "src"
    got = hermes_source(str(py))
    if not got or Path(got).resolve() != src.resolve():
        raise RuntimeError(f"the Hermes {ref} install imports Hermes from {got or 'nowhere'}, not {src}; refusing to compare")
    return str(py), ref, commit


# ----------------------------------------------------------------------------- your plugins

def user_plugins() -> list[Path]:
    """The plugins enabled on this profile (other than Ariadne), as directories."""
    from .util import read_yaml
    home = Path(runtime.hermes_home())
    cfg = read_yaml(home / "config.yaml")
    enabled = (cfg.get("plugins") or {}).get("enabled") if isinstance(cfg.get("plugins"), dict) else None
    out = []
    for name in enabled or []:
        d = home / "plugins" / str(name)
        if name != "ariadne" and (d / "plugin.yaml").is_file():
            out.append(d.resolve())
    return out


def install_plugin_deps(new_py: str, plugins: list[Path], progress: Progress = _quiet) -> list[str]:
    """Install the Python packages your plugins declare into the throwaway Hermes."""
    from .util import read_yaml
    reqs: list[str] = []
    for d in plugins:
        meta = read_yaml(d / "plugin.yaml")
        for key in ("python_dependencies", "pip_dependencies"):
            reqs += [r for r in (meta.get(key) or []) if isinstance(r, str) and r.strip()]
    uv = find_uv()
    if not reqs or not uv:
        return []
    progress({"stage": "install", "note": f"installing {len(reqs)} package(s) your plugins need"})
    from .util import env_for
    out = subprocess.run([uv, "pip", "install", "--quiet", "--python", new_py, *sorted(set(reqs))],
                         capture_output=True, text=True, timeout=900, env=env_for(new_py))
    return [] if out.returncode == 0 else [(out.stderr or out.stdout).strip()[-300:]]


# ----------------------------------------------------------------------------- verdicts

def _tool_map(live: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in live:
        if e.get("kind") != "llm":
            continue
        for t in (e.get("request") or {}).get("tools") or []:
            if isinstance(t, dict):
                fn = t.get("function") if isinstance(t.get("function"), dict) else t
                if isinstance(fn.get("name"), str):
                    out.setdefault(fn["name"], fn)
    return out


def _params(fn: dict[str, Any]) -> Any:
    return fn.get("parameters") or fn.get("input_schema") or {}


def _names(xs: list[str], cap: int = 6) -> str:
    xs = sorted(xs)
    return ", ".join(xs[:cap]) + (f" and {len(xs) - cap} more" if len(xs) > cap else "")


def explain(changes: list[dict[str, Any]], live_a: list[dict[str, Any]], live_b: list[dict[str, Any]]) -> list[str]:
    """Plain sentences for what the new Hermes does differently."""
    out: list[str] = []
    sections: set[str] = set()
    for ch in changes:
        k = ch["kind"]
        if k == "model_request":
            sections.update(ch["sections"])
        elif k == "model_call_count":
            out.append(f"Makes {ch['b']} model calls where the current Hermes makes {ch['a']}.")
        elif k == "tool_calls":
            out.append("Runs a different sequence of tool calls.")
        elif k == "final_replies":
            out.append("Ends the session with a different reply.")
        elif k == "replay_error":
            side = "the new Hermes" if ch.get("side") == "new" else "the current Hermes"
            out.append(f"The replay failed on {side}: {str((ch.get('errors') or [''])[0])[:200]}")
    if "tools" in sections:
        added, removed, args, desc = set(), set(), set(), set()
        for ch in changes:
            for d in ch.get("diffs") or []:
                path = str(d.get("path") or "")
                if not path.startswith(".tools."):
                    continue
                name, _, rest = path[len(".tools."):].partition(".")
                if not rest:
                    (added if d.get("change") == "added" else removed if d.get("change") == "removed" else args).add(name)
                elif rest.endswith("description"):
                    desc.add(name)
                else:
                    args.add(name)
        desc -= args
        if not (added or removed or args or desc):  # tools sent as a list: compare by name
            ta, tb = _tool_map(live_a), _tool_map(live_b)
            added, removed = set(tb) - set(ta), set(ta) - set(tb)
            args = {n for n in set(ta) & set(tb) if _params(ta[n]) != _params(tb[n])}
            desc = {n for n in set(ta) & set(tb) if n not in args and ta[n] != tb[n]}
        if added:
            out.append(f"Gives the model new tools: {_names(list(added))}.")
        if removed:
            out.append(f"Takes tools away from the model: {_names(list(removed))}.")
        if args:
            out.append(f"Changes the arguments of: {_names(list(args))}.")
        if desc:
            out.append(f"Rewords how it describes: {_names(list(desc))}.")
        if not (added or removed or args or desc):
            out.append("Sends the tool list in a different order or format.")
    if "system" in sections:
        out.append("Gives the model different instructions (system prompt).")
    if "messages" in sections:
        out.append("Sends your conversation to the model differently.")
    rest = sections - {"tools", "system", "messages"}
    if rest:
        out.append("Changes request settings: " + ", ".join(sorted(rest)) + ".")
    return out


MINOR = ("Rewords how it describes", "Gives the model different instructions", "Sends the tool list in a different order")


_NOTE = re.compile(r"^\s*\[System note:.*?\](?:\s*\n|\s*$)", re.S)  # the note ends at a "]" that closes a line


def session_title(tape: cas.Cassette) -> str:
    """The first thing the user actually typed (Hermes's own bracketed notes removed)."""
    for turn in tape.turns():
        msg = turn.get("user_message")
        text = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
        text = _NOTE.sub("", text).strip()
        if text:
            return " ".join(text.split())[:140]
    return "First-run onboarding" if tape.turns() else tape.id


def has_content(path: Path) -> bool:
    """A recording worth checking: at least one user turn and one model call."""
    try:
        tape = cas.load(path)
    except cas.CassetteError:
        return False
    return bool(tape.turns()) and bool(tape.of("llm"))


@dataclass
class CassetteCheck:
    cassette: str
    title: str
    verdict: str                     # same | changed | broken
    reasons: list[str] = field(default_factory=list)
    new_violations: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    minor: bool = False              # only wording changed


def _violations(result: driver.PlayResult | None) -> list[dict[str, Any]]:
    return [e for e in (result.events if result else []) if e.get("type") == "contract_violation"]


def _problems(result: driver.PlayResult | None) -> list[dict[str, Any]]:
    return [e for e in (result.events if result else []) if e.get("type") == "plugin_problem"]


def check_one(path: Path, current_py: str, new_py: str, plugins: list[Path] | None = None) -> CassetteCheck:
    tape = cas.load(path)
    turns = tape.turns()
    title = session_title(tape)
    cmp = compare.compare(path, current_py, new_py, label_a="current", label_b="new",
                          opts=driver.PlayOptions(quiet=True, extra_plugins=[str(p) for p in plugins or []]))
    va, vb = _violations(cmp.a.result), _violations(cmp.b.result)
    seen = {v.get("rule") for v in va}
    new_v = [v for v in vb if v.get("rule") not in seen]
    for ch in cmp.changes:
        if ch["kind"] == "replay_error":
            ch["side"] = "new" if ch.get("side") == "new" else "current"
    reasons = explain(cmp.changes, cmp.a.live, cmp.b.live)
    for v in new_v:
        reasons.append(f"Breaks a promise plugins rely on: {v.get('promise') or v.get('rule')}.")
    before = {p.get("line", "")[:160] for p in _problems(cmp.a.result)}
    new_p = [p for p in _problems(cmp.b.result) if p.get("line", "")[:160] not in before]
    for p in new_p:
        g = p.get("groups") or []
        if p["kind"] == "plugin_load_failed":
            reasons.append(f"Your plugin {g[0] if g else ''} no longer loads: {(g[1] if len(g) > 1 else '')[:160]}")
        else:
            reasons.append(f"A plugin hook crashes: {g[1] if len(g) > 1 else ''} on {g[0] if g else ''}"
                           + (f" ({g[2][:120]})" if len(g) > 2 else "") + ".")
    new_v = new_v + [{"rule": "plugin", **p} for p in new_p]
    def timed_out(c: dict[str, Any]) -> bool:
        return any("timed out" in str(e) for e in c.get("errors") or [])

    new_errors = [c for c in cmp.changes if c["kind"] == "replay_error" and c.get("side") == "new"]
    cur_errors = [c for c in cmp.changes if c["kind"] == "replay_error" and c.get("side") != "new"]
    slow = bool(new_errors) and all(timed_out(c) for c in new_errors)
    if slow or cur_errors:
        # A replay that ran out of time (or failed on the Hermes you run now) says nothing about
        # the new Hermes; report it as unchecked rather than as a breakage.
        reasons = [r for r in reasons if not r.startswith("The replay failed")]
        pending = any("source-update completion failed" in str(e) for c in new_errors + cur_errors for e in c.get("errors") or [])
        reasons.insert(0, "Hermes has an unfinished update (run `hermes update` once), which stalls offline replays; not compared."
                       if pending else "Could not finish this replay in time, so it was not compared. Check it again on its own."
                       if slow else "This session does not replay on the Hermes you run now, so it was not compared.")
        row = CassetteCheck(path.name.removesuffix(cas.SUFFIX), title, "unchecked", reasons[:1], new_v, cmp.changes)
        return row
    broken = bool(new_v) or bool(new_errors)
    verdict = "broken" if broken else ("changed" if cmp.changes else "same")
    row = CassetteCheck(path.name.removesuffix(cas.SUFFIX), title, verdict, reasons, new_v, cmp.changes)
    row.minor = verdict == "changed" and all(r.startswith(MINOR) for r in reasons)
    return row


def run(to: str = "release", cassettes: list[Path] | None = None, limit: int = 5,
        progress: Progress = _quiet, with_plugins: bool = True, workers: int = 2) -> dict[str, Any]:
    started = time.time()
    current_py = sys.executable
    current = compare.hermes_version(current_py)
    tapes = cassettes if cassettes is not None else [p for p in cas.iter_dir(runtime.cassette_dir()) if has_content(p)][:limit]
    if not tapes:
        raise RuntimeError("no recordings yet: turn recording on and use Hermes, then check again")
    progress({"stage": "resolve", "note": f"finding Hermes {to}"})
    new_py, label, commit = resolve(to, progress)
    _, cur_commit, _ = compare.version_parts(current)
    if commit and cur_commit and commit.startswith(cur_commit):
        # Same code on both sides: nothing to replay.
        result = {"verdict": "safe", "minor": False, "same_version": True, "target": label, "target_commit": commit,
                  "current": current, "new": current, "counts": {"same": 0, "changed": 0, "broken": 0},
                  "sessions": [], "plugins": [], "plugin_dep_errors": [], "started": started,
                  "seconds": round(time.time() - started, 1)}
        progress({"stage": "done", "result": result})
        return result
    new = compare.hermes_version(new_py)
    if not new or "unknown" in new.lower():
        new = f"{label} ({commit[:8]})" if commit else label
    plugins = user_plugins() if with_plugins else []
    dep_errors = install_plugin_deps(new_py, plugins, progress) if plugins else []
    def one(path: Path) -> CassetteCheck:
        try:
            return check_one(Path(path), current_py, new_py, plugins)
        except Exception as exc:  # one bad cassette must not hide the others
            return CassetteCheck(Path(path).name.removesuffix(cas.SUFFIX), Path(path).name, "broken",
                                 [f"Could not replay: {type(exc).__name__}: {exc}"])

    # Each session replays on both installs at once (compare.py); `workers` sessions run side by
    # side, so 2 workers keep 4 Hermes processes busy. Results keep the order of the recordings.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    progress({"stage": "replay", "done": 0, "total": len(tapes)})
    found: dict[int, CassetteCheck] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, Path(p)): i for i, p in enumerate(tapes)}
        for fut in as_completed(futures):
            row = fut.result()
            found[futures[fut]] = row
            progress({"stage": "replay", "done": len(found), "total": len(tapes), "row": row.__dict__})
    rows = [found[i] for i in range(len(tapes))]
    counts = {v: sum(r.verdict == v for r in rows) for v in ("same", "changed", "broken", "unchecked")}
    verdict = "broken" if counts["broken"] else ("changed" if counts["changed"] else "safe")
    if rows and counts["unchecked"] == len(rows):
        verdict = "unchecked"
    minor = verdict == "changed" and all(r.minor for r in rows if r.verdict == "changed")
    result = {
        "verdict": verdict, "minor": minor, "target": label, "target_commit": commit, "current": current, "new": new,
        "counts": counts, "sessions": [r.__dict__ for r in rows],
        "plugins": [driver._plugin_name(p) for p in plugins], "plugin_dep_errors": dep_errors,
        "started": started, "seconds": round(time.time() - started, 1),
    }
    out = runtime.data_dir() / "upgrade"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}--{_safe(label)}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    progress({"stage": "done", "result": {k: v for k, v in result.items() if k != "sessions"}})
    return result


def latest_result() -> dict[str, Any] | None:
    folder = runtime.data_dir() / "upgrade"
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def render(result: dict[str, Any]) -> str:
    if result.get("same_version"):
        return f"You already run {result['target']} ({result['target_commit'][:8]}): there is nothing to update to. Try --to main."
    head = {"safe": "SAFE TO UPDATE", "changed": "REVIEW BEFORE UPDATING", "broken": "DO NOT UPDATE YET",
            "unchecked": "COULD NOT CHECK"}[result["verdict"]]
    if result.get("minor"):
        head = "LOOKS SAFE (WORDING CHANGES ONLY)"
    c = result["counts"]
    lines = [f"Ariadne upgrade check  ·  {result['current']}  ->  {result['new']}",
             f"{head}: {c['same']} same, {c['changed']} changed, {c['broken']} broken"
             + (f", {c['unchecked']} not checked" if c.get("unchecked") else "") + f"  ({result['seconds']} s, offline replays)"]
    if result.get("plugins"):
        lines.append(f"with your plugins loaded: {', '.join(result['plugins'])}")
    mark = {"same": "=", "changed": "~", "broken": "!", "unchecked": "?"}
    for s in result["sessions"]:
        lines.append(f"\n  {mark[s['verdict']]} {s['title'][:90]}")
        lines.extend(f"      {r}" for r in s["reasons"][:6])
    return "\n".join(lines)
