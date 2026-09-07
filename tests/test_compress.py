"""Pure-function tests for compress.py: boundaries, hashes, plans."""

from types import SimpleNamespace

from awecompress.compress import (
    SUMMARY_MARKER,
    _is_turn_start,
    estimate_tokens,
    plan,
    prefix_hash,
    render_transcript,
    safe_cut,
    session_key,
    summary_message,
)
from awecompress.store import SessionRecord

CFG = SimpleNamespace(threshold_tokens=1000, keep_recent_turns=2, min_span_tokens=100)


def user(text):
    return {"role": "user", "content": text}


def assistant_tool_use(name="Read", args=None, tid="t1"):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": args or {"file": "a.py"}}]}


def tool_result(tid="t1", text="result text"):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}


def assistant_text(text):
    return {"role": "assistant", "content": text}


class TestEstimate:
    def test_ascii(self):
        assert estimate_tokens("abcd") == 1

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_cjk_denser(self):
        assert estimate_tokens("你好世界") > estimate_tokens("abcd")


class TestTurnStart:
    def test_plain_user_is_start(self):
        assert _is_turn_start(user("hi"))

    def test_assistant_is_not(self):
        assert not _is_turn_start(assistant_text("ok"))

    def test_tool_result_carrier_is_not(self):
        assert not _is_turn_start(tool_result())

    def test_non_dict(self):
        assert not _is_turn_start("junk")


class TestSafeCut:
    def test_keeps_recent_turns(self):
        msgs = [user("1"), assistant_text("a"), user("2"), assistant_text("b"), user("3")]
        assert safe_cut(msgs, 2) == 2  # span = messages[:2], keeps turns 2 and 3

    def test_too_short_returns_zero(self):
        msgs = [user("1"), assistant_text("a"), user("2")]
        assert safe_cut(msgs, 2) == 0

    def test_tool_handshakes_are_not_boundaries(self):
        msgs = [
            user("1"),
            assistant_tool_use(),
            tool_result(),          # user role, but not a turn start
            user("2"),
            assistant_text("b"),
            user("3"),
        ]
        boundaries = [0, 3, 5]
        assert safe_cut(msgs, 2) == boundaries[1]

    def test_exact_keep_plus_one(self):
        msgs = [user("1"), assistant_text("a"), user("2"), assistant_text("b"), user("3")]
        # keep 3 of 3 turns -> nothing to cut
        assert safe_cut(msgs, 3) == 0


class TestKeys:
    def base(self):
        return {"system": "sys", "messages": [user("task")]}

    def test_stable_across_appends(self):
        a = self.base()
        b = {"system": "sys", "messages": [user("task"), assistant_text("x"), user("y")]}
        assert session_key(a) == session_key(b)

    def test_differs_on_first_message(self):
        b = {"system": "sys", "messages": [user("other")]}
        assert session_key(self.base()) != session_key(b)

    def test_empty_messages(self):
        assert session_key({"messages": []}) == ""

    def test_prefix_hash_stable_then_changes(self):
        msgs = [user("1"), assistant_text("a"), user("2")]
        assert prefix_hash(msgs, 2) == prefix_hash(list(msgs), 2)
        assert prefix_hash(msgs, 2) != prefix_hash([user("X"), assistant_text("a"), user("2")], 2)


class TestSummaryMessage:
    def test_shape(self):
        msg = summary_message("the summary")
        assert msg["role"] == "user"
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"].startswith(SUMMARY_MARKER)
        assert "the summary" in msg["content"][0]["text"]


class TestRenderTranscript:
    def test_includes_roles_and_tools(self):
        msgs = [
            user("do it"),
            assistant_tool_use("Read", {"file": "a.py"}, "t1"),
            tool_result("t1", "file contents"),
        ]
        text = render_transcript(msgs, result_cap=100)
        assert "user: do it" in text
        assert "calls Read" in text
        assert "a.py" in text
        assert "file contents" in text

    def test_caps_long_results(self):
        msgs = [tool_result("t1", "x" * 500)]
        text = render_transcript(msgs, result_cap=100)
        assert "x" * 100 in text
        assert "chars truncated" in text

    def test_error_marked(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": "boom"}]}]
        assert "[error] boom" in render_transcript(msgs, 100)

    def test_thinking_skipped(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "visible"}]}]
        text = render_transcript(msgs, 100)
        assert "internal" not in text
        assert "visible" in text


class TestPlan:
    def big_body(self, turns=6, chunk="x" * 400):
        """Six turns of ~500 est. tokens each: cut at turn 5's start keeps 2."""
        messages = []
        for i in range(turns):
            messages.append(user(f"turn {i} " + chunk))
            messages.append(assistant_text(f"reply {i} " + chunk))
        return {"system": "s", "messages": messages}

    def stored(self, body, upto, summary="old summary"):
        return SessionRecord(
            key=session_key(body), upto=upto,
            prefix_hash=prefix_hash(body["messages"], upto),
            summary=summary, saved_tokens=5000, calls=1)

    def test_small_session_passthrough(self):
        body = {"system": "s", "messages": [user("hi"), assistant_text("ok")]}
        assert plan(body, None, CFG).action == "passthrough"

    def test_over_threshold_init(self):
        body = self.big_body()
        p = plan(body, None, CFG)
        assert p.action == "init"
        assert p.upto == 8          # covers turns 0..3, keeps the last two turns
        assert p.span_tokens >= CFG.min_span_tokens
        assert p.prev_summary == ""

    def test_stored_and_covered_reuses(self):
        body = self.big_body()
        p = plan(body, self.stored(body, 8), CFG)
        assert p.action == "reuse"
        assert p.upto == 8
        assert p.saved_tokens > 0

    def test_stale_record_ignored(self):
        body = self.big_body()
        rec = self.stored(body, 8)
        body["messages"][0]["content"] = "rewound task"   # checkpoint rewind
        p = plan(body, rec, CFG)
        assert p.action == "init"
        assert p.base_upto == 0

    def test_growth_extends(self):
        body = self.big_body()
        stored = self.stored(body, 4)
        # Above threshold still (reuse would apply) and new span available:
        # grow the tail so the reused body exceeds the threshold.
        body["messages"] += [user("turn 7 " + "y" * 400), assistant_text("reply 7 " + "y" * 400),
                             user("turn 8 " + "y" * 400), assistant_text("reply 8 " + "y" * 400)]
        p = plan(body, stored, CFG)
        assert p.action == "extend"
        assert p.base_upto == 4
        assert p.prev_summary == "old summary"

    def test_below_threshold_with_growth_reuses(self):
        body = self.big_body()
        stored = self.stored(body, 8, summary="covers everything")
        p = plan(body, stored, CFG)
        assert p.action == "reuse"

    def test_tiny_span_waits(self):
        cfg = SimpleNamespace(threshold_tokens=1000, keep_recent_turns=2, min_span_tokens=10_000)
        body = self.big_body()
        p = plan(body, None, cfg)
        assert p.action == "passthrough"

    def test_deterministic(self):
        body = self.big_body()
        assert plan(body, None, CFG).__dict__ == plan(body, None, CFG).__dict__

    def test_non_dict_body(self):
        assert plan(None, None, CFG).action == "passthrough"
