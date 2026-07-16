"""Token Coach CLI."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parser import ingest
from report import dashboard, weekly
from rules import cache_efficiency, claude_md_tax, model_mismatch, stale_context

# Session-scoped diagnosis rules. Add new rule modules here as they land
# (subagent_overuse — see PLAN.md Faz 3).
SESSION_RULES = [stale_context, cache_efficiency, model_mismatch]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="Token Coach")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_p = sub.add_parser("ingest", help="Claude Code JSONL loglarını SQLite'a aktar")
    ingest_p.add_argument(
        "--db",
        default="token_coach.db",
        type=Path,
        help="SQLite veritabanı dosyasının yolu (varsayılan: ./token_coach.db)",
    )
    ingest_p.add_argument(
        "--rebuild",
        action="store_true",
        help="Değişmemiş olsalar bile tüm log dosyalarını yeniden işle "
             "(şema geçişlerinde eklenen kolonları doldurur)",
    )

    lint_p = sub.add_parser("lint", help="Proje kökü altındaki CLAUDE.md dosyalarını lintle")
    lint_p.add_argument("project_path", type=Path, help="CLAUDE.md aranacak proje kökü")

    report_p = sub.add_parser("report", help="Token Coach bulgu raporu üret")
    report_p.add_argument(
        "--weekly", action="store_true", required=True, help="Haftalık raporu üret (son 7 gün)"
    )
    report_p.add_argument(
        "--db",
        default="token_coach.db",
        type=Path,
        help="SQLite veritabanı dosyasının yolu (varsayılan: ./token_coach.db)",
    )

    dashboard_p = sub.add_parser("dashboard", help="Statik HTML panel üret")
    dashboard_p.add_argument(
        "--db",
        default="token_coach.db",
        type=Path,
        help="SQLite veritabanı dosyasının yolu (varsayılan: ./token_coach.db)",
    )
    dashboard_p.add_argument(
        "--project",
        dest="project_paths",
        action="append",
        default=None,
        type=Path,
        help="CLAUDE.md lint bulguları dahil edilecek proje kökü (tekrarlanabilir)",
    )
    dashboard_p.add_argument(
        "--projects-root",
        dest="projects_root",
        default=None,
        type=Path,
        help="Bu klasörün alt klasörlerini CLAUDE.md için tara ve hepsini dahil et "
             "(panele proje seçici dropdown eklenir)",
    )
    dashboard_p.add_argument(
        "--out",
        default="dashboard.html",
        type=Path,
        help="Çıktı HTML dosyasının yolu (varsayılan: ./dashboard.html)",
    )

    diagnose_p = sub.add_parser(
        "diagnose", help="Tüm teşhis kurallarını çalıştır ve bulguları yazdır (metin veya JSON)"
    )
    diagnose_p.add_argument(
        "--db",
        default="token_coach.db",
        type=Path,
        help="SQLite veritabanı dosyasının yolu (varsayılan: ./token_coach.db)",
    )
    diagnose_p.add_argument(
        "--project",
        dest="project_paths",
        action="append",
        default=None,
        type=Path,
        help="Bu proje kökü için CLAUDE.md lint bulgularını da dahil et (tekrarlanabilir)",
    )
    diagnose_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Makine tarafından okunabilir JSON üret (VS Code eklentisi ve diğer araçlar için)",
    )
    diagnose_p.add_argument(
        "--days",
        type=int,
        default=0,
        help="Yalnızca son N gün içinde başlayan oturumların bulgularını dahil et "
             "(0 = tüm geçmiş; CLAUDE.md dosya bulguları her zaman dahildir)",
    )

    return parser


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = ingest.connect(args.db)
    try:
        stats = ingest.run_ingest(conn, force=args.rebuild)
    finally:
        conn.close()
    print(
        f"{stats['new_turns']} yeni turn alındı ({stats['files_ingested']} dosya işlendi, "
        f"{stats['files_skipped']} değişmemiş dosya atlandı; "
        f"toplam {stats['files_scanned']} dosya tarandı)."
    )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    findings = claude_md_tax.lint_project(args.project_path)
    if not findings:
        print(f"Bulgu yok — {args.project_path} altında CLAUDE.md yok (ya da sorun yok).")
        return 0

    findings_by_path: dict[Path, list[claude_md_tax.Finding]] = {}
    for finding in findings:
        findings_by_path.setdefault(finding.path, []).append(finding)

    for path, path_findings in findings_by_path.items():
        print(f"\n{path}")
        for finding in path_findings:
            print(f"  [{finding.rule}] {finding.message} (~{finding.est_wasted_tokens} tokens)")

    total_wasted = sum(f.est_wasted_tokens for f in findings)
    print(f"\n{len(findings)} bulgu, tahmini israf ~{total_wasted} token.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = ingest.connect(args.db)
    try:
        report = weekly.weekly_report(conn)
    finally:
        conn.close()
    print(report)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    project_paths = list(args.project_paths or [])
    if args.projects_root is not None:
        project_paths.extend(
            p for p in dashboard.discover_projects(args.projects_root) if p not in project_paths
        )

    conn = ingest.connect(args.db)
    try:
        document = dashboard.render_document(conn, project_paths or None)
    finally:
        conn.close()
    args.out.write_text(document, encoding="utf-8")
    print(f"Dashboard yazildi: {args.out} — tarayicida ac. ({len(project_paths)} proje tarandi)")
    return 0


def collect_diagnose_findings(
    conn, project_paths: list[Path] | None, days: int = 0
) -> list[dict[str, object]]:
    """Run every session rule (plus optional CLAUDE.md lint) and normalize
    findings into plain dicts: rule, kind, scope_type (session|file), scope,
    message, est_wasted_tokens, est_wasted_usd, counterfactual_usd, line
    (1-based, file findings only — editor diagnostics anchor on it).

    `kind` is "waste" (default) or "info". Waste findings report a directly
    measured cost that a different habit would not have paid. Info findings
    report a fact worth knowing that is *not* a waste claim — e.g.
    model_mismatch's price comparison, which rests on the unknowable
    assumption that a cheaper model would have produced the same token
    profile. Only waste findings feed the headline total; conflating the two
    would over-claim.

    days > 0 keeps only session findings whose session started within that
    window; file findings always pass (they describe current files, not
    time-scoped activity)."""
    valid_sessions = None
    if days > 0:
        now = datetime.now(timezone.utc)
        valid_sessions = weekly.sessions_in_window(conn, now - timedelta(days=days), now)

    findings: list[dict[str, object]] = []
    for rule_module in SESSION_RULES:
        for f in rule_module.run(conn):
            if valid_sessions is not None and f.session_id not in valid_sessions:
                continue
            findings.append(
                {
                    "rule": f.rule,
                    "kind": getattr(f, "kind", "waste"),
                    "scope_type": "session",
                    "scope": f.session_id,
                    "message": f.message,
                    "est_wasted_tokens": f.est_wasted_tokens,
                    "est_wasted_usd": getattr(f, "est_wasted_usd", 0.0) or 0.0,
                    "counterfactual_usd": getattr(f, "counterfactual_usd", 0.0) or 0.0,
                    "line": None,
                }
            )
    for project_path in project_paths or []:
        for f in claude_md_tax.lint_project(project_path):
            findings.append(
                {
                    "rule": f.rule,
                    "kind": "waste",
                    "scope_type": "file",
                    "scope": str(f.path),
                    "message": f.message,
                    "est_wasted_tokens": f.est_wasted_tokens,
                    "est_wasted_usd": 0.0,
                    "counterfactual_usd": 0.0,
                    "line": f.line,
                }
            )
    # Dollar-bearing findings first (by USD), then token-only findings — the
    # two units aren't directly comparable, so this keeps each group coherent.
    findings.sort(
        key=lambda f: (f["est_wasted_usd"], f["est_wasted_tokens"]), reverse=True
    )
    return findings


def cmd_diagnose(args: argparse.Namespace) -> int:
    conn = ingest.connect(args.db)
    try:
        findings = collect_diagnose_findings(conn, args.project_paths, days=args.days)
    finally:
        conn.close()

    # Yalnızca ölçülen israf manşete girer; bilgi amaçlı karşılaştırmalar
    # (kind="info") toplanmaz — karşı-olgusal bir farkı israf saymak, aracın
    # tek sermayesi olan güveni harcar.
    waste = [f for f in findings if f.get("kind", "waste") == "waste"]
    info = [f for f in findings if f.get("kind", "waste") == "info"]
    total_wasted = sum(f["est_wasted_tokens"] for f in waste)
    total_usd = round(sum(f["est_wasted_usd"] for f in waste), 2)

    if args.as_json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(args.db),
            "total_est_wasted_tokens": total_wasted,
            "total_est_wasted_usd": total_usd,
            "findings": findings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not findings:
        print("Bulgu yok.")
        return 0
    scope_labels = {"session": "oturum", "file": "dosya"}
    for finding in waste:
        scope_label = scope_labels.get(finding["scope_type"], finding["scope_type"])
        print(f"[{finding['rule']}] {scope_label} {finding['scope']}")
        usd = finding["est_wasted_usd"]
        estimate = f"~{finding['est_wasted_tokens']} token" + (f", = ${usd:.2f}" if usd else "")
        print(f"  {finding['message']} ({estimate})")
    print(
        f"\n{len(waste)} bulgu, tahmini israf ~{total_wasted} token "
        f"(= ${total_usd:.2f})."
    )
    if info:
        print("\n--- Bilgi (israf değil, karşılaştırma) ---")
        for finding in info:
            scope_label = scope_labels.get(finding["scope_type"], finding["scope_type"])
            print(f"[{finding['rule']}] {scope_label} {finding['scope']}")
            print(f"  {finding['message']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "lint":
        return cmd_lint(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)
    if args.command == "diagnose":
        return cmd_diagnose(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
