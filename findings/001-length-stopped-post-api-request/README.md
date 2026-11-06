# 001 — A length-stopped model reply never fires `post_api_request`

**Status:** found by Ariadne on 2026-11-06 · reproduces on Hermes v0.23.0 and on `main` @ `6b21d056` · fix attached (`fix.patch`) · not yet filed upstream

## What happens
When a model reply stops because it hit its output limit (`finish_reason: "length"`), Hermes runs
truncation recovery and asks the model to continue. The cut-off attempt is a real, completed (and
billed) provider call, but Hermes never fires `post_api_request` or `api_request_error` for it:

```
pre_api_request   …:api:1                 ← cut-off reply (finish_reason=length)
pre_api_request   …:api:2                 ← continuation
post_api_request  …:api:2  finish=stop
```

`developer-guide/observer-hooks.md` describes `post_api_request` as firing "after a successful provider
response". Every observer that opens a span or a timer on `pre_api_request` (Langfuse, OpenTelemetry-
style collectors, cost and latency plugins) is left with a call that never ends, and never sees the
truncated attempt's usage.

## Why
`agent/turn_response_check.py::check_api_response` handles `finish_reason == "length"` by calling
`recover_from_truncation(...)` and returning (`continue` / `break` / `return`) **before**
`normalize_model_response` (`agent/turn_response_intake.py`), which is the only place
`post_api_request` fires.

## Related, already reported (not this finding)
The same early exit also skips `record_response_usage`, so the cut-off attempt's tokens never reach
session usage, `/usage` or `state.db`: that part is **#126413** / **#125492**, with open fix PRs
**#126414**, **#125505**, **#128746**. None of those PRs fire `post_api_request`, so the hook gap
remains after they merge. Ariadne reproduces the usage part too:

| turn | provider calls made | Hermes records |
|---|---|---|
| normal 2-call turn (control) | 2 | 2 calls · 20 in / 10 out tokens |
| first reply cut off (`length`) | 2 | **1 call · 10 in / 5 out tokens** |

## Reproduce (offline, $0)
```bash
# with Ariadne installed in an editable Hermes checkout's venv
python tests/scenarios/run.py <hermes-python> truncated_length
```
The contract checker reports:
`api_pairs: every pre_api_request ends with post_api_request or api_request_error`.

## Fix
`fix.patch` fires `post_api_request` for the length-stopped attempt (best-effort, inside the same
try/except contract as every observer) before truncation recovery runs. With the patch:

```
pre_api_request   …:api:1
post_api_request  …:api:1  finish=length
pre_api_request   …:api:2
post_api_request  …:api:2  finish=stop
```

Hermes's own tests for this area pass with the patch (349 passed): `test_anthropic_truncation_continuation`,
`test_length_continuation_thinking_exhaustion`, `test_length_continuation_window_headroom`,
`test_truncated_tool_call_boost`, `test_run_agent`, `test_first_chunk_at_hook`,
`test_moa_observability_bridge`, `test_side_question_lease`, `test_stream_serving_provider`,
`test_auxiliary_hooks`.
