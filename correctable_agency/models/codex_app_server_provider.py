"""Codex app-server provider: OpenAI GPT models on the user's ChatGPT plan through `codex app-server`, not a billed API.

One long-lived `codex app-server` (JSON-RPC over stdio) per provider. Each call is one ephemeral thread with one turn:
- The harness system prompt is the thread's `baseInstructions`, which replaces Codex's own instructions. The rendered
  user message is the turn's only input.
- The call's tools are the thread's `dynamicTools`, so the model sees them natively under their own names and
  schemas. A call reaches us as an `item/tool/call` request. We never answer it: the turn is interrupted there, so
  Codex never executes anything. Codex sends `parallel_tool_calls: false`, so that call is the model's whole response.
- Codex is otherwise switched off. A frozen model catalog (codex_harness_model_catalog.json, tool_mode "direct")
  removes the code-mode `exec` wrapper, the sub-agent tools and tool search. `environments: []` removes shell and
  apply_patch. The CONFIG overrides turn off web search, skills, apps, plugins, memories, hooks, goals, and the
  permissions, environment and collaboration-mode blocks. CODEX_HOME is a dedicated directory with its own ChatGPT
  login (scripts/codex-harness-home-device-code-login.py) and no config, AGENTS.md or MCP servers. What Codex sends
  is checked against a mock Responses server (scripts/codex-app-server-request-audit-against-mock-responses.py).
- One Codex tool remains: `request_user_input`. Codex itself answers "unavailable in Default mode" without telling
  the client. A response that ends with neither a harness call nor a final message can only be that call. It is
  reported as a call to the tool `request_user_input`, which the dispatcher rejects as unknown.

Known differences from the vLLM / hosted-API runs (to be stated with any result): one tool call per response; no
temperature or seed; reasoning is not returned; whatever the ChatGPT backend adds server-side is not visible.
"""

import os
import queue
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from correctable_agency.models.claude_code_cli_provider import split_messages
from correctable_agency.models.codex_app_server_json_rpc_client import (
    DEFAULT_HOME, AppServer, AppServerError, app_server_argv,
)
from correctable_agency.models.model_gateway_protocol import (
    BudgetExhausted, ModelReply, ModelUnavailable, Sampling, ToolCall,
)

RETRIES = 3
BACKOFF_S = 5.0
TIMEOUT_S = 600  # one turn: reasoning at high effort on a long context
BUILTIN_CALL = "request_user_input"


def dynamic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI-format tools as app-server dynamic tools (same names, descriptions and schemas)."""
    return [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
             "inputSchema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]


def usage_of(last: dict, rate_limits: dict) -> dict:
    """Codex's per-response token usage in the gateway's terms (prompt includes cached input), raw parts kept."""
    return {"prompt_tokens": int(last.get("inputTokens") or 0), "completion_tokens": int(last.get("outputTokens") or 0),
            "total_tokens": int(last.get("totalTokens") or 0),
            "cache_read_tokens": int(last.get("cachedInputTokens") or 0),
            "reasoning_tokens": int(last.get("reasoningOutputTokens") or 0),
            "plan_used_percent": ((rate_limits.get("primary") or {}).get("usedPercent"))}


class CodexAppServerProvider:
    def __init__(self, model: str, effort: str = "high", codex_home: Path = DEFAULT_HOME,
                 budget_ok: Callable[[], bool] = lambda: True, on_usage: Callable[[dict], None] | None = None,
                 codex_bin: str = "codex", extra_args: tuple[str, ...] = ()):
        if not (Path(codex_home) / "auth.json").exists():
            raise RuntimeError(f"no ChatGPT login in {codex_home}: run scripts/codex-harness-home-device-code-login.py")
        self._model, self._effort, self._home, self._bin = model, effort, Path(codex_home), codex_bin
        self._extra_args = list(extra_args)  # the mock-Responses audit's provider override; empty in runs
        self._budget_ok, self._on_usage = budget_ok, on_usage
        self._cwd = tempfile.mkdtemp(prefix="agent-")  # empty: no AGENTS.md, nothing to read
        self._stderr = os.path.join(tempfile.mkdtemp(prefix="codex-app-server-log-"), "stderr.log")
        self._env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        self._env["CODEX_HOME"] = str(self._home)
        self._server: AppServer | None = None
        self._start_lock = threading.Lock()

    def _live_server(self) -> AppServer:
        with self._start_lock:
            if self._server is None or not self._server.alive:
                if self._server is not None:
                    self._server.close()
                self._server = AppServer(app_server_argv(self._home, self._bin) + self._extra_args, self._env, self._cwd, self._stderr)
            return self._server

    def _turn(self, system: str, prompt: str, tools: list[dict]) -> tuple[str, list[ToolCall], dict, str, str]:
        server = self._live_server()
        thread = server.request("thread/start", {
            "model": self._model, "baseInstructions": system, "dynamicTools": dynamic_tools(tools),
            "environments": [], "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
            "cwd": self._cwd})["thread"]
        events = server.subscribe(thread["id"])
        try:
            turn = server.request("turn/start", {"threadId": thread["id"], "effort": self._effort,
                                                 "input": [{"type": "text", "text": prompt}]})["turn"]
            texts, calls, final, usage, deadline, interrupted = [], [], False, None, time.time() + TIMEOUT_S, False
            served = thread.get("model") or self._model

            def interrupt():
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    server._send({"id": f"interrupt-{turn['id']}", "method": "turn/interrupt",
                                  "params": {"threadId": thread["id"], "turnId": turn["id"]}})
            while True:
                try:
                    m = events.get(timeout=max(1.0, deadline - time.time()))
                except queue.Empty:
                    raise AppServerError(f"turn timed out after {TIMEOUT_S} s") from None
                if m is None:
                    raise AppServerError("app-server exited during the turn")
                method, params = m.get("method"), m.get("params") or {}
                if method == "item/tool/call" and not calls:  # the model's call: never answered
                    args = params.get("arguments")
                    calls.append(ToolCall(params.get("callId") or "call_0", str(params.get("tool") or ""), args)
                                 if isinstance(args, dict) else
                                 ToolCall(params.get("callId") or "call_0", str(params.get("tool") or ""), {},
                                          f"arguments are not an object: {args!r}"[:500]))
                    interrupt()
                elif "id" in m and method:
                    server.refuse(m["id"])  # approvals etc.: nothing else may run
                elif method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
                    item = params["item"]
                    texts.append(item.get("text") or "")
                    final = final or item.get("phase") != "commentary"
                elif method == "thread/tokenUsage/updated" and usage is None:  # the first response is complete
                    usage = (params.get("tokenUsage") or {}).get("last") or {}
                    if not calls and not final:  # neither a harness call nor an answer: Codex's own tool
                        calls.append(ToolCall("builtin_0", BUILTIN_CALL, {}))
                        interrupt()
                elif method == "model/rerouted":  # the backend answered with another model: record which
                    served = params.get("toModel") or served
                elif method == "turn/completed":
                    status = (params.get("turn") or {}).get("status")
                    if status == "failed":
                        raise AppServerError(f"turn failed: {(params['turn'].get('error') or {}).get('message')}"[:500])
                    if usage is None:
                        raise AppServerError("turn ended without token usage")
                    return "".join(texts), calls, usage, str(status), served
        finally:
            server.unsubscribe(thread["id"])
            server.notify_unsubscribe(thread["id"])

    def complete(self, messages: list[dict], tools: list[dict], sampling: Sampling) -> ModelReply:
        system, prompt = split_messages(messages)
        last = ""
        for attempt in range(RETRIES):
            if not self._budget_ok():
                raise BudgetExhausted("billing guard: no budget for another model call")
            t0 = time.time()
            try:
                text, calls, last_usage, status, model_id = self._turn(system, prompt, tools)
            except (AppServerError, OSError) as e:
                last = str(e)[:500]
                time.sleep(BACKOFF_S * 2 ** attempt)
                continue
            usage = usage_of(last_usage, self._server.rate_limits if self._server else {})
            if self._on_usage is not None:
                self._on_usage(usage)
            return ModelReply(text=text, tool_calls=calls, model_id=model_id, usage=usage, latency_s=time.time() - t0,
                              finish_reason="builtin_call" if calls and calls[0].name == BUILTIN_CALL else status)
        raise ModelUnavailable(f"{RETRIES} attempts failed: {last}")
