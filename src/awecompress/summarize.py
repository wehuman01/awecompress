"""The one LLM call awecompress makes: merge a conversation span into a
frozen summary.

The call travels through a host-injected `sender`: an async callable that
takes a complete protocol-shaped request body and returns the parsed
response object. The standalone proxy sends it to its upstream with
passthrough auth; awerouter forwards it to the flash destination with the
provider's own credentials. This module owns neither URLs nor auth.

The prompt follows DCP's public Compress principles (exhaustive technical
summary, user-intent fidelity, protected planning state verbatim, strip
noise); this is an independent implementation — no DCP code.
"""

from __future__ import annotations

from awecompress.compress import render_transcript
from awecompress.protocols import PROTOCOLS

SYSTEM_PROMPT = """You compress a coding agent's conversation transcript into one authoritative summary. The original messages will be deleted; your summary is the only record that survives, so it must stand alone.

Capture exhaustively:
- The task and its acceptance criteria. Quote short user messages verbatim; never narrow, reinterpret, or drop a user constraint.
- Files touched (full paths), functions and modules named, and the current state of each.
- Decisions made and why; constraints and gotchas discovered.
- Failed approaches, one line each ("X failed because Y — avoid"), so they are not retried.

Lines marked [protected] carry live planning state — todo lists, plans, task/skill outcomes. Restate their content fully and faithfully in the summary; never compress or paraphrase them away.

Strip verbose tool output and exploration back-and-forth — keep what changed understanding, drop what only filled context.

If a summary of earlier conversation is provided, merge the new segment into it: keep it organized, keep every durable fact from both, do not repeat yourself.

Output only the summary text — no preamble, no commentary, no markdown fences."""


class SummaryError(Exception):
    """The summary call failed — the caller falls back to forwarding the
    request uncompressed (fail-open)."""


async def summarize(sender, protocol: str, model: str, prev_summary: str,
                    messages: list, cfg) -> str:
    """Summarize `messages` (merging `prev_summary` when present) via the
    injected sender. Raises SummaryError on any failure; never returns empty
    text."""
    parts = []
    if prev_summary:
        parts.append(
            "Summary of the conversation so far (merge this with the new segment below):\n\n"
            + prev_summary)
    parts.append(
        ("Transcript segment to compress now" if prev_summary else "Transcript to compress")
        + ":\n\n" + render_transcript(messages, cfg, protocol))

    body = PROTOCOLS[protocol].summary_request(
        model, SYSTEM_PROMPT, "\n\n".join(parts), cfg.summary_max_tokens)
    try:
        payload = await sender(body)
    except Exception as exc:  # the sender's failures are not ours to name
        raise SummaryError(f"summary call failed: {exc}")

    text = PROTOCOLS[protocol].response_text(payload)
    if not text.strip():
        raise SummaryError("summary call returned no text")
    return text.strip()
