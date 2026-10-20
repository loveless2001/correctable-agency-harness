"""Fake `codex app-server` for CPU tests of the GPT provider: newline-delimited JSON-RPC on stdio.

Each turn plays the next scenario of $FAKE_CODEX_SCENARIOS (JSON list; the last one repeats), counted across
restarts through the record file $FAKE_CODEX_RECORD, where every message in and the argv/env are logged:
- call:    the model calls `read_file` (an item/tool/call request); usage and turn/completed only after interrupt
- badargs: same, with a non-object `arguments`
- text:    a final agentMessage, usage, turn/completed "completed"
- builtin: usage with no item (Codex answered its own request_user_input); turn/completed after interrupt
- fail:    turn/completed "failed"
- crash:   exit right after turn/start
- rerouted: model/rerouted to gpt-6-luna, then as call
"""

import json
import os
import sys

RECORD = os.environ["FAKE_CODEX_RECORD"]
SCENARIOS = json.loads(os.environ.get("FAKE_CODEX_SCENARIOS", '["call"]'))
USAGE = {"totalTokens": 1234, "inputTokens": 1200, "cachedInputTokens": 800, "outputTokens": 34,
         "reasoningOutputTokens": 20}


def log(entry: dict) -> None:
    with open(RECORD, "a") as f:
        f.write(json.dumps(entry) + "\n")


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def turns_so_far() -> int:
    with open(RECORD) as f:
        return sum(1 for line in f if json.loads(line).get("method") == "turn/start")


def usage(tid: str, turn: str) -> None:
    send({"method": "thread/tokenUsage/updated", "params": {"threadId": tid, "turnId": turn,
                                                            "tokenUsage": {"total": USAGE, "last": USAGE}}})


def done(tid: str, turn: str, status: str, error=None) -> None:
    send({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": turn, "status": status,
                                                                          "error": error, "items": []}}})


def main() -> None:
    log({"argv": sys.argv[1:], "codex_home": os.environ.get("CODEX_HOME"),
         "api_key_seen": bool(os.environ.get("OPENAI_API_KEY")), "cwd_files": os.listdir(os.getcwd())})
    n_threads = 0
    waiting: dict[str, tuple[str, bool]] = {}  # turn id -> (thread id, usage still due): turns that end on interrupt
    for line in sys.stdin:
        m = json.loads(line)
        log(m)
        method, params, rid = m.get("method"), m.get("params") or {}, m.get("id")
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            n_threads += 1
            send({"id": rid, "result": {"thread": {"id": f"thr-{n_threads}", "model": params.get("model")}}})
        elif method == "turn/start":
            tid, turn = params["threadId"], f"turn-{params['threadId']}"
            scenario = SCENARIOS[min(turns_so_far() - 1, len(SCENARIOS) - 1)]
            send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress"}}})
            send({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 7}}}})
            if scenario == "crash":
                sys.exit(1)
            if scenario == "rerouted":
                send({"method": "model/rerouted", "params": {"threadId": tid, "turnId": turn, "fromModel": "gpt-6.1-sol",
                                                             "toModel": "gpt-6-luna", "reason": "capacity"}})
                scenario = "call"
            if scenario in ("call", "badargs"):
                args = {"path": "notes/plan.md"} if scenario == "call" else "-la"
                send({"id": f"srv-{tid}", "method": "item/tool/call", "params": {
                    "threadId": tid, "turnId": turn, "callId": f"call-{tid}", "tool": "read_file", "arguments": args}})
                waiting[turn] = (tid, True)
            elif scenario == "text":
                send({"method": "item/completed", "params": {"threadId": tid, "turnId": turn, "item": {
                    "type": "agentMessage", "id": "msg", "text": "all clear", "phase": "final_answer"}}})
                usage(tid, turn)
                done(tid, turn, "completed")
            elif scenario == "builtin":
                usage(tid, turn)
                waiting[turn] = (tid, False)
            elif scenario == "fail":
                done(tid, turn, "failed", {"message": "stream disconnected"})
        elif method == "turn/interrupt":
            send({"id": rid, "result": {}})
            tid, due = waiting.pop(params["turnId"], (None, False))
            if tid is not None:
                if due:
                    usage(tid, params["turnId"])
                done(tid, params["turnId"], "interrupted")
        elif rid is not None and method:
            send({"id": rid, "result": {}})


if __name__ == "__main__":
    main()
