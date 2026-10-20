"""Minimal stdio MCP server that only declares tools: it lets `claude -p` see the harness tools as native tools.

argv[1] is a JSON file with the OpenAI-format tool list of the current call. tools/list returns those tools under
their own names and schemas. The harness never lets a call execute: the CLI provider stops after the model's first
response and reads its tool_use blocks. So tools/call only answers a neutral line in case the CLI asks. Stdlib only,
newline-delimited JSON-RPC 2.0.
"""

import json
import sys


def mcp_tools(path: str) -> list[dict]:
    tools = json.loads(open(path).read())
    return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "inputSchema": t["function"].get("parameters") or {"type": "object"}} for t in tools]


def handle(req: dict, tools: list[dict]) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # a notification (e.g. notifications/initialized): no reply
        return None
    if method == "initialize":
        version = (req.get("params") or {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "local", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "queued"}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> None:
    tools = mcp_tools(sys.argv[1])
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line), tools)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
