"""Compression planning over a request body: pure functions, no I/O. The
integrate/server layers turn a Plan into one LLM call (summarize.py) and a
frozen replacement (store.py).

Model: a session's oldest whole turns are replaced by a single summary
message. A cut may only land on a turn boundary — a message a human actually
sent — so a tool call is never separated from its result and upstream pairing
validation never sees a half pair. History shapes are per-protocol
(protocols.py); this module only plans.

Protected content (DCP's Compress idea, proxy-shaped): tool calls whose name
is in protectedTools, or whose path-ish arguments match protectedFilePatterns,
render into the summarizer transcript uncapped and marked [protected]; the
summary prompt demands their content survive compression verbatim — todo
lists, plans, and task/skill outcomes are live planning state, not history
noise.

Summaries are frozen: every later request reuses the same stored bytes for
the same covered prefix, so the provider prompt cache sees a stable prefix.
Growing the covered span rewrites the summary once — a one-time cache miss.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatch

from awecompress.protocols import (
    ENDPOINT_PATHS,  # noqa: F401  (re-export: standalone server relays by it)
    PROTOCOLS,
    canonical,
    estimate_tokens,  # noqa: F401  (re-export: callers import it from here)
)

# Marks the synthetic message we inject, in place of the covered prefix.
SUMMARY_MARKER = "[awecompress: summary of earlier turns — original messages removed]"

# Per-tool-input cap when flattening history for the summarizer: the summary
# needs what a call did, not every byte of its arguments.
TOOL_INPUT_CAP = 2000

# Protected by default: planning-state tools whose trace must survive
# compression intact (same convention as DCP's compress.protectedTools).
DEFAULT_PROTECTED_TOOLS = ("task", "skill", "todowrite", "todoread", "updateplan")

# Argument keys treated as file paths for protectedFilePatterns matching.
_PATH_KEYS = ("file_path", "path", "filepath", "notebook_path")


def _norm_tool(name: str) -> str:
    """Tool-name identity: lowercase with separators stripped, so TodoWrite
    == todo_write == todowrite (same convention as awerouter's odcp)."""
    return name.lower().replace("_", "").replace("-", "")


def is_protected(name, args, protected_tools, patterns) -> bool:
    """One tool call's protection verdict. Name match is on the normalized
    form; pattern match tests the call's path-ish argument values."""
    if _norm_tool(name or "") in protected_tools:
        return True
    if patterns and isinstance(args, dict):
        for key in _PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and any(fnmatch(value, p) for p in patterns):
                return True
    return False


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------

def _is_turn_start(msg) -> bool:
    """Anthropic-shaped turn test (kept for direct callers/tests)."""
    return PROTOCOLS["anthropic"].is_turn_start(msg)


def safe_cut(messages: list, keep_recent_turns: int, protocol: str = "anthropic") -> int:
    """Index where the compressed span may end: the start of the
    keep_recent_turns-th-from-last genuine user turn, or 0 when the history
    is too short to cut anything."""
    adapter = PROTOCOLS[protocol]
    boundaries = [i for i, m in enumerate(messages) if adapter.is_turn_start(m)]
    if len(boundaries) <= keep_recent_turns:
        return 0
    return boundaries[-keep_recent_turns]


# ---------------------------------------------------------------------------
# Identity and change detection
# ---------------------------------------------------------------------------

def session_key(body: dict, protocol: str = "anthropic") -> str:
    """Stable identity across one session's requests: the protocol's standing
    instructions plus the first message (coding agents resend both
    byte-identical every turn). Empty string when there is nothing stable to
    hold on to."""
    adapter = PROTOCOLS[protocol]
    messages = adapter.message_list(body)
    if not messages:
        return ""
    return hashlib.sha256(canonical([adapter.system_identity(body),
                                      messages[0]]).encode()).hexdigest()[:16]


def prefix_hash(messages: list, upto: int) -> str:
    """Fingerprint of messages[:upto] — detects a session rewound to a
    checkpoint or forked under a stored summary."""
    return hashlib.sha256(canonical(messages[:upto]).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Token estimates
# ---------------------------------------------------------------------------

def estimate_messages_tokens(messages: list, protocol: str = "anthropic") -> int:
    adapter = PROTOCOLS[protocol]
    return sum(adapter.item_tokens(m) for m in messages)


def estimate_body_tokens(body: dict, messages: list, protocol: str = "anthropic") -> int:
    """Rough size of the request as sent: standing instructions plus messages.
    Tool definitions are constant per session and deliberately excluded."""
    return PROTOCOLS[protocol].system_tokens(body) \
        + estimate_messages_tokens(messages, protocol)


# ---------------------------------------------------------------------------
# Transcript rendering (input to the summarizer)
# ---------------------------------------------------------------------------

def render_transcript(messages: list, cfg, protocol: str = "anthropic") -> str:
    """Flatten messages to compact text for the summarizer, applying the
    protection rules. Protected calls render uncapped and carry the
    [protected] marker; everything else is capped (result cap from cfg,
    TOOL_INPUT_CAP for arguments). Thinking/reasoning never renders — the
    assistant's visible text restates whatever mattered."""
    adapter = PROTOCOLS[protocol]
    tools = {_norm_tool(t) for t in (getattr(cfg, "protected_tools", None) or ())}
    patterns = tuple(getattr(cfg, "protected_file_patterns", None) or ())
    lines = []
    for seg in adapter.segments(messages):
        kind = seg[0]
        if kind == "text":
            _, role, text = seg
            if text.strip():
                lines.append(f"{role}: {text}")
        elif kind == "call":
            _, name, args = seg
            protected = is_protected(name, args, tools, patterns)
            text = canonical(args)
            if not protected and len(text) > TOOL_INPUT_CAP:
                text = text[:TOOL_INPUT_CAP] + f" [... {len(text) - TOOL_INPUT_CAP} chars truncated]"
            mark = " [protected]" if protected else ""
            lines.append(f"assistant calls {name}{mark}: {text}")
        else:  # result
            _, name, args, text, errored = seg
            protected = is_protected(name, args, tools, patterns)
            cap = cfg.transcript_result_cap
            if not protected and len(text) > cap:
                text = text[:cap] + f" [... {len(text) - cap} chars truncated]"
            if errored:
                text = f"[error] {text}"
            mark = " [protected]" if protected else ""
            lines.append(f"tool (result){mark}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The synthetic summary message
# ---------------------------------------------------------------------------

def summary_message(summary: str, protocol: str = "anthropic") -> dict:
    return PROTOCOLS[protocol].summary_message(f"{SUMMARY_MARKER}\n\n{summary}")


def apply_summary(body: dict, summary: str, upto: int, protocol: str) -> list:
    """The rewritten history: protected preamble (openai-chat standing
    instructions), the summary message, then everything from `upto` on."""
    adapter = PROTOCOLS[protocol]
    items = adapter.message_list(body) or []
    return list(items[:adapter.preamble(items)]) \
        + [summary_message(summary, protocol)] + list(items[upto:])


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
    span_tokens: int = 0           # estimate of the compressible new span
    body_tokens: int = 0           # estimate of the body as it would be sent now
    raw_tokens: int = 0            # estimate of the body with no compression
    saved_tokens: int = 0          # raw_tokens - body_tokens (reuse path)


def plan(body, stored, cfg, protocol: str = "anthropic") -> Plan:
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
    adapter = PROTOCOLS[protocol]
    messages = adapter.message_list(body)
    if not messages:
        return Plan("passthrough")
    key = session_key(body, protocol)
    if not key:
        return Plan("passthrough")
    start = adapter.preamble(messages)   # standing instructions stay messages

    if stored is not None and stored.prefix_hash != prefix_hash(messages, min(stored.upto, len(messages))):
        stored = None
    base = stored.upto if stored is not None else 0

    raw_tokens = estimate_body_tokens(body, messages, protocol)
    if stored is not None:
        summary_item = summary_message(stored.summary, protocol)
        body_tokens = adapter.system_tokens(body) \
            + estimate_messages_tokens(messages[:start], protocol) \
            + adapter.item_tokens(summary_item) \
            + estimate_messages_tokens(messages[base:], protocol)
    else:
        body_tokens = raw_tokens

    def reuse() -> Plan:
        return Plan("reuse", key, base, base, stored.summary, 0,
                    body_tokens, raw_tokens, max(0, raw_tokens - body_tokens))

    cut = safe_cut(messages, cfg.keep_recent_turns, protocol)
    if cut <= max(base, start):
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)
    if body_tokens <= cfg.threshold_tokens:
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)

    span_tokens = estimate_messages_tokens(messages[max(base, start):cut], protocol)
    if span_tokens < cfg.min_span_tokens:
        # Over threshold but the new span is crumbs — wait for more history
        # rather than burn a summary call on nothing.
        return reuse() if stored is not None else Plan("passthrough", key, raw_tokens=raw_tokens)

    action = "extend" if base > 0 else "init"
    return Plan(action, key, base, cut,
                stored.summary if stored is not None else "",
                span_tokens, body_tokens, raw_tokens)
