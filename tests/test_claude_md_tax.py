from pathlib import Path

from rules import claude_md_tax

FIXTURE = Path(__file__).parent / "fixtures" / "sample_claude_md.md"


def test_estimate_tokens():
    assert claude_md_tax.estimate_tokens("abcd") == 1
    assert claude_md_tax.estimate_tokens("ab") == 0
    assert claude_md_tax.estimate_tokens("a" * 4000) == 1000


def test_check_total_size_below_threshold():
    text = "short file"
    findings = claude_md_tax.check_total_size(FIXTURE, text)
    assert findings == []


def test_check_total_size_above_threshold():
    text = "a" * (4 * claude_md_tax.SIZE_THRESHOLD_TOKENS + 400)
    findings = claude_md_tax.check_total_size(FIXTURE, text)
    assert len(findings) == 1
    assert findings[0].rule == "total_size"
    assert findings[0].est_wasted_tokens == 100


def test_split_sections_groups_by_heading():
    text = FIXTURE.read_text(encoding="utf-8")
    sections = claude_md_tax.split_sections(text)
    headings = [s.heading for s in sections]
    assert headings == ["Sample Project", "Frontend (`src/web/app/`)", "Backend"]


def test_find_subdirectory_mentions_detects_backticked_paths():
    mentions = claude_md_tax.find_subdirectory_mentions(
        "This applies to `src/web/app/` and its `src/web/app/components/`."
    )
    assert "src/web/app" in mentions


def test_find_subdirectory_mentions_ignores_bare_word_pairs():
    mentions = claude_md_tax.find_subdirectory_mentions(
        "This is true/false and clone/dupe, not a real path — and/or so on."
    )
    assert mentions == set()


def test_check_path_scoped_candidates_flags_only_dir_mentioning_sections():
    text = FIXTURE.read_text(encoding="utf-8")
    findings = claude_md_tax.check_path_scoped_candidates(FIXTURE, text)
    assert len(findings) == 1
    assert "Frontend" in findings[0].message
    assert "src/web/app" in findings[0].message


def test_check_duplicated_lines_finds_repeated_line():
    text = FIXTURE.read_text(encoding="utf-8")
    findings = claude_md_tax.check_duplicated_lines(text=text, path=FIXTURE)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "duplicated_line"
    assert "3x" in finding.message
    assert finding.est_wasted_tokens == claude_md_tax.estimate_tokens(
        "Always run the test suite before committing changes."
    ) * 2


def test_lint_file_runs_all_checks():
    findings = claude_md_tax.lint_file(FIXTURE)
    rules = {f.rule for f in findings}
    assert rules == {"path_scoped_candidate", "duplicated_line"}


def test_find_claude_md_files_recurses(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("root", encoding="utf-8")
    nested = tmp_path / "src" / "web"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("nested", encoding="utf-8")
    (tmp_path / "src" / "notes.md").write_text("ignore me", encoding="utf-8")

    found = claude_md_tax.find_claude_md_files(tmp_path)
    assert found == sorted([tmp_path / "CLAUDE.md", nested / "CLAUDE.md"])


def test_lint_project_aggregates_across_files(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("short", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "CLAUDE.md").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    findings = claude_md_tax.lint_project(tmp_path)
    assert all(f.path in (tmp_path / "CLAUDE.md", nested / "CLAUDE.md") for f in findings)
    assert any(f.path == nested / "CLAUDE.md" for f in findings)
