"""The one LLM call awecompress makes: merge a conversation span into a
frozen summary.

The prompt follows DCP's public Compress principles (exhaustive technical
summary, user-intent fidelity, strip noise); this is an independent
implementation for the proxy setting — no DCP code.
"""

from __future__ import annotations

import asyncio

import aiohttp

from awecompress.compress import render_transcript

SYSTEM_PROMPT = """You compress a coding agent's conversation transcript into one authoritative summary. The original messages will be deleted; your summary is the only record that survives, so it must stand alone.

Capture exhaustively:
- The task and its acceptance criteria. Quote short user messages verbatim; never narrow, reinterpret, or drop a user constraint.
- Files touched (full paths), functions and modules named, and the current state of each.
- Decisions made and why; constraints and gotchas discovered.
- Failed approaches, one line each ("X failed because Y — avoid"), so they are not retried.

Strip verbose tool output and exploration back-and-forth — keep what changed understanding, drop what only filled context.

If a summary of earlier conversation is provided, merge the new segment into it: keep it organized, keep every durable fact from both, do not repeat yourself.

Output only the summary text — no preamble, no commentary, no markdown fences."""


class SummaryError(Exception):
    """The summary call failed — the caller falls back to forwarding the
    request uncompressed (fail-open)."""


async def summarize(session: aiohttp.ClientSession, url: str, headers: dict,
                    model: str, prev_summary: str, messages: list, cfg) -> str:
    """Summarize `messages` (merging `prev_summary` when present). Raises
    SummaryError on any failure; never returns empty text."""
    parts = []
    if prev_summary:
        parts.append(
            "Summary of the conversation so far (merge this with the new segment below):\n\n"
            + prev_summary)
    parts.append(
        ("Transcript segment to compress now" if prev_summary else "Transcript to compress")
        + ":\n\n" + render_transcript(messages, cfg.transcript_result_cap))

    body = {
        "model": model,
        "max_tokens": cfg.summary_max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
        "stream": False,
    }
    timeout = aiohttp.ClientTimeout(connect=10, total=cfg.summary_timeout_seconds)
    try:
        async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:200]
                raise SummaryError(f"summary call HTTP {resp.status}: {detail}")
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise SummaryError(f"summary call failed: {exc}")

    text = _response_text(payload)
    if not text.strip():
        raise SummaryError("summary call returned no text")
    return text.strip()


def _response_text(payload) -> str:
    """Join text blocks from a Messages response (non-streaming shape)."""
    if not isinstance(payload, dict):
        return ""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return ""
    return "".join(str(b.get("text") or "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
