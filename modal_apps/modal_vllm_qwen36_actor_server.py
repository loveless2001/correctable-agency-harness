"""Modal vLLM deploy of Qwen3.6-27B for the correctable-agency pilot actor.

Copied from drylab/modal-vllm-bixbench/modal_vllm_bixbench.py (natively run
2026-06) with only the app name changed, so it gets its own endpoint and never
contends with drylab's deploys. Keep the diff against that file minimal. Defaults match Codex's proven Qwen3.6-27B
config: TP=2 on A100-80GB, 65536 ctx, hermes tool parser, GDN prefill triton,
language-model-only.

Deploy:  REASONING_PARSER=qwen3 modal deploy modal_apps/modal_vllm_qwen36_actor_server.py
URL:     https://<workspace>--correctable-agency-vllm-qwen36-serve.modal.run
Stop:    modal app stop correctable-agency-vllm-qwen36
"""
from __future__ import annotations

import os

import modal

APP_NAME = "correctable-agency-vllm-qwen36"
VLLM_PORT = 8000
MINUTES = 60

MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen3.6-27B")
SERVED_MODEL_NAME = os.environ.get("SERVED_MODEL_NAME", "llm")
VLLM_VERSION = os.environ.get("VLLM_VERSION", "0.22.1")
GPU = os.environ.get("MODAL_GPU", "A100-80GB")
N_GPU = int(os.environ.get("N_GPU", "2"))
MAX_MODEL_LEN = os.environ.get("MAX_MODEL_LEN", "65536")
TOOL_CALL_PARSER = os.environ.get("TOOL_CALL_PARSER", "qwen3_coder")  # Qwen3.6 recipe parser (NOT hermes)
REASONING_PARSER = os.environ.get("REASONING_PARSER", "qwen3")  # MUST set (Modal env vars don't reach container)
FAST_BOOT = os.environ.get("FAST_BOOT", "1") != "0"
LANGUAGE_MODEL_ONLY = os.environ.get("LANGUAGE_MODEL_ONLY", "1") == "1"
GDN_PREFILL_BACKEND = os.environ.get("GDN_PREFILL_BACKEND", "triton")

# Own volumes (no runtime dependency on drylab's Modal resources). The first
# boot downloads Qwen3.6-27B into them; later boots reuse the cache.
hf_cache_vol = modal.Volume.from_name("correctable-agency-huggingface-cache", create_if_missing=True)
vllm_cache_vol = modal.Volume.from_name("correctable-agency-vllm-cache", create_if_missing=True)

vllm_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.9.0-devel-ubuntu22.04",
        add_python="3.12",
    )
    .entrypoint([])
    .uv_pip_install(f"vllm=={VLLM_VERSION}", "aiohttp")
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
            "VLLM_LOG_STATS_INTERVAL": "1",
            "MODEL_NAME": MODEL_NAME,
            "SERVED_MODEL_NAME": SERVED_MODEL_NAME,
            "N_GPU": str(N_GPU),
            "MAX_MODEL_LEN": str(MAX_MODEL_LEN),
            "TOOL_CALL_PARSER": TOOL_CALL_PARSER,
            "REASONING_PARSER": REASONING_PARSER,
            "FAST_BOOT": "1" if FAST_BOOT else "0",
            "LANGUAGE_MODEL_ONLY": "1" if LANGUAGE_MODEL_ONLY else "0",
            "GDN_PREFILL_BACKEND": GDN_PREFILL_BACKEND,
        }
    )
)

app = modal.App(APP_NAME)


def _vllm_cmd() -> list[str]:
    cmd = [
        "vllm", "serve", MODEL_NAME,
        "--served-model-name", SERVED_MODEL_NAME, MODEL_NAME,
        "--host", "0.0.0.0",
        "--port", str(VLLM_PORT),
        "--uvicorn-log-level", "info",
        "--max-model-len", str(MAX_MODEL_LEN),
        "--tensor-parallel-size", str(N_GPU),
        "--gpu-memory-utilization", os.environ.get("GPU_MEMORY_UTILIZATION", "0.85"),
        "--enable-auto-tool-choice",
        "--tool-call-parser", TOOL_CALL_PARSER,
    ]
    if REASONING_PARSER:
        cmd += ["--reasoning-parser", REASONING_PARSER]
    if FAST_BOOT:
        cmd.append("--enforce-eager")
    if LANGUAGE_MODEL_ONLY:
        cmd.append("--language-model-only")
    if GDN_PREFILL_BACKEND:
        cmd += ["--gdn-prefill-backend", GDN_PREFILL_BACKEND]
    return cmd


@app.function(
    image=vllm_image,
    gpu=f"{GPU}:{N_GPU}",
    scaledown_window=10 * MINUTES,
    timeout=20 * MINUTES,
    max_containers=1,  # billing guard: never scale out to a second 2xA100 container
    volumes={
        "/root/.cache/huggingface": hf_cache_vol,
        "/root/.cache/vllm": vllm_cache_vol,
    },
)
@modal.concurrent(max_inputs=20)
@modal.web_server(port=VLLM_PORT, startup_timeout=20 * MINUTES)
def serve() -> None:
    import subprocess

    cmd = _vllm_cmd()
    print("Starting vLLM:", " ".join(cmd), flush=True)
    subprocess.Popen(cmd)


@app.local_entrypoint()
def url() -> None:
    print(f"MODAL_VLLM_URL={serve.get_web_url()}")
    print(f"SERVED_MODEL_NAME={SERVED_MODEL_NAME}")
