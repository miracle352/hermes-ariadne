"""Unit tests for the pure-code parts of Ariadne (no Hermes needed)."""

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent))
pkg = REPO.name
codec = importlib.import_module(f"{pkg}.codec")
redact = importlib.import_module(f"{pkg}.redact")
normalize = importlib.import_module(f"{pkg}.normalize")
cassette = importlib.import_module(f"{pkg}.cassette")


def test_codec_roundtrip_namespace_tree():
    resp = SimpleNamespace(id="x", choices=[SimpleNamespace(index=0, message=SimpleNamespace(
        role="assistant", content=None, tool_calls=[SimpleNamespace(id="c1", type="function",
        function=SimpleNamespace(name="terminal", arguments='{"command": "ls"}'))]), finish_reason="tool_calls")],
        usage=None, extra=("a", b"\x00\x01"), meta={"$weird": 1})
    enc, lossy = codec.encode_checked(resp)
    assert lossy == []
    back = codec.decode(json.loads(json.dumps(enc)))
    assert back.choices[0].message.tool_calls[0].function.name == "terminal"
    assert back.extra == ("a", b"\x00\x01")
    assert back.meta == {"$weird": 1}
    assert codec.encode(back) == enc


def test_codec_pydantic_roundtrip():
    pydantic = __import__("pydantic")

    class Usage(pydantic.BaseModel):
        prompt_tokens: int
        completion_tokens: int

    globals()["Usage"] = Usage
    Usage.__module__ = __name__
    enc = codec.encode(Usage(prompt_tokens=3, completion_tokens=4))
    back = codec.decode(enc)
    assert back.prompt_tokens == 3


def test_redact_is_stable_and_idempotent():
    text = "key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX and ghp_" + "a" * 36 + " api_key=supersecretvalue1"
    once = redact.scrub_text(text)
    assert "ABCDEFGHIJ" not in once and "supersecretvalue1" not in once and "ghp_aaaa" not in once
    assert redact.scrub_text(once) == once
    assert redact.scrub_text(text) == once


def test_masker_paths_and_volatile_values():
    m = normalize.Masker({r"C:\Users\Ada\AppData\Local\hermes": "$HERMES_HOME", r"C:\Users\Ada": "$CWD"},
                         user_homes=[r"C:\Users\Ada"])
    assert m.text(r"~/AppData/Local/hermes/x") == "$HERMES_HOME/x"
    assert m.text(r"C:\Users\Ada\proj") == r"$CWD\proj"
    assert m.text("at 2026-11-06T19:14:43Z id 123e4567-e89b-12d3-a456-426614174000") == "at <datetime> id <uuid>"


def test_canonical_request_sections_and_diff():
    req = {"model": "m", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
           {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "t", "arguments": '{"a":1}'}}]}],
           "tools": [{"type": "function", "function": {"name": "t", "parameters": {}}}], "temperature": 0.2,
           "extra_headers": {"x": "y"}}
    c = normalize.canonical_request(req)
    assert c["system"] == ["S"] and len(c["messages"]) == 2 and "t" in c["tools"]
    assert c["messages"][1]["tool_calls"][0]["function"]["arguments"] == {"a": 1}
    assert "extra_headers" not in c["params"] and c["params"]["temperature"] == 0.2
    live = json.loads(json.dumps(c)); live["system"][0] = "S2"
    d = normalize.diff(c, live)
    assert d and normalize.section_of(d[0]["path"]) == "system"


def test_cassette_write_load_and_torn_tail(tmp_path):
    p = tmp_path / f"x{cassette.SUFFIX}"
    w = cassette.Writer(p, {"id": "x"})
    w.append("turn", user_message="hi")
    w.append("llm", request={}, response={"$ns": {}})
    with open(p, "a") as fh:
        fh.write('{"seq": 3, "kind": "tool", "trunc')  # crash mid-write
    tape = cassette.load(p)
    assert [e["kind"] for e in tape.events] == ["turn", "llm"]


def test_secret_key_filter_keeps_token_settings():
    util = importlib.import_module(f"{pkg}.util")
    cfg = {"compression": {"threshold_tokens": 12000, "max_tokens": 5}, "api_key": "x", "providers": {"a": {"token": "t", "client_secret": "s", "base_url": "u"}},
           "password": "p", "auth": {"refresh_token": "r", "mode": "oauth"}}
    out = util.strip_secrets(cfg)
    assert out["compression"] == {"threshold_tokens": 12000, "max_tokens": 5}
    assert "api_key" not in out and "password" not in out
    assert out["providers"]["a"] == {"base_url": "u"}
    assert out["auth"] == {"mode": "oauth"}


def test_contract_checker_flags_unpaired_api_request():
    contracts = importlib.import_module(f"{pkg}.contracts")
    seen = []
    c = contracts.ContractChecker(lambda t, d: seen.append((t, d)), mode=lambda: "replay")
    base = {"telemetry_schema_version": "hermes.observer.v1", "turn_id": "T"}
    c.hook("pre_llm_call")(**base)
    c.hook("pre_api_request")(**base, api_request_id="T:api:1")
    c.hook("pre_api_request")(**base, api_request_id="T:api:2")
    c.hook("post_api_request")(**base, api_request_id="T:api:2")
    c.hook("post_tool_call")(**base, tool_call_id="x")  # dispatcher rule: ignored in replay
    c.hook("post_llm_call")(**base)
    c.hook("on_session_end")(**base, completed=True)
    rules = [d["rule"] for t, d in seen if t == "contract_violation"]
    assert rules == ["api_pairs"]


def test_trajectory_matches_tool_results_by_position():
    export = importlib.import_module(f"{pkg}.export")
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "<uuid>", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
                {"id": "<uuid>", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]},
            {"role": "tool", "tool_call_id": "<uuid>", "content": '{"content": "A"}'},
            {"role": "tool", "tool_call_id": "<uuid>", "content": "ok"},
            {"role": "assistant", "content": "done", "reasoning_content": "thought"}]
    conv = export._openai_messages_to_sharegpt(msgs)
    assert [c["from"] for c in conv] == ["human", "gpt", "tool", "gpt"]
    assert conv[1]["value"].count("<tool_call>") == 2
    tool = conv[2]["value"]
    assert tool.index('"name": "read_file"') < tool.index('"name": "terminal"')
    assert conv[3]["value"].startswith("<think>\nthought\n</think>\n") and conv[3]["value"].endswith("done")


def test_skills_section_presence_is_environment():
    split_environment, settle_skills, environment_delta = normalize.split_environment, normalize.settle_skills, normalize.environment_delta
    section = ("\n\n## Skills\nBefore replying, scan the skills below.\n\n<available_skills>\n  - a: b\n"
               "</available_skills>\n\nOnly proceed without loading a skill if genuinely none are relevant to the task.\n\n")
    bare = ["Intro.\n\nOutro."]
    skilled = ["Intro." + section + "Outro."]
    rc, re_ = split_environment(bare)
    lc, le = split_environment(skilled)
    rc, lc = settle_skills(rc, re_, lc, le)
    assert rc == lc
    assert environment_delta(re_, le)["skills_section"] == "added"
    # with the section on both sides, a wording change is still a difference
    lc2, le2 = split_environment([skilled[0].replace("scan the skills", "read the skills")])
    a, b = settle_skills(*split_environment(skilled), lc2, le2)
    assert a != b
