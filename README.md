<div align="center">
  <h1>awecompress: Context Compression Proxy</h1>
  <p><strong>Freeze old turns into one summary before they reach your provider.</strong></p>
  <p>Local context compression for coding agents — three wire protocols, standalone proxy or in-process inside awerouter. When a session's history crosses a token threshold, the oldest whole turns are replaced by a single frozen LLM summary — cached, so every later request reuses the same bytes and your provider's prompt cache stays warm.</p>
  <p>
    <strong>English</strong> ·
    <a href="./README_cn.md">简体中文</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/version-0.1.0-7C3AED?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A53.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip">
    <img src="https://img.shields.io/badge/platform-terminal-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/github/stars/wehuman01/awecompress?style=flat-square" alt="Stars">
  </p>
</div>

> Compress long coding-agent context: old turns become one frozen summary, requests shrink, sessions run for days without a `/clear`. Standalone proxy, or one flag inside awerouter.

## How it works

Claude Code resubmits the whole conversation every turn. Hours in, most of that is dead weight — old file reads, finished exploration, failed attempts.

awecompress sits between the agent and whatever speaks its protocol upstream — Anthropic Messages, OpenAI Chat Completions, or OpenAI Responses:

```
Claude Code → awecompress (:8808) → awerouter → providers
```

For each request it estimates the context size. Above a threshold, it picks a cut point on a turn boundary (a message a human actually sent — so a tool call is never separated from its result), summarizes everything before it with one LLM call, replaces those messages with a single summary message, and freezes the result in a local SQLite store. Protected content — todo lists, plans, task/skill outcomes, files you name by pattern — renders into the summarizer uncapped and must survive the summary verbatim.

Three properties matter:

- **Frozen, not recomputed.** The summary is stored once. Every later request reuses the same bytes, so the provider prompt cache sees a stable prefix. Growing the covered span rewrites the summary once — a one-time cache miss.
- **Fail-open.** Any failure in the compression path forwards the original body untouched. A compression problem never breaks the session.
- **No auth, no routing.** Auth headers pass through; routing and failover stay in [awerouter](https://github.com/wehuman01/awerouter) (or whatever your upstream is). The summary calls themselves are ordinary requests through that upstream — awerouter's flash routing applies to them like anything else.

Set `X-Awecompress: off` on a request to bypass compression entirely.

## Install

```bash
pip install awecompress
```

Or from source:

```bash
git clone https://github.com/wehuman01/awecompress
cd awecompress && pip install -e .
```

## Quick Start

Stack with awerouter (the intended setup):

```bash
awerouter serve run          # your routing daemon, as usual
awecompress serve            # the compression proxy, foreground

# point Claude Code at awecompress instead of awerouter
export ANTHROPIC_BASE_URL=http://127.0.0.1:8808
claude

# openai-chat / openai-responses clients work the same way
export OPENAI_BASE_URL=http://127.0.0.1:8808/v1
```

**Or skip the proxy entirely** — with awerouter installed, flip the profile flag and the compression runs inside the router (see below).

Standalone against any Anthropic-protocol endpoint:

```bash
awecompress serve --upstream https://api.anthropic.com
```

Watch it work — one line per compressed request, and stats on demand:

```
[awecompress] 3f9a2c1b: init — summarized messages 0..61 (est 41200 tok) into 1100
              via claude-sonnet in 2.8s; body est 48900 -> 8800 tokens
[awecompress] 3f9a2c1b: applied frozen summary (messages 0..61) — est 48900 -> 8800 tokens
```

```bash
awecompress status
```

## With awerouter (in-process, no proxy)

`awerouter` accepts an `awecompress` profile flag, exactly like `rtk`/`odcp`. The compression core runs inside the router's pipeline — ahead of odcp pruning and rtk compression — so clients keep pointing at the router port and the flag hot-reloads with `routing.json`:

```json
"cc-router-1": {
  "protocol": "anthropic",
  "destinations": { "flash": "stepfun,step-3.7-flash", "pro": "glm,glm-5.3" },
  "odcp": true,
  "awecompress": true
}
```

An object tunes it — `summaryModel` picks who serves the summary calls: `"flash"` (default, the flash destination — routed directly, never re-priced to pro by the long-context rule), `"pro"`, or any model a provider declares in `providers.json` (validated at serve start). The other keys mirror the standalone config:

```json
"awecompress": {
  "summaryModel": "flash",
  "thresholdTokens": 60000,
  "keepRecentTurns": 4,
  "protectedTools": ["task", "skill", "todowrite", "todoread", "updateplan"],
  "protectedFilePatterns": ["**/*.schema.json"]
}
```

Requires the package on the router's side: `pip install awerouter[compress]` (the flag dies at serve start with that hint when it is missing). Savings land in the usage log next to rtk/odcp (`awecompress_saved`, shown by `awerouter usage`), `X-Awerouter-Token-Saver: off` disables all lossy layers at once, and the frozen store is shared with the standalone proxy (`awecompress status` / `clear` manage it either way).

## Config

`~/.config/awecompress/config.json` (or `$AWECOMPRESS_CONFIG_DIR`), written with defaults on first run:

```json
{
  "port": 8808,
  "upstream": "http://127.0.0.1:20128",
  "thresholdTokens": 60000,
  "keepRecentTurns": 4,
  "minSpanTokens": 8000,
  "summaryModel": "",
  "summaryMaxTokens": 2048
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | `8808` | Listen port. |
| `upstream` | `http://127.0.0.1:20128` | Where requests go — awerouter by default. |
| `thresholdTokens` | `60000` | Estimated context above which compression triggers. |
| `keepRecentTurns` | `4` | Human turns always kept verbatim. |
| `minSpanTokens` | `8000` | Don't summarize spans smaller than this — not worth a call. |
| `summaryModel` | `""` | Model for summary calls. Empty = the request's own model, routed by your upstream (usually flash). |
| `summaryMaxTokens` | `2048` | Max output tokens for a summary. |
| `summaryTimeoutSeconds` | `60` | Give up on a summary call after this; the request forwards uncompressed. |
| `protectedTools` | see below | Tools whose calls/results render into the summarizer uncapped and must survive the summary verbatim. |
| `protectedFilePatterns` | `[]` | Glob patterns; a call whose `file_path`/`path` argument matches renders uncapped too. |
| `transcriptResultCap` | `4000` | Per-tool-result cap (chars) when flattening history for the summarizer. |
| `dbPath` | config dir | SQLite store for frozen summaries. |

## Commands

```bash
awecompress serve                  # run the proxy in the foreground
awecompress serve --port 8809 --upstream http://127.0.0.1:20128
awecompress status                 # running state + compression stats
awecompress config path            # where the config lives
awecompress config show            # print it
awecompress clear --yes            # drop all frozen summaries
```

## Notes and limits

- **Three protocols** — Anthropic Messages, OpenAI Chat Completions, OpenAI Responses. Requests to other paths are relayed untouched.
- **Compression is lossy by design.** The summarizer prompt demands exhaustive technical detail and verbatim short user messages, but a summary is still a summary. `keepRecentTurns` keeps the working set verbatim; raise it if you want more raw history.
- **A session rewound to a checkpoint** (changed history under a stored summary) is detected by hash and recompressed from scratch.
- **`/v1/messages/count_tokens`** applies existing summaries but never triggers a new summary call.
- Inspired by [DCP](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning)'s Compress strategy (AGPL) and the closed-source Sleev — both harness-integrated. awecompress is an independent, proxy-native implementation; no DCP code is used.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for architecture and the design contract.

## License

MPL-2.0. Compression behavior inspired by DCP's public Compress documentation; implementation written from scratch.
