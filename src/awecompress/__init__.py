"""awecompress: local context compression for coding agents.

Sits between the harness (Claude Code, OpenCode, ...) and its upstream, or
runs inside awerouter beside odcp/rtk. When a session's history crosses a
token threshold, the oldest whole turns are replaced by one frozen LLM
summary — cached, so every later request reuses the same bytes.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("awecompress")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.2.0"
