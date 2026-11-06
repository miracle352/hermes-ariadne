"""Offline check of `fork` with two scripted models.

Model A records: write a.txt -> read a.txt -> answer.
Fork at call 2 with model B as the live model: B reads a.txt (identical to the recording, so
it must get the recorded result), then tries to delete it (new, so it must be refused without
running), then answers differently. Call 1 must be served from the cassette byte-identically.

Usage: python tests/e2e/fork_check.py <hermes-python>
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

PKG = REPO.name


def main() -> int:
    py = sys.argv[1]
    work = Path(tempfile.mkdtemp(prefix="ariadne-fork-"))
    home, project = work / "home", work / "project"
    (home / "plugins").mkdir(parents=True)
    project.mkdir()
    (home / "plugins" / "ariadne").symlink_to(REPO, target_is_directory=True)
    target = project / "a.txt"

    def config(port: int) -> None:
        (home / "config.yaml").write_text(json.dumps({
            "model": {"provider": "custom", "model": "scripted", "base_url": f"http://127.0.0.1:{port}/v1", "api_key": "none"},
            "plugins": {"enabled": ["ariadne"], "disabled": []}}))

    a = scripted_model.serve(0, [
        {"tool_calls": [{"name": "write_file", "arguments": {"path": str(target), "content": "alpha\n"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": str(target)}}]},
        {"text": "a.txt says alpha."},
    ], work / "log-a")
    config(a.server_address[1])
    (home / "plugin-data" / "ariadne").mkdir(parents=True)
    (home / "plugin-data" / "ariadne" / "control.json").write_text('{"mode": "record"}')
    os.environ["HERMES_HOME"] = str(home)
    driver = __import__(f"{PKG}.driver", fromlist=["x"])
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    replies, errors, _, _ = driver.drive_session(py, env, str(project), ["write alpha to a.txt and read it"], 120)
    a.shutdown()
    (home / "plugin-data" / "ariadne" / "control.json").unlink()
    print("recorded:", replies, errors)
    tape = sorted((home / "plugin-data" / "ariadne" / "cassettes").glob("*.cassette.jsonl"))[-1]

    target.write_text("ALTERED ON DISK\n")  # the live world differs; a frozen fork must not see it
    b = scripted_model.serve(0, [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": str(target)}}]},
        {"tool_calls": [{"name": "terminal", "arguments": {"command": f"rm {target}"}}]},
        {"text": "Forked answer: a.txt says alpha, and I was not allowed to delete it."},
    ], work / "log-b")
    config(b.server_address[1])
    fork = __import__(f"{PKG}.fork", fromlist=["x"])
    r = fork.fork(tape, at_call=2, hermes_python=py, home=str(home), turn_timeout=120)
    b.shutdown()
    print(fork.render(r))
    d = r.to_json()
    ok = (r.prefix_identical and r.prefix_calls == 1 and r.live_calls >= 1
          and d["frozen_tools"] == 1 and d["refused_tools"] == 1 and target.exists()
          and target.read_text() == "ALTERED ON DISK\n")
    print("deleted file survived:", target.exists())
    print("RESULT:", "PASS" if ok else "FAIL")
    if ok:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print("kept for inspection:", work)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
