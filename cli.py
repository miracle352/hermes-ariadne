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
    play.add_argument("--provider", default=None, help="Provider to replay as (default: the recorded one)")
    play.add_argument("--surface", choices=["desktop", "tui"], default=None, help="Surface to replay as (default: recorded)")
    play.add_argument("--with-plugin", action="append", default=[], metavar="DIR",
                      help="Also load this plugin directory in the replay (repeatable), e.g. the plugin under test")
    play.add_argument("--trust-config", action="store_true",
                      help="Replay every recorded setting, including hooks, MCP servers and terminal (only for cassettes you made)")
    play.add_argument("--no-clone", action="store_true",
                      help="Do not copy this profile's skills, memories, SOUL.md and config (never credentials) into the replay")

    cmp = subs.add_parser("compare", help="Replay one cassette on two Hermes installs and show what Hermes changed")
    cmp.add_argument("cassette", help="Cassette id, prefix, path, or `latest`")
    cmp.add_argument("--a", required=True, help="Python interpreter of Hermes install A (the baseline)")
    cmp.add_argument("--b", default=sys.executable, help="Python interpreter of Hermes install B (default: this one)")
    cmp.add_argument("--json", action="store_true")
    cmp.add_argument("--timeout", type=float, default=300.0)

    bis = subs.add_parser("bisect", help="Find the Hermes commit that changed how a cassette replays")
    bis.add_argument("cassette", help="Cassette id, prefix, path, or `latest`")
    bis.add_argument("--repo", required=True, help="Hermes source checkout (installed editable into --python)")
    bis.add_argument("--python", required=True, help="Python interpreter whose Hermes is the editable --repo install")
    bis.add_argument("--good", required=True, help="Commit/tag that replays as expected")
    bis.add_argument("--bad", default="HEAD", help="Commit/tag that replays differently (default HEAD)")
    bis.add_argument("--json", action="store_true")
    bis.add_argument("--timeout", type=float, default=300.0)

    fk = subs.add_parser("fork", help="Replay up to a point, then let the live model take over")
    fk.add_argument("cassette", help="Cassette id, prefix, path, or `latest`")
    where = fk.add_mutually_exclusive_group(required=True)
    where.add_argument("--at", type=int, help="Model call number where the live model takes over")
    where.add_argument("--at-turn", type=int, help="User turn number to replace (use with --prompt)")
    fk.add_argument("--prompt", help="A different user message at the fork point")
    fk.add_argument("--continue", dest="continue_turns", action="store_true", help="Also send the recorded turns after the fork point")
    fk.add_argument("--live-tools", action="store_true", help="Let tools run for real after the fork point (approvals apply)")
    fk.add_argument("--json", action="store_true")
    fk.add_argument("--timeout", type=float, default=300.0)

    ex = subs.add_parser("export", help="Export a cassette as a trajectory, or a fork as a preference pair")
    ex.add_argument("cassette", nargs="?", help="Cassette id, prefix, path, or `latest` (trajectory export)")
    ex.add_argument("--fork", help="A fork run directory (preference-pair export)")
    ex.add_argument("--prefer", choices=["forked", "original"], default="forked", help="Which side of a pair is preferred")
    ex.add_argument("--out", help="Append the JSON line to this file (default: print it)")

    up = subs.add_parser("upgrade-check", help="Replay your recordings on a newer Hermes before you update")
    up.add_argument("--to", default="release",
                    help="release (default: the newest Hermes release), main, a git ref, or a Hermes python/venv path")
    up.add_argument("--cassette", action="append", default=None, help="Check only this cassette (repeatable)")
    up.add_argument("--limit", type=int, default=10, help="How many of your newest recordings to check (default 10)")
    up.add_argument("--json", action="store_true")

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
    if cmd == "compare":
        return _cmd_compare(args)
    if cmd == "bisect":
        return _cmd_bisect(args)
    if cmd == "fork":
        return _cmd_fork(args)
    if cmd == "export":
        return _cmd_export(args)
    if cmd == "upgrade-check":
        return _cmd_upgrade(args)
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
    result = driver.play(path, driver.PlayOptions(
        hermes_python=args.hermes_python, keep=args.keep, turn_timeout=args.timeout,
        provider=args.provider, surface=args.surface, extra_plugins=args.with_plugin,
        clone_home=None if args.no_clone else runtime.hermes_home(), trust_config=args.trust_config))
    if args.json:
        print((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    else:
        print(report.render(result))
    return {"identical": 0, "drift": 1}.get(result.verdict, 2)


def _cmd_compare(args: argparse.Namespace) -> int:
    from . import compare, driver
    path = resolve(args.cassette)
    c = compare.compare(path, args.a, args.b, label_a="A", label_b="B",
                        opts=driver.PlayOptions(turn_timeout=args.timeout, clone_home=runtime.hermes_home()))
    print(json.dumps(c.to_json(), indent=2, ensure_ascii=False) if args.json else compare.render(c))
    return 0 if c.verdict == "same" else 1


def _cmd_bisect(args: argparse.Namespace) -> int:
    from . import bisector as bisect, driver
    path = resolve(args.cassette)
    try:
        r = bisect.bisect(path, args.repo, args.python, args.good, args.bad,
                          opts=driver.PlayOptions(turn_timeout=args.timeout, clone_home=runtime.hermes_home()),
                          log=(lambda _m: None) if args.json else print)
    except bisect.BisectError as exc:
        print(f"ariadne bisect: {exc}")
        return 2
    print(json.dumps(r.to_json(), indent=2, ensure_ascii=False) if args.json else bisect.render(r))
    return 0 if r.first_bad is None else 1


def _cmd_fork(args: argparse.Namespace) -> int:
    from . import fork
    try:
        r = fork.fork(resolve(args.cassette), at_call=args.at, at_turn=args.at_turn, prompt=args.prompt,
                      live_tools=args.live_tools, continue_turns=args.continue_turns, turn_timeout=args.timeout)
    except ValueError as exc:
        print(f"ariadne fork: {exc}")
        return 2
    print(json.dumps(r.to_json(), indent=2, ensure_ascii=False) if args.json else fork.render(r))
    return 0 if r.prefix_identical and not r.errors else 1


def _cmd_upgrade(args: argparse.Namespace) -> int:
    from . import upgrade
    tapes = [resolve(c) for c in args.cassette] if args.cassette else None

    def say(ev: dict[str, Any]) -> None:
        if args.json:
            return
        if ev.get("note"):
            print(f"· {ev['note']}", flush=True)
        elif ev.get("stage") == "replay" and "row" not in ev:
            print(f"· replaying {ev['done'] + 1}/{ev['total']}  {ev.get('cassette', '')}", flush=True)

    result = upgrade.run(args.to, tapes, args.limit, say)
    print(json.dumps(result, indent=2, default=str) if args.json else "\n" + upgrade.render(result))
    return {"safe": 0, "changed": 1, "broken": 2}[result["verdict"]]


def _cmd_export(args: argparse.Namespace) -> int:
    from . import export
    try:
        if args.fork:
            entry = export.pair(args.fork, prefer=args.prefer)
        elif args.cassette:
            entry = export.trajectory(cas.load(resolve(args.cassette)))
        else:
            print("ariadne export: give a cassette, or --fork RUN_DIR")
            return 2
    except (ValueError, OSError, KeyError) as exc:
        print(f"ariadne export: {exc}")
        return 2
    line = json.dumps(entry, ensure_ascii=False)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"wrote 1 entry to {args.out}")
    else:
        print(line)
    return 0
