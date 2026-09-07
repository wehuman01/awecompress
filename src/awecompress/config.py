"""Config: one JSON file, written with defaults on first run.

Location: $AWECOMPRESS_CONFIG_DIR or ~/.config/awecompress/config.json
(same convention as awerouter). The summary store lives beside it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from awecompress.compress import DEFAULT_PROTECTED_TOOLS

DEFAULT_PORT = 8808
DEFAULT_UPSTREAM = "http://127.0.0.1:20128"  # awerouter's default listen port

# Rough token estimates (chars/4 heuristic) — a 200k-token model's usable
# history comfortably crosses 60k long before the hard limit; compressing
# early keeps the summary small relative to what it replaces.
DEFAULT_THRESHOLD_TOKENS = 60000
DEFAULT_KEEP_RECENT_TURNS = 4
DEFAULT_MIN_SPAN_TOKENS = 8000
DEFAULT_TRANSCRIPT_RESULT_CAP = 4000
DEFAULT_SUMMARY_MAX_TOKENS = 2048
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 60

def die(message: str) -> "SystemExit":
    raise SystemExit(f"awecompress: {message}")


def config_dir() -> Path:
    return Path(os.environ.get("AWECOMPRESS_CONFIG_DIR", "~/.config/awecompress")).expanduser()


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    return config_dir() / "summaries.db"


@dataclass
class Config:
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    upstream: str = DEFAULT_UPSTREAM
    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    min_span_tokens: int = DEFAULT_MIN_SPAN_TOKENS
    transcript_result_cap: int = DEFAULT_TRANSCRIPT_RESULT_CAP
    # Empty = summarize with the request's own model, which the upstream
    # (awerouter) then routes like any other request — usually flash.
    summary_model: str = ""
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
    summary_timeout_seconds: int = DEFAULT_SUMMARY_TIMEOUT_SECONDS
    # Protected content (see compress.py): these tools' calls and results
    # render into the summarizer transcript uncapped and must survive the
    # summary verbatim; file patterns protect path-matching calls the same way.
    protected_tools: tuple = DEFAULT_PROTECTED_TOOLS
    protected_file_patterns: tuple = ()
    db_path: str = ""  # empty = db_path() default


def _default_file() -> dict:
    return {
        "port": DEFAULT_PORT,
        "upstream": DEFAULT_UPSTREAM,
        "thresholdTokens": DEFAULT_THRESHOLD_TOKENS,
        "keepRecentTurns": DEFAULT_KEEP_RECENT_TURNS,
        "minSpanTokens": DEFAULT_MIN_SPAN_TOKENS,
        "summaryModel": "",
        "summaryMaxTokens": DEFAULT_SUMMARY_MAX_TOKENS,
        "protectedTools": list(DEFAULT_PROTECTED_TOOLS),
        "protectedFilePatterns": [],
    }


# File keys are camelCase (hand-edited like awerouter's routing.json);
# dataclass fields stay snake_case.
_KEY_MAP = {
    "port": "port",
    "host": "host",
    "upstream": "upstream",
    "thresholdTokens": "threshold_tokens",
    "keepRecentTurns": "keep_recent_turns",
    "minSpanTokens": "min_span_tokens",
    "transcriptResultCap": "transcript_result_cap",
    "summaryModel": "summary_model",
    "summaryMaxTokens": "summary_max_tokens",
    "summaryTimeoutSeconds": "summary_timeout_seconds",
    "protectedTools": "protected_tools",
    "protectedFilePatterns": "protected_file_patterns",
    "dbPath": "db_path",
}

_INT_FIELDS = {
    "port", "threshold_tokens", "keep_recent_turns", "min_span_tokens",
    "transcript_result_cap", "summary_max_tokens", "summary_timeout_seconds",
}
_STR_FIELDS = {"host", "upstream", "summary_model", "db_path"}
_LIST_FIELDS = {"protected_tools", "protected_file_patterns"}


def load_config(path: "Path | None" = None) -> Config:
    """Load config, creating the default file on first run. Dies on unknown
    keys or invalid values — a typo must not silently become a default."""
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_default_file(), indent=2) + "\n")
        print(f"awecompress: wrote default config to {path}")
        cfg = Config()
    else:
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            die(f"{path}: invalid JSON ({exc})")
        if not isinstance(raw, dict):
            die(f"{path}: expected a JSON object")

        cfg = Config()
        for key, val in raw.items():
            if key not in _KEY_MAP:
                die(f"{path}: unknown key '{key}' (see README for the full list)")
            field = _KEY_MAP[key]
            if field in _INT_FIELDS:
                if not isinstance(val, int) or isinstance(val, bool):
                    die(f"{path}: '{key}' must be an integer")
            elif field in _STR_FIELDS:
                if not isinstance(val, str):
                    die(f"{path}: '{key}' must be a string")
            elif field in _LIST_FIELDS:
                if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                    die(f"{path}: '{key}' must be an array of strings")
                val = tuple(val)
            setattr(cfg, field, val)

    _validate(cfg)
    return replace(cfg, db_path=cfg.db_path or str(db_path()))


def _validate(cfg: Config) -> None:
    if not (1 <= cfg.port <= 65535):
        die(f"port must be 1..65535, got {cfg.port}")
    parsed = urlparse(cfg.upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        die(f"upstream must be an http(s) URL, got '{cfg.upstream}'")
    if cfg.threshold_tokens < 1000:
        die(f"thresholdTokens must be >= 1000, got {cfg.threshold_tokens}")
    if cfg.keep_recent_turns < 1:
        die(f"keepRecentTurns must be >= 1, got {cfg.keep_recent_turns}")
    if cfg.min_span_tokens < 1000:
        die(f"minSpanTokens must be >= 1000, got {cfg.min_span_tokens}")
    if cfg.summary_max_tokens < 256:
        die(f"summaryMaxTokens must be >= 256, got {cfg.summary_max_tokens}")
    if not (10 <= cfg.summary_timeout_seconds <= 600):
        die(f"summaryTimeoutSeconds must be 10..600, got {cfg.summary_timeout_seconds}")
