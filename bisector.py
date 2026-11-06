"""``git bisect`` for Hermes, driven by a cassette.

Given a Hermes source checkout installed in editable mode, a known-good and a known-bad
commit, Ariadne replays the cassette at each midpoint and compares what Hermes built with
what the good commit built (differential replay). The first commit whose replay differs is
named, with the diff that changed.

Safety: the checkout's working tree must be clean; Ariadne restores the original HEAD at
the end, even on error. Nothing is pushed or committed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import compare, driver


class BisectError(Exception):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise BisectError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


@dataclass
class Step:
    commit: str
    subject: str
    verdict: str          # "same" | "changed" | "error"
    changes: int = 0


@dataclass
class BisectResult:
    cassette: str
    good: str
    bad: str
    first_bad: str | None
    subject: str = ""
    steps: list[Step] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    candidates: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"cassette": self.cassette, "good": self.good, "bad": self.bad, "first_bad": self.first_bad,
                "subject": self.subject, "candidates": self.candidates,
                "steps": [s.__dict__ for s in self.steps], "changes": self.changes}


def _replay_live(cassette: Path, python: str, opts: driver.PlayOptions) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    o = driver.PlayOptions(**{**opts.__dict__, "hermes_python": python})
    res = driver.play(cassette, o)
    return compare._load_live(res), res.errors, res.replies


def bisect(cassette: str | Path, repo: str | Path, python: str, good: str, bad: str,
           opts: driver.PlayOptions | None = None, log: Callable[[str], None] = print) -> BisectResult:
    repo = Path(repo).resolve()
    cassette = Path(cassette).resolve()
    opts = opts or driver.PlayOptions()
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise BisectError(f"{repo} has uncommitted changes; commit or stash them first")
    original = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if original == "HEAD":
        original = _git(repo, "rev-parse", "HEAD")
    good_sha, bad_sha = _git(repo, "rev-parse", good), _git(repo, "rev-parse", bad)
    commits = _git(repo, "rev-list", "--first-parent", "--reverse", f"{good_sha}..{bad_sha}").split()
    if not commits:
        raise BisectError(f"no commits between {good} and {bad}")
    result = BisectResult(str(cassette), good_sha, bad_sha, None, candidates=len(commits))

    def at(sha: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        _git(repo, "checkout", "-q", "--detach", sha)
        return _replay_live(cassette, python, opts)

    try:
        log(f"baseline: replaying on good {good_sha[:10]}")
        base_live, base_err, base_replies = at(good_sha)
        if base_err:
            raise BisectError(f"replay failed on the good commit: {base_err[:2]}")

        cache: dict[str, list[dict[str, Any]]] = {}

        def changed(sha: str) -> list[dict[str, Any]]:
            if sha not in cache:
                live, err, replies = at(sha)
                diffs = compare.diff_live(base_live, live)
                if replies != base_replies:
                    diffs.append({"kind": "final_replies", "a": base_replies, "b": replies})
                if err:
                    diffs.append({"kind": "replay_error", "errors": err[:2]})
                cache[sha] = diffs
                subject = _git(repo, "log", "-1", "--format=%s", sha)
                result.steps.append(Step(sha, subject, "changed" if diffs else "same", len(diffs)))
                log(f"  {sha[:10]}  {'CHANGED' if diffs else 'same   '}  {subject[:70]}")
            return cache[sha]

        if not changed(bad_sha):
            log("the bad commit replays the same as the good one: nothing to bisect")
            return result
        lo, hi = 0, len(commits) - 1          # invariant: commits[hi] changed; everything before lo is same
        while lo < hi:
            mid = (lo + hi) // 2
            if changed(commits[mid]):
                hi = mid
            else:
                lo = mid + 1
        first = commits[hi]
        result.first_bad = first
        result.subject = _git(repo, "log", "-1", "--format=%s", first)
        result.changes = cache.get(first) or changed(first)
        return result
    finally:
        _git(repo, "checkout", "-q", original, check=False)


def render(r: BisectResult) -> str:
    lines = [f"Ariadne bisect  ·  {Path(r.cassette).name}",
             f"range     {r.good[:10]}..{r.bad[:10]}  ({r.candidates} commits, {len(r.steps)} replays)"]
    if not r.first_bad:
        lines.append("result    no change between good and bad")
        return "\n".join(lines)
    lines.append(f"first changed commit  {r.first_bad[:12]}  {r.subject}")
    for ch in r.changes[:6]:
        lines.append("  " + json.dumps(ch, ensure_ascii=False)[:700])
    return "\n".join(lines)
