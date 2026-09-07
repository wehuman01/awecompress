"""The summarize call: prompt shape, response parsing, failure modes.

Uses a fake ClientSession — the network layer is exercised in
test_server.py against a stub upstream.
"""

from types import SimpleNamespace

import pytest

from awecompress import summarize
from awecompress.summarize import SummaryError, summarize as summarize_fn

CFG = SimpleNamespace(transcript_result_cap=100, summary_max_tokens=512,
                      summary_timeout_seconds=30)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    """Records the request; answers with a canned response."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


def ok(text):
    return FakeResponse(payload={"content": [{"type": "text", "text": text}]})


def messages():
    return [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": "reading src/a.py"},
    ]


async def test_prompt_contains_prev_summary_and_transcript():
    session = FakeSession(ok("merged summary"))
    out = await summarize_fn(session, "http://up/v1/messages", {"x-api-key": "k"},
                             "model-x", "earlier summary text", messages(), CFG)
    assert out == "merged summary"
    body = session.calls[0]["json"]
    assert body["model"] == "model-x"
    assert body["stream"] is False
    assert body["system"] == summarize.SYSTEM_PROMPT
    prompt = body["messages"][0]["content"]
    assert "earlier summary text" in prompt
    assert "fix the bug" in prompt
    assert "Transcript segment to compress now" in prompt
    assert session.calls[0]["url"] == "http://up/v1/messages"


async def test_prompt_without_prev_summary():
    session = FakeSession(ok("fresh summary"))
    await summarize_fn(session, "http://up/v1/messages", {}, "m", "", messages(), CFG)
    prompt = session.calls[0]["json"]["messages"][0]["content"]
    assert "Transcript to compress" in prompt
    assert "Summary of the conversation so far" not in prompt


async def test_http_error_raises():
    session = FakeSession(FakeResponse(status=500, text="boom"))
    with pytest.raises(SummaryError, match="HTTP 500"):
        await summarize_fn(session, "http://up/v1/messages", {}, "m", "", messages(), CFG)


async def test_empty_summary_raises():
    session = FakeSession(ok("   "))
    with pytest.raises(SummaryError, match="no text"):
        await summarize_fn(session, "http://up/v1/messages", {}, "m", "", messages(), CFG)


async def test_joins_multiple_text_blocks():
    resp = FakeResponse(payload={"content": [
        {"type": "text", "text": "part one. "},
        {"type": "text", "text": "part two"},
    ]})
    out = await summarize_fn(FakeSession(resp), "http://up/v1/messages", {}, "m", "", messages(), CFG)
    assert out == "part one. part two"
