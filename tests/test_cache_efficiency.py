import sqlite3

import pytest

from parser import ingest
from rules import cache_efficiency


def _turn(ts, model, cache_creation, cache_5m=0, cache_1h=0):
    return {
        "ts": ts,
        "model": model,
        "cache_creation": cache_creation,
        "cache_5m": cache_5m,
        "cache_1h": cache_1h,
    }


def test_detect_no_findings_without_a_model_switch():
    turns = [
        _turn("t1", "claude-sonnet-5", 100_000),  # first turn writes a lot — fine
        _turn("t2", "claude-sonnet-5", 5_000),
        _turn("t3", "claude-sonnet-5", 3_000),
    ]
    assert cache_efficiency.detect("s", turns) == []


def test_detect_flags_switch_with_large_recache():
    turns = [
        _turn("t1", "claude-sonnet-5", 2_000),
        _turn("t2", "claude-sonnet-5", 3_000),
        _turn("t3", "claude-opus-4-8", 80_000),  # switch: whole context re-cached
    ]
    findings = cache_efficiency.detect("sess-x", turns)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "cache_efficiency"
    assert f.session_id == "sess-x"
    assert f.est_wasted_tokens == 80_000
    # 80k unsplit cache_creation priced as 1h writes on opus: 80k/1M * $5 * 2
    assert f.est_wasted_usd == pytest.approx(0.8)
    assert "claude-sonnet-5 -> claude-opus-4-8" in f.message


def test_detect_ignores_switch_below_rewrite_threshold():
    # switching right after /clear (or at session start) re-writes almost
    # nothing — that's the recommended pattern, so it must not be flagged
    turns = [
        _turn("t1", "claude-sonnet-5", 2_000),
        _turn("t2", "claude-opus-4-8", 5_000),
    ]
    assert cache_efficiency.detect("s", turns) == []


def test_detect_flags_each_qualifying_switch():
    turns = [
        _turn("t1", "claude-sonnet-5", 2_000),
        _turn("t2", "claude-opus-4-8", 60_000),
        _turn("t3", "claude-opus-4-8", 1_000),
        _turn("t4", "claude-sonnet-5", 90_000),
    ]
    findings = cache_efficiency.detect("s", turns)
    assert [f.est_wasted_tokens for f in findings] == [60_000, 90_000]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def test_run_excludes_sidechain_synthetic_and_error_turns(conn):
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) "
        "VALUES ('sess-db', 'proj', 't0', 'claude-sonnet-5')"
    )

    def insert(uuid, ts, model, cache_creation, is_sidechain=0, is_error=0):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, model, cache_creation, "
            "is_sidechain, is_error, record_uuid) "
            "VALUES ('sess-db', ?, 'assistant', ?, ?, ?, ?, ?)",
            (ts, model, cache_creation, is_sidechain, is_error, uuid),
        )

    insert("a-1", "t1", "claude-sonnet-5", 2_000)
    insert("side", "t1.5", "claude-haiku-4-5", 999_999, is_sidechain=1)  # excluded
    insert("synth", "t2", "<synthetic>", 0)  # excluded
    insert("err", "t2.5", "claude-opus-4-8", 0, is_error=1)  # excluded
    insert("a-2", "t3", "claude-opus-4-8", 70_000)  # real switch
    conn.commit()

    findings = cache_efficiency.run(conn)
    assert len(findings) == 1
    assert findings[0].est_wasted_tokens == 70_000
    assert "claude-sonnet-5 -> claude-opus-4-8" in findings[0].message
