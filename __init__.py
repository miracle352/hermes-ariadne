"""Ariadne — record any Hermes session, replay it with no model, no network and no tokens.

Registration only; the engines live in runtime.py, the CLI in cli.py.
"""

from __future__ import annotations

import logging
from typing import Any

from . import runtime

logger = logging.getLogger("ariadne")

_recorder: runtime.Recorder | None = None
_replayer: runtime.Replayer | None = None
_replayer_key: tuple | None = None


def _mode() -> dict[str, Any]:
    return runtime.CONTROL.get()


def _active_replayer(control: dict[str, Any]) -> runtime.Replayer | None:
    global _replayer, _replayer_key
    key = (control.get("cassette"), control.get("report"), control.get("run_id"))
    if _replayer is None or _replayer_key != key:
        try:
            _replayer = runtime.Replayer(control)
            _replayer_key = key
        except Exception:
            logger.exception("ariadne: could not load cassette %s", control.get("cassette"))
            _replayer = None
    return _replayer


def _recorder_get() -> runtime.Recorder:
    global _recorder
    if _recorder is None:
        _recorder = runtime.Recorder()
    return _recorder


def _replaying(control: dict[str, Any]) -> bool:
    return control.get("mode") in ("replay", "fork") and bool(control.get("cassette"))


# ---------------------------------------------------------------- middleware

def _llm_execution(**kw: Any) -> Any:
    control = _mode()
    if control.get("mode") == "warmup":
        return runtime.synthetic_response(kw.get("api_mode") or "", "ok")
    if _replaying(control):
        rp = _active_replayer(control)
        if rp is not None:
            return rp.llm_execution(**kw)
        # A replay that cannot load its cassette must not silently go live.
        return runtime.synthetic_response(kw.get("api_mode") or "", "[ariadne] replay could not load its cassette; no model was called.")
    if control.get("mode") == "record":
        return _recorder_get().llm_execution(**kw)
    return kw["next_call"](kw["request"])


def _tool_request(**kw: Any) -> Any:
    control = _mode()
    if control.get("mode") == "warmup":
        return {"args": dict(runtime.NEUTRAL_ARGS), "source": "ariadne"}
    if _replaying(control):
        rp = _active_replayer(control)
        return rp.tool_request(**kw) if rp is not None else {"args": dict(runtime.NEUTRAL_ARGS), "source": "ariadne"}
    return None


def _tool_execution(**kw: Any) -> Any:
    control = _mode()
    if control.get("mode") == "warmup":
        return '{"error": "ariadne warm-up: nothing is executed"}'
    if _replaying(control):
        rp = _active_replayer(control)
        if rp is not None:
            return rp.tool_execution(**kw)
        return '{"error": "ariadne: replay could not load its cassette; nothing was executed"}'
    if control.get("mode") == "record":
        return _recorder_get().tool_execution(**kw)
    return kw["next_call"](kw["args"])


# ---------------------------------------------------------------- hooks

def _when_recording(method: str):
    def hook(**kw: Any) -> None:
        if _mode().get("mode") == "record":
            getattr(_recorder_get(), method)(**kw)
        return None
    hook.__name__ = f"ariadne_{method}"
    return hook


def _approval(phase: str):
    def hook(**kw: Any) -> None:
        if _mode().get("mode") == "record":
            _recorder_get().on_approval(phase)(**kw)
        return None
    hook.__name__ = f"ariadne_approval_{phase}"
    return hook


def _subagent_start(**kw: Any) -> None:
    control = _mode()
    if control.get("mode") == "record":
        _recorder_get().on_subagent_start(**kw)
    elif _replaying(control):
        rp = _active_replayer(control)
        if rp is not None:
            rp.on_subagent_start(**kw)
    return None


def _contract_emit(type_: str, data: dict[str, Any]) -> None:
    control = _mode()
    if _replaying(control):
        rp = _active_replayer(control)
        if rp is not None:
            rp.emit(type_, **data)
        return
    if control.get("mode") == "record" and type_ == "contract_violation":
        import json as _json
        import time as _time
        with open(runtime.data_dir() / "contracts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"t": round(_time.time(), 3), **data}, default=str) + "\n")


_checker = None


def _contract_hook(name: str):
    def cb(**kw: Any) -> None:
        if _mode().get("mode") in ("record", "replay", "fork") and _checker is not None:
            _checker.hook(name)(**kw)
        return None
    cb.__name__ = f"ariadne_contract_{name}"
    return cb


def _contracts_finish(**kw: Any) -> None:
    if _mode().get("mode") in ("record", "replay", "fork") and _checker is not None:
        _checker.finish()
    return None


def register(ctx: Any) -> None:
    global _checker
    from . import contracts
    _checker = contracts.ContractChecker(_contract_emit, mode=lambda: str(_mode().get("mode")))
    try:
        runtime.CONTROL.setting_record = bool(ctx.get_config("record", default=False))
    except Exception:
        runtime.CONTROL.setting_record = False

    ctx.register_middleware("llm_execution", _llm_execution)
    ctx.register_middleware("tool_request", _tool_request)
    ctx.register_middleware("tool_execution", _tool_execution)

    ctx.register_hook("pre_llm_call", _when_recording("on_pre_llm_call"))
    ctx.register_hook("post_auxiliary_call", _when_recording("on_post_auxiliary_call"))
    ctx.register_hook("post_compaction", _when_recording("on_post_compaction"))
    ctx.register_hook("subagent_start", _subagent_start)
    ctx.register_hook("pre_approval_request", _approval("request"))
    ctx.register_hook("post_approval_response", _approval("response"))

    # Hook-contract checks (observation only; every callback returns None).
    for hook_name in contracts.HOOKS:
        ctx.register_hook(hook_name, _contract_hook(hook_name))
    ctx.register_hook("on_session_finalize", _contracts_finish)

    from . import cli
    ctx.register_cli_command(
        name="ariadne",
        help="Record Hermes sessions and replay them offline",
        setup_fn=cli.setup_argparse,
        handler_fn=cli.handle,
    )
