"""aiohttp proxy: transform request bodies on three wire protocols, relay
every response byte untouched.

Position in the stack: harness → awecompress → upstream (usually awerouter)
→ provider. Auth headers pass through untouched — awecompress never owns
credentials; routing and failover stay where they already live. Any failure
inside the transform path forwards the original body (fail-open): a
compression problem must never break the session.

The planning/replacement/summary machinery lives in integrate.py (shared
with awerouter's in-process mode); this module only wires it to HTTP.

Protocols served (endpoint path picks the wire shape):
    anthropic        POST /v1/messages   (+ /v1/messages/count_tokens)
    openai-chat      POST /v1/chat/completions
    openai-responses POST /v1/responses
Anything else relays untouched.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import aiohttp
from aiohttp import web

from awecompress import __version__
from awecompress.integrate import Compressor, Knobs
from awecompress.protocols import ENDPOINT_PATHS, PROTOCOLS

# Headers forwarded to the upstream. Host/Content-Length/Encoding are
# hop-by-hop or body-bound and left to aiohttp; everything not listed
# (cookies, acceptance headers, client-specific extras) is dropped.
PASS_HEADERS = frozenset({
    "anthropic-version", "anthropic-beta", "authorization", "x-api-key",
    "x-request-id", "traceparent", "tracestate", "user-agent",
})

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

# Endpoint path -> protocol id for the three transformable routes.
PATH_PROTOCOLS = {
    MESSAGES_PATH: "anthropic",
    "/v1/chat/completions": "openai-chat",
    "/v1/responses": "openai-responses",
}

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
# Summary sender: one non-streaming call to the upstream, passthrough auth
# ---------------------------------------------------------------------------

def _make_sender(session: aiohttp.ClientSession, cfg, headers: dict, protocol: str):
    url = _upstream_url(cfg, ENDPOINT_PATHS[protocol])
    timeout = aiohttp.ClientTimeout(connect=10, total=cfg.summary_timeout_seconds)

    async def sender(body: dict) -> dict:
        async with session.post(url, json=body, headers=headers,
                                timeout=timeout, allow_redirects=False) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:200]
                raise RuntimeError(f"summary call HTTP {resp.status}: {detail}")
            return await resp.json(content_type=None)

    return sender


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_messages(request: web.Request) -> web.StreamResponse:
    return await _proxy(request, "anthropic", allow_summary=True)


async def handle_count_tokens(request: web.Request) -> web.StreamResponse:
    # count_tokens must never mint a new summary (no surprise LLM calls),
    # but an existing one is applied so counts match what gets sent.
    return await _proxy(request, "anthropic", allow_summary=False)


async def handle_chat(request: web.Request) -> web.StreamResponse:
    return await _proxy(request, "openai-chat", allow_summary=True)


async def handle_responses(request: web.Request) -> web.StreamResponse:
    return await _proxy(request, "openai-responses", allow_summary=True)


async def _proxy(request: web.Request, protocol: str,
                 allow_summary: bool) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    cfg = request.app["cfg"]
    compressor: Compressor = request.app["compressor"]
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

    if isinstance(body, dict) and PROTOCOLS[protocol].message_list(body) is not None \
            and request.headers.get("x-awecompress", "").lower() != "off":
        sender = _make_sender(session, cfg, _forward_headers(request), protocol) \
            if allow_summary else None
        outcome = await compressor.transform(body, protocol, cfg.summary_model, sender,
                                             Knobs.from_config(cfg))
        if outcome is not None:
            print(outcome.line)
            payload = json.dumps(body).encode()
            mode = outcome.action

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
                        raw, headers, aiohttp.ClientTimeout(connect=10, total=None),
                        "passthrough")


async def handle_status(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    return web.json_response({
        "service": "awecompress",
        "version": __version__,
        "upstream": cfg.upstream,
        "stats": request.app["compressor"].stats(),
    })


async def _relay(request: web.Request, session: aiohttp.ClientSession, cfg,
                 method: str, path: str, payload: bytes, headers: dict,
                 timeout: aiohttp.ClientTimeout, mode: str = "passthrough") -> web.StreamResponse:
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

def create_app(cfg, compressor: Compressor) -> web.Application:
    app = web.Application(client_max_size=CLIENT_MAX_SIZE)
    app["cfg"] = cfg
    app["compressor"] = compressor
    session = aiohttp.ClientSession()
    app["session"] = session

    app.router.add_post(MESSAGES_PATH, handle_messages)
    app.router.add_post(COUNT_TOKENS_PATH, handle_count_tokens)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_get("/", handle_status)
    app.router.add_route("*", "/{tail:.*}", handle_relay)

    async def on_cleanup(app):
        await session.close()
        compressor.close()
    app.on_cleanup.append(on_cleanup)
    return app


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _banner(cfg, compressor: Compressor, port: int) -> None:
    stats = compressor.stats()
    model = cfg.summary_model or "request model (routed by upstream)"
    print(f"awecompress {__version__} — context compression proxy")
    print(f"  listen    -> http://{cfg.host}:{port}")
    print(f"  upstream  -> {cfg.upstream}")
    print(f"  compress  -> above {_fmt_tokens(cfg.threshold_tokens)} est. tokens; "
          f"keep last {cfg.keep_recent_turns} turns; min span {_fmt_tokens(cfg.min_span_tokens)}")
    print(f"  protected -> {len(cfg.protected_tools)} tools"
          + (f", {len(cfg.protected_file_patterns)} file patterns"
             if cfg.protected_file_patterns else ""))
    print(f"  summaries -> {cfg.db_path} "
          f"({stats['sessions']} sessions, {stats['calls']} summary calls)")
    print(f"  model     -> {model}")
    print()
    print(f"  Claude Code:  export ANTHROPIC_BASE_URL=http://{cfg.host}:{port}")
    print(f"  openai-chat:  OPENAI_BASE_URL=http://{cfg.host}:{port}/v1")
    print("  with awerouter: flip the profile 'awecompress' flag and skip this "
          "proxy entirely (in-process), or point 'upstream' at it")
    print()


async def serve(cfg, compressor: Compressor, port: "int | None" = None,
                host: "str | None" = None) -> None:
    """Run the proxy in the foreground until Ctrl-C."""
    if port is not None:
        cfg = replace(cfg, port=port)
    if host is not None:
        cfg = replace(cfg, host=host)

    app = create_app(cfg, compressor)
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
    _banner(cfg, compressor, cfg.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
