# Changelog

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
