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


def test_decode_never_imports_modules_a_cassette_names():
    sys.modules.pop("this", None)                            # stdlib module with an import side effect
    out = codec.decode({"$pyd": "this:s", "data": {"a": 1}})
    assert "this" not in sys.modules                         # never imported
    assert out.a == 1                                        # decoded as an attribute tree
    usage = codec.decode({"$pyd": "openai.types.completion_usage:CompletionUsage",
                          "data": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
    assert usage.total_tokens == 3


def test_replay_config_drops_settings_that_run_things():
    util = importlib.import_module(f"{pkg}.util")
    cfg = {"hooks": {"pre_tool_call": [{"command": "curl evil"}]}, "hooks_auto_accept": True,
           "mcp_servers": {"x": {"command": "sh"}}, "terminal": {"backend": "ssh"},
           "auxiliary": {"compression": {"base_url": "http://127.0.0.1:1"}},
           "skills": {"inline_shell": True, "auto_load": ["a"]}, "compression": {"threshold": 0.5},
           "toolsets": ["hermes-cli"]}
    kept, dropped = util.replay_settings(cfg)
    assert set(kept) == {"skills", "compression", "toolsets"} and kept["skills"] == {"auto_load": ["a"]}
    assert {"hooks", "hooks_auto_accept", "mcp_servers", "terminal", "auxiliary", "skills.*"} <= set(dropped)
    assert util.replay_settings(cfg, trust=True)[0] == cfg


def test_upgrade_picks_the_release_tag_and_explains_tool_changes():
    up = importlib.import_module(f"{pkg}.upgrade")
    tags = "\n".join(f"abc\trefs/tags/{t}" for t in ["v0.22.9", "v0.23.1", "v2026.9.24", "v0.23.2-rc.1", "v0.23.1+canary.1"])
    assert up.pick_release(tags) == "v0.23.1"
    changes = [{"kind": "model_request", "sections": ["tools"], "diffs": [
        {"path": ".tools.delegate_task.description", "change": "changed"},
        {"path": ".tools.patch.parameters.properties.mode", "change": "added"},
        {"path": ".tools.web_fetch", "change": "added"}]}]
    out = up.explain(changes, [], [])
    assert "Gives the model new tools: web_fetch." in out
    assert "Changes the arguments of: patch." in out
    assert "Rewords how it describes: delegate_task." in out


def test_replay_reports_plugin_crashes_from_hermes_logs(tmp_path):
    driver = importlib.import_module(f"{pkg}.driver")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "agent.log").write_text(
        "2026 WARNING hermes_cli.plugins: Hook 'pre_llm_call' callback boom raised: crashy says no "
        "(hook provides: model, session_id; identical failures are logged at DEBUG from now on)\n"
        "2026 WARNING hermes_cli.plugins_loader: Failed to load plugin 'crashy': No module named 'x'\n", encoding="utf-8")
    found = driver.plugin_problems(tmp_path)
    assert [p["kind"] for p in found] == ["plugin_hook_raised", "plugin_load_failed"]
    assert found[0]["groups"] == ["pre_llm_call", "boom", "crashy says no"]


def test_another_hermes_never_inherits_our_import_path(monkeypatch):
    util = importlib.import_module(f"{pkg}.util")
    monkeypatch.setenv("PYTHONPATH", "/opt/current-hermes")
    other = util.env_for("/somewhere/else/venv/bin/python")
    assert "PYTHONPATH" not in other and other["PYTHONSAFEPATH"] == "1"
    assert util.env_for(sys.executable)["PYTHONPATH"] == "/opt/current-hermes"


def test_compare_ignores_token_counts_in_command_output():
    compare = importlib.import_module(f"{pkg}.compare")
    a = '<session.compress: {"before_tokens": 15172, "after_tokens": 13708}>'
    b = '<session.compress: {"before_tokens": 15274, "after_tokens": 13810}>'
    assert compare._reply_shape(a) == compare._reply_shape(b)
    assert compare._reply_shape("Noted 7.") != compare._reply_shape("Noted 8.")


def test_upgrade_titles_skip_system_notes_and_empty_recordings(tmp_path):
    up = importlib.import_module(f"{pkg}.upgrade")

    class Tape:
        id = "20261107-x"
        def __init__(self, msgs): self._m = msgs
        def turns(self): return [{"user_message": m} for m in self._m]
    assert up.session_title(Tape(["[System note: first time opening the app. Run [onboarding].]\n\nhi there"])) == "hi there"
    assert up.session_title(Tape(["[System note: first time.]"])) == "First-run onboarding"
    assert up.session_title(Tape([])) == "20261107-x"


def test_upgrade_can_delete_read_only_git_files(tmp_path):
    import os, stat
    up = importlib.import_module(f"{pkg}.upgrade")
    f = tmp_path / "old" / ".git" / "objects" / "pack" / "x.pack"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    os.chmod(f.parent, stat.S_IREAD | stat.S_IEXEC)  # also blocks deletion on Linux, like Windows read-only
    up._rmtree(tmp_path / "old")
    assert not (tmp_path / "old").exists()
