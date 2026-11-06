# Ariadne

Record a Hermes session once. Replay it later with no model, no network and no tokens, and see
exactly what Hermes does differently.

Ariadne sits on Hermes's public middleware. While recording, it saves every model call, tool call
and user turn to a cassette. During a replay it answers the model calls and tool calls from that
cassette, so nothing runs and nothing is billed, while the rest of Hermes (prompt building,
tool-call parsing, compaction, hooks, subagents) runs for real. Any difference from the recording
is a change in Hermes.

```
hermes ariadne record on          # start saving new sessions
hermes ariadne play latest        # replay the last one offline
```

## What you can do with it

**Check an upgrade before you trust it.** Replay your own sessions on a new Hermes build. Same
requests means nothing changed for you.

**Compare two installs.** `compare` replays one cassette on two Hermes installs side by side, so
anything that differs between the machines cancels out.

```
hermes ariadne compare latest --a ~/hermes-old/bin/python --b ~/hermes-new/bin/python
```

**Find the commit that changed something.** `bisect` replays a cassette across the commits of a
Hermes checkout and names the first one that behaves differently. Searching the 358 commits
between v0.23.0 and main took 10 replays and about a minute.

```
hermes ariadne bisect latest --repo ~/src/hermes-agent --python ~/src/hermes-agent/.venv/bin/python --good v0.23.0
```

**Fork a session.** Replay up to model call N, then let a live model take over from exactly that
state. Tools stay frozen by default: a call that matches the recording gets the recorded result,
anything new is refused without running.

```
hermes ariadne fork latest --at 3
hermes ariadne fork latest --at-turn 2 --prompt "do it without the terminal"
```

**Catch broken hook promises.** On every recording and replay Ariadne checks the promises in
Hermes's hook docs (every `pre_api_request` gets a `post_api_request`, `pre_llm_call` once per
turn, subagents start and stop in pairs, and so on). It found one that Hermes breaks:
[findings/001](findings/001-length-stopped-post-api-request/) (NousResearch/hermes-agent#150324).

## Commands

| command | what it does |
|---|---|
| `record on \| off \| status` | turn recording on or off for this profile |
| `list` | list cassettes |
| `show <id>` | print a cassette's turns, model calls and tool calls |
| `play <id>` | replay offline and report every difference |
| `compare <id> --a PY --b PY` | replay on two installs and diff them |
| `bisect <id> --repo DIR --python PY --good REF [--bad REF]` | find the first commit that changed the replay |
| `fork <id> --at N \| --at-turn K [--prompt TEXT] [--live-tools]` | live model from a recorded point |

`<id>` can be a cassette id, a unique prefix, a path, or `latest`.

## What a replay compares

Both sides are scrubbed of secrets and have known-volatile values masked (dates, ids, durations,
paths). Some differences describe the machine rather than Hermes and are reported on their own
line, never counted as drift: the skills list, the toolchain and host lines, the conversation date
and the first-message note. A replay runs on a local stand-in for the recorded provider (signed-in
providers need credentials a replay never reads), so the provider's request extras are compared
separately too.

## Safety and what it touches

- **Off by default.** Nothing is recorded until you run `hermes ariadne record on` or set the
  `record` setting.
- **Local only.** Cassettes live in `<hermes home>/plugin-data/ariadne/`. Ariadne sends nothing
  anywhere. No telemetry.
- **Secrets.** API keys, tokens, private keys and similar are replaced with a hash before a
  cassette is written. Tool output can still contain private file contents, so read a cassette
  before you share it.
- **Replays run nothing.** Tools get placeholder arguments and their results come from the
  cassette; the replay driver answers every approval prompt with deny; outbound proxies point at a
  closed local port, and model calls go to a local stand-in that reports any call that reaches it.
- **Child processes.** `play`, `compare`, `bisect` and `fork` start Hermes
  (`python -m tui_gateway.entry`) as a child process and talk to it over its JSON-RPC protocol.
- **Reads outside its own data.** `play` copies this profile's `config.yaml` (with credential-
  looking keys removed), `SOUL.md`, `memories/` and `skills/` into a throwaway home so the replay
  sees the same context. It never reads `.env`, `auth.json` or any other credential store.
- **`fork` makes real model calls** with your configured provider, inside your own Hermes home, and
  the forked session shows up in your history. With `--live-tools`, tools after the fork point run
  for real, with Hermes's normal approvals.
- **`bisect` checks out commits** in the Hermes checkout you point it at. It refuses to start on a
  dirty tree and puts the original commit back when it finishes.

## Limits

- Streaming is not replayed token by token; a replay hands Hermes the assembled response.
- Hooks that fire inside the real tool dispatcher (`pre_tool_call`, terminal output transforms)
  don't fire during a replay, because tools never run.
- Recordings made in one surface replay in that surface (Desktop or TUI). `hermes -z` one-shot
  recordings replay through the TUI protocol and will show surface differences.
- Chat Completions and Anthropic Messages are covered. Other wire formats are recorded and
  replayed, but untested.

## Tests

```
python -m pytest -c tests/pytest.ini --rootdir tests tests/unit
python tests/e2e/roundtrip.py <hermes-python>
python tests/e2e/fork_check.py <hermes-python>
python tests/scenarios/run.py <hermes-python> [<other-hermes-python>]
```

The scenarios use a scripted model server (plain code, `tests/support/scripted_model.py`), so the
whole suite runs offline for free: malformed tool arguments, unknown tools, 300 KB tool output,
empty, cut-off and refused replies, reasoning carriers, parallel tools, memory, write and patch,
async subagents, compaction, deferred tools and the Anthropic format.

Requires Hermes 0.23.0 or newer. MIT licensed.
