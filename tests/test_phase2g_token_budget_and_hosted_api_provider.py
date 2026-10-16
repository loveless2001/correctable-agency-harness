"""Phase 2G prep: a token-budget billing guard and a hosted, token-billed API (Z.ai GLM-5.3-Flash shape), tested on
CPU against a local fake that serves the Z.ai path, returns tool-call arguments as objects and records requests."""
import json
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from correctable_agency.broker import bend_policy_build as build_mod
from correctable_agency.models.heuristic_policy_stub_provider import HeuristicPolicyStubProvider
from correctable_agency.models.model_gateway_protocol import Sampling
from correctable_agency.models.openai_compatible_chat_provider import OpenAICompatibleChatProvider
from correctable_agency.runner.token_budget_billing_guard import TokenBudgetGuard, usage_tokens

REPO = Path(__file__).resolve().parents[1]
ZAI_PATH = "/api/paas/v4/chat/completions"
SECRET = "sk-test-not-a-real-key-1234567890"
USAGE = {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000,
         "prompt_tokens_details": {"cached_tokens": 200}}


def _guard(tmp_path, cap=10_000, workers=2, call=1_000, episode=3_000, extra=()):
    return TokenBudgetGuard([(tmp_path / "study.json", cap), *extra], workers, call, episode)


def test_usage_tokens_counts_total_or_prompt_plus_completion():
    assert usage_tokens(USAGE) == 1000
    assert usage_tokens({"prompt_tokens": 5, "completion_tokens": 7}) == 12
    assert usage_tokens({}) is None


def test_guard_admits_calls_only_while_the_worst_case_fits(tmp_path):
    g = _guard(tmp_path, cap=2_500)
    assert g.allow_call()
    g.record({"total_tokens": 1_200})
    assert g.allow_call()  # 1200 + 1000 <= 2500
    g.record({"total_tokens": 1_000})
    assert not g.allow_call()  # 2200 + 1000 > 2500
    assert json.loads((tmp_path / "study.json").read_text()) == {"used": 2_200, "calls": 2}


def test_inflight_calls_of_other_threads_are_reserved(tmp_path):
    g = _guard(tmp_path, cap=1_500)
    assert g.allow_call()  # main thread holds 1000
    seen = []
    t = threading.Thread(target=lambda: seen.append(g.allow_call()))
    t.start(), t.join()
    assert seen == [False]  # 1000 in flight + 1000 > 1500
    assert g.allow_call()  # a retry of the same thread's call replaces its reserve, never adds


def test_allow_start_keeps_a_reserve_per_worker(tmp_path):
    g = _guard(tmp_path, cap=10_000, workers=2, episode=3_000)
    assert g.allow_start()
    g.record({"total_tokens": 4_500})
    assert not g.allow_start()  # 4500 + 2 x 3000 > 10000


def test_shared_study_ledger_and_phase_allocation_both_bind(tmp_path):
    phase_a = (tmp_path / "a.json", 5_000)
    g1 = _guard(tmp_path, cap=6_000, extra=[phase_a])
    g2 = _guard(tmp_path, cap=6_000, extra=[(tmp_path / "b.json", 5_000)])
    g1.record({"total_tokens": 3_000})
    g2.record({"total_tokens": 2_500})
    assert json.loads((tmp_path / "study.json").read_text())["used"] == 5_500
    assert not g2.allow_call()  # the study ledger (5500 + 1000 > 6000) binds although phase b has room


def test_missing_usage_is_charged_the_full_reserve_and_a_broken_ledger_fails_closed(tmp_path):
    g = _guard(tmp_path)
    g.record({})
    assert json.loads((tmp_path / "study.json").read_text())["used"] == 1_000
    (tmp_path / "study.json").write_text("not json")
    assert not g.allow_call() and not g.allow_start()


def _zai_fake(style="compliant"):
    agent, seen = HeuristicPolicyStubProvider(style), []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path != ZAI_PATH:
                self.send_response(404), self.end_headers()
                return
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": req})
            reply = agent.complete(req["messages"], req.get("tools", []), Sampling()) if req.get("tools") else None
            calls = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                     for c in (reply.tool_calls if reply else [])]  # Z.ai schema: arguments as an object
            body = json.dumps({"model": "glm-5.3-flash", "usage": USAGE, "choices": [{
                "finish_reason": "tool_calls" if calls else "stop", "message": {
                    "role": "assistant", "content": None if calls else "ok", "reasoning_content": "thinking",
                    "tool_calls": calls or None}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}", seen


def test_provider_hosted_api_options_and_vllm_defaults_unchanged():
    server, base, seen = _zai_fake()
    try:
        charged = []
        p = OpenAICompatibleChatProvider(base, "glm-5.3-flash", api_key=SECRET, chat_url=base + ZAI_PATH,
                                         extra_body={"reasoning_effort": "high"}, send_seed=False,
                                         on_usage=charged.append)
        r = p.complete([{"role": "user", "content": "[recent events]\n(none yet)"}],
                       [{"type": "function", "function": {"name": "run", "parameters": {}}}], Sampling(seed=0))
        body = seen[-1]["body"]
        assert seen[-1]["auth"] == f"Bearer {SECRET}" and body["reasoning_effort"] == "high" and "seed" not in body
        assert charged == [USAGE] and r.model_id == "glm-5.3-flash" and r.reasoning == "thinking"
        assert r.tool_calls[0].name == "run" and r.tool_calls[0].arguments == {"program": "validate", "args": []}
    finally:
        server.shutdown()
    v = OpenAICompatibleChatProvider("http://h", "llm")  # vLLM path: no new fields
    assert v._url == "http://h/v1/chat/completions" and v._extra == {} and v._send_seed and v._on_usage is None


@pytest.mark.skipif(not (shutil.which("bwrap") and Path(build_mod.bend_binary()).exists() and build_mod.lean_available()),
                    reason="bwrap, bend or Lean missing")
def test_cli_token_mode_end_to_end_without_leaking_the_key(tmp_path):
    build_mod.build(verdict=True)
    server, base, seen = _zai_fake()
    env = tmp_path / "keys.env"
    env.write_text(f"# test\nZAI_API_KEY={SECRET}\n")
    try:
        def cli(out, study_cap):
            return subprocess.run(
                [sys.executable, "-m", "correctable_agency.runner.episode_grid_runner", "--out", str(out),
                 "--provider", "openai", "--base-url", base, "--model", "glm-5.3-flash", "--arms", "C3",
                 "--limit", "2", "--workers", "2", "--api-key-env", "ZAI_API_KEY", "--env-file", str(env),
                 "--chat-url", base + ZAI_PATH, "--extra-body", '{"reasoning_effort": "high"}', "--no-seed",
                 "--token-budget", f"{tmp_path / 'study.json'}:{study_cap}",
                 "--token-budget", f"{out / 'token-ledger.json'}:1000000",
                 "--call-token-reserve", "2000", "--episode-token-reserve", "20000",
                 "--luna-cmd", json.dumps([sys.executable, str(Path(__file__).parent / "fixtures" / "stub_luna_cli.py")])],
                cwd=REPO, capture_output=True, text=True, timeout=600)
        r = cli(tmp_path / "run", 1_000_000)
        assert r.returncode == 0, r.stderr
        assert SECRET not in r.stdout + r.stderr and "API preflight ok" in r.stdout
        outcomes = [json.loads(p.read_text()) for p in (tmp_path / "run").glob("*/*/outcome.json")]
        assert len(outcomes) == 2 and all(o["status"] == "finished" for o in outcomes)
        study = json.loads((tmp_path / "study.json").read_text())
        assert study["used"] == 1000 * len(seen) and study["calls"] == len(seen)  # every response charged
        assert all("seed" not in s["body"] and s["body"]["reasoning_effort"] == "high" for s in seen)
        refused = cli(tmp_path / "run2", 1_000)  # the study ledger is already past this cap
        assert refused.returncode != 0 and "no token budget left" in refused.stderr
    finally:
        server.shutdown()
