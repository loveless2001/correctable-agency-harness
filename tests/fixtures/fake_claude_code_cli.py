"""Fake `claude -p --output-format stream-json` for CPU tests.

Records argv, stdin, cwd, cwd contents and two env facts (one JSON line per run to $FAKE_CLAUDE_RECORD). If
--mcp-config is given, it really starts that MCP server and lists its tools over JSON-RPC, so the init event carries
the tools as the real CLI reports them (`mcp__<server>__<name>`). Then it prints one model response whose content
blocks are $FAKE_CLAUDE_BLOCKS (a JSON list), a later second response (which the provider must ignore), and a
result event. FAKE_CLAUDE_FAIL=1: an error result and no response, exit 1 (as on a usage limit).
FAKE_CLAUDE_NO_MCP=1: the init event lists no tools (the MCP server was not up in time).
"""
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_RECORD"], "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": prompt, "cwd": os.getcwd(), "cwd_files": os.listdir("."),
                        "api_key_seen": "ANTHROPIC_API_KEY" in os.environ,
                        "tool_search": os.environ.get("ENABLE_TOOL_SEARCH")}) + "\n")


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


tools = []
if "--mcp-config" in argv and os.environ.get("FAKE_CLAUDE_NO_MCP") != "1":
    (name, server), = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"].items()
    p = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate("\n".join(json.dumps(r) for r in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]) + "\n", timeout=30)
    listed = [json.loads(line) for line in out.splitlines()][1]["result"]["tools"]
    tools = [f"mcp__{name}__{t['name']}" for t in listed]
emit({"type": "system", "subtype": "init", "tools": tools, "mcp_servers": []})
if os.environ.get("FAKE_CLAUDE_FAIL") == "1":
    emit({"type": "result", "subtype": "error", "is_error": True, "result": "usage limit reached"})
    sys.exit(1)
blocks = json.loads(os.environ["FAKE_CLAUDE_BLOCKS"])
for b in [{"type": "thinking", "thinking": ""}, *blocks]:
    emit({"type": "assistant", "message": {"id": "msg_1", "model": "claude-sonnet-5-5", "content": [b]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "not granted", "is_error": True}]}})
emit({"type": "assistant", "message": {"id": "msg_2", "content": [{"type": "text", "text": "SECOND TURN"}]}})
emit({"type": "result", "subtype": "error_max_turns", "is_error": False, "num_turns": 2, "stop_reason": "tool_use",
      "total_cost_usd": 0.01, "duration_ms": 1200, "modelUsage": {"claude-sonnet-5-5": {}},
      "usage": {"input_tokens": 3, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 1500,
                "output_tokens": 200, "output_tokens_details": {"thinking_tokens": 120}, "iterations": [{}]}})
