"""Store round-trips and stats."""

from awecompress.store import SessionRecord, Store


def test_roundtrip(tmp_path):
    store = Store(tmp_path / "summaries.db")
    store.put(SessionRecord("k1", 8, "abc123", "summary text", 4000, 1, 1.0))
    rec = store.get("k1")
    assert rec.upto == 8
    assert rec.prefix_hash == "abc123"
    assert rec.summary == "summary text"
    assert rec.saved_tokens == 4000
    assert rec.calls == 1
    store.close()


def test_missing_key(tmp_path):
    store = Store(tmp_path / "summaries.db")
    assert store.get("nope") is None
    store.close()


def test_put_replaces(tmp_path):
    store = Store(tmp_path / "summaries.db")
    store.put(SessionRecord("k1", 4, "h1", "old"))
    store.put(SessionRecord("k1", 8, "h2", "new"))
    rec = store.get("k1")
    assert rec.upto == 8 and rec.summary == "new"
    store.close()


def test_stats_and_clear(tmp_path):
    store = Store(tmp_path / "summaries.db")
    assert store.stats() == {"sessions": 0, "calls": 0, "saved_tokens": 0}
    store.put(SessionRecord("k1", 8, "h", "s", saved_tokens=4000, calls=2))
    store.log_event("k1", "init", 9000, 1200, "model-x")
    stats = store.stats()
    assert stats["sessions"] == 1
    assert stats["calls"] == 1
    assert stats["saved_tokens"] == 4000
    store.clear()
    assert store.stats() == {"sessions": 0, "calls": 0, "saved_tokens": 0}
    store.close()


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "summaries.db"
    store = Store(path)
    store.put(SessionRecord("k1", 8, "h", "frozen summary"))
    store.close()
    again = Store(path)
    assert again.get("k1").summary == "frozen summary"
    again.close()
