"""Per-wire-protocol adapters: one history shape per protocol awecompress
serves — anthropic Messages, openai-chat, openai-responses.

The planning core in compress.py is protocol-blind. It asks an adapter for
turn boundaries, token estimates, an ordered segment walk (tool calls paired
with their results), and the shapes of the synthetic summary message and the
summary side-call. The shapes mirror what awerouter's odcp/vision modules
walk; this module stays dependency-free so standalone installs need no
awerouter.

Segments (tuples, walked in history order):
    ("text",   role, text)
    ("call",   name, args_obj)                 # args_obj: dict or raw string
    ("result", name, args_obj, text, is_error) # name/args from the paired call
"""

from __future__ import annotations

import json
import re

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


def canonical(value) -> str:
    """Stable JSON rendering (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _args_obj(raw):
    """Tool-call arguments as a dict when parseable, else the raw value —
    openai wire formats carry arguments as a JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _text_of_parts(parts, types) -> str:
    """Concatenate text of content parts whose type is in `types`."""
    out = []
    for p in parts if isinstance(parts, list) else []:
        if isinstance(p, dict) and p.get("type") in types \
                and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out)


class BaseProtocol:
    id = ""
    list_key = ""   # body key carrying the history ("messages" | "input")

    # -- history access ----------------------------------------------------
    def message_list(self, body) -> "list | None":
        items = body.get(self.list_key) if isinstance(body, dict) else None
        return items if isinstance(items, list) else None

    # -- session identity (stable across one session's requests) -----------
    def system_identity(self, body):
        return None

    def system_tokens(self, body) -> int:
        return 0

    # -- compressible span --------------------------------------------------
    def preamble(self, items) -> int:
        """Leading items never summarized away (openai-chat system/developer
        messages carry standing instructions that must stay messages)."""
        return 0

    def is_turn_start(self, item) -> bool:
        raise NotImplementedError

    # -- sizing ---------------------------------------------------------------
    def item_tokens(self, item) -> int:
        raise NotImplementedError

    # -- ordered walk with call pairing --------------------------------------
    def segments(self, items):
        raise NotImplementedError

    # -- synthetic summary ------------------------------------------------------
    def summary_message(self, text: str) -> dict:
        raise NotImplementedError

    def summary_request(self, model: str, system: str, user_text: str,
                        max_tokens: int) -> dict:
        raise NotImplementedError

    def response_text(self, payload) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

class AnthropicProtocol(BaseProtocol):
    id = "anthropic"
    list_key = "messages"

    def system_identity(self, body):
        return body.get("system")

    def system_tokens(self, body) -> int:
        system = body.get("system")
        if isinstance(system, str):
            return estimate_tokens(system)
        if isinstance(system, list):
            return sum(estimate_tokens(str(p.get("text") or "")) for p in system
                       if isinstance(p, dict))
        return 0

    def is_turn_start(self, item) -> bool:
        """True for a user message that starts a genuine human turn. A user
        message carrying tool_result blocks is a tool handshake, not a turn —
        cutting there would orphan the tool_use before it."""
        if not isinstance(item, dict) or item.get("role") != "user":
            return False
        content = item.get("content")
        if isinstance(content, str):
            return True
        if not isinstance(content, list):
            return False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                return False
        return True

    def item_tokens(self, item) -> int:
        if not isinstance(item, dict):
            return 0
        content = item.get("content")
        if isinstance(content, str):
            return estimate_tokens(content)
        if not isinstance(content, list):
            return 0
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                total += estimate_tokens(str(part.get("text") or ""))
            elif kind == "tool_use":
                total += estimate_tokens(canonical(part.get("input") or {}))
            elif kind == "tool_result":
                total += estimate_tokens(self._result_text(part))
        return total

    @staticmethod
    def _result_text(part: dict) -> str:
        content = part.get("content")
        if isinstance(content, str):
            return content
        return _text_of_parts(content, ("text",))

    def segments(self, items):
        calls: dict = {}
        for item in items:
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    calls[part.get("id")] = (part.get("name"),
                                             part.get("input") or {})
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "?"
            content = item.get("content")
            if isinstance(content, str):
                if content.strip():
                    yield ("text", role, content)
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
                        yield ("text", role, text)
                elif kind == "tool_use":
                    yield ("call", part.get("name") or "?", part.get("input") or {})
                elif kind == "tool_result":
                    name, args = calls.get(part.get("tool_use_id"), ("?", {}))
                    yield ("result", name, args, self._result_text(part),
                           part.get("is_error") is True)

    def summary_message(self, text: str) -> dict:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def summary_request(self, model: str, system: str, user_text: str,
                        max_tokens: int) -> dict:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
            "stream": False,
        }

    def response_text(self, payload) -> str:
        if not isinstance(payload, dict):
            return ""
        return _text_of_parts(payload.get("content"), ("text",))


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------

class OpenAIChatProtocol(BaseProtocol):
    id = "openai-chat"
    list_key = "messages"

    def preamble(self, items) -> int:
        n = 0
        for item in items:
            if isinstance(item, dict) and item.get("role") in ("system", "developer"):
                n += 1
            else:
                break
        return n

    def is_turn_start(self, item) -> bool:
        """Tool results ride role:"tool" messages, so every role:"user"
        message is a genuine human turn."""
        return isinstance(item, dict) and item.get("role") == "user"

    def item_tokens(self, item) -> int:
        if not isinstance(item, dict):
            return 0
        total = 0
        content = item.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            total += estimate_tokens(_text_of_parts(content, ("text",)))
        for call in item.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                total += estimate_tokens(str(fn.get("name") or "")
                                         + str(fn.get("arguments") or ""))
        return total

    def segments(self, items):
        calls: dict = {}
        for item in items:
            for call in (item.get("tool_calls") or []) if isinstance(item, dict) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    calls[call.get("id")] = (fn["name"],
                                             _args_obj(fn.get("arguments")))
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "?"
            if role == "tool":
                name, args = calls.get(item.get("tool_call_id"), ("?", {}))
                content = item.get("content")
                text = content if isinstance(content, str) \
                    else _text_of_parts(content, ("text",))
                yield ("result", name, args, text, False)
                continue
            content = item.get("content")
            if isinstance(content, str):
                if content.strip():
                    yield ("text", role, content)
            elif isinstance(content, list):
                text = _text_of_parts(content, ("text",)).strip()
                if text:
                    yield ("text", role, text)
            for call in item.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    yield ("call", fn["name"], _args_obj(fn.get("arguments")))

    def summary_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def summary_request(self, model: str, system: str, user_text: str,
                        max_tokens: int) -> dict:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
        }

    def response_text(self, payload) -> str:
        if not isinstance(payload, dict):
            return ""
        msg = ((payload.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return _text_of_parts(content, ("text",))
        return ""


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------

class ResponsesProtocol(BaseProtocol):
    id = "openai-responses"
    list_key = "input"

    def system_identity(self, body):
        return body.get("instructions")

    def system_tokens(self, body) -> int:
        instructions = body.get("instructions")
        return estimate_tokens(instructions) if isinstance(instructions, str) else 0

    def is_turn_start(self, item) -> bool:
        return isinstance(item, dict) and item.get("role") == "user"

    def item_tokens(self, item) -> int:
        if not isinstance(item, dict):
            return 0
        itype = item.get("type")
        if itype == "function_call":
            return estimate_tokens(str(item.get("name") or "")
                                   + str(item.get("arguments") or ""))
        if itype == "function_call_output":
            return estimate_tokens(str(item.get("output") or ""))
        if itype == "reasoning":
            return 0  # the assistant's visible text restates what mattered
        content = item.get("content")
        if isinstance(content, str):
            return estimate_tokens(content)
        return estimate_tokens(_text_of_parts(content, ("input_text", "output_text", "text")))

    def segments(self, items):
        calls: dict = {}
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call" \
                    and isinstance(item.get("name"), str):
                calls[item.get("call_id")] = (item["name"],
                                              _args_obj(item.get("arguments")))
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call":
                yield ("call", item.get("name") or "?",
                       _args_obj(item.get("arguments")))
            elif itype == "function_call_output":
                name, args = calls.get(item.get("call_id"), ("?", {}))
                yield ("result", name, args, str(item.get("output") or ""), False)
            elif itype == "reasoning":
                continue
            else:  # message item
                role = item.get("role") or "?"
                content = item.get("content")
                text = content if isinstance(content, str) \
                    else _text_of_parts(content, ("input_text", "output_text", "text"))
                if text.strip():
                    yield ("text", role, text)

    def summary_message(self, text: str) -> dict:
        return {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}

    def summary_request(self, model: str, system: str, user_text: str,
                        max_tokens: int) -> dict:
        return {
            "model": model,
            "instructions": system,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": user_text}]}],
            "max_output_tokens": max_tokens,
            "stream": False,
        }

    def response_text(self, payload) -> str:
        if not isinstance(payload, dict):
            return ""
        parts = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    parts.append(str(part.get("text") or ""))
        return "".join(parts)


PROTOCOLS = {
    "anthropic": AnthropicProtocol(),
    "openai-chat": OpenAIChatProtocol(),
    "openai-responses": ResponsesProtocol(),
}

# Where each protocol's completion endpoint lives on the wire (standalone
# proxy relays and summary side-calls; awerouter has its own table).
ENDPOINT_PATHS = {
    "anthropic": "/v1/messages",
    "openai-chat": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}
