"""Deterministic provider for CPU tests and dry runs: replays scripted replies.

A script is a list of turns; each turn is a list of (tool name, arguments) pairs
or a plain string (text with no tool call). After the script ends it calls
`finish`, so an episode always terminates.
"""

from correctable_agency.models.model_gateway_protocol import ModelReply, Sampling, ToolCall

Turn = list[tuple[str, dict]] | str


class ScriptedStubProvider:
    def __init__(self, script: list[Turn], model_id: str = "stub-scripted"):
        self._script, self._i, self._model = list(script), 0, model_id
        self.seen: list[list[dict]] = []  # messages of every call, for assertions

    def complete(self, messages: list[dict], tools: list[dict], sampling: Sampling) -> ModelReply:
        self.seen.append(messages)
        turn: Turn = self._script[self._i] if self._i < len(self._script) else [("finish", {"summary": "done"})]
        self._i += 1
        if isinstance(turn, str):
            return ModelReply(turn, [], self._model, {"prompt_tokens": 0, "completion_tokens": 0})
        calls = [ToolCall(f"call_{self._i}_{n}", name, args) for n, (name, args) in enumerate(turn)]
        return ModelReply("", calls, self._model, {"prompt_tokens": 0, "completion_tokens": 0})
