"""Claude Code CLI provider (`claude -p`): Claude models on the user's subscription, not a billed API.

Each call is one stateless `claude -p` run with native tool calls:
- The harness system prompt replaces Claude Code's (`--system-prompt`), and the rendered user message goes in on stdin.
- The call's tools are declared by a small stdio MCP server (claude_cli_tool_schema_mcp_server), so the model sees
  them natively, as `mcp__local__<name>` with the harness schemas.
- `--max-turns 1` and stream-json output: the provider reads the text and tool_use blocks of the model's first
  response. Each block becomes the same ToolCall the OpenAI-compatible provider returns (prefix stripped), so the
  actor loop, dispatcher and scorer are unchanged. The CLI never executes a call (no permission is granted).
- Claude Code is otherwise switched off: no built-in tools (`--tools ""`), no other MCP servers, no setting files (so
  no hooks), no saved session, tool search off. ANTHROPIC_API_KEY is removed from the child's environment, so the call
  always uses the subscription login.
- The CLI runs in a bwrap mount namespace (the host filesystem bound as is) whose cwd is an empty /work. Claude Code
  tells the model its working directory, and /work is where the episode's programs really run (bwrap executor), so
  that line is true. Absolute /work paths are escalated by the classifier as for every other model. No CLAUDE.md is
  found above /work.

Known differences from the vLLM / hosted-API runs (to be stated with any result): tool names carry the
`mcp__local__` prefix; there is no temperature or seed; reasoning is not returned; Claude Code still adds a short
context block (account email, cwd, date, model name).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from correctable_agency.models.model_gateway_protocol import (
    BudgetExhausted, ModelReply, ModelUnavailable, Sampling, ToolCall,
)

RETRIES = 3
BACKOFF_S = 5.0
TIMEOUT_S = 600  # one `claude -p` run: thinking at high effort on a long context, plus CLI start-up
SERVER = "local"  # "workspace" is reserved by Claude Code
PREFIX = f"mcp__{SERVER}__"
MCP_SERVER_PY = Path(__file__).with_name("claude_cli_tool_schema_mcp_server.py")


def work_dir_jail(empty_dir: str) -> list[str]:
    """bwrap prefix: the host root entries bound as they are on a tmpfs root, plus an empty dir as cwd /work."""
    if not shutil.which("bwrap"):
        raise RuntimeError("bwrap is required: the CLI must see /work as its working directory")
    argv = ["bwrap", "--tmpfs", "/"]
    for name in sorted(os.listdir("/")):
        path = "/" + name
        if name == "proc":
            argv += ["--proc", "/proc"]
        elif name == "dev":
            argv += ["--dev-bind", "/dev", "/dev"]
        elif os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif name != "work":
            argv += ["--bind", path, path]
    return argv + ["--bind", empty_dir, "/work", "--chdir", "/work", "--"]


def split_messages(messages: list[dict]) -> tuple[str, str]:
    """(system prompt, stdin prompt). The renderer sends [system, user]; any other turns are labelled by role."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    if len(rest) == 1 and rest[0]["role"] == "user":
        return system, rest[0]["content"]
    return system, "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in rest)


def parse_stream(stdout: str) -> tuple[dict, list[dict], dict]:
    """(init event, content blocks of the FIRST model response, result event) from stream-json output."""
    init, blocks, result, first_id = {}, [], {}, None
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = e
        elif e.get("type") == "assistant":
            msg = e.get("message") or {}
            first_id = first_id or msg.get("id")
            if msg.get("id") == first_id:
                blocks += [dict(b, _stop=msg.get("stop_reason")) for b in msg.get("content") or []]
        elif e.get("type") == "result":
            result = e
    return init, blocks, result


def tool_call(i: int, block: dict) -> ToolCall:
    """A tool_use block as the gateway's ToolCall. Malformed JSON from the model reaches us as
    {"__unparsedToolInput": {"raw": ...}} and becomes a parse error, as unparseable arguments do on the API path."""
    args, name = block.get("input"), str(block.get("name") or "").removeprefix(PREFIX)
    if isinstance(args, dict) and "__unparsedToolInput" in args:
        raw = (args["__unparsedToolInput"] or {}).get("raw", "")
        return ToolCall(block.get("id") or f"call_{i}", name, {}, f"unparseable arguments: {raw!r}"[:500])
    if not isinstance(args, dict):
        return ToolCall(block.get("id") or f"call_{i}", name, {}, f"arguments are not an object: {args!r}"[:500])
    return ToolCall(block.get("id") or f"call_{i}", name, args)


def usage_of(result: dict) -> dict:
    """The CLI's usage in the gateway's terms (prompt = uncached + cache writes + cache reads), raw parts kept."""
    u = result.get("usage") or {}
    cached, written = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
    return {"prompt_tokens": int(u.get("input_tokens") or 0) + cached + written,
            "completion_tokens": int(u.get("output_tokens") or 0),
            "cache_read_tokens": cached, "cache_write_tokens": written,
            "thinking_tokens": int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
            "api_calls": len(u.get("iterations") or []) or None,
            "notional_cost_usd": result.get("total_cost_usd"), "cli_turns": result.get("num_turns"),
            "cli_subtype": result.get("subtype"), "cli_duration_ms": result.get("duration_ms")}


class ClaudeCodeCliProvider:
    def __init__(self, model: str, effort: str = "high", budget_ok: Callable[[], bool] = lambda: True,
                 on_usage: Callable[[dict], None] | None = None, claude_bin: str = "claude"):
        self._model, self._effort, self._bin = model, effort, claude_bin
        self._budget_ok, self._on_usage = budget_ok, on_usage
        self._jail = work_dir_jail(tempfile.mkdtemp(prefix="agent-"))  # empty, seen by the CLI as /work
        self._spec_dir = Path(tempfile.mkdtemp(prefix="agent-tools-"))  # tool lists for the MCP server
        self._env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        self._env["ENABLE_TOOL_SEARCH"] = "false"  # all tools loaded up front, never deferred

    def _mcp_config(self, tools: list[dict]) -> str:
        blob = json.dumps(tools, sort_keys=True)
        path = self._spec_dir / f"{hashlib.sha256(blob.encode()).hexdigest()[:16]}.json"
        if not path.exists():
            path.write_text(blob)
        return json.dumps({"mcpServers": {SERVER: {"type": "stdio", "command": sys.executable,
                                                   "args": [str(MCP_SERVER_PY), str(path)]}}})

    def argv(self, system: str, tools: list[dict]) -> list[str]:
        mcp = ["--mcp-config", self._mcp_config(tools)] if tools else []
        return [*self._jail, self._bin, "-p", "--model", self._model, "--effort", self._effort, "--system-prompt", system,
                "--tools", "", *mcp, "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--max-turns", "1", "--output-format", "stream-json", "--verbose"]

    def _run(self, argv: list[str], prompt: str, tool_names: set[str]) -> tuple[list[dict], dict]:
        last = ""
        for attempt in range(RETRIES):
            if not self._budget_ok():
                raise BudgetExhausted("billing guard: no budget for another model call")
            try:
                proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S,
                                      env=self._env)
                init, blocks, result = parse_stream(proc.stdout)
                missing = {PREFIX + n for n in tool_names} - set(init.get("tools") or [])
                if missing:  # the MCP server was not up for this call: the model could not see its tools
                    last = f"tools not loaded: {sorted(missing)}"
                elif blocks:
                    return blocks, result
                else:
                    last = f"exit {proc.returncode}, no model response: {(result.get('result') or proc.stderr)!r}"[:500]
            except subprocess.TimeoutExpired:
                last = f"timed out after {TIMEOUT_S} s"
            except OSError as e:
                last = str(e)[:500]
            time.sleep(BACKOFF_S * 2 ** attempt)
        raise ModelUnavailable(f"{RETRIES} attempts failed: {last}")

    def complete(self, messages: list[dict], tools: list[dict], sampling: Sampling) -> ModelReply:
        system, prompt = split_messages(messages)
        names = {(t.get("function") or {}).get("name", "") for t in tools}
        t0 = time.time()
        blocks, result = self._run(self.argv(system, tools), prompt, names)
        usage = usage_of(result)
        if self._on_usage is not None:
            self._on_usage(usage)
        calls = [tool_call(i, b) for i, b in enumerate(b for b in blocks if b.get("type") == "tool_use")]
        text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
        stops = {b.get("_stop") for b in blocks} | {result.get("stop_reason")}
        return ModelReply(text=text, tool_calls=calls, model_id=next(iter(result.get("modelUsage") or {}), self._model),
                          usage=usage, latency_s=time.time() - t0,
                          finish_reason="length" if "max_tokens" in stops else str(result.get("stop_reason") or ""),
                          reasoning="")
