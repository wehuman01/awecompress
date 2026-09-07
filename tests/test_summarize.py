"""The summarize call: prompt shape, response parsing, failure modes.

Uses a fake sender — the network layer is exercised in test_server.py
against a stub upstream. Sender injection is the design under test too:
the core must not care where the call physically goes.
"""

from types import SimpleNamespace

import pytest

from awecompress import summarize
from awecompress.summarize import SummaryError, summarize as summarize_fn

CFG = SimpleNamespace(transcript_result_cap=100, summary_max_tokens=512,
                      summary_timeout_seconds=30,
                      protected_tools=("task", "todowrite"),
                      protected_file_patterns=())


class FakeSender:
    """Records the request body; answers with a canned response payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __call__(self, body):
        self.calls.append(body)
        return self.payload


def ok(text):
    return {"content": [{"type": "text", "text": text}]}


def messages():
    return [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": "reading src/a.py"},
    ]


async def test_prompt_contains_prev_summary_and_transcript():
    sender = FakeSender(ok("merged summary"))
    out = await summarize_fn(sender, "anthropic", "model-x",
                             "earlier summary text", messages(), CFG)
    assert out == "merged summary"
    body = sender.calls[0]
    assert body["model"] == "model-x"
    assert body["stream"] is False
    assert body["system"] == summarize.SYSTEM_PROMPT
    prompt = body["messages"][0]["content"]
    assert "earlier summary text" in prompt
    assert "fix the bug" in prompt
    assert "Transcript segment to compress now" in prompt


async def test_prompt_without_prev_summary():
    sender = FakeSender(ok("fresh summary"))
    await summarize_fn(sender, "anthropic", "m", "", messages(), CFG)
    prompt = sender.calls[0]["messages"][0]["content"]
    assert "Transcript to compress" in prompt
    assert "Summary of the conversation so far" not in prompt


async def test_protection_instruction_in_prompt():
    assert "[protected]" in summarize.SYSTEM_PROMPT
    assert "planning state" in summarize.SYSTEM_PROMPT


async def test_sender_failure_raises():
    async def boom(body):
        raise RuntimeError("HTTP 500: boom")

    with pytest.raises(SummaryError, match="summary call failed"):
        await summarize_fn(boom, "anthropic", "m", "", messages(), CFG)


async def test_empty_summary_raises():
    with pytest.raises(SummaryError, match="no text"):
        await summarize_fn(FakeSender(ok("   ")), "anthropic", "m", "", messages(), CFG)


async def test_joins_multiple_text_blocks():
    payload = {"content": [
        {"type": "text", "text": "part one. "},
        {"type": "text", "text": "part two"},
    ]}
    out = await summarize_fn(FakeSender(payload), "anthropic", "m", "", messages(), CFG)
    assert out == "part one. part two"


async def test_openai_chat_request_and_response_shapes():
    sender = FakeSender({"choices": [{"message": {"content": "chat summary"}}]})
    out = await summarize_fn(sender, "openai-chat", "m", "", messages(), CFG)
    assert out == "chat summary"
    body = sender.calls[0]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == summarize.SYSTEM_PROMPT


async def test_responses_request_and_response_shapes():
    sender = FakeSender({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "responses summary"}]}]})
    out = await summarize_fn(sender, "openai-responses", "m", "", messages(), CFG)
    assert out == "responses summary"
    body = sender.calls[0]
    assert body["instructions"] == summarize.SYSTEM_PROMPT
    assert body["input"][0]["content"][0]["type"] == "input_text"
