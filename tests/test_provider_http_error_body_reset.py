"""Full-grid crash 2026-10-14: an HTTP 500 whose body read hit a connection reset escaped the
provider as ConnectionResetError and stopped the grid. Reading the body must be best-effort."""

import io
import json
import urllib.error

import pytest

from correctable_agency.models import openai_compatible_chat_provider as mod
from correctable_agency.models.model_gateway_protocol import ModelUnavailable, Sampling


class _ResetBody(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        raise ConnectionResetError(104, "Connection reset by peer")


def _http_500():
    return urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, io.BufferedReader(_ResetBody()))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


OK = {"model": "llm", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}


def _patch(monkeypatch, outcomes):
    calls = iter(outcomes)

    def fake_urlopen(req, timeout):
        o = next(calls)
        if isinstance(o, Exception):
            raise o
        return _Resp(json.dumps(o).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod, "BACKOFF_S", 0.0)


def test_reset_while_reading_500_body_is_retried(monkeypatch):
    _patch(monkeypatch, [_http_500(), OK])
    reply = mod.OpenAICompatibleChatProvider("http://x", "llm").complete([], [], Sampling())
    assert reply.text == "hi"


def test_repeated_resets_become_model_unavailable(monkeypatch):
    _patch(monkeypatch, [_http_500() for _ in range(mod.RETRIES)])
    with pytest.raises(ModelUnavailable, match="unreadable"):
        mod.OpenAICompatibleChatProvider("http://x", "llm").complete([], [], Sampling())
