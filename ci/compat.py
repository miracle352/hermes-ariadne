"""Plugin compatibility: do real catalog plugins still work on a given Hermes?

For each plugin in ci/compat-plugins.txt this fetches the exact commit the Hermes plugin
catalog pins, installs the Python packages it declares, runs `hermes plugins validate` with
the Hermes under test, then replays Ariadne's committed cassettes with the plugin loaded and
records whether it loaded, whether any of its hooks raised or hung, and whether Hermes kept
its hook promises. Replays are offline: no model, no network, the plugin's hooks run in a
scratch home.

Usage: python ci/compat.py --hermes-python PY --hermes-src DIR --label v0.23.1 --out compat/v0.23.1.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))
driver = __import__(f"{HERE.name}.driver", fromlist=["driver"])
util = __import__(f"{HERE.name}.util", fromlist=["util"])

CASSETTES = ["anthropic_tools", "write_then_patch", "parallel_tools", "subagent"]


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 600), **kw)


def catalog_entry(src: Path, name: str) -> dict:
    data = util.read_yaml(src / "plugin-catalog" / f"{name}.yaml")
    if not data:
        raise RuntimeError(f"{name} is not in this Hermes's plugin catalog")
    return data


def fetch(entry: dict, into: Path) -> Path:
    repo = into / entry["name"]
    if not repo.exists():
        r = _run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", entry["repo"], str(repo)])
        if r.returncode:
            raise RuntimeError(f"clone failed: {r.stderr.strip()[-200:]}")
        r = _run(["git", "-C", str(repo), "checkout", "--quiet", entry["sha"]])
        if r.returncode:
            raise RuntimeError(f"checkout {entry['sha'][:8]} failed: {r.stderr.strip()[-200:]}")
    path = repo / entry["subdir"] if entry.get("subdir") else repo
    if not (path / "plugin.yaml").is_file():
        raise RuntimeError("no plugin.yaml at the pinned commit")
    return path


def install_deps(python: str, plugin: Path) -> str:
    meta = util.read_yaml(plugin / "plugin.yaml")
    reqs = [r for k in ("python_dependencies", "pip_dependencies") for r in (meta.get(k) or []) if isinstance(r, str)]
    if not reqs:
        return ""
    uv = shutil.which("uv")
    r = _run([uv or "uv", "pip", "install", "--quiet", "--python", python, *reqs], env=util.env_for(python), timeout=900)
    return "" if r.returncode == 0 else r.stderr.strip()[-200:]


def validate(python: str, plugin: Path) -> tuple[bool, str]:
    home = tempfile.mkdtemp(prefix="ariadne-compat-")
    try:
        env = util.env_for(python)
        env["HERMES_HOME"] = home
        r = _run([python, "-m", "hermes_cli.main", "plugins", "validate", str(plugin)], env=env, cwd=home, timeout=300)
        text = (r.stdout + r.stderr).strip()
        return r.returncode == 0, "\n".join(text.splitlines()[-40:])[-4000:]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def check(python: str, src: Path, name: str, work: Path) -> dict:
    row: dict = {"plugin": name, "status": "ok", "notes": []}
    try:
        entry = catalog_entry(src, name)
        row.update(repo=entry["repo"], sha=entry["sha"], version=str(entry.get("version") or ""),
                   requires_hermes=str(entry.get("requires_hermes") or ""))
        plugin = fetch(entry, work)
    except Exception as exc:
        row.update(status="skipped", notes=[str(exc)[:200]])
        return row
    dep_err = install_deps(python, plugin)
    if dep_err:
        row["notes"].append(f"its Python packages did not install: {dep_err}")
    ok, out = validate(python, plugin)
    row["validate"] = "pass" if ok else "fail"
    if not ok:
        row["status"] = "fail"
        row["validate_output"] = out
        why = [l.strip() for l in out.splitlines() if l.strip() and any(k in l.lower() for k in ("error", "fail", "✗", "rule", "missing", "not "))]
        row["notes"].append("hermes plugins validate failed: " + (why[0] if why else out.splitlines()[-1] if out else "")[:300])
    problems, violations, errors = [], [], []
    for cid in CASSETTES:
        tape = HERE / "tests" / "cassettes" / f"{cid}.cassette.jsonl"
        if not tape.is_file():
            continue
        res = driver.play(tape, driver.PlayOptions(hermes_python=python, quiet=True, turn_timeout=120,
                                                   extra_plugins=[str(plugin)]))
        problems += [e for e in res.events if e.get("type") == "plugin_problem"]
        violations += [e for e in res.events if e.get("type") == "contract_violation"]
        if res.verdict == "error":
            errors += res.errors[:1]
    seen = set()
    for p in problems:
        g = p.get("groups") or []
        key = (p["kind"], tuple(g[:2]))
        if key in seen:
            continue
        seen.add(key)
        if p["kind"] == "plugin_load_failed":
            row["notes"].append(f"does not load: {(g[1] if len(g) > 1 else '')[:200]}")
        elif p["kind"] == "plugin_hook_raised":
            row["notes"].append(f"hook {g[0]} ({g[1]}) raised: {(g[2] if len(g) > 2 else '')[:500]}")
        else:
            row["notes"].append(f"hook {g[0]} ({g[1]}) timed out")
    if problems or errors:
        row["status"] = "fail"
    if errors:
        row["notes"].append(f"replay failed: {str(errors[0])[:200]}")
    row["hook_problems"] = len(problems)
    row["contract_violations"] = len(violations)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-python", required=True)
    ap.add_argument("--hermes-src", required=True, help="Hermes checkout (its plugin-catalog/ pins the plugins)")
    ap.add_argument("--catalog", default=None, help="Hermes checkout whose plugin-catalog/ to read (default: --hermes-src)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--plugins", default=str(HERE / "ci" / "compat-plugins.txt"))
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    only = [n.strip() for o in args.only for n in o.split(",") if n.strip()]
    names = only or [l.split("#")[0].strip() for l in Path(args.plugins).read_text().splitlines() if l.split("#")[0].strip()]
    src = Path(args.hermes_src)
    commit = _run(["git", "-C", str(src), "rev-parse", "HEAD"]).stdout.strip()
    rows = []
    with tempfile.TemporaryDirectory(prefix="ariadne-plugins-") as work:
        for name in names:
            t = time.time()
            row = check(args.hermes_python, Path(args.catalog) if args.catalog else src, name, Path(work))
            row["seconds"] = round(time.time() - t, 1)
            print(f"{row['status']:<8} {name:<28} {'; '.join(row['notes'])[:600]}", flush=True)
            if row.get("validate_output"):
                print(row["validate_output"], flush=True)
            rows.append(row)
    out = {"hermes": args.label, "commit": commit, "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "plugins": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
