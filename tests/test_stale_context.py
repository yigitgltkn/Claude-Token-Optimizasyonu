import sqlite3

import pytest

from parser import ingest
from rules import stale_context


def _turns(*context_tokens_seq):
    return [{"ts": f"t{i}", "context_tokens": tok} for i, tok in enumerate(context_tokens_seq)]


def test_detect_empty_for_fewer_than_two_turns():
    assert stale_context.detect("s", []) == []
    assert stale_context.detect("s", _turns(60_000)) == []


def test_detect_ignores_fluctuating_growth_that_never_crosses_threshold():
    # two runs (2000 -> 1500 is a drop), both peak well under 50k
    turns = _turns(1000, 2000, 1500, 3000)
    assert stale_context.detect("s", turns) == []


def test_detect_ignores_monotonic_growth_below_threshold():
    turns = _turns(1000, 5000, 10000, 20000, 40000)
    assert stale_context.detect("s", turns) == []


def test_detect_flags_monotonic_growth_past_threshold():
    turns = _turns(5_000, 8_000, 52_000, 55_000, 58_000)
    findings = stale_context.detect("sess-x", turns)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "stale_context"
    assert finding.session_id == "sess-x"
    # largest jump: 8_000 -> 52_000 (+44_000); baseline=8_000; 3 turns after
    # (incl. jump turn). Rebuild floor is the run's opening turn (5_000) — a
    # /clear here would have re-paid that — so only 8_000-5_000 was clearable.
    assert finding.est_wasted_tokens == 3_000 * 3


def test_detect_subtracts_rebuild_floor_not_the_whole_prefix():
    """A /clear does not restart at zero: the system prompt, tools and
    CLAUDE.md come straight back. The run's first turn measures exactly what
    that fresh start costs in this session, so only the excess above it is
    genuinely clearable. Charging the full prefix would inflate the finding."""
    turns = _turns(20_000, 30_000, 100_000, 101_000)
    findings = stale_context.detect("sess-floor", turns)
    assert len(findings) == 1
    # baseline=30_000, rebuild floor=20_000 -> clearable=10_000, 2 turns after
    assert findings[0].est_wasted_tokens == 10_000 * 2
    # the naive "whole prefix" answer would have been 6x larger
    assert findings[0].est_wasted_tokens < 30_000 * 2


def test_detect_no_finding_when_nothing_is_clearable():
    """Largest jump right at the run's start: the prefix carried forward *is*
    the fresh-start floor, so there is nothing a /clear could have shed."""
    turns = _turns(10_000, 90_000, 91_000, 92_000)
    assert stale_context.detect("sess-nofloor", turns) == []


def test_detect_picks_the_largest_jump_not_first_or_last():
    turns = _turns(1_000, 31_000, 32_000, 90_000, 91_000)
    findings = stale_context.detect("sess-y", turns)
    assert len(findings) == 1
    # largest jump: 32_000 -> 90_000 (+58_000); baseline=32_000;
    # rebuild floor=1_000 -> clearable=31_000; 2 turns after
    assert findings[0].est_wasted_tokens == 31_000 * 2


def test_detect_resets_after_a_real_clear_drop():
    # run 1: 5_000 -> 20_000 -> 60_000 -> 62_000 (peak 62_000, flagged)
    # drop to 10_000 simulates a /clear
    # run 2: 10_000 -> 25_000 -> 30_000 (peak 30_000, under threshold, not flagged)
    turns = _turns(5_000, 20_000, 60_000, 62_000, 10_000, 25_000, 30_000)
    findings = stale_context.detect("sess-z", turns)
    assert len(findings) == 1
    # largest jump in run 1: 20_000 -> 60_000 (+40_000); baseline=20_000;
    # rebuild floor=5_000 -> clearable=15_000; 2 turns after
    assert findings[0].est_wasted_tokens == 15_000 * 2


def test_detect_each_run_uses_its_own_rebuild_floor():
    """After a /clear the floor is re-measured: run 2's fresh start is its own
    opening turn, not the session's."""
    # run 1: 5_000 -> 10_000 -> 70_000 (clearable 5_000 x 1)
    # /clear -> run 2 opens at 30_000 (a heavier CLAUDE.md-laden restart)
    # run 2: 30_000 -> 40_000 -> 120_000 -> 121_000 (clearable 10_000 x 2)
    turns = _turns(5_000, 10_000, 70_000, 30_000, 40_000, 120_000, 121_000)
    findings = stale_context.detect("sess-multi", turns)
    assert len(findings) == 2
    assert findings[0].est_wasted_tokens == 5_000 * 1
    assert findings[1].est_wasted_tokens == 10_000 * 2


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def _insert_turn(conn, session_id, uuid, ts, role, model, input_tokens,
                  cache_read=0, cache_creation=0, is_sidechain=False):
    conn.execute(
        """
        INSERT INTO turns
            (session_id, ts, role, model, input_tokens, output_tokens,
             cache_creation, cache_read, is_sidechain, record_uuid)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (session_id, ts, role, model, input_tokens, cache_creation, cache_read,
         int(is_sidechain), uuid),
    )


def test_run_filters_sidechain_and_synthetic_turns(conn):
    session_id = "sess-db"
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) VALUES (?, ?, ?, ?)",
        (session_id, "proj", "t0", "claude-sonnet-5"),
    )

    # main-thread turns, same context_tokens sequence as
    # test_detect_flags_monotonic_growth_past_threshold, but split across
    # input_tokens/cache_read/cache_creation the way a real cached
    # conversation looks (small input_tokens, most of the size in cache_read)
    main_thread = [
        ("t1", 100, 4_900, 0),
        ("t2", 100, 7_900, 0),
        ("t3", 100, 51_900, 0),
        ("t4", 100, 54_900, 0),
        ("t5", 100, 57_900, 0),
    ]
    for i, (ts, input_tokens, cache_read, cache_creation) in enumerate(main_thread):
        _insert_turn(conn, session_id, f"a-{i}", ts, "assistant", "claude-sonnet-5",
                     input_tokens, cache_read=cache_read, cache_creation=cache_creation)

    # user turns interleaved — always input_tokens 0, should be excluded by role filter
    _insert_turn(conn, session_id, "u-1", "t0.5", "user", None, 0)

    # sidechain (subagent) turn with a huge context — must be excluded
    _insert_turn(conn, session_id, "side-1", "t2.5", "assistant", "claude-sonnet-5",
                 999_999, is_sidechain=True)

    # synthetic/error turn — must be excluded (would otherwise look like a drop to 0)
    _insert_turn(conn, session_id, "synth-1", "t3.5", "assistant", "<synthetic>", 0)

    conn.commit()

    findings = stale_context.run(conn)
    assert len(findings) == 1
    assert findings[0].session_id == session_id
    # context_tokens per turn = input_tokens + cache_read + cache_creation = 5000/8000/52000/55000/58000
    # largest jump: 8_000 -> 52_000 (+44_000); baseline=8_000;
    # rebuild floor=5_000 -> clearable=3_000; 3 turns after
    assert findings[0].est_wasted_tokens == 3_000 * 3


def test_run_uses_input_plus_cache_not_input_tokens_alone(conn):
    """Regression test for the bug this module used to have: input_tokens
    alone (the uncached delta) almost never grows past a normal threshold
    because prompt caching keeps it small turn-over-turn, while the real
    context size lives mostly in cache_read. A rule that only looked at
    input_tokens would never fire here even though the real context clearly
    blows past the threshold."""
    session_id = "sess-cached"
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) VALUES (?, ?, ?, ?)",
        (session_id, "proj", "t0", "claude-sonnet-5"),
    )
    # input_tokens stays tiny every turn (a few hundred), like a real cached
    # session; cache_read is what actually grows to reflect history size.
    turns = [
        ("t1", 200, 2_000),
        ("t2", 150, 10_000),
        ("t3", 300, 60_000),
        ("t4", 120, 65_000),
    ]
    for i, (ts, input_tokens, cache_read) in enumerate(turns):
        _insert_turn(conn, session_id, f"a-{i}", ts, "assistant", "claude-sonnet-5",
                     input_tokens, cache_read=cache_read)
    conn.commit()

    findings = stale_context.run(conn)
    assert len(findings) == 1
    assert findings[0].session_id == session_id


def test_run_returns_empty_for_no_sessions(conn):
    assert stale_context.run(conn) == []
