# Changelog

## v0.2.0

Three wire protocols, protected content, and an in-process awerouter mode.

### Fixed
- A stored frozen summary is never spliced onto a rewound or forked history:
  `transform()` now re-checks the record's `prefix_hash` itself, so a session
  whose covered prefix changed (checkpoint rewind, fork under the summary)
  starts over honestly instead of applying a stale summary — including the
  summary-failure fail-open path and the no-sender `count_tokens` path, both
  of which previously reused the stale record.

### Highlights

- **Three protocols** — anthropic Messages, openai-chat, and openai-responses
  histories all compress now (endpoint path picks the shape; per-protocol
  adapters in `protocols.py`). openai-chat standing system/developer messages
  are never summarized away — they stay messages ahead of the summary.
- **Protected content (DCP's Compress idea, proxy-shaped)** — `protectedTools`
  (default task/skill/todowrite/todoread/updateplan) and
  `protectedFilePatterns` render uncapped into the summarizer transcript and
  are marked `[protected]`; the summary prompt demands their content survive
  verbatim. TodoWrite inputs render in full too — the plan lives in the
  arguments, not the trivial result.
- **In-process awerouter mode** — awerouter accepts an `"awecompress"` profile
  flag (like rtk/odcp) and runs the compression core inside its pipeline,
  ahead of odcp/rtk: clients keep pointing at the router port, the flag
  hot-reloads, summary calls go straight to the flash destination (or `pro`,
  or any provider-declared model via `summaryModel`), and savings land in the
  usage log (`awecompress_saved`). Requires `pip install awerouter[compress]`;
  the standalone proxy remains for no-awerouter setups.
- **Sender injection** — the summary call travels through a host-injected
  sender (`integrate.Compressor`), so the core owns no URLs and no auth.

## v0.1.0

## v0.1.0

First release. A local Anthropic-protocol context-compression proxy that
sits between a coding agent (Claude Code) and its upstream (usually
awerouter): when a session's estimated context crosses a threshold, the
oldest whole turns are replaced by one frozen LLM summary.

### Highlights

- **Frozen summaries** — one summary call per span growth; every later
  request reuses the same stored bytes, keeping the provider prompt cache
  stable on the compressed prefix.
- **Turn-boundary cuts** — compression only ever replaces whole turns
  (user messages without tool results), so tool calls are never separated
  from their results.
- **Fail-open** — any failure in the compression path forwards the original
  request untouched; `X-Awecompress: off` bypasses per request.
- **Composable with awerouter** — auth passes through untouched; summary
  calls are ordinary requests through the upstream, so flash routing
  applies to them automatically.
- SQLite store with per-session records and an event log; `status` and
  `clear` commands; threshold / keep-recent / min-span knobs in
  `~/.config/awecompress/config.json`.
- Inspired by DCP's Compress strategy and Sleev; independent,
  proxy-native implementation (see docs/CONTRIBUTING.md#attribution).
