"""Counterfactual model-cost comparison per session — informational only.

For each (session, model) segment, this rule aggregates the segment's token
profile, prices it at the actual model's rates, and re-prices it at the next
cheaper tier (Fable/Opus -> Sonnet 5, Sonnet -> Haiku 4.5).

**This rule reports `kind="info"` and contributes nothing to the waste
total.** The difference between the two prices is a *price comparison*, not
waste, and the distinction is load-bearing:

  - The re-priced figure assumes the cheaper model would have produced the
    same token profile — the same answers in the same number of turns. That
    is an unknowable best case. A cheaper model needing 30% more turns would
    erase the entire difference.
  - Using a strong model on hard work is not waste; it is the point. Nothing
    in the token counts can tell hard work from easy work.

So the number is offered as a fact to think about ("this segment cost $X;
the same profile on Sonnet would price at $Y"), never as a verdict. Summing
it into "you wasted $N" would over-claim in exactly the way stale_context
used to before its rebuild-floor fix.

Difficulty gate: sessions whose API error rate or assistant-turns-per-user-
prompt run high are skipped, since those signal work the cheaper tier likely
could not have carried. MAX_TURNS_PER_PROMPT is calibrated for *agentic*
Claude Code usage, where one user prompt routinely fans out into a long tool
loop; the original chat-shaped thresholds (8 / 3) excluded every real session
outright and left the rule permanently silent. Even so, turns-per-prompt is
a weak proxy — another reason this rule only informs and never accuses.

Tokenizer note: Fable 5, Opus 4.7/4.8 and Sonnet 5 share the same tokenizer,
so Opus/Fable -> Sonnet 5 needs no token adjustment. Haiku 4.5 still uses the
older tokenizer, which encodes the same text into roughly 1/1.3x the tokens;
HAIKU_TOKEN_FACTOR approximates that when re-pricing a Sonnet segment on
Haiku. Rough by design — the comparison dominates any factor error.
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
#
# Calibrated for agentic usage, not chat. In Claude Code a single user prompt
# fans out into a tool loop, so even a light session runs ~10 turns/prompt and
# a heavy one 25-30+; the previous chat-shaped values (8 / 3) were unreachable
# and silently disabled the rule on every real session. These are rough lines
# on a weak signal — acceptable only because the rule merely informs.
MAX_TURNS_PER_PROMPT = {
    "claude-sonnet-5": 25.0,
    "claude-haiku-4-5": 10.0,
}
MIN_DIFFERENCE_USD = 0.50


@dataclass
class Finding:
    rule: str
    session_id: str
    message: str
    est_wasted_tokens: int  # always 0 — a price comparison is not waste
    est_wasted_usd: float = 0.0  # always 0.0 — never counted as waste
    # Informational: excluded from the waste total by cli.collect_diagnose_findings.
    kind: str = "info"
    # What the cheaper tier would have cost less, if the token profile held.
    counterfactual_usd: float = 0.0


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
        difference = actual - counterfactual
        if difference < MIN_DIFFERENCE_USD:
            continue

        confidence = (
            "güçlü sinyal" if error_rate == 0 and turns_per_prompt <= max_tpp / 2 else "zayıf sinyal"
        )
        findings.append(
            Finding(
                rule="model_mismatch",
                session_id=session_id,
                message=(
                    f"{seg['model']} üzerindeki {turns} assistant turn = ${actual:.2f}; aynı "
                    f"token profili {target} ile = ${counterfactual:.2f} olurdu — aradaki fark "
                    f"${difference:.2f} ({confidence}: hata oranı %{error_rate * 100:.0f}, istek "
                    f"başına {turns_per_prompt:.1f} turn). "
                    f"Bu bir fiyat karşılaştırmasıdır, israf değil: ucuz modelin aynı işi aynı "
                    f"turn sayısında bitireceğinin garantisi yok — daha fazla turn gerekseydi fark "
                    f"kapanırdı. Zor işte güçlü model kullanmak israf değildir; karar senin."
                ),
                est_wasted_tokens=0,
                est_wasted_usd=0.0,
                kind="info",
                counterfactual_usd=round(difference, 4),
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
