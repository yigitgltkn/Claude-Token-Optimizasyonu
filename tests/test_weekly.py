import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from parser import ingest
from report import weekly
from rules import stale_context

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def _insert_session(conn, session_id, started_at, project="proj"):
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) VALUES (?, ?, ?, ?)",
        (session_id, project, started_at, "claude-sonnet-5"),
    )


def _insert_assistant_turn(conn, session_id, uuid, ts, input_tokens):
    conn.execute(
        """
        INSERT INTO turns
            (session_id, ts, role, model, input_tokens, output_tokens,
             cache_creation, cache_read, is_sidechain, record_uuid)
        VALUES (?, ?, 'assistant', 'claude-sonnet-5', ?, 0, 0, 0, 0, ?)
        """,
        (session_id, ts, input_tokens, uuid),
    )


def test_sessions_in_window_filters_by_started_at(conn):
    _insert_session(conn, "in-window", "2026-07-09T10:00:00.000Z")
    _insert_session(conn, "too-old", "2026-06-01T10:00:00.000Z")
    conn.commit()

    ids = weekly.sessions_in_window(conn, NOW - timedelta(days=7), NOW)
    assert ids == {"in-window"}


def test_render_markdown_no_findings():
    md = weekly.render_markdown([], NOW - timedelta(days=7), NOW)
    assert "Bu hafta bulgu yok." in md
    assert "Haftalık Rapor" in md


def test_render_markdown_sorts_by_est_wasted_tokens_desc():
    small = stale_context.Finding(rule="stale_context", session_id="s1", message="small", est_wasted_tokens=100)
    big = stale_context.Finding(rule="stale_context", session_id="s2", message="big", est_wasted_tokens=9000)
    md = weekly.render_markdown([small, big], NOW - timedelta(days=7), NOW)

    assert md.index("s2") < md.index("s1")
    assert "**Kanıt:** big" in md
    assert "**Öneri:**" in md
    assert "**Tahmini tasarruf:** ~9000 token" in md
    assert "2 bulgu, tahmini israf ~9100 token." in md


def test_collect_findings_only_includes_sessions_in_window(conn):
    # in-window session: turns cross the stale_context threshold
    _insert_session(conn, "recent", "2026-07-09T10:00:00.000Z")
    for i, tokens in enumerate([5_000, 8_000, 52_000, 55_000]):
        _insert_assistant_turn(conn, "recent", f"recent-{i}", f"2026-07-09T10:0{i}:00.000Z", tokens)

    # same stale pattern, but the session is over a month old -> must be excluded
    _insert_session(conn, "old", "2026-06-01T10:00:00.000Z")
    for i, tokens in enumerate([5_000, 8_000, 52_000, 55_000]):
        _insert_assistant_turn(conn, "old", f"old-{i}", f"2026-06-01T10:0{i}:00.000Z", tokens)
    conn.commit()

    findings = weekly.collect_findings(conn, NOW - timedelta(days=7), NOW)
    assert len(findings) == 1
    assert findings[0].session_id == "recent"


def test_weekly_report_end_to_end(conn):
    _insert_session(conn, "sess-1", "2026-07-09T10:00:00.000Z")
    for i, tokens in enumerate([5_000, 8_000, 52_000, 55_000]):
        _insert_assistant_turn(conn, "sess-1", f"u-{i}", f"2026-07-09T10:0{i}:00.000Z", tokens)
    conn.commit()

    report = weekly.weekly_report(conn, now=NOW)
    assert "# Token Coach — Haftalık Rapor" in report
    assert "stale_context" in report
    assert "sess-1" in report
    assert "**Kanıt:**" in report
    assert "**Öneri:**" in report
    assert "**Tahmini tasarruf:**" in report


def test_weekly_report_no_findings_message(conn):
    report = weekly.weekly_report(conn, now=NOW)
    assert "Bu hafta bulgu yok." in report
