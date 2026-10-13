"""The paid code path, end to end on CPU: the real CLI as a subprocess, with --provider openai
against a local fake vLLM endpoint (warm-up poll, request/response shape, kernel-check gate, guard)."""

import json
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from correctable_agency.broker import bend_policy_build as build_mod
from correctable_agency.models.model_gateway_protocol import Sampling
from correctable_agency.models.openai_compatible_chat_provider import OpenAICompatibleChatProvider

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from fake_openai_chat_server import serve  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (shutil.which("bwrap") and Path(build_mod.bend_binary()).exists()),
                                reason="bwrap or bend missing")


@pytest.fixture(scope="module")
def endpoint():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = serve(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_provider_parses_vllm_shape(endpoint):
    reply = OpenAICompatibleChatProvider(endpoint, "llm").complete(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "[recent events]\n(none yet)"}], [], Sampling())
    assert reply.model_id == "fake-qwen" and reply.reasoning == "thinking..."
    assert reply.tool_calls[0].name == "run" and reply.tool_calls[0].arguments == {"program": "validate", "args": []}


def _cli(endpoint, out, *extra):
    return subprocess.run(
        [sys.executable, "-m", "correctable_agency.runner.episode_grid_runner", "--out", str(out), "--provider",
         "openai", "--base-url", endpoint, "--arms", "C3,leaky", "--limit", "2", "--workers", "2",
         "--luna-cmd", json.dumps([sys.executable, str(Path(__file__).parent / "fixtures" / "stub_luna_cli.py")]),
         *extra],
        cwd=REPO, capture_output=True, text=True, timeout=600)


def test_cli_refuses_without_cap_or_kernel_check(endpoint, tmp_path):
    assert "--cap-usd is required" in _cli(endpoint, tmp_path / "a").stderr
    build_mod.build(verdict=False)
    r = _cli(endpoint, tmp_path / "b", "--cap-usd", "5")
    assert r.returncode != 0 and "Lean kernel" in r.stderr


@pytest.mark.skipif(not build_mod.lean_available(), reason="Lean (elan) missing")
def test_cli_runs_paid_path_with_kernel_checked_policy(endpoint, tmp_path):
    build_mod.build(verdict=True)
    out = tmp_path / "paid"
    r = _cli(endpoint, out, "--cap-usd", "5")
    assert r.returncode == 0, r.stderr
    assert "endpoint ready after" in r.stdout and "projected spend incl. idle window" in r.stdout
    assert (out / "spend-ledger.json").exists()
    outcomes = [json.loads(p.read_text()) for p in out.glob("*/*/outcome.json")]
    assert len(outcomes) == 4 and all(o["status"] == "finished" and o["model_ids"] == ["fake-qwen"] for o in outcomes)
