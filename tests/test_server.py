"""End-to-end proxy tests against a stub upstream.

The key property under test: once a session is compressed, the SAME request
history produces byte-identical bodies every time (frozen summaries — the
provider prompt cache must not thrash). All three wire protocols run the
same scenario.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from awecompress import compress as compress_mod
from awecompress import server, summarize as summarize_mod
from awecompress.config import Config
from awecompress.integrate import Compressor

# Tiny thresholds so ordinary fixtures cross them; the reused body of the
# extended session (~930 est. tokens) must also sit above threshold.
CFG = dict(threshold_tokens=700, keep_recent_turns=2, min_span_tokens=100)


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def big_history(turns=6, chunk="x" * 600):
    messages = []
    for i in range(turns):
        messages.append(user(f"turn {i} {chunk}"))
        messages.append(assistant(f"reply {i} {chunk}"))
    return messages


class Upstream:
    """Stub upstream recording every completion body it receives, on the
    three protocol paths."""
    def __init__(self):
        self.bodies = []
        self.paths = []

    def app(self):
        up = web.Application()

        async def record(request):
            self.paths.append(request.path_qs)
            try:
                self.bodies.append(await request.json())
            except Exception:
                self.bodies.append(None)
            if request.path == "/v1/chat/completions":
                return web.json_response({"choices": [{"message": {"content": "upstream reply"}}]})
            if request.path == "/v1/responses":
                return web.json_response({"output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "upstream reply"}]}]})
            return web.json_response({"content": [{"type": "text", "text": "upstream reply"}]})

        async def models(request):
            return web.json_response({"data": [{"id": "m"}]})

        up.router.add_post("/v1/messages", record)
        up.router.add_post("/v1/messages/count_tokens", record)
        up.router.add_post("/v1/chat/completions", record)
        up.router.add_post("/v1/responses", record)
        up.router.add_get("/v1/models", models)
        return up


@pytest.fixture
async def client(tmp_path, monkeypatch):
    upstream = Upstream()
    up_server = TestServer(upstream.app())
    await up_server.start_server()

    calls = {"n": 0}

    async def fake_summarize(sender, protocol, model, prev_summary, messages, cfg):
        calls["n"] += 1
        return f"SUMMARY#{calls['n']}" + (f" (merged: {prev_summary})" if prev_summary else "")

    monkeypatch.setattr(summarize_mod, "summarize", fake_summarize)

    cfg = replace(Config(), upstream=str(up_server.make_url("/")),
                  db_path=str(tmp_path / "summaries.db"), **CFG)
    app = server.create_app(cfg, Compressor(cfg.db_path))
    tc = TestClient(TestServer(app))
    await tc.start_server()

    yield SimpleNamespace(client=tc, upstream=upstream, up_server=up_server,
                          calls=calls, cfg=cfg)

    await tc.close()
    await up_server.close()


def body_for(messages):
    return {"model": "claude-x", "system": "be brief", "messages": messages,
            "max_tokens": 100, "stream": False}


def chat_body_for(messages):
    return {"model": "chat-x", "messages": messages, "stream": False}


def responses_body_for(items):
    return {"model": "resp-x", "instructions": "be brief", "input": items, "stream": False}


class TestPassthrough:
    async def test_small_body_forwarded_identical(self, client):
        resp = await client.client.post("/v1/messages", json=body_for(
            [user("hi"), assistant("hello")]))
        assert resp.status == 200
        assert client.upstream.bodies[-1]["messages"] == [user("hi"), assistant("hello")]
        assert resp.headers.get("x-awecompress") is None

    async def test_off_header_bypasses(self, client):
        big = body_for(big_history())
        resp = await client.client.post("/v1/messages", json=big,
                                        headers={"x-awecompress": "off"})
        assert resp.status == 200
        assert client.upstream.bodies[-1]["messages"] == big["messages"]
        assert client.calls["n"] == 0

    async def test_invalid_json_relayed(self, client):
        resp = await client.client.post("/v1/messages", data=b"not json{",
                                        headers={"content-type": "application/json"})
        assert resp.status == 200
        assert client.calls["n"] == 0

    async def test_other_paths_relayed(self, client):
        resp = await client.client.get("/v1/models")
        assert resp.status == 200
        assert (await resp.json()) == {"data": [{"id": "m"}]}

    async def test_query_string_preserved(self, client):
        await client.client.post("/v1/messages?beta=true", json=body_for(
            [user("hi"), assistant("hello")]))
        assert client.upstream.paths[-1] == "/v1/messages?beta=true"
        await client.client.post("/v1/messages/count_tokens?beta=true", json=body_for(
            [user("hi"), assistant("hello")]))
        assert client.upstream.paths[-1] == "/v1/messages/count_tokens?beta=true"


class TestCompress:
    async def test_init_replaces_span_with_summary(self, client):
        resp = await client.client.post("/v1/messages", json=body_for(big_history()))
        assert resp.status == 200
        assert resp.headers["x-awecompress"] == "init"
        assert client.calls["n"] == 1
        sent = client.upstream.bodies[-1]["messages"]
        # One synthetic summary message + the kept recent turns
        assert sent[0]["content"][0]["text"].startswith(compress_mod.SUMMARY_MARKER)
        assert sent[0]["content"][0]["text"].count("SUMMARY#1") == 1
        assert sent[1]["content"].startswith("turn 4")
        # the model and system pass through untouched
        assert client.upstream.bodies[-1]["model"] == "claude-x"
        assert client.upstream.bodies[-1]["system"] == "be brief"

    async def test_reuse_is_byte_identical_no_new_calls(self, client):
        body = body_for(big_history())
        first = await client.client.post("/v1/messages", json=body)
        assert first.headers["x-awecompress"] == "init"
        second = await client.client.post("/v1/messages", json=body)
        assert second.headers["x-awecompress"] == "reuse"
        assert client.calls["n"] == 1  # frozen — no second LLM call
        assert client.upstream.bodies[-1] == client.upstream.bodies[-2]  # same bytes

    async def test_extend_after_growth(self, client):
        body = body_for(big_history())
        await client.client.post("/v1/messages", json=body)
        # grow the session past the threshold again
        body["messages"] += [user("turn 9 " + "y" * 600), assistant("reply 9 " + "y" * 600)]
        resp = await client.client.post("/v1/messages", json=body)
        assert resp.headers["x-awecompress"] == "extend"
        assert client.calls["n"] == 2
        text = client.upstream.bodies[-1]["messages"][0]["content"][0]["text"]
        assert "merged: SUMMARY#1" in text  # prev summary fed to the merger

    async def test_summary_failure_reuses_existing_frozen_summary(self, client, monkeypatch):
        body = body_for(big_history())
        first = await client.client.post("/v1/messages", json=body)
        assert first.headers["x-awecompress"] == "init"
        before_calls = client.calls["n"]

        async def boom(*args, **kwargs):
            raise summarize_mod.SummaryError("upstream summarizer down")

        monkeypatch.setattr(summarize_mod, "summarize", boom)
        body["messages"] += [user("turn 9 " + "y" * 600),
                              assistant("reply 9 " + "y" * 600)]
        resp = await client.client.post("/v1/messages", json=body)
        assert resp.status == 200
        assert resp.headers["x-awecompress"] == "reuse"
        assert client.calls["n"] == before_calls
        sent = client.upstream.bodies[-1]["messages"]
        assert compress_mod.SUMMARY_MARKER in sent[0]["content"][0]["text"]
        assert sent[-1]["content"].startswith("reply 9")
        assert all("turn 0" not in str(message) for message in sent[1:])

    async def test_summary_failure_fails_open(self, tmp_path, monkeypatch):
        upstream = Upstream()
        up_server = TestServer(upstream.app())
        await up_server.start_server()

        async def boom(*args, **kwargs):
            raise summarize_mod.SummaryError("upstream summarizer down")

        monkeypatch.setattr(summarize_mod, "summarize", boom)
        cfg = replace(Config(), upstream=str(up_server.make_url("/")),
                      db_path=str(tmp_path / "s.db"), **CFG)
        app = server.create_app(cfg, Compressor(cfg.db_path))
        tc = TestClient(TestServer(app))
        await tc.start_server()

        body = body_for(big_history())
        resp = await tc.post("/v1/messages", json=body)
        assert resp.status == 200  # uncompressed, but delivered
        assert upstream.bodies[-1]["messages"] == body["messages"]
        assert resp.headers.get("x-awecompress") is None
        await tc.close()
        await up_server.close()

    async def test_count_tokens_never_summarizes(self, client):
        await client.client.post("/v1/messages", json=body_for(big_history()))
        before = client.calls["n"]
        await client.client.post("/v1/messages/count_tokens", json=body_for(big_history()))
        assert client.calls["n"] == before
        # ...but the frozen summary is applied so counts match the real request
        sent = client.upstream.bodies[-1]["messages"]
        assert len(sent) < len(big_history())

    async def test_status_endpoint(self, client):
        resp = await client.client.get("/")
        data = await resp.json()
        assert data["service"] == "awecompress"
        assert data["stats"]["calls"] == 0


class TestOpenAIChat:
    async def test_compresses_and_keeps_system_preamble(self, client):
        body = chat_body_for([{"role": "system", "content": "standing orders"}]
                             + big_history())
        resp = await client.client.post("/v1/chat/completions", json=body)
        assert resp.status == 200
        assert resp.headers["x-awecompress"] == "init"
        assert client.calls["n"] == 1
        sent = client.upstream.bodies[-1]["messages"]
        # Standing instructions stay a message; then one summary; then kept turns
        assert sent[0] == {"role": "system", "content": "standing orders"}
        assert sent[1]["role"] == "user"
        assert compress_mod.SUMMARY_MARKER in sent[1]["content"]
        assert sent[2]["content"].startswith("turn 4")

    async def test_reuse_byte_identical(self, client):
        body = chat_body_for([{"role": "system", "content": "so"}] + big_history())
        await client.client.post("/v1/chat/completions", json=body)
        await client.client.post("/v1/chat/completions", json=body)
        assert client.calls["n"] == 1
        assert client.upstream.bodies[-1] == client.upstream.bodies[-2]


class TestResponses:
    def _items(self):
        items = []
        for i in range(6):
            items.append({"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": f"turn {i} " + "x" * 600}]})
            items.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": f"reply {i} " + "x" * 600}]})
        return items

    async def test_string_input_is_transparent(self, client):
        body = responses_body_for("hello")
        resp = await client.client.post("/v1/responses", json=body)
        assert resp.status == 200
        assert client.upstream.bodies[-1]["input"] == "hello"
        assert client.calls["n"] == 0
        assert resp.headers.get("x-awecompress") is None

    async def test_compresses(self, client):
        resp = await client.client.post("/v1/responses", json=responses_body_for(self._items()))
        assert resp.status == 200
        assert resp.headers["x-awecompress"] == "init"
        assert client.calls["n"] == 1
        sent = client.upstream.bodies[-1]["input"]
        assert sent[0]["type"] == "message" and sent[0]["role"] == "user"
        assert compress_mod.SUMMARY_MARKER in sent[0]["content"][0]["text"]
        assert sent[1]["content"][0]["text"].startswith("turn 4")
