"""awecompress: a local context-compression proxy for coding agents.

Sits between the harness (Claude Code, ...) and any Anthropic-protocol
upstream (usually awerouter). When a session's history crosses a token
threshold, the oldest whole turns are replaced by one frozen LLM summary —
cached, so every later request reuses the same bytes.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("awecompress")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.1.0"
