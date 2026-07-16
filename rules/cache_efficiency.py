"""Flags in-session model switches that reset the prompt cache.

The prompt cache is model-scoped: a mid-session /model switch invalidates the
entire cached prefix, and the first request on the new model re-writes the
accumulated context as cache_creation — billed at the cache-write premium
(2x input price for the 1-hour TTL Claude Code uses). This rule walks each
session's main-thread assistant turns chronologically and, whenever the model
changes between consecutive turns, treats the switch turn's cache_creation as
the re-cache bill for that switch.

Switches whose re-write is under MIN_REWRITE_TOKENS are ignored as noise:
a switch early in a session (or right after /clear) re-writes almost nothing
and is exactly the recommended way to change models, so it shouldn't be
flagged. A genuine mid-session switch re-writes the whole accumulated
context, which is large by definition.
"""

import sqlite3
from dataclasses import dataclass

from rules import pricing

MIN_REWRITE_TOKENS = 20_000
SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Finding:
    rule: str
    session_id: str
    message: str
    est_wasted_tokens: int
    est_wasted_usd: float = 0.0


def detect(session_id: str, turns: list[dict]) -> list[Finding]:
    """turns: one session's main-thread assistant turns (dicts with ts, model,
    cache_creation, cache_5m, cache_1h), chronological, synthetic/error rows
    already excluded by the caller — see run()."""
    findings = []
    prev_model = None
    for turn in turns:
        model = turn["model"]
        if prev_model is not None and model != prev_model:
            rewrite = turn["cache_creation"]
            if rewrite >= MIN_REWRITE_TOKENS:
                usd = (
                    pricing.turn_cost_usd(
                        model,
                        cache_creation=rewrite,
                        cache_5m=turn.get("cache_5m", 0),
                        cache_1h=turn.get("cache_1h", 0),
                    )
                    or 0.0
                )
                findings.append(
                    Finding(
                        rule="cache_efficiency",
                        session_id=session_id,
                        message=(
                            f"Oturum ortasında model değişti: {prev_model} -> {model} "
                            f"({turn['ts']}). ~{rewrite} token bağlam cache'e yeniden yazıldı "
                            f"(= ${usd:.2f}). /model değişimi prompt cache'ini sıfırlar — "
                            f"modeli /clear'dan hemen sonra değiştir ya da yeni oturum aç."
                        ),
                        est_wasted_tokens=rewrite,
                        est_wasted_usd=round(usd, 4),
                    )
                )
        prev_model = model
    return findings


def run(conn: sqlite3.Connection) -> list[Finding]:
    findings = []
    session_ids = [row[0] for row in conn.execute("SELECT id FROM sessions")]
    for session_id in session_ids:
        rows = conn.execute(
            """
            SELECT ts, model, cache_creation, cache_5m, cache_1h FROM turns
            WHERE session_id = ?
              AND role = 'assistant'
              AND is_sidechain = 0
              AND is_error = 0
              AND model IS NOT NULL
              AND model != ?
            ORDER BY ts, id
            """,
            (session_id, SYNTHETIC_MODEL),
        ).fetchall()
        turns = [
            {"ts": ts, "model": model, "cache_creation": cc, "cache_5m": c5, "cache_1h": c1}
            for ts, model, cc, c5, c1 in rows
        ]
        findings.extend(detect(session_id, turns))
    return findings
