"""Compression planning over an Anthropic Messages request body: pure
functions, no I/O. The server layer turns a Plan into one LLM call
(summarize.py) and a frozen replacement (store.py).

Model: a session's oldest whole turns are replaced by a single summary
message. A cut may only land on a turn boundary — a user message carrying no
tool_result blocks (a genuinely new human turn) — so an assistant tool_use is
never separated from its paired tool_result, and upstream pairing validation
never sees a half pair.

Summaries are frozen: every later request reuses the same stored bytes for
the same covered prefix, so the provider prompt cache sees a stable prefix.
Growing the covered span rewrites the summary once — a one-time cache miss,
the same trade awerouter's odcp accepts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# Marks the synthetic message we inject, in place of the covered prefix.
SUMMARY_MARKER = "[awecompress: summary of earlier turns — original messages removed]"

# Per-tool-result cap when flattening history for the summarizer: the summary
# needs what a result established, not every line of a 3k-line log.
TOOL_INPUT_CAP = 2000

# Same heuristic as awerouter.protocols.estimate_tokens (chars/4; CJK-heavy
# text tokenizes denser, so it counts at 2/3 the rate). Only consistency
# matters: the threshold compares this estimate against itself over time.
_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    total = len(text)
    cjk = len(_CJK.findall(text))
    return ((total - cjk) * 3 + cjk * 8) // 12 or 1


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------

def _is_turn_start(msg) -> bool:
    """True for a user message that starts a genuine human turn. A user
    message carrying tool_result blocks is a tool handshake, not a turn —
    cutting there would orphan the tool_use before it."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_result":
            return False
    return True


def safe_cut(messages: list, keep_recent_turns: int) -> int:
    """Index where the compressed span may end: the start of the
    keep_recent_turns-th-from-last genuine user turn, or 0 when the history
    is too short to cut anything."""
    boundaries = [i for i, m in enumerate(messages) if _is_turn_start(m)]
    if len(boundaries) <= keep_recent_turns:
        return 0
    return boundaries[-keep_recent_turns]


# ---------------------------------------------------------------------------
# Identity and change detection
# ---------------------------------------------------------------------------

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def session_key(body: dict) -> str:
    """Stable identity across one session's requests: system prompt plus the
    first message (coding agents resend both byte-identical every turn).
    Empty string when there is nothing stable to hold on to."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""
    return hashlib.sha256(_canonical([body.get("system"), messages[0]]).encode()).hexdigest()[:16]


def prefix_hash(messages: list, upto: int) -> str:
    """Fingerprint of messages[:upto] — detects a session rewound to a
    checkpoint or forked under a stored summary."""
    return hashlib.sha256(_canonical(messages[:upto]).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Token estimates
# ---------------------------------------------------------------------------

def estimate_messages_tokens(messages: list) -> int:
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_tokens(str(part.get("text") or ""))
                elif part.get("type") == "tool_use":
                    total += estimate_tokens(_canonical(part.get("input") or {}))
                elif part.get("type") == "tool_result":
                    total += estimate_tokens(_result_text(part))
    return total


def _parts(content):
    return content if isinstance(content, list) else []


def _result_text(part: dict) -> str:
    """tool_result content is a string or a list of text blocks."""
    content = part.get("content")
    if isinstance(content, str):
        return content
    return "".join(str(p.get("text") or "") for p in _parts(content)
                   if isinstance(p, dict) and p.get("type") == "text")


def estimate_body_tokens(body: dict, messages: list) -> int:
    """Rough size of the request as sent: system prompt plus messages. Tool
    definitions are constant per session and deliberately excluded."""
    system = body.get("system")
    if isinstance(system, str):
        extra = estimate_tokens(system)
    elif isinstance(system, list):
        extra = sum(estimate_tokens(str(p.get("text") or "")) for p in system
                    if isinstance(p, dict))
    else:
        extra = 0
    return extra + estimate_messages_tokens(messages)


# ---------------------------------------------------------------------------
# Transcript rendering (input to the summarizer)
# ---------------------------------------------------------------------------

def render_transcript(messages: list, result_cap: int) -> str:
    """Flatten messages to compact text for the summarizer. Thinking blocks
    are skipped: the assistant's visible text restates whatever mattered."""
    lines = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "?"
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                lines.append(f"{role}: {content}")
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    lines.append(f"{role}: {text}")
            elif kind == "tool_use":
                args = _canonical(part.get("input") or {})
                lines.append(f"{role} calls {part.get('name') or '?'}: {args[:TOOL_INPUT_CAP]}")
            elif kind == "tool_result":
                text = _result_text(part)
                if part.get("is_error") is True:
                    text = f"[error] {text}"
                if len(text) > result_cap:
                    text = text[:result_cap] + f" [... {len(text) - result_cap} chars truncated]"
                lines.append(f"{role} (result): {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The synthetic summary message
# ---------------------------------------------------------------------------

def summary_message(summary: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": f"{SUMMARY_MARKER}\n\n{summary}"}]}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    action: str                    # "passthrough" | "reuse" | "init" | "extend"
    key: str = ""                  # session key (empty when unusable)
    base_upto: int = 0             # messages already covered by a stored summary
    upto: int = 0                  # messages covered once the plan runs
    prev_summary: str = ""         # stored summary to merge into ("" when fresh)
    span_tokens: int = 0           # estimate of messages[base_upto:upto]
    body_tokens: int = 0           # estimate of the body as it would be sent now
    raw_tokens: int = 0            # estimate of the body with no compression
    saved_tokens: int = 0          # raw_tokens - body_tokens (reuse path)


def plan(body, stored, cfg) -> Plan:
    """Decide what to do with this request body.

    passthrough — nothing to do (session small, or nothing new to cover).
    reuse       — stored summary still covers the prefix; apply it, no LLM call.
    init/extend — over threshold with a new compressible span; summarize
                  messages[base_upto:cut] and freeze the result.

    A stored record whose prefix no longer hashes right (session rewound to a
    checkpoint, or forked) is ignored: the honest reading of a changed
    history is to start over, never to splice an old summary onto it.
    """
    if not isinstance(body, dict):
        return Plan("passthrough")
    messages = body.get("messages")
    if not isinstance(messages, list):
        return Plan("passthrough")
    key = session_key(body)
    if not key:
        return Plan("passthrough")

    if stored is not None and stored.prefix_hash != prefix_hash(messages, min(stored.upto, len(messages))):
        stored = None
    base = stored.upto if stored is not None else 0

    raw_tokens = estimate_body_tokens(body, messages)
    if stored is not None:
        body_tokens = estimate_body_tokens(body, [summary_message(stored.summary)] + messages[base:])
    else:
        body_tokens = raw_tokens

    def reuse() -> Plan:
        return Plan("reuse", key, base, base, stored.summary, 0,
                    body_tokens, raw_tokens, max(0, raw_tokens - body_tokens))

    cut = safe_cut(messages, cfg.keep_recent_turns)
    if cut <= base:
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)
    if body_tokens <= cfg.threshold_tokens:
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)

    span_tokens = estimate_messages_tokens(messages[base:cut])
    if span_tokens < cfg.min_span_tokens:
        # Over threshold but the new span is crumbs — wait for more history
        # rather than burn a summary call on nothing.
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)

    action = "extend" if base > 0 else "init"
    return Plan(action, key, base, cut,
                stored.summary if stored is not None else "",
                span_tokens, body_tokens, raw_tokens)
