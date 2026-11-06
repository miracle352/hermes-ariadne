"""Export cassettes and forks as training data.

* ``trajectory``: one cassette -> one entry in Hermes's own trajectory format (ShareGPT
  roles, ``<think>``, ``<tool_call>`` and ``<tool_response>`` blocks), the format
  ``agent/trajectory.py`` writes, so it loads with the same tooling.
* ``pair``: one fork run -> one preference pair. Both sides start from the same prompt
  state (the request at the fork point); one is the recorded reply, the other the live
  model's. The caller says which side is preferred.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import cassette as cas
from . import codec

_SYSTEM_TEMPLATE = (
    "You are a function calling AI model. You are provided with function signatures within <tools> </tools> "
    "XML tags. You may call one or more functions to assist with the user query. If available tools are not "
    "relevant in assisting with user query, just respond in natural conversational language. Don't make "
    "assumptions about what values to plug into functions. After calling & executing the functions, you will "
    "be provided with function results within <tool_response> </tool_response> XML tags. Here are the "
    "available tools:\n<tools>\n{tools}\n</tools>\nFor each function call return a JSON object, with the "
    "following pydantic model json schema for each:\n{{'title': 'FunctionCall', 'type': 'object', "
    "'properties': {{'name': {{'title': 'Name', 'type': 'string'}}, 'arguments': {{'title': 'Arguments', "
    "'type': 'object'}}}}, 'required': ['name', 'arguments']}}\nEach function call should be enclosed within "
    "<tool_call> </tool_call> XML tags.\nExample:\n<tool_call>\n{{'name': <function-name>,'arguments': "
    "<args-dict>}}\n</tool_call>"
)


def _loads(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _tool_defs(tools: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, t in tools.items():
        fn = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
        out.append({"name": fn.get("name", name), "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or fn.get("input_schema") or {}, "required": None})
    return out


def _gpt(content: Any, tool_calls: list[dict[str, Any]], reasoning: Any) -> dict[str, str]:
    think = f"<think>\n{reasoning.strip()}\n</think>\n" if isinstance(reasoning, str) and reasoning.strip() else "<think>\n</think>\n"
    parts = [think + (content if isinstance(content, str) else "")]
    for tc in tool_calls:
        parts.append("<tool_call>\n" + json.dumps({"name": tc["name"], "arguments": tc["arguments"]}, ensure_ascii=False) + "\n</tool_call>")
    return {"from": "gpt", "value": "\n".join(p for p in parts if p)}


def _openai_messages_to_sharegpt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    conv: list[dict[str, str]] = []
    pending_tools: list[str] = []
    last_calls: list[str] = []  # tool names of the parent assistant turn, matched by position

    def flush() -> None:
        if pending_tools:
            conv.append({"from": "tool", "value": "\n".join(pending_tools)})
            pending_tools.clear()

    for m in messages:
        role = m.get("role")
        if role == "tool":
            cid = m.get("tool_call_id") or ""
            name = last_calls[len(pending_tools)] if len(pending_tools) < len(last_calls) else ""
            pending_tools.append("<tool_response>\n" + json.dumps(
                {"tool_call_id": cid, "name": name, "content": _loads(m.get("content"))},
                ensure_ascii=False) + "\n</tool_response>")
            continue
        flush()
        if role == "user":
            conv.append({"from": "human", "value": m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False)})
        elif role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls.append({"name": fn.get("name", ""), "arguments": _loads(fn.get("arguments")) or {}})
            last_calls = [c["name"] for c in calls]
            conv.append(_gpt(m.get("content"), calls, m.get("reasoning_content") or m.get("reasoning")))
    flush()
    return conv


def _anthropic_messages_to_sharegpt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    conv: list[dict[str, str]] = []
    names: dict[str, str] = {}
    for m in messages:
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        if m.get("role") == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            reasoning = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
            calls = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    names[b.get("id", "")] = b.get("name", "")
                    calls.append({"name": b.get("name", ""), "arguments": b.get("input") or {}})
            conv.append(_gpt(text, calls, reasoning))
            continue
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            conv.append({"from": "tool", "value": "\n".join(
                "<tool_response>\n" + json.dumps({"tool_call_id": b.get("tool_use_id", ""), "name": names.get(b.get("tool_use_id", ""), ""),
                                                  "content": _loads(b.get("content") if isinstance(b.get("content"), str) else
                                                                    "".join(x.get("text", "") for x in b.get("content") or [] if isinstance(x, dict)))},
                                                 ensure_ascii=False) + "\n</tool_response>" for b in results)})
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            conv.append({"from": "human", "value": "".join(texts)})
    return conv


def _response_message(encoded: Any, api_mode: str) -> dict[str, Any]:
    """An assistant message (OpenAI or Anthropic shape) from a cassette-encoded response."""
    resp = codec.decode(encoded)
    if api_mode == "anthropic_messages":
        blocks = []
        for b in getattr(resp, "content", None) or []:
            dump = b.model_dump() if hasattr(b, "model_dump") else vars(b)
            blocks.append(dump)
        return {"role": "assistant", "content": blocks}
    msg = resp.choices[0].message
    calls = [{"id": getattr(tc, "id", ""), "type": "function",
              "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
             for tc in getattr(msg, "tool_calls", None) or []]
    return {"role": "assistant", "content": getattr(msg, "content", None), "tool_calls": calls,
            "reasoning_content": getattr(msg, "reasoning_content", None)}


def _to_sharegpt(messages: list[dict[str, Any]], api_mode: str) -> list[dict[str, str]]:
    return (_anthropic_messages_to_sharegpt if api_mode == "anthropic_messages" else _openai_messages_to_sharegpt)(messages)


def trajectory(tape: cas.Cassette) -> dict[str, Any]:
    llm = tape.of("llm")
    if not llm:
        raise ValueError("cassette has no model calls")
    last = llm[-1]
    api_mode = last.get("api_mode") or "chat_completions"
    req = last.get("request") or {}
    messages = list(req.get("messages") or []) + [_response_message(last["response"], api_mode)]
    conv = [{"from": "system", "value": _SYSTEM_TEMPLATE.format(tools=json.dumps(_tool_defs(req.get("tools") or {}), ensure_ascii=False))}]
    conv += _to_sharegpt(messages, api_mode)
    return {"conversations": conv, "timestamp": tape.header.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": last.get("model") or tape.header.get("model"), "completed": True,
            "metadata": {"source": "ariadne", "cassette": tape.id, "api_calls": len(llm)}}


def pair(fork_run: str | Path, prefer: str = "forked") -> dict[str, Any]:
    run = Path(fork_run)
    control = json.loads((run / "control.json").read_text(encoding="utf-8"))
    tape = cas.load(control["cassette"])
    n = int(control["fork_at"])
    events = [json.loads(l) for l in (run / "report.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    live = next((e for e in events if e.get("type") == "fork_response" and int(e.get("n") or 0) == n), None)
    if live is None:
        raise ValueError("this fork run has no live reply at the fork point")
    llm = tape.of("llm")
    if n > len(llm):
        raise ValueError("the recording has no model call at the fork point to compare against")
    recorded_ev = llm[n - 1]
    api_mode = recorded_ev.get("api_mode") or "chat_completions"
    lives = [json.loads(l) for l in (run / "live.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    prompt_req = next((e.get("request") for e in lives if e.get("kind") == "llm" and int(e.get("n") or 0) == n),
                      recorded_ev.get("request")) or {}
    prompt = [{"from": "system", "value": _SYSTEM_TEMPLATE.format(tools=json.dumps(_tool_defs(prompt_req.get("tools") or {}), ensure_ascii=False))}]
    prompt += _to_sharegpt(list(prompt_req.get("messages") or []), api_mode)
    original = _to_sharegpt([_response_message(recorded_ev["response"], api_mode)], api_mode)[0]
    forked = _to_sharegpt([_response_message(live["response"], api_mode)], api_mode)[0]
    chosen, rejected = (forked, original) if prefer == "forked" else (original, forked)
    return {"prompt": prompt, "chosen": chosen, "rejected": rejected,
            "metadata": {"source": "ariadne", "cassette": tape.id, "fork_at": n, "preferred": prefer,
                         "recorded_model": recorded_ev.get("model"), "fork_run": run.name}}
