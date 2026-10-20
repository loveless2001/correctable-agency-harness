"""Fake `codex app-server` for CPU tests of the GPT provider: newline-delimited JSON-RPC on stdio.

Each turn plays the next scenario of $FAKE_CODEX_SCENARIOS (JSON list; the last one repeats), counted across
restarts through the record file $FAKE_CODEX_RECORD, where every message in and the argv/env are logged:
Raw events as Codex sends them with experimentalRawEvents (the model's output items, then rawResponse/completed
with usage); turns that end on interrupt send thread/tokenUsage/updated only if "usage still due".
- call:     commentary text + a `read_file` call (also an item/tool/call request), then the response completes
- badargs:  same, with malformed JSON arguments
- text:     a final message; turn/completed "completed"
- builtin:  a request_user_input call, the response completes, then a second response starts (must be ignored)
- retry:    a call, then a stream error with willRetry, then as call (the first call must be dropped)
- nousage:  a call, and the turn ends without rawResponse/completed
- fail:     turn/completed "failed"
- crash:    exit right after turn/start
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


def raw_item(tid: str, turn: str, item: dict) -> None:
    send({"method": "rawResponseItem/completed", "params": {"threadId": tid, "turnId": turn, "item": item}})


def raw_done(tid: str, turn: str) -> None:
    send({"method": "rawResponse/completed", "params": {"threadId": tid, "turnId": turn, "responseId": "resp",
                                                        "usage": USAGE}})


def call_item(name: str, arguments: str, call_id: str) -> dict:
    return {"type": "function_call", "id": f"fc-{call_id}", "name": name, "arguments": arguments, "call_id": call_id}


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
            raw_item(tid, turn, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]})
            if scenario == "retry":  # a stream that drops after the call: Codex reconnects and re-sends
                raw_item(tid, turn, call_item("read_file", '{"path": "stale"}', "call-stale"))
                send({"method": "error", "params": {"threadId": tid, "turnId": turn, "willRetry": True,
                                                    "error": {"message": "Reconnecting... 1/5"}}})
                scenario = "call"
            if scenario in ("call", "badargs"):
                raw_item(tid, turn, {"type": "message", "role": "assistant", "phase": "commentary",
                                     "content": [{"type": "output_text", "text": "Checking the plan. "}]})
                args = '{"path": "notes/plan.md"}' if scenario == "call" else '{"args": -la}'
                raw_item(tid, turn, call_item("read_file", args, f"call-{tid}"))
                send({"id": f"srv-{tid}", "method": "item/tool/call", "params": {
                    "threadId": tid, "turnId": turn, "callId": f"call-{tid}", "tool": "read_file", "arguments": {}}})
                raw_done(tid, turn)  # the response completes after the call was dispatched (the smoke's race)
                waiting[turn] = (tid, True)
            elif scenario == "text":
                raw_item(tid, turn, {"type": "message", "role": "assistant", "phase": "final_answer",
                                     "content": [{"type": "output_text", "text": "all clear"}]})
                raw_done(tid, turn)
                usage(tid, turn)
                done(tid, turn, "completed")
            elif scenario == "builtin":  # Codex answers request_user_input itself and asks the model again at once
                raw_item(tid, turn, call_item("request_user_input", '{"questions": []}', f"call-{tid}"))
                raw_done(tid, turn)
                raw_item(tid, turn, call_item("finish", '{"summary": "second response"}', "call-second"))
                waiting[turn] = (tid, False)
            elif scenario == "nousage":
                raw_item(tid, turn, call_item("read_file", '{"path": "x"}', f"call-{tid}"))
                done(tid, turn, "interrupted")
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
