"""JSONL -> SQLite ingestion for Claude Code session logs."""

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    model_main  TEXT,
    title       TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    ts              TEXT NOT NULL,
    role            TEXT NOT NULL,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_creation  INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    is_sidechain    INTEGER NOT NULL DEFAULT 0,
    record_uuid     TEXT,
    prompt_id       TEXT,
    parent_uuid     TEXT,
    stop_reason     TEXT,
    is_error        INTEGER NOT NULL DEFAULT 0,
    cache_5m        INTEGER NOT NULL DEFAULT 0,
    cache_1h        INTEGER NOT NULL DEFAULT 0,
    speed           TEXT,
    service_tier    TEXT,
    num_iterations  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, record_uuid)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id  TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    turn_uuid    TEXT,
    name         TEXT NOT NULL DEFAULT '',
    input_chars  INTEGER NOT NULL DEFAULT 0,
    result_chars INTEGER,
    is_error     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);

CREATE TABLE IF NOT EXISTS ingested_files (
    path         TEXT PRIMARY KEY,
    mtime        REAL NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    ingested_at  TEXT NOT NULL
);
"""

# Columns added after the original release. init_db() ALTERs them onto
# pre-existing databases; values stay at their defaults until a
# `cli.py ingest --rebuild` re-reads the logs and backfills.
_TURNS_MIGRATIONS = {
    "prompt_id": "TEXT",
    "parent_uuid": "TEXT",
    "stop_reason": "TEXT",
    "is_error": "INTEGER NOT NULL DEFAULT 0",
    "cache_5m": "INTEGER NOT NULL DEFAULT 0",
    "cache_1h": "INTEGER NOT NULL DEFAULT 0",
    "speed": "TEXT",
    "service_tier": "TEXT",
    "num_iterations": "INTEGER NOT NULL DEFAULT 0",
}
_SESSIONS_MIGRATIONS = {
    "title": "TEXT",
}

CONVERSATIONAL_TYPES = {"user", "assistant"}
SYNTHETIC_MODEL = "<synthetic>"


def _migrate(conn: sqlite3.Connection) -> None:
    for table, migrations in (("turns", _TURNS_MIGRATIONS), ("sessions", _SESSIONS_MIGRATIONS)):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in migrations.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def discover_jsonl_files() -> list[Path]:
    root = claude_projects_dir()
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*.jsonl"))


def parse_jsonl_file(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def record_to_turn(record: dict[str, Any], line_no: int) -> Optional[dict[str, Any]]:
    """Map a single JSONL record to a turns row dict, or None if it's not conversational."""
    record_type = record.get("type")
    if record_type not in CONVERSATIONAL_TYPES:
        return None

    session_id = record.get("sessionId")
    ts = record.get("timestamp")
    is_sidechain = bool(record.get("isSidechain", False))
    uuid = record.get("uuid") or f"{session_id}:{ts}:{record_type}:{line_no}"
    prompt_id = record.get("promptId")
    parent_uuid = record.get("parentUuid")

    if record_type == "user":
        return {
            "session_id": session_id,
            "ts": ts,
            "role": "user",
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation": 0,
            "cache_read": 0,
            "is_sidechain": is_sidechain,
            "record_uuid": uuid,
            "prompt_id": prompt_id,
            "parent_uuid": parent_uuid,
            "stop_reason": None,
            "is_error": False,
            "cache_5m": 0,
            "cache_1h": 0,
            "speed": None,
            "service_tier": None,
            "num_iterations": 0,
        }

    message = record.get("message") or {}
    usage = message.get("usage") or {}
    cache_detail = usage.get("cache_creation") or {}
    return {
        "session_id": session_id,
        "ts": ts,
        "role": "assistant",
        "model": message.get("model"),
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_creation": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "is_sidechain": is_sidechain,
        "record_uuid": uuid,
        "prompt_id": prompt_id,
        "parent_uuid": parent_uuid,
        "stop_reason": message.get("stop_reason"),
        "is_error": bool(record.get("isApiErrorMessage") or record.get("error")),
        "cache_5m": cache_detail.get("ephemeral_5m_input_tokens", 0) or 0,
        "cache_1h": cache_detail.get("ephemeral_1h_input_tokens", 0) or 0,
        "speed": usage.get("speed"),
        "service_tier": usage.get("service_tier"),
        "num_iterations": len(usage.get("iterations") or []),
    }


def _content_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def extract_tool_calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    """tool_use blocks on an assistant record -> tool_calls row dicts (result side left NULL)."""
    if record.get("type") != "assistant":
        return []
    calls = []
    for block in _content_blocks(record):
        if block.get("type") != "tool_use" or not block.get("id"):
            continue
        calls.append(
            {
                "tool_use_id": block["id"],
                "session_id": record.get("sessionId"),
                "turn_uuid": record.get("uuid"),
                "name": block.get("name") or "",
                "input_chars": len(json.dumps(block.get("input") or {}, ensure_ascii=False)),
            }
        )
    return calls


def extract_tool_results(record: dict[str, Any]) -> list[dict[str, Any]]:
    """tool_result blocks on a user record -> {tool_use_id, result_chars, is_error} dicts."""
    if record.get("type") != "user":
        return []
    results = []
    for block in _content_blocks(record):
        if block.get("type") != "tool_result" or not block.get("tool_use_id"):
            continue
        content = block.get("content")
        if content is None:
            result_chars = 0
        elif isinstance(content, str):
            result_chars = len(content)
        else:
            result_chars = len(json.dumps(content, ensure_ascii=False))
        results.append(
            {
                "tool_use_id": block["tool_use_id"],
                "session_id": record.get("sessionId"),
                "result_chars": result_chars,
                "is_error": bool(block.get("is_error")),
            }
        )
    return results


def derive_session(
    session_id: str,
    project: str,
    records: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the sessions row (id, project, started_at, model_main, title) for one session."""
    started_at = None
    title = None
    for record in records:
        if record.get("sessionId") != session_id:
            continue
        if started_at is None and record.get("timestamp"):
            started_at = record["timestamp"]
        if record.get("type") == "ai-title" and record.get("aiTitle"):
            title = record["aiTitle"]  # keep the latest title in the file

    model_counts: Counter = Counter()
    first_seen: dict[str, int] = {}
    for idx, turn in enumerate(turns):
        model = turn.get("model")
        if turn["role"] != "assistant" or not model or model == SYNTHETIC_MODEL:
            continue
        model_counts[model] += 1
        first_seen.setdefault(model, idx)

    model_main = None
    if model_counts:
        model_main = max(model_counts, key=lambda m: (model_counts[m], -first_seen[m]))

    return {
        "id": session_id,
        "project": project,
        "started_at": started_at,
        "model_main": model_main,
        "title": title,
    }


def ingest_file(conn: sqlite3.Connection, path: Path) -> int:
    """Parse one JSONL file, upsert its session(s), turns and tool calls.

    Returns the count of turn rows that didn't exist before. Existing turn rows
    are updated in place (enrichment fields), so re-ingesting after a schema
    migration backfills the new columns.
    """
    records = parse_jsonl_file(path)
    project = path.parent.name

    turns_by_session: dict[str, list[dict[str, Any]]] = {}
    for line_no, record in enumerate(records, start=1):
        turn = record_to_turn(record, line_no)
        if turn is None or not turn["session_id"]:
            continue
        turns_by_session.setdefault(turn["session_id"], []).append(turn)

    new_turns = 0
    for session_id, turns in turns_by_session.items():
        session = derive_session(session_id, project, records, turns)
        conn.execute(
            """
            INSERT INTO sessions (id, project, started_at, model_main, title)
            VALUES (:id, :project, :started_at, :model_main, :title)
            ON CONFLICT(id) DO UPDATE SET
                project = excluded.project,
                started_at = MIN(sessions.started_at, excluded.started_at),
                model_main = excluded.model_main,
                title = COALESCE(excluded.title, sessions.title)
            """,
            session,
        )
        for turn in turns:
            exists = conn.execute(
                "SELECT 1 FROM turns WHERE session_id = ? AND record_uuid = ?",
                (turn["session_id"], turn["record_uuid"]),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO turns
                    (session_id, ts, role, model, input_tokens, output_tokens,
                     cache_creation, cache_read, is_sidechain, record_uuid,
                     prompt_id, parent_uuid, stop_reason, is_error,
                     cache_5m, cache_1h, speed, service_tier, num_iterations)
                VALUES
                    (:session_id, :ts, :role, :model, :input_tokens, :output_tokens,
                     :cache_creation, :cache_read, :is_sidechain, :record_uuid,
                     :prompt_id, :parent_uuid, :stop_reason, :is_error,
                     :cache_5m, :cache_1h, :speed, :service_tier, :num_iterations)
                ON CONFLICT(session_id, record_uuid) DO UPDATE SET
                    prompt_id = excluded.prompt_id,
                    parent_uuid = excluded.parent_uuid,
                    stop_reason = excluded.stop_reason,
                    is_error = excluded.is_error,
                    cache_5m = excluded.cache_5m,
                    cache_1h = excluded.cache_1h,
                    speed = excluded.speed,
                    service_tier = excluded.service_tier,
                    num_iterations = excluded.num_iterations
                """,
                turn,
            )
            if exists is None:
                new_turns += 1

    _ingest_tool_calls(conn, records)
    conn.commit()
    return new_turns


def _ingest_tool_calls(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    """Upsert tool_use / tool_result info. The two sides arrive on different
    records (assistant emits tool_use, the following user record carries the
    tool_result), so each side only updates its own columns."""
    for record in records:
        for call in extract_tool_calls(record):
            conn.execute(
                """
                INSERT INTO tool_calls (tool_use_id, session_id, turn_uuid, name, input_chars)
                VALUES (:tool_use_id, :session_id, :turn_uuid, :name, :input_chars)
                ON CONFLICT(tool_use_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    turn_uuid = excluded.turn_uuid,
                    name = excluded.name,
                    input_chars = excluded.input_chars
                """,
                call,
            )
        for result in extract_tool_results(record):
            conn.execute(
                """
                INSERT INTO tool_calls (tool_use_id, session_id, result_chars, is_error)
                VALUES (:tool_use_id, :session_id, :result_chars, :is_error)
                ON CONFLICT(tool_use_id) DO UPDATE SET
                    result_chars = excluded.result_chars,
                    is_error = excluded.is_error
                """,
                result,
            )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_ingest(conn: sqlite3.Connection, path: Path) -> bool:
    """True if path is new, or its content changed since the last recorded ingest."""
    row = conn.execute(
        "SELECT mtime, size, sha256 FROM ingested_files WHERE path = ?", (str(path),)
    ).fetchone()
    if row is None:
        return True
    stored_mtime, stored_size, stored_hash = row
    stat = path.stat()
    if stat.st_mtime == stored_mtime and stat.st_size == stored_size:
        return False
    return file_sha256(path) != stored_hash


def mark_ingested(conn: sqlite3.Connection, path: Path) -> None:
    stat = path.stat()
    conn.execute(
        """
        INSERT INTO ingested_files (path, mtime, size, sha256, ingested_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            mtime = excluded.mtime,
            size = excluded.size,
            sha256 = excluded.sha256,
            ingested_at = excluded.ingested_at
        """,
        (str(path), stat.st_mtime, stat.st_size, file_sha256(path)),
    )
    conn.commit()


def run_ingest(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Discover and incrementally ingest all Claude Code JSONL logs.

    force=True re-ingests every file even if unchanged — use after a schema
    migration to backfill new columns on existing rows.
    """
    files = discover_jsonl_files()
    stats = {"files_scanned": len(files), "files_ingested": 0, "files_skipped": 0, "new_turns": 0}
    for path in files:
        if not force and not needs_ingest(conn, path):
            stats["files_skipped"] += 1
            continue
        stats["new_turns"] += ingest_file(conn, path)
        mark_ingested(conn, path)
        stats["files_ingested"] += 1
    return stats
