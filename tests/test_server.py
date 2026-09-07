"""End-to-end proxy tests against a stub upstream.

The key property under test: once a session is compressed, the SAME request
history produces byte-identical bodies every time (frozen summaries — the
provider prompt cache must not thrash).
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from awecompress import server
from awecompress.config import Config
from awecompress.store import Store

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
    """Stub upstream recording every /v1/messages body it receives."""
    def __init__(self):
        self.bodies = []
        self.paths = []

    def app(self):
        up = web.Application()

        async def messages(request):
            self.paths.append(request.path_qs)
            try:
                self.bodies.append(await request.json())
            except Exception:
                self.bodies.append(None)
            return web.json_response({"content": [{"type": "text", "text": "upstream reply"}]})

        async def models(request):
            return web.json_response({"data": [{"id": "m"}]})

        up.router.add_post("/v1/messages", messages)
        up.router.add_post("/v1/messages/count_tokens", messages)
        up.router.add_get("/v1/models", models)
        return up


@pytest.fixture
async def client(tmp_path, monkeypatch):
    upstream = Upstream()
    up_server = TestServer(upstream.app())
    await up_server.start_server()

    calls = {"n": 0}

    async def fake_summarize(session, url, headers, model, prev_summary, messages, cfg):
        calls["n"] += 1
        return f"SUMMARY#{calls['n']}" + (f" (merged: {prev_summary})" if prev_summary else "")

    monkeypatch.setattr(server.summarize, "summarize", fake_summarize)

    cfg = replace(Config(), upstream=str(up_server.make_url("/")),
                  db_path=str(tmp_path / "summaries.db"), **CFG)
    app = server.create_app(cfg, Store(cfg.db_path))
    tc = TestClient(TestServer(app))
    await tc.start_server()

    yield SimpleNamespace(client=tc, upstream=upstream, up_server=up_server,
                          calls=calls, cfg=cfg)

    await tc.close()
    await up_server.close()


def body_for(messages):
    return {"model": "claude-x", "system": "be brief", "messages": messages,
            "max_tokens": 100, "stream": False}


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
        assert sent[0]["content"][0]["text"].startswith(server.compress.SUMMARY_MARKER)
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

    async def test_summary_failure_fails_open(self, tmp_path, monkeypatch):
        upstream = Upstream()
        up_server = TestServer(upstream.app())
        await up_server.start_server()

        async def boom(*args, **kwargs):
            raise server.summarize.SummaryError("upstream summarizer down")

        monkeypatch.setattr(server.summarize, "summarize", boom)
        cfg = replace(Config(), upstream=str(up_server.make_url("/")),
                      db_path=str(tmp_path / "s.db"), **CFG)
        app = server.create_app(cfg, Store(cfg.db_path))
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
