# 001: a cut-off reply never fires `post_api_request`

Found by Ariadne's hook-contract check on Nov 6, 2026. Filed as NousResearch/hermes-agent#150324, fix in #150333. Happens on Hermes v0.23.0 and on `main`
(6b21d056). Fix: `fix.patch`, also on branch `fix/length-stopped-post-api-request` of
miracle352/hermes-agent.

## What happens

When a reply stops on `finish_reason: "length"`, Hermes asks the model to continue. The cut-off
call never gets a `post_api_request` or `api_request_error`. Only the continuation does:

```
pre_api_request   ...:api:1               cut-off reply
pre_api_request   ...:api:2               continuation
post_api_request  ...:api:2  finish=stop
```

The docs describe `post_api_request` as firing after a successful provider response. A plugin that
starts a span or a timer on `pre_api_request` (tracing, latency, cost) never sees that call end.

## Why

`check_api_response` in `agent/turn_response_check.py` sends a `length` reply to
`recover_from_truncation`, which returns `break` and arms a restart. The loop restarts before
`normalize_model_response` runs, and that is the only place `post_api_request` fires.

## Already reported, but a different part

The same early return skips usage accounting, so the cut-off call's tokens never reach
`/usage` or `state.db`. That is #126413 and #125492, with open PRs #126414, #125505 and #128746.
None of them fire the hook, so this stays broken after they merge. Ariadne sees that part too:

| turn | calls billed | Hermes records |
|---|---|---|
| normal two-call turn | 2 | 2 calls, 20 in / 10 out |
| first reply cut off | 2 | 1 call, 10 in / 5 out |

## Reproduce

```bash
python tests/scenarios/run.py <hermes-python> truncated_length
```

The contract check prints
`api_pairs: every pre_api_request ends with post_api_request or api_request_error`.

## Fix

Fire `post_api_request` for the cut-off call before truncation recovery runs. With the fix:

```
pre_api_request   ...:api:1
post_api_request  ...:api:1  finish=length
pre_api_request   ...:api:2
post_api_request  ...:api:2  finish=stop
```

`tests/agent/test_length_stopped_post_api_request.py` fails without the fix and passes with it.
The nearby suites pass (322 tests across the truncation, continuation, hook and run-loop files),
and `scripts/check` is clean.
