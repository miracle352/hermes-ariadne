"""Replay cassettes in CI and write a summary (used by action.yml).

Usage: python ci/run_cassettes.py --hermes-python PY --glob 'tests/cassettes/*.cassette.jsonl'
                                  [--plugin DIR] [--fail-on drift|contract|never]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def _load_ariadne():
    """Import Ariadne as a package called `ariadne`, whatever directory the action lives in."""
    root = Path(tempfile.mkdtemp(prefix="ariadne-pkg-"))
    (root / "ariadne").symlink_to(HERE, target_is_directory=True)
    sys.path.insert(0, str(root))
    import ariadne.driver as driver  # noqa: E402
    import ariadne.report as report  # noqa: E402
    return driver, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-python", required=True)
    ap.add_argument("--glob", required=True)
    ap.add_argument("--plugin", action="append", default=[])
    ap.add_argument("--fail-on", choices=["drift", "contract", "never"], default="drift")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="ariadne-ci-home-"))
    os.environ["HERMES_HOME"] = str(home)
    driver, report = _load_ariadne()
    tapes = sorted(glob.glob(args.glob, recursive=True))
    if not tapes:
        print(f"ariadne: no cassettes match {args.glob!r}")
        return 1
    plugins = [p for p in args.plugin if p and (Path(p) / "plugin.yaml").is_file() and Path(p).resolve() != HERE]

    rows, failed = [], False
    for tape in tapes:
        res = driver.play(tape, driver.PlayOptions(hermes_python=args.hermes_python, turn_timeout=args.timeout,
                                                   extra_plugins=plugins))
        violations = [e for e in res.events if e.get("type") == "contract_violation"]
        c = res.count
        print(report.render(res))
        print()
        bad = res.verdict != "identical" if args.fail_on == "drift" else bool(violations) if args.fail_on == "contract" else False
        if args.fail_on == "drift" and violations:
            bad = True
        failed = failed or bad or res.verdict in ("error", "leak")
        rows.append((Path(tape).name, res.verdict, f"{c('llm_match')}/{c('llm_match') + c('llm_drift')}",
                     f"{c('tool_match')}/{c('tool_match') + c('tool_drift') + c('off_script_tool')}", len(violations)))

    summary = ["## Ariadne replays", "", "| cassette | verdict | model calls identical | tool calls identical | contract violations |",
               "|---|---|---|---|---|"]
    summary += [f"| `{n}` | {'✅' if v == 'identical' else '❌'} {v} | {m} | {t} | {cv} |" for n, v, m, t, cv in rows]
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    print("\n".join(summary))
    shutil.rmtree(home, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
