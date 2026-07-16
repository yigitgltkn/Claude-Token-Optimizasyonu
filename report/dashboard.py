"""Self-contained static HTML dashboard for Token Coach.

Renders a single dependency-free HTML file from the ingested SQLite DB:
summary stats, a daily token-usage chart, a per-model usage chart, the
stale_context findings (and CLAUDE.md-lint findings if a project path is
given), and a sessions table. No external JS/CSS — charts are hand-rolled
inline SVG so the file opens offline, as a local file or as a hosted
Artifact preview.

Colors follow the dataviz skill's validated default palette
(see the skill's references/palette.md) rather than ad hoc hex values.
"""

import html
import sqlite3
from pathlib import Path

from parser.ingest import SYNTHETIC_MODEL
from report import weekly
from rules import claude_md_tax, stale_context

# --- palette (dataviz skill's validated reference instance) ---------------

# Light/dark palette variables (dataviz skill's validated reference instance).
# Emitted three ways: prefers-color-scheme as the default signal, plus explicit
# :root[data-theme=...] overrides so a host page's theme toggle (e.g. the
# claude.ai Artifact viewer) wins in both directions.
_LIGHT_VARS = """
  --surface-1:      #fcfcfb;
  --page:           #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --gridline:       #e1e0d9;
  --baseline:       #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --series-1: #2a78d6; /* blue   - input tokens */
  --series-2: #1baf7a; /* aqua   - output tokens */
  --series-3: #eda100; /* yellow - cache creation */
  --series-4: #008300; /* green  - cache read */
"""

_DARK_VARS = """
  --surface-1:      #1a1a19;
  --page:           #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --gridline:       #2c2c2a;
  --baseline:       #383835;
  --border:         rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --series-2: #199e70;
  --series-3: #c98500;
  --series-4: #008300;
"""

_PALETTE_CSS = f"""
.tc-dash {{{_LIGHT_VARS}}}
@media (prefers-color-scheme: dark) {{
  .tc-dash {{{_DARK_VARS}}}
}}
:root[data-theme="light"] .tc-dash {{{_LIGHT_VARS}}}
:root[data-theme="dark"] .tc-dash {{{_DARK_VARS}}}
"""

_CLAUDE_MD_RECOMMENDATIONS = {
    "total_size": "CLAUDE.md dosyasini kucultmeyi veya path-scoped alt dosyalara bolmeyi dusun.",
    "path_scoped_candidate": "Bu bolumu, bahsedilen alt dizine ozel bir CLAUDE.md dosyasina tasi.",
    "duplicated_line": "Tekrarlanan satiri kaldir, tek bir yerde tut.",
}


def _recommendation_for(finding) -> str:
    if finding.rule in weekly.RECOMMENDATIONS:
        return weekly.RECOMMENDATIONS[finding.rule]
    return _CLAUDE_MD_RECOMMENDATIONS.get(finding.rule, weekly.DEFAULT_RECOMMENDATION)


def _finding_context(finding) -> str:
    session_id = getattr(finding, "session_id", None)
    if session_id is not None:
        return f"session {session_id}"
    path = getattr(finding, "path", None)
    if path is not None:
        return str(path)
    return ""


# --- data functions ---------------------------------------------------------


def overview_stats(conn: sqlite3.Connection, findings: list) -> dict:
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    turn_count, total_tokens = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens + cache_creation + cache_read), 0) "
        "FROM turns"
    ).fetchone()
    return {
        "session_count": session_count,
        "turn_count": turn_count,
        "total_tokens": total_tokens,
        "total_wasted_tokens": sum(f.est_wasted_tokens for f in findings),
    }


def session_summaries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id, s.project, s.started_at, s.model_main,
               COUNT(t.id),
               COALESCE(SUM(t.input_tokens + t.output_tokens + t.cache_creation + t.cache_read), 0)
        FROM sessions s
        LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id
        ORDER BY s.started_at DESC
        """
    ).fetchall()
    return [
        {
            "id": r[0],
            "project": r[1],
            "started_at": r[2],
            "model_main": r[3],
            "turn_count": r[4],
            "total_tokens": r[5],
        }
        for r in rows
    ]


def daily_token_usage(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT date(ts),
               COALESCE(SUM(input_tokens), 0),
               COALESCE(SUM(output_tokens), 0),
               COALESCE(SUM(cache_creation), 0),
               COALESCE(SUM(cache_read), 0)
        FROM turns
        WHERE role = 'assistant'
        GROUP BY date(ts)
        ORDER BY date(ts) ASC
        """
    ).fetchall()
    return [
        {
            "date": r[0],
            "input_tokens": r[1],
            "output_tokens": r[2],
            "cache_creation": r[3],
            "cache_read": r[4],
        }
        for r in rows
    ]


def model_usage(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT model, COUNT(*),
               COALESCE(SUM(input_tokens + output_tokens + cache_creation + cache_read), 0)
        FROM turns
        WHERE role = 'assistant' AND model IS NOT NULL AND model != ?
        GROUP BY model
        ORDER BY 3 DESC
        """,
        (SYNTHETIC_MODEL,),
    ).fetchall()
    return [{"model": r[0], "turn_count": r[1], "total_tokens": r[2]} for r in rows]


# --- SVG chart helpers -------------------------------------------------------


def _nice_max(value: int) -> int:
    """Round up to a visually clean axis maximum (1/2/5 * 10^n)."""
    if value <= 0:
        return 10
    import math

    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return int(candidate)
    return int(10 * magnitude)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _daily_usage_chart(daily: list[dict]) -> str:
    if not daily:
        return '<p class="tc-empty">Henuz gunluk kullanim verisi yok.</p>'

    series = [
        ("input_tokens", "var(--series-1)", "Girdi"),
        ("output_tokens", "var(--series-2)", "Çıktı"),
        ("cache_creation", "var(--series-3)", "Cache yazma"),
        ("cache_read", "var(--series-4)", "Cache okuma"),
    ]
    totals = [sum(d[key] for key, _, _ in series) for d in daily]
    axis_max = _nice_max(max(totals))

    bar_w = 24
    gap = 20
    chart_h = 240
    left_pad = 56
    top_pad = 16
    width = left_pad + len(daily) * (bar_w + gap) + gap
    height = chart_h + top_pad + 40

    # Explicit width/height so the SVG renders at natural size; without them the
    # browser scales the viewBox to the container and a short (few-day) chart
    # blows up into giant axis text.
    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'class="tc-chart" role="img" aria-label="Gunluk token kullanimi">']

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gy = top_pad + chart_h - frac * chart_h
        parts.append(
            f'<line x1="{left_pad}" y1="{gy:.1f}" x2="{width}" y2="{gy:.1f}" '
            f'class="tc-grid" />'
        )
        parts.append(
            f'<text x="{left_pad - 8}" y="{gy + 4:.1f}" class="tc-axis-label" text-anchor="end">'
            f'{html.escape(_fmt_tokens(int(axis_max * frac)))}</text>'
        )

    for i, day in enumerate(daily):
        x = left_pad + gap + i * (bar_w + gap)
        cursor = top_pad + chart_h
        tooltip_parts = [f"{day['date']}"]
        for key, color, label in series:
            value = day[key]
            if value <= 0:
                continue
            seg_h = (value / axis_max) * chart_h
            seg_h = max(seg_h - 2, 0)  # 2px surface gap between stacked segments
            top = cursor - seg_h
            parts.append(
                f'<rect x="{x}" y="{top:.1f}" width="{bar_w}" height="{seg_h:.1f}" '
                f'fill="{color}" class="tc-bar" '
                f'data-tooltip="{html.escape(label)}: {value}"/>'
            )
            cursor = top - 2
            tooltip_parts.append(f"{label} {value}")
        day_total = sum(day[key] for key, _, _ in series)
        parts.append(
            f'<rect x="{x - gap / 2}" y="{top_pad}" width="{bar_w + gap}" height="{chart_h}" '
            f'fill="transparent" class="tc-bar-hit" '
            f'data-tooltip="{html.escape(chr(10).join(tooltip_parts))} | Toplam {day_total}"/>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2}" y="{top_pad + chart_h + 16}" class="tc-axis-label" '
            f'text-anchor="middle">{html.escape(day["date"][5:])}</text>'
        )

    parts.append("</svg>")

    legend = "".join(
        f'<span class="tc-legend-item"><span class="tc-swatch" style="background:{color}"></span>{html.escape(label)}</span>'
        for _, color, label in series
    )
    return f'<div class="tc-legend">{legend}</div><div class="tc-chart-scroll">{"".join(parts)}</div>'


def _model_usage_chart(models: list[dict]) -> str:
    if not models:
        return '<p class="tc-empty">Henuz model kullanim verisi yok.</p>'

    axis_max = _nice_max(max(m["total_tokens"] for m in models))
    bar_w = 24
    slot_w = 150  # room for model-name labels under each bar
    chart_h = 220
    left_pad = 56
    top_pad = 16
    width = left_pad + len(models) * slot_w + 20
    height = chart_h + top_pad + 50

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'class="tc-chart" role="img" aria-label="Model basina token kullanimi">']

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gy = top_pad + chart_h - frac * chart_h
        parts.append(f'<line x1="{left_pad}" y1="{gy:.1f}" x2="{width}" y2="{gy:.1f}" class="tc-grid" />')
        parts.append(
            f'<text x="{left_pad - 8}" y="{gy + 4:.1f}" class="tc-axis-label" text-anchor="end">'
            f'{html.escape(_fmt_tokens(int(axis_max * frac)))}</text>'
        )

    for i, m in enumerate(models):
        slot_center = left_pad + i * slot_w + slot_w / 2
        x = slot_center - bar_w / 2
        bar_h = (m["total_tokens"] / axis_max) * chart_h
        top = top_pad + chart_h - bar_h
        parts.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w}" height="{bar_h:.1f}" '
            f'fill="var(--series-1)" class="tc-bar" '
            f'data-tooltip="{html.escape(m["model"])}: {m["total_tokens"]} token, {m["turn_count"]} turn"/>'
        )
        parts.append(
            f'<text x="{slot_center:.1f}" y="{top - 6:.1f}" class="tc-axis-label" text-anchor="middle">'
            f'{html.escape(_fmt_tokens(m["total_tokens"]))}</text>'
        )
        parts.append(
            f'<text x="{slot_center:.1f}" y="{top_pad + chart_h + 18}" class="tc-axis-label" '
            f'text-anchor="middle">{html.escape(m["model"])}</text>'
        )

    parts.append("</svg>")
    return f'<div class="tc-chart-scroll">{"".join(parts)}</div>'


# --- HTML assembly ------------------------------------------------------------


def _stat_tile(label: str, value: str) -> str:
    return (
        f'<div class="tc-tile"><div class="tc-tile-label">{html.escape(label)}</div>'
        f'<div class="tc-tile-value">{html.escape(value)}</div></div>'
    )


def _findings_section(findings: list) -> str:
    if not findings:
        return '<p class="tc-empty">Bulgu yok — harika!</p>'

    rows = []
    for finding in findings:
        rows.append(
            "<li class=\"tc-finding\">"
            f'<div class="tc-finding-head"><span class="tc-rule">{html.escape(finding.rule)}</span>'
            f'<span class="tc-context">{html.escape(_finding_context(finding))}</span></div>'
            f'<div><strong>Kanit:</strong> {html.escape(finding.message)}</div>'
            f'<div><strong>Oneri:</strong> {html.escape(_recommendation_for(finding))}</div>'
            f'<div><strong>Tahmini tasarruf:</strong> ~{finding.est_wasted_tokens} token</div>'
            "</li>"
        )
    return f'<ul class="tc-findings">{"".join(rows)}</ul>'


def _sessions_table(sessions: list[dict]) -> str:
    if not sessions:
        return '<p class="tc-empty">Henuz oturum yok.</p>'

    rows = "".join(
        "<tr>"
        f'<td>{html.escape(s["id"])}</td>'
        f'<td>{html.escape(s["project"])}</td>'
        f'<td>{html.escape(s["model_main"] or "-")}</td>'
        f'<td>{html.escape(s["started_at"])}</td>'
        f'<td class="tc-num">{s["turn_count"]}</td>'
        f'<td class="tc-num">{s["total_tokens"]}</td>'
        "</tr>"
        for s in sessions
    )
    return (
        '<table class="tc-table"><thead><tr>'
        "<th>Oturum</th><th>Proje</th><th>Model</th><th>Basladi</th>"
        '<th class="tc-num">Turn</th><th class="tc-num">Token</th>'
        "</tr></thead><tbody>" + rows + "</tbody></table>"
    )


def discover_projects(root: Path) -> list[Path]:
    """Return `root` itself plus its direct subdirectories that contain a CLAUDE.md."""
    found = []
    if (root / "CLAUDE.md").is_file():
        found.append(root)
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "CLAUDE.md").is_file():
            found.append(child)
    return found


def coaching_tips(conn: sqlite3.Connection, session_findings: list) -> list[tuple[str, str]]:
    """Data-driven coaching advice as (title, body) pairs, derived from the
    ingested sessions — not generic boilerplate: each tip only appears when the
    user's own data shows the pattern it addresses."""
    tips = []

    if session_findings:
        worst = max(session_findings, key=lambda f: f.est_wasted_tokens)
        total = sum(f.est_wasted_tokens for f in session_findings)
        tips.append((
            "Faz bittiginde /clear kullan",
            f"{len(session_findings)} oturum bolumunde context 50 bin tokeni gecip hic temizlenmemis "
            f"(toplam ~{_fmt_tokens(total)} tahmini israf). En buyugu: {worst.message} "
            "Bir isi/fazi bitirip yeni bir konuya gecerken /clear ile sifirla; ayni ise devam etmen "
            "gerekiyorsa /compact ile ozetletip kucult."
        ))

    multi_model = conn.execute(
        """
        SELECT session_id, COUNT(DISTINCT model) FROM turns
        WHERE role = 'assistant' AND is_sidechain = 0
          AND model IS NOT NULL AND model != ?
        GROUP BY session_id HAVING COUNT(DISTINCT model) > 1
        """,
        (SYNTHETIC_MODEL,),
    ).fetchall()
    if multi_model:
        tips.append((
            "Model degisimini faz sinirina tasi",
            f"{len(multi_model)} oturumda ayni oturum icinde model degistirilmis (/model). Prompt cache "
            "modele ozel oldugu icin oturum ortasinda model degistirmek biriken cache'i gecersiz kilar "
            "ve context yeniden yazilir. Model degistireceksen en verimli an, zaten /clear yaptigin faz "
            "sinirlaridir — PLAN.md'deki 'her faz ayri oturum + faza uygun model' disiplini tam da bu."
        ))

    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + cache_creation), 0), COALESCE(SUM(cache_read), 0) FROM turns"
    ).fetchone()
    uncached, cached = row
    if cached > 0 and cached >= uncached * 5:
        tips.append((
            "Cache kullanimi saglikli",
            f"Token trafiginin buyuk kismi cache okumasi (~{_fmt_tokens(cached)} cache'ten, "
            f"~{_fmt_tokens(uncached)} yeni yazilan) — cache okumasi normal input'tan ~10 kat ucuz, "
            "yani mevcut aliskanligin bu acidan verimli. Buradaki israf sorunu miktar degil, "
            "ayni buyuk context'i gereginden uzun sure tasimak (ustteki /clear onerisi)."
        ))

    if not tips:
        tips.append((
            "Su an belirgin bir sorun yok",
            "Mevcut veride 50 bin tokeni asan temizlenmemis context, oturum ici model degisimi gibi "
            "kaliplar tespit edilmedi. Veri biriktikce (yeni oturumlar ingest ettikce) burasi guncellenir."
        ))
    return tips


_GUIDE_HTML = """
<details class="tc-guide">
<summary>Kisa kilavuz: /clear, /compact ve /model ne zaman kullanilir?</summary>
<ul>
<li><strong>/clear</strong> — konusma gecmisini tamamen sifirlar. En dogru an: bir is/faz bitti,
simdi farkli bir seye geciyorsun. Eski konusmanin tamami sonraki her mesajda tekrar tasinir;
konuyla ilgisi kalmadiysa tasimak sadece maliyet. Kural: "bu gecmisi bir daha kullanacak miyim?"
sorusunun cevabi hayirsa /clear.</li>
<li><strong>/compact</strong> — gecmisi silmek yerine ozetletir. Ayni ise devam etmen gerekiyorsa
(gecmisteki kararlar/baglam hala lazim) ama context cok buyudumse bunu kullan. Ozet, tam gecmisten
cok daha kucuktur ama bilgi kaybi olabilir — kritik detaylari ozete girmeden once not almak iyi olur.</li>
<li><strong>/model</strong> — is basitse kucuk/ucuz modele gec (rapor yazimi, basit refactor icin
Haiku gibi), karmasiksa buyuge. Dikkat: cache modele ozel — oturum ortasinda model degistirmek
cache'i sifirlar. En iyi pratik: model degisimini /clear ile ayni ana, yani faz sinirina koy.</li>
<li><strong>Plan modu (Shift+Tab)</strong> — buyuk/riskli degisikliklerden once planla; yanlis yolda
kod yazip geri almak, en pahali token israfi turudur.</li>
<li><strong>Path-scoped CLAUDE.md</strong> — sadece belirli bir alt dizinle ilgili talimatlari ana
CLAUDE.md yerine o dizinin icindeki CLAUDE.md'ye koy; boylece sadece oradayken yuklenir.</li>
</ul>
</details>
"""


def _coaching_section(tips: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<li class="tc-finding"><div class="tc-rule">{html.escape(title)}</div>'
        f"<div>{html.escape(body)}</div></li>"
        for title, body in tips
    )
    return f'<ul class="tc-findings">{items}</ul>{_GUIDE_HTML}'


def _projects_section(projects: list[dict]) -> str:
    """Project picker (<select>) + one findings panel per project, toggled client-side."""
    if not projects:
        return (
            '<p class="tc-note">CLAUDE.md bulgulari dahil degil — '
            "<code>cli.py dashboard --project &lt;yol&gt;</code> veya "
            "<code>--projects-root &lt;kok-dizin&gt;</code> ile ekleyebilirsin.</p>"
        )

    options = "".join(
        f'<option value="tc-proj-{i}">{html.escape(p["path"].name)}</option>'
        for i, p in enumerate(projects)
    )
    panels = []
    for i, p in enumerate(projects):
        wasted = sum(f.est_wasted_tokens for f in p["findings"])
        display = "block" if i == 0 else "none"
        panels.append(
            f'<div class="tc-proj-panel" id="tc-proj-{i}" style="display:{display}">'
            f'<p class="tc-note">Taranan yol: <code>{html.escape(str(p["path"]))}</code> — '
            f'{len(p["findings"])} bulgu, ~{wasted} token tahmini israf</p>'
            f'{_findings_section(p["findings"])}</div>'
        )
    return (
        f'<p><label class="tc-note" for="tc-proj-select">Proje sec: </label>'
        f'<select id="tc-proj-select" class="tc-select">{options}</select></p>'
        + "".join(panels)
    )


def render_html(conn: sqlite3.Connection, project_paths: list[Path] | None = None) -> str:
    session_findings = sorted(
        stale_context.run(conn), key=lambda f: f.est_wasted_tokens, reverse=True
    )
    projects = []
    for path in project_paths or []:
        findings = sorted(
            claude_md_tax.lint_project(path), key=lambda f: f.est_wasted_tokens, reverse=True
        )
        projects.append({"path": path, "findings": findings})

    stats = overview_stats(conn, session_findings)
    sessions = session_summaries(conn)
    daily = daily_token_usage(conn)
    models = model_usage(conn)
    tips = coaching_tips(conn, session_findings)

    return f"""<div class="tc-dash">
<style>{_PALETTE_CSS}
.tc-dash {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--page);
  color: var(--text-primary); padding: 24px; min-height: 100vh; box-sizing: border-box; }}
.tc-wrap {{ max-width: 960px; margin: 0 auto; }}
.tc-dash h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
.tc-dash h2 {{ font-size: 1.15rem; margin: 32px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
.tc-intro {{ color: var(--text-secondary); max-width: 68ch; line-height: 1.5; }}
.tc-note {{ color: var(--text-muted); font-size: 0.85rem; }}
.tc-tiles {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }}
.tc-tile {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; min-width: 140px; flex: 1; }}
.tc-tile-label {{ color: var(--text-secondary); font-size: 0.8rem; }}
.tc-tile-value {{ font-size: 1.6rem; font-weight: 600; margin-top: 2px; }}
.tc-chart-scroll {{ overflow-x: auto; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px; }}
.tc-chart {{ display: block; }}
.tc-grid {{ stroke: var(--gridline); stroke-width: 1; }}
.tc-axis-label {{ fill: var(--text-muted); font-size: 10px; }}
.tc-bar {{ transition: opacity 0.1s; }}
.tc-bar:hover {{ opacity: 0.8; }}
.tc-bar-hit {{ cursor: pointer; }}
.tc-legend {{ display: flex; gap: 16px; margin-bottom: 8px; color: var(--text-secondary); font-size: 0.85rem; }}
.tc-legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
.tc-swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
.tc-findings {{ list-style: none; padding: 0; display: flex; flex-direction: column; gap: 10px; }}
.tc-finding {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; }}
.tc-finding-head {{ display: flex; justify-content: space-between; margin-bottom: 6px; }}
.tc-rule {{ font-weight: 600; }}
.tc-context {{ color: var(--text-muted); font-size: 0.85rem; }}
.tc-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
.tc-table th, .tc-table td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
.tc-num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.tc-empty {{ color: var(--text-muted); }}
.tc-select {{ background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 10px; font-size: 0.9rem; }}
.tc-guide {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; margin-top: 12px; }}
.tc-guide summary {{ cursor: pointer; font-weight: 600; }}
.tc-guide ul {{ margin: 10px 0 4px; padding-left: 20px; color: var(--text-secondary); line-height: 1.5; }}
.tc-guide li {{ margin-bottom: 8px; }}
.tc-tooltip {{ position: fixed; background: var(--text-primary); color: var(--surface-1);
  padding: 6px 10px; border-radius: 6px; font-size: 0.8rem; pointer-events: none; white-space: pre;
  display: none; z-index: 10; max-width: 260px; }}
</style>

<div class="tc-wrap">
<h1>Token Coach — Panel</h1>
<p class="tc-intro">Token Coach, Claude Code oturum loglarini analiz ederek nerede gereksiz token
harcandigini bulur. Asagida: genel ozet (makinedeki tum projelerin oturum verisi), veriye dayali
kocluk onerileri, gunluk token kullanimi, model bazinda dagilim, oturum bulgulari ve proje
secerek bakabilecegin CLAUDE.md bulgulari.</p>

<h2>Ozet <span class="tc-note">(tum projelerin oturumlari)</span></h2>
<div class="tc-tiles">
{_stat_tile("Oturum sayisi", str(stats["session_count"]))}
{_stat_tile("Turn sayisi", str(stats["turn_count"]))}
{_stat_tile("Toplam token", _fmt_tokens(stats["total_tokens"]))}
{_stat_tile("Tahmini israf (oturumlar)", f'~{_fmt_tokens(stats["total_wasted_tokens"])}')}
</div>

<h2>Kocluk — bu veriden cikan oneriler</h2>
{_coaching_section(tips)}

<h2>Gunluk token kullanimi</h2>
{_daily_usage_chart(daily)}

<h2>Model basina kullanim</h2>
{_model_usage_chart(models)}

<h2>Oturum bulgulari ({len(session_findings)}) <span class="tc-note">(tum projeler)</span></h2>
{_findings_section(session_findings)}

<h2>CLAUDE.md bulgulari <span class="tc-note">(proje bazli)</span></h2>
{_projects_section(projects)}

<h2>Oturumlar</h2>
{_sessions_table(sessions)}
</div>

<div class="tc-tooltip" id="tc-tooltip"></div>
<script>
(function() {{
  var tip = document.getElementById("tc-tooltip");
  document.querySelectorAll("[data-tooltip]").forEach(function(el) {{
    el.addEventListener("pointermove", function(e) {{
      tip.textContent = el.getAttribute("data-tooltip");
      tip.style.left = (e.clientX + 12) + "px";
      tip.style.top = (e.clientY + 12) + "px";
      tip.style.display = "block";
    }});
    el.addEventListener("pointerleave", function() {{ tip.style.display = "none"; }});
  }});
  var sel = document.getElementById("tc-proj-select");
  if (sel) {{
    sel.addEventListener("change", function() {{
      document.querySelectorAll(".tc-proj-panel").forEach(function(p) {{ p.style.display = "none"; }});
      var target = document.getElementById(sel.value);
      if (target) target.style.display = "block";
    }});
  }}
}})();
</script>
</div>
"""


def render_document(conn: sqlite3.Connection, project_paths: list[Path] | None = None) -> str:
    """Full standalone HTML document (doctype/head/body) wrapping render_html()'s
    fragment — for writing a local file that opens directly in a browser. Use
    render_html() directly instead when embedding into something that supplies
    its own document skeleton (e.g. a claude.ai Artifact)."""
    body = render_html(conn, project_paths)
    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Coach — Panel</title>
<style>
  body {{ margin: 0; background: #f9f9f7; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #0d0d0d; }} }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
