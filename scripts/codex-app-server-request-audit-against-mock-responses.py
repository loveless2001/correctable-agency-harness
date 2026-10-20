"""Audit what the GPT provider makes Codex send to the model, against a local mock of the Responses API ($0).

The provider runs exactly as in an episode (same CODEX_HOME, catalog, overrides and thread parameters), except that
the model provider points at a mock server on 127.0.0.1. Three scenarios, one per model reply:
- call: the model calls a harness tool. The reply must be that ToolCall, and Codex must send no second request (the
  call was never answered, so nothing ran).
- text: the model answers in text. The reply has the text and no calls.
- builtin: the model calls Codex's request_user_input. The reply is a call to `request_user_input`.
Every request must carry only the harness system prompt (as the one developer message), the rendered user message
(the one user message), and the harness tools plus `request_user_input`; no top-level instructions, no other tools.
Exit 0 and "PASS" only if every check holds. Writes a JSON report with the first request of each scenario.

usage: codex-app-server-request-audit-against-mock-responses.py [--codex-home DIR] [--model M] [--report PATH]
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from correctable_agency.actor.actor_tool_schemas import TOOLS, tool_schemas
from correctable_agency.actor.context_renderer import RULES
from correctable_agency.models.codex_app_server_json_rpc_client import DEFAULT_HOME
from correctable_agency.models.codex_app_server_provider import BUILTIN_CALL, CodexAppServerProvider
from correctable_agency.models.model_gateway_protocol import Sampling

SYSTEM = f"You are the release agent of a small team (audit).\n\n{RULES}"
REQUESTS: list[dict] = []


def _scenario(body: dict) -> str:
    users = [c.get("text", "") for it in body.get("input", []) if it.get("role") == "user"
             for c in it.get("content") or []]
    return next((s for s in ("call", "text", "builtin") if any(f"SCENARIO {s}" in u for u in users)), "?")


def _items(scenario: str, n: int) -> list[dict]:
    if scenario == "call" and n == 1:
        return [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file",
                 "arguments": json.dumps({"path": "notes/plan.md"})}]
    if scenario == "builtin" and n == 1:
        return [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": BUILTIN_CALL,
                 "arguments": json.dumps({"questions": [{"id": "q", "header": "Deploy?", "question": "Deploy now?",
                                                         "options": [{"label": "Yes", "description": "go"}]}]})}]
    return [{"type": "message", "id": f"msg_{n}", "role": "assistant", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "release looks fine"}]}]


class MockResponses(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        scenario = _scenario(body)
        REQUESTS.append({"scenario": scenario, "body": body})
        n = sum(r["scenario"] == scenario for r in REQUESTS)
        ev = [{"type": "response.created", "response": {"id": f"resp_{len(REQUESTS)}"}}]
        ev += [{"type": "response.output_item.done", "output_index": i, "item": it}
               for i, it in enumerate(_items(scenario, n))]
        ev += [{"type": "response.completed", "response": {"id": f"resp_{len(REQUESTS)}", "usage": {
            "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 5}, "total_tokens": 1020}}}]
        out = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ev).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def request_checks(body: dict, prompt: str, tool_names: list[str]) -> dict:
    """What the model sees in one request: only ours, plus request_user_input."""
    developer = [c.get("text") for it in body["input"] if it.get("type") == "message" and it.get("role") == "developer"
                 for c in it.get("content") or []]
    user = [c.get("text") for it in body["input"] if it.get("type") == "message" and it.get("role") == "user"
            for c in it.get("content") or []]
    groups = [t for it in body["input"] if it.get("type") == "additional_tools" for t in it.get("tools") or []]
    seen = [(g.get("name"), [s.get("name") for s in g.get("tools") or []]) for g in groups]
    return {"no_top_level_instructions": not body.get("instructions"), "no_top_level_tools": not body.get("tools"),
            "only_our_system_prompt": developer == [SYSTEM], "only_our_user_message": user == [prompt],
            "only_our_tools_plus_request_user_input": seen == [("functions", sorted([BUILTIN_CALL, *tool_names]))]
            or seen == [("functions", [BUILTIN_CALL, *tool_names])],
            "parallel_tool_calls_off": body.get("parallel_tool_calls") is False,
            "api_default_verbosity": (body.get("text") or {}).get("verbosity") == "medium"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--codex-home", type=Path, default=DEFAULT_HOME)
    p.add_argument("--model", default="gpt-6.1-sol")
    p.add_argument("--report", type=Path, default=None)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockResponses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    mock = ['model_provider="harness_audit"', 'model_providers.harness_audit.name="harness audit mock"',
            f'model_providers.harness_audit.base_url="{base}"', 'model_providers.harness_audit.wire_api="responses"',
            "model_providers.harness_audit.requires_openai_auth=false",
            "model_providers.harness_audit.supports_websockets=false"]
    provider = CodexAppServerProvider(args.model, "high", args.codex_home,
                                      extra_args=tuple(a for c in mock for a in ("-c", c)))
    tools = tool_schemas(list(TOOLS))
    names = [t["function"]["name"] for t in tools]
    checks, firsts = {}, {}
    for scenario in ("call", "text", "builtin"):
        prompt = f"## Task\nSCENARIO {scenario}: check the release notes."
        reply = provider.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                  tools, Sampling())
        time.sleep(2.0)  # a second request, if Codex were to send one, has time to arrive
        sent = [r["body"] for r in REQUESTS if r["scenario"] == scenario]
        firsts[scenario] = sent[0] if sent else None
        got = {f"{scenario}:{k}": v for k, v in request_checks(sent[0], prompt, names).items()} if sent else {}
        got[f"{scenario}:usage_reported"] = reply.usage.get("total_tokens") == 1020
        if scenario == "call":
            got["call:reply_is_the_tool_call"] = [(c.name, c.arguments) for c in reply.tool_calls] == [
                ("read_file", {"path": "notes/plan.md"})]
            got["call:no_second_request"] = len(sent) == 1
        elif scenario == "text":
            got["text:reply_is_text_only"] = reply.text == "release looks fine" and not reply.tool_calls
        else:
            got["builtin:reported_as_request_user_input"] = [c.name for c in reply.tool_calls] == [BUILTIN_CALL]
        checks.update(got)
    verdict = "PASS" if checks and all(checks.values()) else "FAIL"
    report = {"verdict": verdict, "model": args.model, "codex_home": str(args.codex_home), "checks": checks,
              "first_requests": firsts}
    if args.report:
        args.report.write_text(json.dumps(report, indent=1))
    print(json.dumps({"verdict": verdict, "failed": [k for k, v in checks.items() if not v]}))
    server.shutdown()
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
