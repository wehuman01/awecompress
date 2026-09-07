"""aiohttp proxy: transform Anthropic Messages request bodies, relay every
response byte untouched.

Position in the stack: harness → awecompress → upstream (usually awerouter)
→ provider. Auth headers pass through untouched — awecompress never owns
credentials; routing and failover stay where they already live. Any failure
inside the transform path forwards the original body (fail-open): a
compression problem must never break the session.

v1 speaks Anthropic Messages only. Requests to any other path, or bodies we
cannot parse, are relayed untouched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace

import aiohttp
from aiohttp import web

from awecompress import __version__, compress, summarize
from awecompress.store import SessionRecord, Store

# Headers forwarded to the upstream. Host/Content-Length/Encoding are
# hop-by-hop or body-bound and left to aiohttp; everything not listed
# (cookies, acceptance headers, client-specific extras) is dropped.
PASS_HEADERS = frozenset({
    "anthropic-version", "anthropic-beta", "authorization", "x-api-key",
    "x-request-id", "traceparent", "tracestate", "user-agent",
})

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

# Claude Code histories run to several MB; the aiohttp default of 1MB would
# 413 the exact requests this proxy exists to shrink.
CLIENT_MAX_SIZE = 128 * 1024 * 1024


def _forward_headers(request: web.Request) -> dict:
    out = {}
    for k, v in request.headers.items():
        if k.lower() in PASS_HEADERS:
            out[k] = v
    return out


def _upstream_url(cfg, path: str) -> str:
    return cfg.upstream.rstrip("/") + path


# ---------------------------------------------------------------------------
# Request transformation
# ---------------------------------------------------------------------------

async def _transform(request, body: dict, store: Store, allow_summary: bool):
    """Apply compression planning to one parsed request body.

    Returns (messages, mode) — messages is the rewritten list, or None when
    the body should be forwarded unchanged. Never raises.
    """
    cfg = request.app["cfg"]
    session: aiohttp.ClientSession = request.app["session"]
    try:
        stored = store.get(compress.session_key(body))
        p = compress.plan(body, stored, cfg)
        if p.action == "passthrough":
            return None, "passthrough"

        if p.action == "reuse":
            print(f"[awecompress] {p.key}: applied frozen summary "
                  f"(messages 0..{p.upto - 1}) — est {p.raw_tokens} -> {p.body_tokens} tokens")
            return _replaced(body, stored.summary, p.upto), "reuse"

        # init / extend — one summary call, then freeze the result
        if not allow_summary:
            # count_tokens must never mint a new summary (no surprise LLM
            # calls), but an existing one is applied so counts match.
            if stored is not None:
                return _replaced(body, stored.summary, stored.upto), "reuse"
            return None, "passthrough"

        model = cfg.summary_model or (body.get("model") or "")
        if not model:
            return None, "passthrough"

        t0 = time.monotonic()
        try:
            summary = await summarize.summarize(
                session, _upstream_url(cfg, MESSAGES_PATH), _forward_headers(request),
                model, p.prev_summary, body["messages"][p.base_upto:p.upto], cfg)
        except summarize.SummaryError as exc:
            # Fail-open: forward what we already have. With a stored summary
            # that means reuse (context stays small); without it, the
            # original body — next request will try again.
            print(f"[awecompress] {p.key}: summary call failed ({exc}); "
                  f"{'reusing previous summary' if stored is not None else 'forwarding uncompressed'}",
                  file=sys.stderr)
            if stored is not None:
                return _replaced(body, stored.summary, stored.upto), "reuse"
            return None, "passthrough"

        prev_summary_tokens = compress.estimate_tokens(p.prev_summary)
        summary_tokens = compress.estimate_tokens(summary)
        record = SessionRecord(
            key=p.key,
            upto=p.upto,
            prefix_hash=compress.prefix_hash(body["messages"], p.upto),
            summary=summary,
            saved_tokens=(stored.saved_tokens if stored is not None else 0)
                         + max(0, prev_summary_tokens + p.span_tokens - summary_tokens),
            calls=(stored.calls if stored is not None else 0) + 1,
            updated_at=time.time(),
        )
        store.put(record)
        store.log_event(p.key, p.action, p.span_tokens, summary_tokens, model)
        ms = (time.monotonic() - t0) * 1000
        replaced = _replaced(body, summary, p.upto)
        after_tokens = compress.estimate_body_tokens(body, replaced)
        print(f"[awecompress] {p.key}: {p.action} — summarized messages "
              f"{p.base_upto}..{p.upto - 1} (est {p.span_tokens} tok) into {summary_tokens} "
              f"via {model} in {ms:.0f}ms; body est {p.raw_tokens} -> {after_tokens} tokens")
        return replaced, p.action
    except Exception as exc:  # noqa: BLE001 — fail-open is the contract
        print(f"[awecompress] transform error: {exc}", file=sys.stderr)
        return None, "error"


def _replaced(body: dict, summary: str, upto: int) -> list:
    return [compress.summary_message(summary)] + list(body["messages"][upto:])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_messages(request: web.Request) -> web.StreamResponse:
    return await _proxy(request, allow_summary=True)


async def handle_count_tokens(request: web.Request) -> web.StreamResponse:
    return await _proxy(request, allow_summary=False)


async def _proxy(request: web.Request, allow_summary: bool) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    cfg = request.app["cfg"]
    # Forward the path the client asked for, query string included (?beta=true
    # and friends); rewriting to the bare route would silently drop it.
    path = request.path_qs

    raw = await request.read()
    payload = raw
    mode = "passthrough"
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        body = None

    if isinstance(body, dict) and isinstance(body.get("messages"), list) \
            and request.headers.get("x-awecompress", "").lower() != "off":
        messages, mode = await _transform(request, body, request.app["store"], allow_summary)
        if messages is not None:
            body = dict(body)
            body["messages"] = messages
            payload = json.dumps(body).encode()

    is_stream = bool(isinstance(body, dict) and body.get("stream"))
    timeout = aiohttp.ClientTimeout(
        connect=10,
        total=None if is_stream else 300,
        sock_read=None if is_stream else 300,
    )
    headers = _forward_headers(request)
    headers.setdefault("content-type", "application/json")
    return await _relay(request, session, cfg, request.method, path, payload,
                        headers, timeout, mode)


async def handle_relay(request: web.Request) -> web.StreamResponse:
    """Catch-all: anything we don't transform (other paths, other methods)
    is proxied as-is."""
    session: aiohttp.ClientSession = request.app["session"]
    cfg = request.app["cfg"]
    raw = await request.read()
    headers = _forward_headers(request)
    if request.headers.get("content-type"):
        headers.setdefault("content-type", request.headers["content-type"])
    return await _relay(request, session, cfg, request.method, request.path_qs,
                        raw, headers, aiohttp.ClientTimeout(connect=10, total=None), "passthrough")


async def handle_status(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    return web.json_response({
        "service": "awecompress",
        "version": __version__,
        "upstream": cfg.upstream,
        "stats": request.app["store"].stats(),
    })


async def _relay(request: web.Request, session: aiohttp.ClientSession, cfg,
                 method: str, path: str, payload: bytes, headers: dict,
                 timeout: aiohttp.ClientTimeout, mode: str) -> web.StreamResponse:
    """One upstream request, response bytes streamed back untouched."""
    try:
        up = await session.request(method, _upstream_url(cfg, path), data=payload,
                                   headers=headers, timeout=timeout,
                                   allow_redirects=False)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": {"type": "awecompress_upstream",
                                       "message": f"upstream error: {exc}"}}),
            content_type="application/json",
        )

    resp = web.StreamResponse(status=up.status)
    for h in ("content-type", "anthropic-version", "anthropic-beta", "x-request-id"):
        val = up.headers.get(h)
        if val:
            resp.headers[h] = val
    if mode != "passthrough":
        resp.headers["x-awecompress"] = mode

    try:
        await resp.prepare(request)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
        pass  # client hung up or upstream died mid-stream; nothing left to save
    finally:
        up.close()
    return resp


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

def create_app(cfg, store: Store) -> web.Application:
    app = web.Application(client_max_size=CLIENT_MAX_SIZE)
    app["cfg"] = cfg
    app["store"] = store
    session = aiohttp.ClientSession()
    app["session"] = session

    app.router.add_post(MESSAGES_PATH, handle_messages)
    app.router.add_post(COUNT_TOKENS_PATH, handle_count_tokens)
    app.router.add_get("/", handle_status)
    app.router.add_route("*", "/{tail:.*}", handle_relay)

    async def on_cleanup(app):
        await session.close()
        store.close()
    app.on_cleanup.append(on_cleanup)
    return app


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _banner(cfg, store: Store, port: int) -> None:
    stats = store.stats()
    model = cfg.summary_model or "request model (routed by upstream)"
    print(f"awecompress {__version__} — context compression proxy")
    print(f"  listen    -> http://{cfg.host}:{port}")
    print(f"  upstream  -> {cfg.upstream}")
    print(f"  compress  -> above {_fmt_tokens(cfg.threshold_tokens)} est. tokens; "
          f"keep last {cfg.keep_recent_turns} turns; min span {_fmt_tokens(cfg.min_span_tokens)}")
    print(f"  summaries -> {cfg.db_path} "
          f"({stats['sessions']} sessions, {stats['calls']} summary calls)")
    print(f"  model     -> {model}")
    print()
    print(f"  Claude Code:  export ANTHROPIC_BASE_URL=http://{cfg.host}:{port}")
    print("  stack with awerouter: point 'upstream' at it — auth and routing stay there")
    print()


async def serve(cfg, store: Store, port: "int | None" = None, host: "str | None" = None) -> None:
    """Run the proxy in the foreground until Ctrl-C."""
    if port is not None:
        cfg = replace(cfg, port=port)
    if host is not None:
        cfg = replace(cfg, host=host)

    app = create_app(cfg, store)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, cfg.host, cfg.port)
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        raise SystemExit(
            f"awecompress: cannot listen on {cfg.host}:{cfg.port} ({exc}) — "
            f"already running? Use --port to pick another.")
    _banner(cfg, store, cfg.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
