"""Weekly markdown report of Token Coach findings.

Runs every session-scoped diagnosis rule against the ingested SQLite DB,
keeps only findings whose session started within the last 7 days, and
renders them as plain-text markdown: findings sorted by est_wasted_tokens
descending, each with evidence, a recommendation, and estimated savings.
No charts — this is meant to be read in a terminal or a plain markdown viewer.

RULES lists every rule this report pulls from. Add new entries here as more
rules land (model_mismatch, cache_efficiency, subagent_overuse — see
PLAN.md Faz 3). CLAUDE.md-linter findings (rules/claude_md_tax.py) are
intentionally excluded: they describe a project's current CLAUDE.md files,
not time-scoped session activity, so they don't fit a "weekly" window.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from rules import cache_efficiency, model_mismatch, stale_context

RULES = [stale_context, cache_efficiency, model_mismatch]

RECOMMENDATIONS = {
    "stale_context": (
        "Benzer oturumlarda bağlamı sınırsız büyütmek yerine işaretlenen "
        "turn'den hemen sonra /clear çalıştır."
    ),
    "cache_efficiency": (
        "Oturum ortasında /model değiştirmekten kaçın: prompt cache modele "
        "bağlıdır — modeli /clear'dan hemen sonra değiştir ya da diğer modelle "
        "yeni oturum aç."
    ),
    "model_mismatch": (
        "Benzer oturumları önerilen daha ucuz modelle başlatmayı dene (oturum "
        "başında /model) ve benimsemeden önce çıktı kalitesini bir oturumda doğrula."
    ),
}
DEFAULT_RECOMMENDATION = (
    "Bu bulguyu incele ve tekrarını önlemek için iş akışını değiştirip "
    "değiştirmeyeceğine karar ver."
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sessions_in_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> set[str]:
    rows = conn.execute("SELECT id, started_at FROM sessions").fetchall()
    return {
        session_id
        for session_id, started_at in rows
        if start <= _parse_ts(started_at) <= end
    }


def collect_findings(conn: sqlite3.Connection, start: datetime, end: datetime) -> list:
    valid_sessions = sessions_in_window(conn, start, end)
    findings = []
    for rule_module in RULES:
        for finding in rule_module.run(conn):
            if finding.session_id in valid_sessions:
                findings.append(finding)
    return findings


def render_markdown(findings: list, start: datetime, end: datetime) -> str:
    lines = [
        "# Token Coach — Haftalık Rapor",
        "",
        f"Dönem: {start.date().isoformat()} – {end.date().isoformat()}",
        "",
    ]

    if not findings:
        lines.append("Bu hafta bulgu yok.")
        return "\n".join(lines) + "\n"

    # Ölçülen israf ile bilgi amaçlı karşılaştırma ayrı raporlanır: bir
    # karşı-olgusal farkı israf toplamına katmak abartı olur (bkz.
    # rules/model_mismatch.py).
    waste = [f for f in findings if getattr(f, "kind", "waste") == "waste"]
    info = [f for f in findings if getattr(f, "kind", "waste") == "info"]

    waste_sorted = sorted(waste, key=lambda f: f.est_wasted_tokens, reverse=True)
    total_wasted = sum(f.est_wasted_tokens for f in waste_sorted)
    lines.append(f"**{len(waste_sorted)} bulgu, tahmini israf ~{total_wasted} token.**")
    lines.append("")

    for i, finding in enumerate(waste_sorted, start=1):
        recommendation = RECOMMENDATIONS.get(finding.rule, DEFAULT_RECOMMENDATION)
        lines.append(f"## {i}. {finding.rule} — oturum `{finding.session_id}`")
        lines.append("")
        lines.append(f"- **Kanıt:** {finding.message}")
        lines.append(f"- **Öneri:** {recommendation}")
        lines.append(f"- **Tahmini tasarruf:** {_format_savings(finding)}")
        lines.append("")

    if info:
        lines.append("## Bilgi — israf değil, karşılaştırma")
        lines.append("")
        lines.append(
            "Aşağıdakiler ölçülen israf değil, düşünmeye değer fiyat karşılaştırmalarıdır; "
            "yukarıdaki toplama dahil değildir."
        )
        lines.append("")
        for finding in sorted(info, key=lambda f: getattr(f, "counterfactual_usd", 0.0), reverse=True):
            lines.append(f"- **{finding.rule}** — oturum `{finding.session_id}`: {finding.message}")
        lines.append("")

    return "\n".join(lines)


def _format_savings(finding) -> str:
    """Token count, with a dollar figure when the rule provides one; rules
    whose estimate is purely monetary (model_mismatch) show only dollars."""
    usd = getattr(finding, "est_wasted_usd", 0.0) or 0.0
    if finding.est_wasted_tokens and usd:
        return f"~{finding.est_wasted_tokens} token (= ${usd:.2f})"
    if usd:
        return f"= ${usd:.2f}"
    return f"~{finding.est_wasted_tokens} token"


def weekly_report(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    findings = collect_findings(conn, start, now)
    return render_markdown(findings, start, now)
