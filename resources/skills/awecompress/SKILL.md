---
name: awecompress
description: "Use when helping users set up or tune awecompress context compression, understand frozen summaries, stack it with awerouter, or clear/reset stored summaries. 中文触发词：awecompress、上下文压缩、压缩代理、冻结摘要、长会话、context compression、token 超限。"
---

# awecompress

This skill covers **running and tuning** awecompress: a local proxy that compresses long coding-agent context before it reaches the upstream.

## Do Not Run Long-Lived Servers

**Never start `awecompress serve` for the user inside this agent** — it blocks the session. Tell the user to run it in their own terminal (e.g. `awecompress serve`), backgrounded however they like.

## Language Behavior

- Reply in the user's language when possible.
- If the user asks in Chinese, continue in Chinese.
- If the user asks in English, continue in English.

## Core Concepts

awecompress sits between the harness and the upstream, speaking Anthropic Messages only:

```
Claude Code → awecompress (:8808) → awerouter (:20128) → providers
```

- **Trigger**: estimated context (system + messages, chars/4 heuristic) crosses `thresholdTokens`.
- **Cut**: lands on a turn boundary — a user message with no tool_result blocks. Whole turns are replaced, so a tool_use is never separated from its tool_result.
- **Freeze**: the summary is made once (one LLM call through the same upstream) and stored in SQLite. Every later request reuses the same bytes — provider prompt cache stays warm. Span growth rewrites it once (one-time cache miss).
- **Fail-open**: any compression failure forwards the original body untouched. `X-Awecompress: off` bypasses per request.
- **No auth, no routing**: auth headers pass through; routing/failover stay in awerouter. Summary calls are ordinary requests — flash routing applies to them.

## Config

`~/.config/awecompress/config.json` (override with `AWECOMPRESS_CONFIG_DIR`). Written with defaults on first run.

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | `8808` | Listen port. |
| `upstream` | `http://127.0.0.1:20128` | Where requests go (awerouter). |
| `thresholdTokens` | `60000` | Estimated context that triggers compression. |
| `keepRecentTurns` | `4` | Human turns always kept verbatim. |
| `minSpanTokens` | `8000` | Smaller spans are not worth a summary call. |
| `summaryModel` | `""` | Empty = the request's model, routed by upstream (usually flash). |
| `summaryMaxTokens` | `2048` | Max summary output tokens. |
| `summaryTimeoutSeconds` | `60` | On timeout, forward uncompressed. |
| `dbPath` | config dir | SQLite store of frozen summaries. |

## Commands

```bash
awecompress serve                 # foreground proxy (user runs this)
awecompress status                # running? config? sessions/calls/tokens saved?
awecompress config path | show    # config file location / contents
awecompress clear --yes           # drop all frozen summaries
```

## Setup Recipe (tell the user)

1. `awecompress serve` in its own terminal (or their process manager).
2. Point the harness at it instead of awerouter: `export ANTHROPIC_BASE_URL=http://127.0.0.1:8808` (for Claude Code; other agents similar).
3. awerouter keeps running as before — it is now the `upstream` of awecompress.
4. Check `awecompress status` after a long session: sessions, summary calls, tokens saved.

## Tuning Notes

- Session blew past the limit anyway → lower `thresholdTokens` or raise `keepRecentTurns` is usually wrong — lower the threshold; keep-recent protects quality.
- Summaries feel lossy → raise `keepRecentTurns`; the summarizer prompt already demands exhaustive detail and verbatim short user messages.
- A checkpoint rewind recompresses from scratch by design (prefix hash mismatch).
- `count_tokens` applies existing summaries but never triggers new summary calls.
