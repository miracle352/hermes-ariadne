"""The cassette file format: one JSON object per line.

Line 1 is the header (``"ariadne": "cassette/1"``); every later line is an event:

    turn        a user turn starts                {user_message, is_first_turn}
    llm         one main-loop provider call       {api_call_count, request, request_digest, response, lossy}
    tool        one tool execution                {tool_name, tool_call_id, args, result}
    aux         one auxiliary call (title, ...)   {aux_task, response_text}
    compaction  context compaction committed      {old_session_id, kind}
    approval    an approval prompt and answer     {phase, ...}
    note        anything else worth keeping

Everything is scrubbed before it is written; a cassette is safe to read but must
still be reviewed by its owner before sharing (tool output can be private).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

FORMAT = "cassette/1"
SUFFIX = ".cassette.jsonl"


def new_id(session_id: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    tail = "".join(ch for ch in (session_id or "") if ch.isalnum())[-8:] or "nosess"
    return f"{stamp}-{tail}"


class Writer:
    """Append-only, thread-safe, flushed per event (a crash loses at most one event)."""

    def __init__(self, path: Path, header: dict[str, Any]):
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ariadne": FORMAT, **header}, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def append(self, kind: str, **event: Any) -> int:
        with self.lock:
            self.seq += 1
            record = {"seq": self.seq, "kind": kind, "t": round(time.time(), 3), **event}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            return self.seq


@dataclass
class Cassette:
    path: Path
    header: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.header.get("id") or self.path.name.removesuffix(SUFFIX)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("kind") == kind]

    def turns(self) -> list[dict[str, Any]]:
        return self.of("turn")


class CassetteError(Exception):
    pass


def load(path: str | Path) -> Cassette:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise CassetteError(f"{path}: empty file")
    header = json.loads(lines[0])
    if header.get("ariadne") != FORMAT:
        raise CassetteError(f"{path}: not an Ariadne {FORMAT} file")
    events = []
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            # A torn last line from a crash is tolerated; anything earlier is corruption.
            if n == len(lines):
                break
            raise CassetteError(f"{path}:{n}: corrupt event line")
    return Cassette(path=path, header=header, events=events)


def iter_dir(directory: Path) -> Iterator[Path]:
    if directory.is_dir():
        yield from sorted(directory.glob(f"*{SUFFIX}"), reverse=True)


def write_cassette(path: Path, header: dict[str, Any], events: list[dict[str, Any]]) -> Path:
    """Write a whole cassette (used by trim/fork/export)."""
    w = Writer(path, {k: v for k, v in header.items() if k != "ariadne"})
    for e in events:
        e = dict(e)
        e.pop("seq", None)
        kind = e.pop("kind")
        e.pop("t", None)
        w.append(kind, **e)
    return path
