"""Tests for `cli.py diagnose` (text + JSON output)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cli
from parser import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


@pytest.fixture
def db_with_stale_session(tmp_path):
    """A DB containing one session whose context grows monotonically past the
    stale_context threshold, guaranteeing at least one finding."""
    db_path = tmp_path / "coach.db"
    conn = ingest.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, project, started_at) "
        "VALUES ('sess-stale', 'proj', '2026-07-14T10:00:00.000Z')"
    )
    for i, cache_read in enumerate((60_000, 90_000, 130_000)):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, model, input_tokens, cache_read, record_uuid) "
            "VALUES ('sess-stale', ?, 'assistant', 'claude-sonnet-5', 1000, ?, ?)",
            (f"2026-07-14T10:0{i}:00.000Z", cache_read, f"t-{i}"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_diagnose_json_structure(db_with_stale_session, capsys):
    rc = cli.main(["diagnose", "--db", str(db_with_stale_session), "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "generated_at",
        "db",
        "total_est_wasted_tokens",
        "total_est_wasted_usd",
        "findings",
    }
    assert payload["db"] == str(db_with_stale_session)
    assert len(payload["findings"]) >= 1

    finding = payload["findings"][0]
    assert set(finding) == {
        "rule",
        "kind",
        "scope_type",
        "scope",
        "message",
        "est_wasted_tokens",
        "est_wasted_usd",
        "counterfactual_usd",
        "line",
    }
    assert finding["rule"] == "stale_context"
    assert finding["kind"] == "waste"
    assert finding["line"] is None
    assert finding["scope_type"] == "session"
    assert finding["scope"] == "sess-stale"
    assert finding["est_wasted_tokens"] > 0
    assert payload["total_est_wasted_tokens"] == sum(
        f["est_wasted_tokens"] for f in payload["findings"]
    )


@pytest.fixture
def db_with_model_mismatch(tmp_path):
    """A DB whose only finding is model_mismatch: cheap-to-downgrade opus work
    (2 turns/prompt, no errors, large output) with no context growth at all,
    so stale_context and cache_efficiency stay silent."""
    db_path = tmp_path / "coach.db"
    conn = ingest.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) "
        "VALUES ('sess-mm', 'proj', '2026-07-14T10:00:00.000Z', 'claude-opus-4-8')"
    )
    for p in range(2):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, prompt_id, record_uuid) "
            "VALUES ('sess-mm', ?, 'user', ?, ?)",
            (f"2026-07-14T10:0{p}:00.000Z", f"p-{p}", f"u-{p}"),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, model, output_tokens, record_uuid) "
            "VALUES ('sess-mm', ?, 'assistant', 'claude-opus-4-8', 500000, ?)",
            (f"2026-07-14T10:1{i}:00.000Z", f"a-{i}"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_diagnose_info_findings_never_reach_the_waste_total(db_with_model_mismatch, capsys):
    """model_mismatch states a counterfactual price difference. Counting it as
    waste would claim the user lost money that a cheaper model *might* not
    have saved — the tool must under-claim, not over-claim."""
    rc = cli.main(["diagnose", "--db", str(db_with_model_mismatch), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    info = [f for f in payload["findings"] if f["kind"] == "info"]
    assert len(info) == 1
    assert info[0]["rule"] == "model_mismatch"
    # the comparison itself is real and preserved...
    assert info[0]["counterfactual_usd"] == pytest.approx(20.0)
    # ...but it is not waste, and the headline says so
    assert info[0]["est_wasted_usd"] == 0.0
    assert payload["total_est_wasted_usd"] == 0.0
    assert payload["total_est_wasted_tokens"] == 0


def test_diagnose_text_output_separates_info_from_waste(db_with_model_mismatch, capsys):
    rc = cli.main(["diagnose", "--db", str(db_with_model_mismatch)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Bilgi (israf değil, karşılaştırma)" in out
    assert "model_mismatch" in out
    # israf satırı sıfırı raporlamalı, karşılaştırmayı israfa saymamalı
    assert "0 bulgu, tahmini israf ~0 token (= $0.00)." in out


def test_diagnose_includes_claude_md_lint_findings(db_with_stale_session, tmp_path, capsys):
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Rules\n\nAlways run the full test suite before committing.\n"
        "Always run the full test suite before committing.\n",
        encoding="utf-8",
    )

    rc = cli.main(
        ["diagnose", "--db", str(db_with_stale_session), "--json", "--project", str(project)]
    )
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    file_findings = [f for f in payload["findings"] if f["scope_type"] == "file"]
    dup = [f for f in file_findings if f["rule"] == "duplicated_line"]
    assert dup
    assert dup[0]["line"] == 4  # the redundant second occurrence

    # Findings are sorted by est_wasted_tokens descending.
    wasted = [f["est_wasted_tokens"] for f in payload["findings"]]
    assert wasted == sorted(wasted, reverse=True)


def test_diagnose_days_filters_old_session_findings(tmp_path, capsys):
    db_path = tmp_path / "coach.db"
    conn = ingest.connect(db_path)
    stale_pattern = (60_000, 90_000, 130_000)
    sessions = [
        ("sess-recent", datetime.now(timezone.utc).isoformat()),
        ("sess-ancient", "2020-01-01T00:00:00.000Z"),
    ]
    for session_id, started_at in sessions:
        conn.execute(
            "INSERT INTO sessions (id, project, started_at) VALUES (?, 'proj', ?)",
            (session_id, started_at),
        )
        for i, cache_read in enumerate(stale_pattern):
            conn.execute(
                "INSERT INTO turns (session_id, ts, role, model, input_tokens, cache_read, record_uuid) "
                "VALUES (?, ?, 'assistant', 'claude-sonnet-5', 1000, ?, ?)",
                (session_id, f"t{i}", cache_read, f"{session_id}-t{i}"),
            )
    conn.commit()
    conn.close()

    rc = cli.main(["diagnose", "--db", str(db_path), "--json", "--days", "7"])
    assert rc == 0
    scopes = {f["scope"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert scopes == {"sess-recent"}

    rc = cli.main(["diagnose", "--db", str(db_path), "--json"])  # days=0 -> hepsi
    assert rc == 0
    scopes = {f["scope"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert scopes == {"sess-recent", "sess-ancient"}


def test_diagnose_text_output_on_empty_db(tmp_path, capsys):
    db_path = tmp_path / "empty.db"
    ingest.connect(db_path).close()

    rc = cli.main(["diagnose", "--db", str(db_path)])
    assert rc == 0
    assert "Bulgu yok." in capsys.readouterr().out


def test_diagnose_text_output_with_findings(db_with_stale_session, capsys):
    rc = cli.main(["diagnose", "--db", str(db_with_stale_session)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[stale_context] oturum sess-stale" in out
    assert "tahmini israf" in out
