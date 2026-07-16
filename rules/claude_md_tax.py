"""Lints CLAUDE.md files for token waste: oversized files, sections that belong
in a path-scoped CLAUDE.md, and duplicated lines."""

import re
from dataclasses import dataclass, field
from pathlib import Path

SIZE_THRESHOLD_TOKENS = 1500
MIN_DUPLICATE_LINE_LENGTH = 10

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_PATH_TOKEN_RE = re.compile(r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)/?`")


@dataclass
class Finding:
    rule: str
    path: Path
    message: str
    est_wasted_tokens: int
    # 1-based line the finding anchors to (for editor diagnostics); None when
    # the finding concerns the whole file.
    line: int | None = None


@dataclass
class Section:
    heading: str | None
    start_line: int
    lines: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.lines)


def estimate_tokens(text: str) -> int:
    return int(len(text) / 4)


def find_claude_md_files(project_root: Path) -> list[Path]:
    return sorted(project_root.rglob("CLAUDE.md"))


def split_sections(text: str) -> list[Section]:
    """Split markdown text into sections by heading line (# ... ######)."""
    sections: list[Section] = []
    current = Section(heading=None, start_line=1)
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if match:
            if current.heading is not None or current.lines:
                sections.append(current)
            current = Section(heading=match.group(2).strip(), start_line=line_no)
        else:
            current.lines.append(line)
    if current.heading is not None or current.lines:
        sections.append(current)
    return sections


def find_subdirectory_mentions(text: str) -> set[str]:
    """Find backtick-wrapped path-like tokens (e.g. `` `src/api/` ``) in text.

    Restricted to backtick-quoted tokens rather than bare word/word text — plain
    English word-pairs like "clone/dupe" or "old/legacy" would otherwise be
    misread as directory paths, while real paths in CLAUDE.md prose are almost
    always backtick-quoted by convention.
    """
    mentions = set()
    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token.lower().startswith(("http://", "https://", "www.")):
            continue
        mentions.add(token)
    return mentions


def check_total_size(path: Path, text: str) -> list[Finding]:
    tokens = estimate_tokens(text)
    if tokens <= SIZE_THRESHOLD_TOKENS:
        return []
    return [
        Finding(
            rule="total_size",
            path=path,
            message=(
                f"{path} ~{tokens} token — {SIZE_THRESHOLD_TOKENS} tokenlik bütçenin üstünde."
            ),
            est_wasted_tokens=tokens - SIZE_THRESHOLD_TOKENS,
            line=1,
        )
    ]


def check_path_scoped_candidates(path: Path, text: str) -> list[Finding]:
    findings = []
    for section in split_sections(text):
        if section.heading is None:
            continue
        dirs = find_subdirectory_mentions(section.body)
        if not dirs:
            continue
        tokens = estimate_tokens(section.body)
        dirs_str = ", ".join(sorted(dirs))
        findings.append(
            Finding(
                rule="path_scoped_candidate",
                path=path,
                message=(
                    f"'{section.heading}' bölümü (satır {section.start_line}) {dirs_str} "
                    f"yolundan bahsediyor — o klasördeki path-scoped bir CLAUDE.md'ye "
                    f"taşımayı düşün."
                ),
                est_wasted_tokens=tokens,
                line=section.start_line,
            )
        )
    return findings


def check_duplicated_lines(path: Path, text: str) -> list[Finding]:
    findings = []
    seen: dict[str, list[int]] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if len(stripped) < MIN_DUPLICATE_LINE_LENGTH or stripped.startswith("#"):
            continue
        seen.setdefault(stripped, []).append(line_no)
    for line, line_nos in seen.items():
        if len(line_nos) <= 1:
            continue
        wasted = estimate_tokens(line) * (len(line_nos) - 1)
        lines_str = ", ".join(str(n) for n in line_nos)
        findings.append(
            Finding(
                rule="duplicated_line",
                path=path,
                message=f"Satır {len(line_nos)}x tekrarlanmış (satırlar: {lines_str}): {line!r}",
                est_wasted_tokens=wasted,
                line=line_nos[1],  # anchor at the first *redundant* occurrence
            )
        )
    return findings


def lint_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    return [
        *check_total_size(path, text),
        *check_path_scoped_candidates(path, text),
        *check_duplicated_lines(path, text),
    ]


def lint_project(project_root: Path) -> list[Finding]:
    findings = []
    for path in find_claude_md_files(project_root):
        findings.extend(lint_file(path))
    return findings
