"""End-to-end check against a real Hermes install, offline and free.

1. Build a fresh HERMES_HOME whose model is the scripted (code-only) server.
2. Record a multi-turn session through the TUI-gateway protocol with Ariadne recording.
3. Replay the cassette with ``ariadne.driver.play`` and assert the verdict.

Usage:  python tests/e2e/roundtrip.py <hermes-python> [workdir]
Exit 0 = identical replay.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent))
sys.path.insert(0, str(REPO / "tests" / "support"))

import scripted_model  # noqa: E402

PKG = REPO.name  # imported as a package so relative imports work


def main() -> int:
    hermes_python = sys.argv[1]
    work = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="ariadne-e2e-"))
    if work.exists():
        shutil.rmtree(work)
    home, project = work / "home", work / "project"
    (home / "plugins").mkdir(parents=True)
    project.mkdir()
    (home / "plugins" / "ariadne").symlink_to(REPO, target_is_directory=True)

    script = [
        {"tool_calls": [{"name": "terminal", "arguments": {"command": f"echo hello-ariadne > {project}/out.txt && cat {project}/out.txt"}}]},
        {"text": "Wrote out.txt; it says hello-ariadne."},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": f"{project}/out.txt"}},
                        {"name": "terminal", "arguments": {"command": "echo second-call"}}]},
        {"text": "The file still says hello-ariadne."},
    ]
    server = scripted_model.serve(0, script, work / "model-log")
    port = server.server_address[1]
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "model": "scripted", "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "none"},
        "plugins": {"enabled": ["ariadne"], "disabled": []},
    }))
    (home / "plugin-data" / "ariadne").mkdir(parents=True)
    (home / "plugin-data" / "ariadne" / "control.json").write_text('{"mode": "record"}')

    os.environ["HERMES_HOME"] = str(home)
    mod = __import__(f"{PKG}.driver", fromlist=["driver"])
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONUNBUFFERED="1")
    replies, errors, sent, tail = mod.drive_session(
        hermes_python, env, str(project),
        ["write hello-ariadne to out.txt and show it", "read it back and check again"], 120)
    server.shutdown()
    print("record replies:", replies)
    if errors:
        print("record errors:", errors, "\n".join(tail[-20:]))
        return 2
    tapes = sorted((home / "plugin-data" / "ariadne" / "cassettes").glob("*.cassette.jsonl"))
    assert tapes, "no cassette was written"
    tape = tapes[-1]
    kinds = [json.loads(line).get("kind") for line in tape.read_text().splitlines()[1:]]
    print("cassette:", tape.name, {k: kinds.count(k) for k in sorted(set(kinds))})

    (home / "plugin-data" / "ariadne" / "control.json").unlink()
    (project / "out.txt").unlink()
    result = mod.play(tape, mod.PlayOptions(hermes_python=hermes_python, turn_timeout=120, keep=True))
    print(__import__(f"{PKG}.report", fromlist=["report"]).render(result))
    side_effect = (project / "out.txt").exists()
    print("replay executed a real tool:", side_effect)
    if side_effect:
        return 3
    return 0 if result.verdict == "identical" else 1


if __name__ == "__main__":
    raise SystemExit(main())
