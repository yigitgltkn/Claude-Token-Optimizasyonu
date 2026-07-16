import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from parser import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def test_parse_jsonl_file_reads_all_lines():
    records = ingest.parse_jsonl_file(FIXTURE)
    assert len(records) == 8


def test_parse_jsonl_file_skips_malformed_lines(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"type": "user", "sessionId": "s1", "timestamp": "t1", "uuid": "u1"}\n'
        "not valid json\n"
        '{"type": "assistant", "sessionId": "s1", "timestamp": "t2", "uuid": "a1", "message": {"model": "m", "usage": {}}}\n',
        encoding="utf-8",
    )
    records = ingest.parse_jsonl_file(bad)
    assert len(records) == 2
    assert records[0]["type"] == "user"
    assert records[1]["type"] == "assistant"


def test_record_to_turn_skips_non_conversational_types():
    records = ingest.parse_jsonl_file(FIXTURE)
    queue_op = records[0]
    ai_title = records[5]
    assert ingest.record_to_turn(queue_op, 1) is None
    assert ingest.record_to_turn(ai_title, 6) is None


def test_record_to_turn_maps_user_record():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[1], 2)
    assert turn["role"] == "user"
    assert turn["model"] is None
    assert turn["input_tokens"] == 0
    assert turn["output_tokens"] == 0
    assert turn["cache_creation"] == 0
    assert turn["cache_read"] == 0
    assert turn["is_sidechain"] is False


def test_record_to_turn_maps_assistant_record_with_usage():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[2], 3)
    assert turn["role"] == "assistant"
    assert turn["model"] == "claude-sonnet-5"
    assert turn["input_tokens"] == 120
    assert turn["output_tokens"] == 45
    assert turn["cache_creation"] == 300
    assert turn["cache_read"] == 900


def test_record_to_turn_synthetic_assistant_has_zero_usage():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[3], 4)
    assert turn["model"] == "<synthetic>"
    assert turn["input_tokens"] == 0
    assert turn["output_tokens"] == 0
    assert turn["cache_creation"] == 0
    assert turn["cache_read"] == 0


def test_record_to_turn_preserves_is_sidechain_flag():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[4], 5)
    assert turn["is_sidechain"] is True


def test_record_to_turn_extracts_enrichment_fields():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[2], 3)  # a-1
    assert turn["prompt_id"] == "p-1"
    assert turn["parent_uuid"] == "u-1"
    assert turn["stop_reason"] == "end_turn"
    assert turn["is_error"] is False
    assert turn["cache_5m"] == 100
    assert turn["cache_1h"] == 200
    assert turn["speed"] == "standard"
    assert turn["service_tier"] == "standard"
    assert turn["num_iterations"] == 1


def test_record_to_turn_flags_api_error():
    records = ingest.parse_jsonl_file(FIXTURE)
    turn = ingest.record_to_turn(records[3], 4)  # a-2, authentication_failed
    assert turn["is_error"] is True


def test_extract_tool_calls_from_assistant_record():
    records = ingest.parse_jsonl_file(FIXTURE)
    calls = ingest.extract_tool_calls(records[6])  # a-4 with tool_use
    assert len(calls) == 1
    assert calls[0]["tool_use_id"] == "toolu-1"
    assert calls[0]["name"] == "Read"
    assert calls[0]["turn_uuid"] == "a-4"
    assert calls[0]["input_chars"] > 0


def test_extract_tool_results_from_user_record():
    records = ingest.parse_jsonl_file(FIXTURE)
    results = ingest.extract_tool_results(records[7])  # u-tr-1 with tool_result
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "toolu-1"
    assert results[0]["result_chars"] == len("print('hello world')")
    assert results[0]["is_error"] is False


def test_ingest_file_creates_session_and_turns(conn):
    new_turns = ingest.ingest_file(conn, FIXTURE)
    assert new_turns == 6

    session = conn.execute(
        "SELECT id, project, started_at, model_main, title FROM sessions"
    ).fetchall()
    assert len(session) == 1
    session_id, project, started_at, model_main, title = session[0]
    assert session_id == "sess-001"
    assert project == "fixtures"
    assert started_at == "2026-07-10T10:00:00.000Z"
    assert model_main == "claude-sonnet-5"
    assert title == "Sample debugging session"

    turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert turns == 6


def test_ingest_file_populates_tool_calls(conn):
    ingest.ingest_file(conn, FIXTURE)
    rows = conn.execute(
        "SELECT tool_use_id, session_id, turn_uuid, name, input_chars, result_chars, is_error "
        "FROM tool_calls"
    ).fetchall()
    assert len(rows) == 1
    tool_use_id, session_id, turn_uuid, name, input_chars, result_chars, is_error = rows[0]
    assert tool_use_id == "toolu-1"
    assert session_id == "sess-001"
    assert turn_uuid == "a-4"
    assert name == "Read"
    assert input_chars > 0
    assert result_chars == len("print('hello world')")
    assert is_error == 0


def test_ingest_file_is_idempotent_on_unchanged_reingest(conn):
    ingest.ingest_file(conn, FIXTURE)
    second_run_new_turns = ingest.ingest_file(conn, FIXTURE)
    assert second_run_new_turns == 0
    turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert turns == 6
    tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert tool_calls == 1


def test_init_db_migrates_old_schema_and_reingest_backfills(tmp_path):
    """A DB created with the original schema gains the new columns on
    init_db(), and a forced re-ingest fills them in for existing rows."""
    old_schema = """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, project TEXT NOT NULL,
        started_at TEXT NOT NULL, model_main TEXT
    );
    CREATE TABLE turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        ts TEXT NOT NULL, role TEXT NOT NULL, model TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation INTEGER NOT NULL DEFAULT 0,
        cache_read INTEGER NOT NULL DEFAULT 0,
        is_sidechain INTEGER NOT NULL DEFAULT 0,
        record_uuid TEXT,
        UNIQUE(session_id, record_uuid)
    );
    CREATE TABLE ingested_files (
        path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL,
        sha256 TEXT NOT NULL, ingested_at TEXT NOT NULL
    );
    """
    db_path = tmp_path / "old.db"
    c = sqlite3.connect(db_path)
    c.executescript(old_schema)
    c.execute(
        "INSERT INTO sessions (id, project, started_at) VALUES ('sess-001', 'fixtures', 't0')"
    )
    c.execute(
        "INSERT INTO turns (session_id, ts, role, model, record_uuid) "
        "VALUES ('sess-001', '2026-07-10T10:00:02.000Z', 'assistant', 'claude-sonnet-5', 'a-1')"
    )
    c.commit()
    c.close()

    conn = ingest.connect(db_path)  # runs the migration
    turn_cols = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
    assert {"prompt_id", "stop_reason", "cache_5m", "cache_1h", "num_iterations"} <= turn_cols
    assert "title" in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}

    # Existing row has defaults until re-ingested...
    assert conn.execute("SELECT prompt_id FROM turns WHERE record_uuid='a-1'").fetchone()[0] is None
    # ...and a re-ingest of the same log backfills it in place.
    new_turns = ingest.ingest_file(conn, FIXTURE)
    assert new_turns == 5  # a-1 already existed, 5 of 6 fixture turns are new
    prompt_id, stop_reason = conn.execute(
        "SELECT prompt_id, stop_reason FROM turns WHERE record_uuid='a-1'"
    ).fetchone()
    assert prompt_id == "p-1"
    assert stop_reason == "end_turn"
    conn.close()


def test_needs_ingest_skips_unchanged_file(conn, tmp_path):
    target = tmp_path / "sample_session.jsonl"
    shutil.copy(FIXTURE, target)

    assert ingest.needs_ingest(conn, target) is True
    ingest.ingest_file(conn, target)
    ingest.mark_ingested(conn, target)
    assert ingest.needs_ingest(conn, target) is False


def test_needs_ingest_detects_touched_but_identical_content(conn, tmp_path):
    target = tmp_path / "sample_session.jsonl"
    shutil.copy(FIXTURE, target)
    ingest.ingest_file(conn, target)
    ingest.mark_ingested(conn, target)

    future = target.stat().st_mtime + 100
    os.utime(target, (future, future))

    assert ingest.needs_ingest(conn, target) is False


def test_needs_ingest_detects_modified_content(conn, tmp_path):
    target = tmp_path / "sample_session.jsonl"
    shutil.copy(FIXTURE, target)
    ingest.ingest_file(conn, target)
    ingest.mark_ingested(conn, target)

    with target.open("a", encoding="utf-8") as f:
        f.write(
            '{"type": "user", "sessionId": "sess-001", "timestamp": "2026-07-10T10:00:06.000Z", '
            '"uuid": "u-2", "isSidechain": false, "message": {"role": "user", "content": []}}\n'
        )

    assert ingest.needs_ingest(conn, target) is True

    new_turns = ingest.ingest_file(conn, target)
    assert new_turns == 1
    total_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert total_turns == 7


def test_run_ingest_second_run_processes_zero_new_records(conn, tmp_path, monkeypatch):
    fake_project_dir = tmp_path / "projects" / "c--fake-project"
    fake_project_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, fake_project_dir / "sample_session.jsonl")

    monkeypatch.setattr(ingest, "claude_projects_dir", lambda: tmp_path / "projects")

    first = ingest.run_ingest(conn)
    assert first["new_turns"] == 6
    assert first["files_ingested"] == 1
    assert first["files_skipped"] == 0

    second = ingest.run_ingest(conn)
    assert second["new_turns"] == 0
    assert second["files_ingested"] == 0
    assert second["files_skipped"] == 1

    forced = ingest.run_ingest(conn, force=True)
    assert forced["new_turns"] == 0
    assert forced["files_ingested"] == 1
    assert forced["files_skipped"] == 0
