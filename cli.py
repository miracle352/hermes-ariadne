"""``hermes ariadne ...`` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import cassette as cas
from . import runtime


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="ariadne_command")

    rec = subs.add_parser("record", help="Turn recording on or off for this profile")
    rec.add_argument("state", choices=["on", "off", "status"], nargs="?", default="status")

    subs.add_parser("list", help="List recorded cassettes")

    show = subs.add_parser("show", help="Summarise a cassette")
    show.add_argument("cassette", help="Cassette id, a unique prefix, a path, or `latest`")

    play = subs.add_parser("play", help="Replay a cassette offline and report every difference")
    play.add_argument("cassette", help="Cassette id, a unique prefix, a path, or `latest`")
    play.add_argument("--hermes-python", default=sys.executable,
                      help="Python interpreter of the Hermes install to replay against (default: this one)")
    play.add_argument("--keep", action="store_true", help="Keep the scratch Hermes home for inspection")
    play.add_argument("--json", action="store_true", help="Print the summary as JSON")
    play.add_argument("--timeout", type=float, default=300.0, help="Seconds allowed per turn")

    parser.set_defaults(func=handle)


def resolve(ref: str) -> Path:
    if ref.lower() in ("latest", "last"):
        newest = next(iter(cas.iter_dir(runtime.cassette_dir())), None)  # newest first
        if newest is None:
            raise SystemExit("ariadne: no cassettes yet (turn recording on with `hermes ariadne record on`)")
        return newest
    p = Path(ref).expanduser()
    if p.is_file():
        return p
    matches = [c for c in cas.iter_dir(runtime.cassette_dir()) if c.name.startswith(ref) or ref in c.name]
    if not matches:
        raise SystemExit(f"ariadne: no cassette matches {ref!r} (see `hermes ariadne list`)")
    if len(matches) > 1:
        exact = [m for m in matches if m.name == f"{ref}{cas.SUFFIX}"]
        if len(exact) == 1:
            return exact[0]
        raise SystemExit(f"ariadne: {ref!r} matches {len(matches)} cassettes; use a longer id")
    return matches[0]


def _write_control(data: dict[str, Any] | None) -> None:
    path = runtime.data_dir() / runtime.CONTROL_FILE
    if data is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps(data), encoding="utf-8")


def handle(args: argparse.Namespace) -> int:
    cmd = getattr(args, "ariadne_command", None)
    if cmd == "record":
        return _cmd_record(args.state)
    if cmd == "list":
        return _cmd_list()
    if cmd == "show":
        return _cmd_show(resolve(args.cassette))
    if cmd == "play":
        return _cmd_play(args)
    print("usage: hermes ariadne {record,list,show,play} ...")
    return 2


def _cmd_record(state: str) -> int:
    ctl = runtime.Control()
    current = ctl.get().get("mode")
    if state == "status":
        print(f"recording: {'on' if current == 'record' else 'off'}")
        print(f"cassettes: {runtime.cassette_dir()}")
        return 0
    _write_control({"mode": "record"} if state == "on" else None)
    print(f"recording {state}. New sessions are saved to {runtime.cassette_dir()}" if state == "on" else "recording off")
    return 0


def _cmd_list() -> int:
    rows = list(cas.iter_dir(runtime.cassette_dir()))
    if not rows:
        print("No cassettes yet. Turn recording on with `hermes ariadne record on`.")
        return 0
    for path in rows:
        try:
            tape = cas.load(path)
        except cas.CassetteError as exc:
            print(f"  ! {path.name}: {exc}")
            continue
        first = next((t.get("user_message") for t in tape.turns()), "") or ""
        first = (first if isinstance(first, str) else json.dumps(first)).replace("\n", " ")
        print(f"  {tape.id}  turns={len(tape.turns()):<3} model_calls={len(tape.of('llm')):<3} "
              f"tools={len(tape.of('tool')):<3} {tape.header.get('model','')[:28]:<28} {first[:50]}")
    return 0


def _cmd_show(path: Path) -> int:
    tape = cas.load(path)
    h = tape.header
    print(f"cassette  {tape.id}")
    print(f"recorded  {h.get('created')}  model={h.get('model')}  provider={h.get('provider')}  api_mode={h.get('api_mode')}")
    for e in tape.events:
        k = e.get("kind")
        if k == "turn":
            msg = e.get("user_message")
            print(f"  [{e['seq']:>3}] user      {str(msg)[:90]!r}")
        elif k == "llm":
            msg = (((e.get("response") or {}).get("$ns") or {}).get("choices") or [{}])
            print(f"  [{e['seq']:>3}] model     call #{e.get('api_call_count')}  request={e.get('request_digest')}")
        elif k == "tool":
            print(f"  [{e['seq']:>3}] tool      {e.get('tool_name')}  {json.dumps(e.get('args'))[:70]}")
        else:
            print(f"  [{e['seq']:>3}] {k}")
    return 0


def _cmd_play(args: argparse.Namespace) -> int:
    from . import driver, report
    path = resolve(args.cassette)
    result = driver.play(path, driver.PlayOptions(hermes_python=args.hermes_python, keep=args.keep, turn_timeout=args.timeout))
    if args.json:
        print((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    else:
        print(report.render(result))
    return {"identical": 0, "drift": 1}.get(result.verdict, 2)
