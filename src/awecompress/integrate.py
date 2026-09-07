"""Host-facing facade: one Compressor, one transform() call per request.

Two hosts share this core:
- the standalone proxy (server.py) — sender posts to its upstream with
  passthrough auth;
- awerouter, in-process beside odcp/rtk — sender forwards to the flash
  destination with the provider's own credentials (awecompress the package
  stays awerouter-agnostic: the sender is injected, never imported).

transform() mutates the body's history in place when it compresses and
returns an Outcome describing what happened; None means passthrough (body
untouched). It never raises — fail-open is the contract: a compression
problem must never break the request.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from awecompress import compress, summarize
from awecompress.compress import DEFAULT_PROTECTED_TOOLS
from awecompress.protocols import PROTOCOLS
from awecompress.store import SessionRecord, Store

# async (protocol-shaped request body) -> parsed response object; raises on
# any failure (the fail-open path turns that into an uncompressed forward).
Sender = Callable[[dict], Awaitable[dict]]


@dataclass
class Knobs:
    """Compression tuning, per host/profile (mirrors awerouter's per-profile
    awecompress object; the standalone Config satisfies the same fields)."""
    threshold_tokens: int = 60000
    keep_recent_turns: int = 4
    min_span_tokens: int = 8000
    transcript_result_cap: int = 4000
    summary_max_tokens: int = 2048
    protected_tools: tuple = DEFAULT_PROTECTED_TOOLS
    protected_file_patterns: tuple = ()

    @classmethod
    def from_config(cls, cfg) -> "Knobs":
        """A standalone Config carries the same knobs (plus unrelated proxy
        fields this picks out by name)."""
        return cls(
            threshold_tokens=cfg.threshold_tokens,
            keep_recent_turns=cfg.keep_recent_turns,
            min_span_tokens=cfg.min_span_tokens,
            transcript_result_cap=cfg.transcript_result_cap,
            summary_max_tokens=cfg.summary_max_tokens,
            protected_tools=cfg.protected_tools,
            protected_file_patterns=cfg.protected_file_patterns,
        )


@dataclass
class Outcome:
    action: str          # "reuse" | "init" | "extend"
    key: str             # session key
    saved_tokens: int    # est. request tokens not sent this request
    line: str            # one-line log for the host to print


class Compressor:
    def __init__(self, db_path: str):
        self.store = Store(db_path)

    def stats(self) -> dict:
        return self.store.stats()

    def clear(self) -> None:
        self.store.clear()

    def close(self) -> None:
        self.store.close()

    async def transform(self, body: dict, protocol: str, summary_model: str,
                        sender: "Sender | None", knobs: "Knobs | None" = None) -> "Outcome | None":
        """Apply compression planning to one parsed request body.

        summary_model: the model id for the summary call — the host resolves
        it (awerouter: the flash destination's model; standalone: config or
        the request's own model). Empty and no body model → passthrough.
        sender: None compresses nothing new (count_tokens: apply existing
        summaries only, never mint one — no surprise LLM calls).
        """
        knobs = knobs or Knobs()
        try:
            adapter = PROTOCOLS[protocol]
            messages = adapter.message_list(body)
            if messages is None:
                return None

            stored = self.store.get(compress.session_key(body, protocol))
            p = compress.plan(body, stored, knobs, protocol)
            if p.action == "passthrough":
                return None

            if p.action == "reuse":
                self._apply(body, stored.summary, p.upto, protocol)
                return Outcome("reuse", p.key, p.saved_tokens,
                               f"[awecompress] {p.key}: applied frozen summary "
                               f"(messages 0..{p.upto - 1}) — est {p.raw_tokens} -> "
                               f"{p.body_tokens} tokens")

            if sender is None:
                # count_tokens-style caller: never mint, but apply what exists
                if stored is not None:
                    self._apply(body, stored.summary, stored.upto, protocol)
                return None

            model = summary_model or (body.get("model") or "")
            if not model:
                return None

            t0 = time.monotonic()
            try:
                summary = await summarize.summarize(
                    sender, protocol, model, p.prev_summary,
                    messages[p.base_upto:p.upto], knobs)
            except summarize.SummaryError as exc:
                # Fail-open: forward what we already have. With a stored summary
                # that means reuse (context stays small); without it, the
                # original body — next request will try again.
                print(f"[awecompress] {p.key}: summary call failed ({exc}); "
                      f"{'reusing previous summary' if stored is not None else 'forwarding uncompressed'}",
                      file=sys.stderr)
                if stored is not None:
                    self._apply(body, stored.summary, stored.upto, protocol)
                return None

            prev_summary_tokens = compress.estimate_tokens(p.prev_summary)
            summary_tokens = compress.estimate_tokens(summary)
            record = SessionRecord(
                key=p.key,
                upto=p.upto,
                prefix_hash=compress.prefix_hash(messages, p.upto),
                summary=summary,
                saved_tokens=(stored.saved_tokens if stored is not None else 0)
                             + max(0, prev_summary_tokens + p.span_tokens - summary_tokens),
                calls=(stored.calls if stored is not None else 0) + 1,
                updated_at=time.time(),
            )
            self.store.put(record)
            self.store.log_event(p.key, p.action, p.span_tokens, summary_tokens, model)
            self._apply(body, summary, p.upto, protocol)
            after_tokens = compress.estimate_body_tokens(body, adapter.message_list(body), protocol)
            ms = (time.monotonic() - t0) * 1000
            return Outcome(p.action, p.key, max(0, p.raw_tokens - after_tokens),
                           f"[awecompress] {p.key}: {p.action} — summarized messages "
                           f"{p.base_upto}..{p.upto - 1} (est {p.span_tokens} tok) into "
                           f"{summary_tokens} via {model} in {ms:.0f}ms; "
                           f"body est {p.raw_tokens} -> {after_tokens} tokens")
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            print(f"[awecompress] transform error: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _apply(body: dict, summary: str, upto: int, protocol: str) -> None:
        adapter = PROTOCOLS[protocol]
        body[adapter.list_key] = compress.apply_summary(body, summary, upto, protocol)
