"""Counterfactual model-cost analysis per session.

For each (session, model) segment, this rule aggregates the segment's token
profile, prices it at the actual model's rates, and re-prices it at the next
cheaper tier (Fable/Opus -> Sonnet 5, Sonnet -> Haiku 4.5). If the session's
difficulty signals stay under the thresholds — API error rate and assistant
turns per user prompt (a proxy for how much back-and-forth each request
needed) — the segment is flagged with the estimated saving.

This is deliberately a *counterfactual price statement*, not a verdict: the
message says what the same token profile would have cost on the cheaper
model, with a confidence qualifier, and leaves the decision to the user.
Whether the cheaper model would actually have produced equally good output is
unknowable from token counts alone.

Tokenizer note: Fable 5, Opus 4.7/4.8 and Sonnet 5 share the same tokenizer,
so Opus/Fable -> Sonnet 5 needs no token adjustment. Haiku 4.5 still uses the
older tokenizer, which encodes the same text into roughly 1/1.3x the tokens;
HAIKU_TOKEN_FACTOR approximates that when re-pricing a Sonnet segment on
Haiku. Rough by design — the savings estimate dominates any factor error.
"""

import sqlite3
from dataclasses import dataclass

from rules import pricing

SYNTHETIC_MODEL = "<synthetic>"

DOWNGRADE_TARGET = {
    "claude-fable-5": "claude-sonnet-5",
    "claude-mythos-5": "claude-sonnet-5",
    "claude-opus-4-8": "claude-sonnet-5",
    "claude-opus-4-7": "claude-sonnet-5",
    "claude-opus-4-6": "claude-sonnet-5",
    "claude-opus-4-5": "claude-sonnet-5",
    "claude-opus-4-1": "claude-sonnet-5",
    "claude-sonnet-5": "claude-haiku-4-5",
    "claude-sonnet-4-6": "claude-haiku-4-5",
    "claude-sonnet-4-5": "claude-haiku-4-5",
}
HAIKU_TOKEN_FACTOR = 0.8

MAX_ERROR_RATE = 0.05
# Max assistant turns per user prompt for a downgrade to stay plausible,
# keyed by *target*: dropping to Haiku demands a much simpler session shape
# than dropping Opus-tier work to Sonnet.
MAX_TURNS_PER_PROMPT = {
    "claude-sonnet-5": 8.0,
    "claude-haiku-4-5": 3.0,
}
MIN_SAVINGS_USD = 0.50


@dataclass
class Finding:
    rule: str
    session_id: str
    message: str
    est_wasted_tokens: int  # always 0 — this rule's estimate is in dollars
    est_wasted_usd: float = 0.0


def _token_factor(target: str) -> float:
    return HAIKU_TOKEN_FACTOR if target == "claude-haiku-4-5" else 1.0


def detect(
    session_id: str,
    segments: list[dict],
    error_rate: float = 0.0,
    session_tpp: float | None = None,
) -> list[Finding]:
    """segments: per-model aggregates for one session (dicts with model,
    turns, prompts, input_tokens, output_tokens, cache_read, cache_creation,
    cache_5m, cache_1h). error_rate is session-wide (error turns are usually
    logged under the synthetic model, so they can't be attributed to a
    specific segment).

    session_tpp: session-level assistant-turns-per-user-prompt, used when a
    segment carries no prompt count of its own. In real Claude Code logs,
    promptId exists only on *user* records — assistant records don't carry it
    (docs/log-format.md) — so per-segment prompt counts are usually 0 and the
    session-level ratio is the usable signal. See run()."""
    findings = []
    if error_rate > MAX_ERROR_RATE:
        return findings

    for seg in segments:
        target = DOWNGRADE_TARGET.get(pricing.canonical(seg["model"]) or "")
        if target is None:
            continue

        turns = seg["turns"]
        prompts = seg["prompts"]
        if prompts:
            turns_per_prompt = turns / prompts
        elif session_tpp is not None:
            turns_per_prompt = session_tpp
        else:
            turns_per_prompt = float(turns)
        max_tpp = MAX_TURNS_PER_PROMPT[target]
        if turns_per_prompt > max_tpp:
            continue

        actual = pricing.turn_cost_usd(
            seg["model"],
            input_tokens=seg["input_tokens"],
            output_tokens=seg["output_tokens"],
            cache_read=seg["cache_read"],
            cache_creation=seg["cache_creation"],
            cache_5m=seg["cache_5m"],
            cache_1h=seg["cache_1h"],
        )
        if actual is None:
            continue

        factor = _token_factor(target)
        counterfactual = pricing.turn_cost_usd(
            target,
            input_tokens=int(seg["input_tokens"] * factor),
            output_tokens=int(seg["output_tokens"] * factor),
            cache_read=int(seg["cache_read"] * factor),
            cache_creation=int(seg["cache_creation"] * factor),
            cache_5m=int(seg["cache_5m"] * factor),
            cache_1h=int(seg["cache_1h"] * factor),
        )
        savings = actual - counterfactual
        if savings < MIN_SAVINGS_USD:
            continue

        confidence = (
            "yüksek güven" if error_rate == 0 and turns_per_prompt <= max_tpp / 2 else "orta güven"
        )
        findings.append(
            Finding(
                rule="model_mismatch",
                session_id=session_id,
                message=(
                    f"{seg['model']} üzerindeki {turns} assistant turn = ${actual:.2f}; aynı "
                    f"token profili {target} ile = ${counterfactual:.2f}, tasarruf = ${savings:.2f} "
                    f"({confidence}: hata oranı %{error_rate * 100:.0f}, istek başına "
                    f"{turns_per_prompt:.1f} turn). Bu karşı-olgusal fiyat hesabıdır; ucuz modelin "
                    f"aynı çıktı kalitesini vereceğinin garantisi değildir."
                ),
                est_wasted_tokens=0,
                est_wasted_usd=round(savings, 4),
            )
        )
    return findings


def run(conn: sqlite3.Connection) -> list[Finding]:
    findings = []
    session_ids = [row[0] for row in conn.execute("SELECT id FROM sessions")]
    for session_id in session_ids:
        total, errors = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(is_error), 0) FROM turns
            WHERE session_id = ? AND role = 'assistant' AND is_sidechain = 0
            """,
            (session_id,),
        ).fetchone()
        error_rate = errors / total if total else 0.0

        # promptId lives only on user records in Claude Code logs, so the
        # turns-per-prompt signal is computed session-wide: main-thread
        # assistant turns divided by distinct user prompts.
        (session_prompts,) = conn.execute(
            """
            SELECT COUNT(DISTINCT prompt_id) FROM turns
            WHERE session_id = ? AND role = 'user' AND is_sidechain = 0
            """,
            (session_id,),
        ).fetchone()
        session_tpp = total / session_prompts if session_prompts else None

        rows = conn.execute(
            """
            SELECT model, COUNT(*), COUNT(DISTINCT prompt_id),
                   SUM(input_tokens), SUM(output_tokens), SUM(cache_read),
                   SUM(cache_creation), SUM(cache_5m), SUM(cache_1h)
            FROM turns
            WHERE session_id = ?
              AND role = 'assistant'
              AND is_sidechain = 0
              AND is_error = 0
              AND model IS NOT NULL
              AND model != ?
            GROUP BY model
            """,
            (session_id, SYNTHETIC_MODEL),
        ).fetchall()
        segments = [
            {
                "model": model,
                "turns": turns,
                "prompts": prompts,
                "input_tokens": inp or 0,
                "output_tokens": out or 0,
                "cache_read": cr or 0,
                "cache_creation": cc or 0,
                "cache_5m": c5 or 0,
                "cache_1h": c1 or 0,
            }
            for model, turns, prompts, inp, out, cr, cc, c5, c1 in rows
        ]
        findings.extend(detect(session_id, segments, error_rate, session_tpp))
    return findings
