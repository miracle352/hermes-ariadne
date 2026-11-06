"""Scenario suite: record each scripted scenario on Hermes A, then
  1. replay on A          -> any drift is non-determinism in Hermes (breaks prompt caching)
  2. replay on A and B    -> any difference is a change between the two versions

Usage: python tests/scenarios/run.py <python-A> [<python-B>] [scenario ...]
Free and offline: the model is tests/support/scripted_model.py (plain code).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO.parent))
sys.path.insert(0, str(REPO / "tests" / "support"))
import scripted_model  # noqa: E402

driver = __import__(f"{REPO.name}.driver", fromlist=["driver"])
compare = __import__(f"{REPO.name}.compare", fromlist=["compare"])


def _fill(obj, project: str):
    if isinstance(obj, str):
        return obj.replace("{P}", project)
    if isinstance(obj, list):
        return [_fill(x, project) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, project) for k, v in obj.items()}
    return obj


def record(scn: dict, python_a: str, work: Path) -> Path | None:
    home, project = work / "home", work / "project"
    if work.exists():
        shutil.rmtree(work)
    (home / "plugins").mkdir(parents=True)
    project.mkdir(parents=True)
    (project / "notes.txt").write_text("hello\n")
    (home / "plugins" / "ariadne").symlink_to(REPO, target_is_directory=True)
    server = scripted_model.serve(0, _fill(scn["script"], str(project)), work / "model-log")
    cfg = {"model": {"provider": "custom", "model": "scripted", "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1", "api_key": "none"},
           "plugins": {"enabled": ["ariadne"], "disabled": []}}
    from importlib import import_module
    cfg = import_module(f"{REPO.name}.util").deep_merge(cfg, scn.get("config") or {})
    (home / "config.yaml").write_text(json.dumps(cfg))
    (home / "plugin-data" / "ariadne").mkdir(parents=True)
    (home / "plugin-data" / "ariadne" / "control.json").write_text('{"mode": "record"}')
    # One tool runtime for every scenario home instead of ~280 MB per home (Hermes's own dev
    # workflow points HERMES_RUNTIME_DIR at a shared store the same way).
    import hashlib
    runtime = Path(tempfile.gettempdir()) / "ariadne-scenario-runtime" / hashlib.sha256(python_a.encode()).hexdigest()[:12]
    runtime.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(runtime), PYTHONUNBUFFERED="1")
    # drain=never: after the last turn keep listening until Hermes goes quiet, so turns Hermes
    # starts by itself (async subagent results) are recorded too.
    replies, errors, _, tail = driver.drive_session(python_a, env, str(project), _fill(scn["turns"], str(project)), 120,
                                                    drain=lambda: False, idle=6.0)
    server.shutdown()
    print("    record replies:", [r[:160] for r in replies])
    (home / "plugin-data" / "ariadne" / "control.json").unlink()
    tapes = sorted((home / "plugin-data" / "ariadne" / "cassettes").glob("*.cassette.jsonl"))
    if errors or not tapes:
        print(f"    record failed: {errors[:2]}")
        return None
    os.environ["HERMES_HOME"] = str(home)
    return tapes[-1]


def main() -> int:
    python_a = sys.argv[1]
    python_b = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].endswith(".json") and "/" in sys.argv[2] else None
    names = [a for a in sys.argv[2 + (1 if python_b else 0):]]
    files = sorted(HERE.glob("*.json")) if not names else [HERE / f"{n}.json" for n in names]
    root = Path(tempfile.mkdtemp(prefix="ariadne-scn-"))
    summary = []
    for f in files:
        scn = json.loads(f.read_text())
        print(f"== {scn['name']}")
        tape = record(scn, python_a, root / scn["name"])
        if tape is None:
            summary.append((scn["name"], "record-failed", "-"))
            continue
        kinds = [json.loads(l).get("kind") for l in tape.read_text().splitlines()[1:]]
        print(f"    cassette: {dict((k, kinds.count(k)) for k in sorted(set(kinds)))}")
        res = driver.play(tape, driver.PlayOptions(hermes_python=python_a, turn_timeout=120, keep=False))  # config from the cassette snapshot
        det = res.verdict
        cv = [e for e in res.events if e.get("type") == "contract_violation"]
        cc = next((e for e in res.events if e.get("type") == "contracts_checked"), None)
        print(f"    replay on A: {det}   contracts: {len(cv)} violation(s){'' if cc else ' (finish not observed)'}")
        for e in cv[:6]:
            print("       !", e.get("rule"), {k: v for k, v in e.items() if k not in ("type", "t", "promise", "rule")})
        rec_cv = (tape.parent.parent / "contracts.jsonl")
        if rec_cv.is_file():
            for line in rec_cv.read_text().splitlines()[:6]:
                print("       ! (during recording)", line[:300])
        if det != "identical":
            for e in res.events:
                if e.get("type") in ("llm_drift", "tool_drift", "off_script_tool", "extra_llm_call", "missing_llm_calls", "leak"):
                    print("      ", json.dumps(e, ensure_ascii=False)[:400])
            for err in res.errors[:2]:
                print("       error:", err[:300])
        cmpv = "-"
        if python_b:
            c = compare.compare(tape, python_a, python_b, label_a="A", label_b="B", opts=driver.PlayOptions(turn_timeout=120))
            cmpv = c.verdict
            print(f"    A vs B: {cmpv}")
            if c.changes:
                print("\n".join("      " + l for l in compare.render(c).splitlines()[3:40]))
        summary.append((scn["name"], det, cmpv))
        shutil.rmtree(root / scn["name"], ignore_errors=True)
    print("\nscenario                 replay-on-A   A-vs-B")
    for n, d, c in summary:
        print(f"  {n:<22} {d:<13} {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
