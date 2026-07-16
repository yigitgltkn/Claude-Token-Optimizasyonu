import sqlite3

import pytest

from parser import ingest
from rules import model_mismatch


def _segment(model, turns=10, prompts=5, **tokens):
    seg = {
        "model": model,
        "turns": turns,
        "prompts": prompts,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cache_5m": 0,
        "cache_1h": 0,
    }
    seg.update(tokens)
    return seg


def test_detect_suggests_sonnet_for_simple_opus_segment():
    # 1M output tokens on opus = $25; same profile on sonnet-5 = $15 (same
    # tokenizer, no adjustment) -> $10 saving; 2 turns/prompt, no errors -> high
    seg = _segment("claude-opus-4-8", turns=10, prompts=5, output_tokens=1_000_000)
    findings = model_mismatch.detect("sess-x", [seg], error_rate=0.0)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "model_mismatch"
    assert f.est_wasted_tokens == 0
    assert f.est_wasted_usd == pytest.approx(10.0)
    assert "claude-sonnet-5" in f.message
    assert "yüksek güven" in f.message


def test_detect_applies_haiku_tokenizer_factor():
    # sonnet actual: 1M out = $15; haiku counterfactual: 0.8M out * $5 = $4
    seg = _segment("claude-sonnet-5", turns=6, prompts=3, output_tokens=1_000_000)
    findings = model_mismatch.detect("s", [seg], error_rate=0.0)
    assert len(findings) == 1
    assert findings[0].est_wasted_usd == pytest.approx(11.0)
    assert "claude-haiku-4-5" in findings[0].message


def test_detect_skips_when_error_rate_is_high():
    seg = _segment("claude-opus-4-8", output_tokens=1_000_000)
    assert model_mismatch.detect("s", [seg], error_rate=0.10) == []


def test_detect_skips_when_turns_per_prompt_too_high():
    # 40 turns over 2 prompts = 20 turns/prompt: genuinely agentic churn
    seg = _segment("claude-opus-4-8", turns=40, prompts=2, output_tokens=1_000_000)
    assert model_mismatch.detect("s", [seg], error_rate=0.0) == []


def test_detect_haiku_target_has_stricter_turns_per_prompt_bar():
    # 6 turns/prompt passes the opus->sonnet bar (8) but not sonnet->haiku (3)
    opus = _segment("claude-opus-4-8", turns=30, prompts=5, output_tokens=1_000_000)
    sonnet = _segment("claude-sonnet-5", turns=30, prompts=5, output_tokens=1_000_000)
    assert len(model_mismatch.detect("s", [opus], error_rate=0.0)) == 1
    assert model_mismatch.detect("s", [sonnet], error_rate=0.0) == []


def test_detect_skips_tiny_savings():
    seg = _segment("claude-opus-4-8", output_tokens=10_000)  # saving ~$0.10
    assert model_mismatch.detect("s", [seg], error_rate=0.0) == []


def test_detect_skips_models_without_downgrade_target():
    seg = _segment("claude-haiku-4-5", output_tokens=1_000_000)
    assert model_mismatch.detect("s", [seg], error_rate=0.0) == []


def test_detect_medium_confidence_near_threshold():
    # 6 turns/prompt is over half the opus->sonnet bar (8/2=4) -> medium
    seg = _segment("claude-opus-4-8", turns=30, prompts=5, output_tokens=1_000_000)
    findings = model_mismatch.detect("s", [seg], error_rate=0.0)
    assert "orta güven" in findings[0].message


def test_detect_falls_back_to_session_tpp_when_segment_has_no_prompts():
    # Real Claude Code logs carry promptId only on user records, so segments
    # come in with prompts=0; the session-level ratio decides instead.
    seg = _segment("claude-opus-4-8", turns=30, prompts=0, output_tokens=1_000_000)
    assert model_mismatch.detect("s", [seg], error_rate=0.0, session_tpp=3.0)
    assert model_mismatch.detect("s", [seg], error_rate=0.0, session_tpp=20.0) == []
    # No session_tpp at all -> raw turn count, way over the bar -> silent
    assert model_mismatch.detect("s", [seg], error_rate=0.0) == []


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ingest.init_db(c)
    yield c
    c.close()


def test_run_aggregates_segments_and_session_error_rate(conn):
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) "
        "VALUES ('sess-db', 'proj', 't0', 'claude-opus-4-8')"
    )
    # As in real logs: promptId only on user records. 2 user prompts,
    # 4 opus turns (2 turns/prompt), 500k output tokens each = 2M out ($50).
    for p in range(2):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, prompt_id, record_uuid) "
            "VALUES ('sess-db', ?, 'user', ?, ?)",
            (f"t{p}u", f"p-{p}", f"u-{p}"),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, model, output_tokens, record_uuid) "
            "VALUES ('sess-db', ?, 'assistant', 'claude-opus-4-8', 500000, ?)",
            (f"t{i}", f"a-{i}"),
        )
    conn.commit()

    findings = model_mismatch.run(conn)
    assert len(findings) == 1
    # opus $50 -> sonnet $30: saving $20
    assert findings[0].est_wasted_usd == pytest.approx(20.0)
    assert findings[0].session_id == "sess-db"


def test_run_error_turns_suppress_the_whole_session(conn):
    conn.execute(
        "INSERT INTO sessions (id, project, started_at, model_main) "
        "VALUES ('sess-err', 'proj', 't0', 'claude-opus-4-8')"
    )
    for p in range(2):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, prompt_id, record_uuid) "
            "VALUES ('sess-err', ?, 'user', ?, ?)",
            (f"t{p}u", f"p-{p}", f"u-{p}"),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO turns (session_id, ts, role, model, output_tokens, record_uuid) "
            "VALUES ('sess-err', ?, 'assistant', 'claude-opus-4-8', 500000, ?)",
            (f"t{i}", f"a-{i}"),
        )
    # a synthetic error turn pushes session error rate to 1/5 = 20% > 5%
    conn.execute(
        "INSERT INTO turns (session_id, ts, role, model, is_error, record_uuid) "
        "VALUES ('sess-err', 't9', 'assistant', '<synthetic>', 1, 'err-1')"
    )
    conn.commit()

    assert model_mismatch.run(conn) == []
