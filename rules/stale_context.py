"""Flags sessions whose main-thread context grew past a token budget without
ever being /clear'd.

This project doesn't store turn *content* (only token counts — see
docs/log-format.md), so there is no way to detect a genuine topic shift in a
session. Instead this rule uses a purely token-count heuristic: within a
session, the total context size on assistant turns normally grows
turn-over-turn as history accumulates, and drops sharply back down when the
user runs /clear (or /compact). A run of turns where that size never drops
(a "monotonic run") that climbs past STALE_CONTEXT_THRESHOLD_TOKENS is
treated as a session that likely should have been cleared but wasn't.

**"Total context size" is `input_tokens + cache_read + cache_creation`, not
`input_tokens` alone.** Claude's prompt caching means the raw `input_tokens`
API field is only the *uncached* delta for that one turn (usually small —
often under a few hundred tokens even deep into a long conversation); the
bulk of an accumulated conversation shows up as `cache_read` (context reused
from cache) and `cache_creation` (context newly written to cache). Using
`input_tokens` alone would make this rule structurally unable to ever fire,
since it rarely exceeds a few thousand tokens regardless of how large the
actual conversation has grown — this was a real bug found by running the
rule against real session data, where `cache_read` alone reached 300k+
tokens while `input_tokens` never crossed 4k in the same session.

Within such a run, the single turn with the largest context-size increase
over its predecessor is flagged as the best-guess /clear point: it's the
turn most likely to have added a large, one-off chunk of content (a big file
read, tool output, etc.) that subsequent turns didn't need to keep paying
for.

**`est_wasted_tokens` counts only the *clearable* part of the prefix, not the
whole prefix.** /clear does not reset the context to zero: a fresh session
immediately pays for the system prompt, tool definitions, CLAUDE.md, and
restating the task. Charging the full pre-jump context as "waste" would
assume a free restart and inflate every finding — this rule used to do
exactly that (`baseline * turns_after`), which overstated the headline
number.

The rebuild floor is *measured, not guessed*: the first turn of a monotonic
run is by definition a fresh start (either the session's opening turn or the
turn right after a /clear), so its context size is what starting over
actually cost in this very session. The honest saving is therefore

    clearable = baseline - rebuild_floor

carried across every turn from the jump point to the end of the run. When
the largest jump happens at the very start of a run there is no stale prefix
to shed (`clearable == 0`) and no finding is emitted at all.

This is a heuristic, not a semantic analysis — the flagged jump could just
be organic, needed growth in a legitimately long task. Precision (e.g. using
turn content once available, or requiring a minimum jump size) can improve
later; for now this surfaces candidates for a human to look at. The bias is
deliberately toward under-claiming: a coaching tool's only asset is trust,
and a number the user can catch being inflated costs more than it earns.
"""

import sqlite3
from dataclasses import dataclass

from rules import pricing

STALE_CONTEXT_THRESHOLD_TOKENS = 50_000
SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Finding:
    rule: str
    session_id: str
    message: str
    est_wasted_tokens: int
    # Wasted tokens are a stale prefix carried forward through later turns —
    # in a cached conversation that bills as cache *reads*, so they're priced
    # at the session main model's cache-read rate (0.1x input). 0.0 when the
    # model is unknown.
    est_wasted_usd: float = 0.0


@dataclass
class Turn:
    ts: str
    context_tokens: int


def _monotonic_runs(turns: list[Turn]) -> list[list[int]]:
    """Split turn indices into contiguous runs where context_tokens never drops.

    A run boundary (a drop) is the signature of a /clear or /compact having
    happened between two turns.
    """
    if not turns:
        return []
    runs = [[0]]
    for i in range(1, len(turns)):
        if turns[i].context_tokens >= turns[i - 1].context_tokens:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _largest_jump_position(turns: list[Turn], run: list[int]) -> int:
    """Position within `run` (not a global `turns` index) of the turn with
    the largest context_tokens increase over the turn before it."""
    best_pos = 1
    best_delta = -1
    for pos in range(1, len(run)):
        delta = turns[run[pos]].context_tokens - turns[run[pos - 1]].context_tokens
        if delta > best_delta:
            best_delta = delta
            best_pos = pos
    return best_pos


def detect(session_id: str, turns_raw: list[dict]) -> list[Finding]:
    """turns_raw: one session's main-thread assistant turns (dicts with `ts`
    and `context_tokens` — see the module docstring for why this is
    input_tokens + cache_read + cache_creation, not input_tokens alone),
    already filtered and ordered chronologically by the caller — see `run()`."""
    turns = [Turn(ts=t["ts"], context_tokens=t["context_tokens"]) for t in turns_raw]
    if len(turns) < 2:
        return []

    findings = []
    for run in _monotonic_runs(turns):
        if len(run) < 2:
            continue
        peak = turns[run[-1]].context_tokens
        if peak <= STALE_CONTEXT_THRESHOLD_TOKENS:
            continue

        jump_pos = _largest_jump_position(turns, run)
        jump_idx = run[jump_pos]
        baseline = turns[run[jump_pos - 1]].context_tokens
        jump_size = turns[jump_idx].context_tokens - baseline
        turns_after = run[jump_pos:]

        # /clear sıfıra indirmez: sistem promptu + CLAUDE.md + görevin yeniden
        # anlatılması geri gelir. Bu koşunun ilk turn'ü tam olarak "taze
        # başlangıç" olduğu için, yeniden kurulum maliyetini tahmin etmek
        # yerine kullanıcının kendi verisinden ölçüyoruz.
        rebuild_floor = turns[run[0]].context_tokens
        clearable = baseline - rebuild_floor
        if clearable <= 0:
            # En büyük sıçrama koşunun hemen başında — atılabilecek bayat
            # önek yok, dolayısıyla ortada bulgu da yok.
            continue
        est_wasted_tokens = clearable * len(turns_after)

        findings.append(
            Finding(
                rule="stale_context",
                session_id=session_id,
                message=(
                    f"Bağlam temizlenmeden {peak} tokene kadar büyüdü. "
                    f"En büyük sıçrama {turns[jump_idx].ts} anında +{jump_size} token. "
                    f"Orada temizleseydin {clearable} token atılabilir ve sonraki "
                    f"{len(turns_after)} turn boyunca taşınmazdı: konu değiştiyse /clear "
                    f"(her şeyi at), aynı işe devam ediyorduysan /compact (özet kalır). "
                    f"(Taze başlangıç bedava değil: bu oturum {rebuild_floor} tokenle "
                    f"açıldı — sistem promptu + CLAUDE.md; o kısım geri gelirdi.)"
                ),
                est_wasted_tokens=est_wasted_tokens,
            )
        )
    return findings


def run(conn: sqlite3.Connection) -> list[Finding]:
    """Query every session's main-thread assistant turns and run detect() per session."""
    findings = []
    sessions = conn.execute("SELECT id, model_main FROM sessions").fetchall()
    for session_id, model_main in sessions:
        rows = conn.execute(
            """
            SELECT ts, input_tokens + cache_read + cache_creation AS context_tokens FROM turns
            WHERE session_id = ?
              AND role = 'assistant'
              AND is_sidechain = 0
              AND model IS NOT NULL
              AND model != ?
            ORDER BY ts, id
            """,
            (session_id, SYNTHETIC_MODEL),
        ).fetchall()
        turns_raw = [{"ts": ts, "context_tokens": context_tokens} for ts, context_tokens in rows]
        session_findings = detect(session_id, turns_raw)
        for finding in session_findings:
            usd = pricing.turn_cost_usd(model_main, cache_read=finding.est_wasted_tokens)
            finding.est_wasted_usd = round(usd, 4) if usd else 0.0
        findings.extend(session_findings)
    return findings
