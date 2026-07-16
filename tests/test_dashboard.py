import sqlite3

import pytest

from parser import ingest
from report import dashboard
from rules import stale_context


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def _insert_session(conn, session_id, started_at, project="proj", model_main="claude-sonnet-5"):
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) VALUES (?, ?, ?, ?)",
        (session_id, project, started_at, model_main),
    )


def _insert_turn(conn, session_id, uuid, ts, role, model, input_tokens, output_tokens=0,
                  cache_creation=0, cache_read=0, is_sidechain=False):
    conn.execute(
        """
        INSERT INTO turns
            (session_id, ts, role, model, input_tokens, output_tokens,
             cache_creation, cache_read, is_sidechain, record_uuid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, ts, role, model, input_tokens, output_tokens, cache_creation, cache_read,
         int(is_sidechain), uuid),
    )


def test_overview_stats_aggregates_tokens_and_findings(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z")
    _insert_turn(conn, "s1", "u1", "2026-07-09T10:00:00.000Z", "user", None, 0)
    _insert_turn(conn, "s1", "a1", "2026-07-09T10:00:01.000Z", "assistant", "claude-sonnet-5",
                 100, 50, 10, 20)
    conn.commit()

    finding = stale_context.Finding(rule="stale_context", session_id="s1", message="m", est_wasted_tokens=42)
    stats = dashboard.overview_stats(conn, [finding])

    assert stats["session_count"] == 1
    assert stats["turn_count"] == 2
    assert stats["total_tokens"] == 100 + 50 + 10 + 20
    assert stats["total_wasted_tokens"] == 42


def test_overview_stats_empty_db(conn):
    stats = dashboard.overview_stats(conn, [])
    assert stats == {"session_count": 0, "turn_count": 0, "total_tokens": 0, "total_wasted_tokens": 0}


def test_session_summaries_orders_most_recent_first(conn):
    _insert_session(conn, "older", "2026-07-01T10:00:00.000Z")
    _insert_session(conn, "newer", "2026-07-09T10:00:00.000Z")
    _insert_turn(conn, "older", "a1", "2026-07-01T10:00:01.000Z", "assistant", "claude-sonnet-5", 100)
    _insert_turn(conn, "newer", "a2", "2026-07-09T10:00:01.000Z", "assistant", "claude-sonnet-5", 200)
    conn.commit()

    summaries = dashboard.session_summaries(conn)
    assert [s["id"] for s in summaries] == ["newer", "older"]
    assert summaries[0]["total_tokens"] == 200
    assert summaries[0]["turn_count"] == 1


def test_daily_token_usage_groups_by_date(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z")
    _insert_turn(conn, "s1", "a1", "2026-07-09T09:00:00.000Z", "assistant", "claude-sonnet-5",
                 100, 10, 1, 2)
    _insert_turn(conn, "s1", "a2", "2026-07-09T15:00:00.000Z", "assistant", "claude-sonnet-5",
                 50, 5, 0, 0)
    _insert_turn(conn, "s1", "a3", "2026-07-10T09:00:00.000Z", "assistant", "claude-sonnet-5",
                 20, 2, 0, 0)
    conn.commit()

    daily = dashboard.daily_token_usage(conn)
    assert [d["date"] for d in daily] == ["2026-07-09", "2026-07-10"]
    assert daily[0]["input_tokens"] == 150
    assert daily[0]["output_tokens"] == 15
    assert daily[0]["cache_creation"] == 1
    assert daily[0]["cache_read"] == 2
    assert daily[1]["input_tokens"] == 20


def test_model_usage_excludes_synthetic_and_user_turns(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z")
    _insert_turn(conn, "s1", "u1", "2026-07-09T09:00:00.000Z", "user", None, 0)
    _insert_turn(conn, "s1", "a1", "2026-07-09T09:00:01.000Z", "assistant", "claude-sonnet-5", 100)
    _insert_turn(conn, "s1", "a2", "2026-07-09T09:00:02.000Z", "assistant", "claude-opus-4-8", 300)
    _insert_turn(conn, "s1", "a3", "2026-07-09T09:00:03.000Z", "assistant", "<synthetic>", 0)
    conn.commit()

    usage = dashboard.model_usage(conn)
    models = {m["model"]: m for m in usage}
    assert set(models) == {"claude-sonnet-5", "claude-opus-4-8"}
    assert models["claude-opus-4-8"]["total_tokens"] == 300
    # ordered by total_tokens desc
    assert usage[0]["model"] == "claude-opus-4-8"


def test_render_html_without_project_path_includes_stale_context_only(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z")
    for i, tokens in enumerate([5_000, 8_000, 52_000, 55_000]):
        _insert_turn(conn, "s1", f"a-{i}", f"2026-07-09T10:0{i}:00.000Z", "assistant",
                     "claude-sonnet-5", tokens)
    conn.commit()

    out = dashboard.render_html(conn)
    assert "stale_context" in out
    assert "s1" in out
    assert "path_scoped_candidate" not in out
    assert "CLAUDE.md bulgulari dahil degil" in out


def test_render_html_with_project_path_includes_claude_md_findings(conn, tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# Proj\n\n## Frontend (`src/web/app/`)\n\nAlways run the test suite before committing.\n"
        "Always run the test suite before committing.\n",
        encoding="utf-8",
    )

    out = dashboard.render_html(conn, project_paths=[tmp_path])
    assert "path_scoped_candidate" in out or "duplicated_line" in out
    assert "Taranan yol:" in out
    assert "tc-proj-select" in out  # project picker present


def test_render_html_multiple_projects_get_dropdown_panels(conn, tmp_path):
    for name in ("proj-a", "proj-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "CLAUDE.md").write_text(
            "# X\n\nSame duplicated line here for the linter.\nSame duplicated line here for the linter.\n",
            encoding="utf-8",
        )

    out = dashboard.render_html(conn, project_paths=[tmp_path / "proj-a", tmp_path / "proj-b"])
    assert out.count("tc-proj-panel") >= 2
    assert "proj-a" in out and "proj-b" in out
    # only the first panel starts visible
    assert 'id="tc-proj-0" style="display:block"' in out
    assert 'id="tc-proj-1" style="display:none"' in out


def test_discover_projects_finds_claude_md_dirs(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("root", encoding="utf-8")
    with_md = tmp_path / "has-md"
    with_md.mkdir()
    (with_md / "CLAUDE.md").write_text("x", encoding="utf-8")
    without_md = tmp_path / "no-md"
    without_md.mkdir()

    found = dashboard.discover_projects(tmp_path)
    assert found == [tmp_path, with_md]


def test_coaching_tips_flags_stale_context_and_multi_model(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z")
    _insert_turn(conn, "s1", "a1", "t1", "assistant", "claude-sonnet-5", 100)
    _insert_turn(conn, "s1", "a2", "t2", "assistant", "claude-opus-4-8", 100)
    conn.commit()

    finding = stale_context.Finding(rule="stale_context", session_id="s1", message="msg", est_wasted_tokens=1000)
    tips = dashboard.coaching_tips(conn, [finding])
    titles = [t for t, _ in tips]
    assert any("/clear" in t for t in titles)
    assert any("Model degisimini" in t for t in titles)


def test_coaching_tips_positive_when_no_issues(conn):
    tips = dashboard.coaching_tips(conn, [])
    assert len(tips) == 1
    assert "belirgin bir sorun yok" in tips[0][0]


def test_render_html_empty_db_does_not_crash(conn):
    out = dashboard.render_html(conn)
    assert "Bulgu yok" in out
    assert "Henuz" in out


def test_render_document_wraps_full_html_skeleton(conn):
    doc = dashboard.render_document(conn)
    assert doc.strip().startswith("<!doctype html>")
    assert "<title>Token Coach" in doc
    assert '<meta charset="utf-8">' in doc


def test_render_html_escapes_untrusted_text(conn):
    _insert_session(conn, "s1", "2026-07-09T10:00:00.000Z", project="<script>alert(1)</script>")
    _insert_turn(conn, "s1", "a1", "2026-07-09T10:00:00.000Z", "assistant", "claude-sonnet-5", 100)
    conn.commit()

    out = dashboard.render_html(conn)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
