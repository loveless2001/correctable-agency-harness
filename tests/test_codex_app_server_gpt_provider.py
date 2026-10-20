"""GPT through `codex app-server` on the ChatGPT plan: one ephemeral thread per call, harness tools as dynamic tools,
the turn interrupted at the model's first call. CPU tests against a fake app-server (thread parameters, isolation,
call / text / Codex-builtin replies, failures as infrastructure errors with restart, usage for the token guard,
concurrency on one server, runner wiring), plus the mock-Responses request audit with the real codex binary."""
import argparse
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from correctable_agency.models import codex_app_server_provider as prov_mod
from correctable_agency.models.codex_app_server_json_rpc_client import CATALOG, DISABLED_FEATURES
from correctable_agency.models.codex_app_server_provider import BUILTIN_CALL, CodexAppServerProvider, dynamic_tools
from correctable_agency.models.model_gateway_protocol import BudgetExhausted, ModelUnavailable, Sampling
from correctable_agency.runner.episode_grid_runner import _provider
from correctable_agency.runner.token_budget_billing_guard import usage_tokens

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a text file in the workspace.",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
         {"type": "function", "function": {"name": "finish", "description": "End the episode.",
                                           "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}}}]
MESSAGES = [{"role": "system", "content": "ROLE TEXT\n\nYou act only through tool calls."},
            {"role": "user", "content": "## Events\nseq 1 operator_signed: ship it"}]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    record = tmp_path / "record.jsonl"
    home = tmp_path / "codex-home"
    (home / "skills" / ".system" / "imagegen").mkdir(parents=True)
    (home / "skills" / ".system" / "imagegen" / "SKILL.md").write_text("x")
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-reach-codex")
    monkeypatch.setattr(prov_mod, "BACKOFF_S", 0.0)
    fake_bin = tmp_path / "codex"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    fake_bin.chmod(0o755)

    def make(scenarios, **kw):
        monkeypatch.setenv("FAKE_CODEX_SCENARIOS", json.dumps(scenarios))
        return CodexAppServerProvider("gpt-6.1-sol", "high", home, codex_bin=str(fake_bin), **kw)

    def log():
        return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return make, log, home


def _sent(log, method):
    return [m for m in log() if m.get("method") == method]


def test_thread_carries_the_harness_prompt_and_tools_with_codex_switched_off(fake):
    make, log, home = fake
    make(["call"]).complete(MESSAGES, TOOLS, Sampling())
    start = log()[0]
    argv = start["argv"]
    cfg = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-c"]
    assert argv[:3] == ["app-server", "--listen", "stdio://"]
    assert f'model_catalog_json="{CATALOG}"' in cfg and 'web_search="disabled"' in cfg
    assert {"include_environment_context=false", "include_permissions_instructions=false",
            "include_collaboration_mode_instructions=false", "project_doc_max_bytes=0"} <= set(cfg)
    assert f'skills.config=[{{path="{home}/skills/.system/imagegen/SKILL.md",enabled=false}}]' in cfg
    assert [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--disable"] == DISABLED_FEATURES
    assert start["codex_home"] == str(home) and not start["api_key_seen"] and start["cwd_files"] == []
    (thread,) = _sent(log, "thread/start")
    p = thread["params"]
    assert p["baseInstructions"] == MESSAGES[0]["content"] and p["dynamicTools"] == dynamic_tools(TOOLS)
    assert p["environments"] == [] and p["ephemeral"] is True and p["sandbox"] == "read-only"
    assert p["approvalPolicy"] == "never" and p["model"] == "gpt-6.1-sol" and "developerInstructions" not in p
    assert p["experimentalRawEvents"] is True
    (turn,) = _sent(log, "turn/start")
    assert turn["params"]["input"] == [{"type": "text", "text": MESSAGES[1]["content"]}]
    assert turn["params"]["effort"] == "high"
    assert dynamic_tools(TOOLS)[0] == {"type": "function", "name": "read_file",
                                       "description": "Read a text file in the workspace.",
                                       "inputSchema": TOOLS[0]["function"]["parameters"]}


def test_the_models_call_is_never_answered_and_the_turn_is_interrupted_once_the_response_is_complete(fake):
    make, log, _ = fake
    r = make(["call"]).complete(MESSAGES, TOOLS, Sampling())
    assert [(c.id, c.name, c.arguments, c.parse_error) for c in r.tool_calls] == [
        ("call-thr-1", "read_file", {"path": "notes/plan.md"}, "")]
    assert r.text == "Checking the plan. " and usage_tokens(r.usage) == 1234
    assert r.finish_reason == "interrupted" and r.model_id == "gpt-6.1-sol"
    assert len(_sent(log, "turn/interrupt")) == 1
    assert not [m for m in log() if m.get("id") == "srv-thr-1"]  # no reply to the tool call: nothing ran
    assert _sent(log, "thread/unsubscribe")[0]["params"] == {"threadId": "thr-1"}


def test_a_text_answer_has_no_calls_and_is_not_interrupted(fake):
    make, log, _ = fake
    r = make(["text"]).complete(MESSAGES, TOOLS, Sampling())
    assert r.text == "all clear" and r.tool_calls == [] and r.finish_reason == "completed"
    assert not _sent(log, "turn/interrupt")


def test_codexs_own_request_user_input_is_reported_and_the_next_response_ignored(fake):
    make, log, _ = fake
    r = make(["builtin"]).complete(MESSAGES, TOOLS, Sampling())
    assert [(c.name, c.arguments) for c in r.tool_calls] == [(BUILTIN_CALL, {"questions": []})]
    assert r.finish_reason == "builtin_call" and len(_sent(log, "turn/interrupt")) == 1


def test_a_reconnected_stream_keeps_only_the_resent_response(fake):
    make, _, _ = fake
    r = make(["retry"]).complete(MESSAGES, TOOLS, Sampling())
    assert [(c.id, c.arguments) for c in r.tool_calls] == [("call-thr-1", {"path": "notes/plan.md"})]


def test_a_turn_without_usage_is_an_infrastructure_error(fake):
    make, _, _ = fake
    with pytest.raises(ModelUnavailable, match="without token usage"):
        make(["nousage"]).complete(MESSAGES, TOOLS, Sampling())


def test_a_rerouted_turn_reports_the_model_that_answered(fake):
    make, _, _ = fake
    assert make(["rerouted"]).complete(MESSAGES, TOOLS, Sampling()).model_id == "gpt-6-luna"


def test_malformed_arguments_are_a_parse_error(fake):
    make, _, _ = fake
    (call,) = make(["badargs"]).complete(MESSAGES, TOOLS, Sampling()).tool_calls
    assert call.name == "read_file" and call.arguments == {} and "unparseable arguments" in call.parse_error


def test_a_failed_turn_is_retried_and_then_an_infrastructure_error(fake):
    make, log, _ = fake
    assert make(["fail", "call"]).complete(MESSAGES, TOOLS, Sampling()).tool_calls[0].name == "read_file"
    with pytest.raises(ModelUnavailable, match="stream disconnected"):
        make(["fail"]).complete(MESSAGES, TOOLS, Sampling())


def test_a_crashed_app_server_is_restarted_for_the_retry(fake):
    make, log, _ = fake
    r = make(["crash", "call"]).complete(MESSAGES, TOOLS, Sampling())
    assert r.tool_calls[0].name == "read_file"
    assert len([m for m in log() if "argv" in m]) == 2  # a second app-server process


def test_usage_reaches_the_token_guard_and_the_budget_check_comes_first(fake):
    make, log, _ = fake
    seen = []
    r = make(["call"], on_usage=seen.append).complete(MESSAGES, TOOLS, Sampling())
    assert usage_tokens(r.usage) == 1234 and seen == [r.usage]
    assert r.usage["cache_read_tokens"] == 800 and r.usage["reasoning_tokens"] == 20
    assert r.usage["plan_used_percent"] == 7
    with pytest.raises(BudgetExhausted):
        make(["call"], budget_ok=lambda: False).complete(MESSAGES, TOOLS, Sampling())
    assert len(_sent(log, "thread/start")) == 1


def test_concurrent_calls_share_one_app_server(fake):
    make, log, _ = fake
    p = make(["call"])
    out = []
    workers = [threading.Thread(target=lambda: out.append(p.complete(MESSAGES, TOOLS, Sampling()))) for _ in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert len(out) == 6 and sorted(r.tool_calls[0].id for r in out) == sorted(f"call-thr-{i}" for i in range(1, 7))
    assert len([m for m in log() if "argv" in m]) == 1


def test_no_login_no_provider(tmp_path):
    with pytest.raises(RuntimeError, match="no ChatGPT login"):
        CodexAppServerProvider("gpt-6.1-sol", codex_home=tmp_path)


def test_the_runner_builds_the_provider(fake):
    _, _, home = fake
    args = argparse.Namespace(provider="codex-app-server", model="gpt-6.1-sol", codex_effort="xhigh", codex_home=home)
    p = _provider(args, lambda: True)
    assert isinstance(p, CodexAppServerProvider) and p._effort == "xhigh" and p._home == home


@pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI missing")
def test_request_audit_against_mock_responses_with_the_real_codex(tmp_path):
    """The model would see only the harness prompt, message and tools (plus request_user_input); calls never run."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    env["CODEX_HOME"] = str(home)
    # first start installs Codex's system skills, which the provider then disables by path
    subprocess.run(["codex", "app-server", "--listen", "stdio://"], input="", env=env, timeout=60,
                   capture_output=True, cwd=tmp_path)
    report = tmp_path / "audit.json"
    proc = subprocess.run([sys.executable, str(REPO / "scripts/codex-app-server-request-audit-against-mock-responses.py"),
                           "--codex-home", str(home), "--report", str(report)], capture_output=True, text=True,
                          timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(report.read_text())["verdict"] == "PASS"
