# Contributing to awecompress

## Development setup

```bash
git clone https://github.com/wehuman01/awecompress
cd awecompress
pip install -e ".[dev]"
pytest
```

Python ≥ 3.9, stdlib + click + aiohttp, no other runtime dependencies.

## Layout

```
src/awecompress/
  compress.py    pure planning: turn boundaries, hashes, token estimates, Plan
  summarize.py   the one LLM call: prompt + request + response parsing
  store.py       SQLite: frozen summaries + event log
  server.py      aiohttp proxy: transform requests, relay responses byte-for-byte
  config.py      JSON config load/validate (dies on typos)
  cli.py         click wiring: serve / status / config / clear
tests/           one file per module; end-to-end tests stub the upstream
```

Separation: `compress.py` has no I/O and is trivially testable; `server.py`
owns the wire; `summarize.py` and `store.py` are the only side-effecting
libraries. Keep it that way — new ideas usually belong in `compress.py` as
pure functions first.

## Design contract (stable constraints)

These rules are what makes the proxy trustworthy; changes here are
architecture changes, not patches.

1. **Fail-open, always.** Any exception in the transform path forwards the
   original body. Compression is an optimization; the session must never
   break because of it.
2. **Summaries are frozen.** A stored summary is applied verbatim, never
   regenerated, until the covered span grows or the prefix hash changes.
   Same history in, same bytes out — the provider prompt cache depends on
   it. (Cache trade: each span growth rewrites the summary once; one-time
   miss, accepted.)
3. **Cuts land on turn boundaries only.** A boundary is a user message with
   no `tool_result` blocks. This guarantees no `tool_use` is ever separated
   from its paired `tool_result`.
4. **No auth, no routing, no response parsing.** Auth headers pass through
   untouched; response bytes are relayed without inspection. Those jobs
   belong to the upstream (usually awerouter).
5. **One LLM call per span growth.** `count_tokens` applies existing
   summaries but never mints new ones. The `X-Awecompress: off` header
   bypasses everything.
6. **Anthropic Messages only (v1).** Everything else relays untouched.

### Session identity and change detection

- `session_key` = hash of (system prompt, first message). Stable across a
  session's requests because agents resend both byte-identical.
- `prefix_hash` = hash of `messages[:upto]`. A mismatch means the history
  changed under a stored summary (checkpoint rewind, fork) — the record is
  ignored and compression starts over for that session.

### Data model

SQLite, two tables: `sessions` (one row per session: covered prefix, its
hash, the frozen summary, cumulative stats) and `events` (append-only log of
summary calls). WAL mode so `status` reads while the proxy writes.

## Code style

- Plain functions; dataclasses for records; no classes with behavior.
- Comments state constraints the code cannot show, nothing else.
- Errors die loudly at config load (`awecompress: <reason>`) and fail-open
  at request time — never silently.

## Engineering Taste

Prefer solutions that are simple, clear, decoupled, honest, focused, and durable.

- Simple: make the smallest change that solves the real problem.
- Clear: optimize for the next reader, not for cleverness.
- Decoupled: keep boundaries clean, but do not add abstractions without a real need.
- Honest: make complexity, state, side effects, assumptions, and failure modes visible; do not hide complexity or create extra complexity.
- Focused: preserve boundaries between modules, and keep top-level convenience commands minimal.
- Durable: choose behavior that is easy to maintain, test, and extend.
- First principles: identify the real problem, hard constraints, and known facts before reaching for patterns, abstractions, or prior solutions.

## Branch model and releases

Work lands on `dev`, promotes to `main`. Releases are tagged `vX.Y.Z`;
CI extracts notes from `docs/CHANGELOG.md` and publishes to PyPI. Bump the
version in `pyproject.toml` and add the changelog entry in the same change.

## Attribution

Compression behavior follows the public documentation of
[DCP](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning)'s
Compress strategy (AGPL-3.0). awecompress shares no code with it — this is a
proxy-native, from-scratch implementation under MPL-2.0.
