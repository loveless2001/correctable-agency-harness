"""OpenAI-compatible /chat/completions provider (vLLM on Modal, or any compatible server).

Stdlib only. Transient failures (connection errors, 5xx, 429) are retried with
backoff; after that the call raises ModelUnavailable, which the runner records
as an infrastructure error for the episode, never as model behaviour.

Hosted APIs (Phase 2G, e.g. Z.ai GLM-5.3-Flash) use the optional arguments; their defaults keep the vLLM request
byte-identical: `chat_url` (the full endpoint, when it is not <base_url>/v1/chat/completions), `extra_body` (extra
request fields such as reasoning_effort), `send_seed` (off for APIs that reject a seed) and `on_usage` (called
with each response's usage, so a token-budget billing guard can charge it).
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from correctable_agency.models.model_gateway_protocol import (
    BudgetExhausted, ModelReply, ModelUnavailable, Sampling, ToolCall,
)

RETRIES = 3
BACKOFF_S = 5.0
TIMEOUT_S = 300  # one call can never outlast the billing guard's in-flight reserve unnoticed


def parse_tool_calls(message: dict) -> list[ToolCall]:
    calls = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args, err = (json.loads(raw) if isinstance(raw, str) else dict(raw)), ""
            if not isinstance(args, dict):
                args, err = {}, f"arguments are not an object: {raw!r}"[:500]
        except json.JSONDecodeError as e:
            args, err = {}, f"unparseable arguments ({e}): {raw!r}"[:500]
        calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name") or "", args, err))
    return calls


class OpenAICompatibleChatProvider:
    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY",
                 budget_ok: Callable[[], bool] = lambda: True, chat_url: str = "", extra_body: dict | None = None,
                 send_seed: bool = True, on_usage: Callable[[dict], None] | None = None):
        self._url = chat_url or base_url.rstrip("/") + "/v1/chat/completions"
        self._model, self._key, self._budget_ok = model, api_key, budget_ok
        self._extra, self._send_seed, self._on_usage = dict(extra_body or {}), send_seed, on_usage

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
        last = ""
        for attempt in range(RETRIES):
            if not self._budget_ok():
                raise BudgetExhausted("billing guard: no budget for another model call")
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                try:
                    body = e.read()[:300]
                except Exception:  # the error body is diagnostic only; a reset while reading it stays transient
                    body = b"<unreadable>"
                last = f"HTTP {e.code}: {body!r}"
                if e.code < 500 and e.code != 429:
                    raise ModelUnavailable(last) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = str(e)
                if self._on_usage is not None:  # a lost response may still bill (codex re-review 261023): charge
                    self._on_usage({})  # the full reserve
            time.sleep(BACKOFF_S * 2 ** attempt)
        raise ModelUnavailable(f"{RETRIES} attempts failed: {last}")

    def complete(self, messages: list[dict], tools: list[dict], sampling: Sampling) -> ModelReply:
        body = {"model": self._model, "messages": messages, "temperature": sampling.temperature,
                "top_p": sampling.top_p, "max_tokens": sampling.max_tokens}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        if sampling.seed is not None and self._send_seed:
            body["seed"] = sampling.seed
        body.update(self._extra)
        t0 = time.time()
        data = self._post(body)
        if self._on_usage is not None:
            self._on_usage(data.get("usage") or {})
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return ModelReply(
            text=msg.get("content") or "", tool_calls=parse_tool_calls(msg),
            model_id=data.get("model", self._model), usage=data.get("usage") or {},
            latency_s=time.time() - t0, finish_reason=choice.get("finish_reason") or "",
            reasoning=msg.get("reasoning_content") or msg.get("reasoning") or "",
        )
