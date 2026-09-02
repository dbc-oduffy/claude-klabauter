"""
coordinator_core.updatedocs.readme_index — docs/README.md index drift detector.

Purpose: pure, read-only comparison of the links `docs/README.md` actually carries
against the files that exist on disk for each indexed corpus (wiki, plans, research,
problems, decisions, top-level reference docs). Answers audit rows A4/A5 of
`state/audits/2026-09-02-updatedocs-distill-mechanization-boundary.md`: which docs/
artifacts are missing from, or dead in, the README.

Negative spec: this module never writes `docs/README.md`, never regenerates its
tables, and never constructs a `GateResult` — verdict mapping (including the
CLEAN vs UNAVAILABLE distinction for a missing target) belongs to
`coordinator_core.ops.updatedocs_gates`, one layer up. It also never parses the
README's table *shape* — the tables are hand-formatted prose, and a row-shape
parser breaks on the next hand edit; only markdown link *targets* are read.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_FIRST_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

_TITLE_READ_BYTES = 800

# Section name (as it appears verbatim after `## ` in docs/README.md) -> the
# corpus subdirectory of `docs/`, relative, non-recursive `*.md` glob. An empty
# string means the top-level `docs/*.md` files themselves (Reference
# Documentation) rather than a subdirectory.
SECTION_CORPORA: dict[str, str] = {
    "Wikis and Guides": "wiki",
    "Plans": "plans",
    "Research": "research",
    "Problems": "problems",
    "Decisions": "decisions",
    "Reference Documentation": "",
}


class ReadmeIndexUnavailable(Exception):
    """Raised when a required path (docs/ or docs/README.md) does not exist.

    Carries `missing_path` so the caller (the gate layer) can report exactly
    which path was absent rather than a bare "something is missing" message.
    This module never swallows this into an empty-but-clean result and never
    builds a `GateResult` itself — that mapping (UNAVAILABLE, never CLEAN)
    belongs to `coordinator_core.ops.updatedocs_gates`.
    """

    def __init__(self, missing_path: Path):
        self.missing_path = missing_path
        super().__init__(f"required path does not exist: {missing_path}")


@dataclass(frozen=True)
class SectionDrift:
    section: str
    linked: int
    on_disk: int
    missing: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    missing_titles: dict[str, str] = field(default_factory=dict)
    """Display name per `missing` filename, via the three-tier title fallback.

    Carried because the consumer's next act is writing a README row for each
    missing entry, and deriving its display name is the deterministic half of
    that. Only the `Topic`/description column is judgment, so only that column
    is left to a model.
    """


@dataclass(frozen=True)
class ReadmeIndexDrift:
    sections: list[SectionDrift]


def _strip_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a possible leading `---`-delimited frontmatter block off `text`.

    Returns (frontmatter_text_or_None, body_text). Tolerates a frontmatter block
    truncated by the caller's read-size cap: if no closing `---` is found within
    the given chunk, the whole chunk is treated as (possibly partial)
    frontmatter and the body is empty, rather than raising.
    """
    if not text.startswith("---"):
        return None, text
    rest = text[3:]
    close = rest.find("\n---")
    if close == -1:
        return rest, ""
    return rest[:close], rest[close + 4 :]


def _extract_title(path: Path) -> str:
    """Three-tier title fallback: frontmatter `title:` -> first `# ` heading ->
    filename stem. Reads at most the first ~800 bytes of the file. Never
    raises and never returns an empty title — a file with neither frontmatter
    `title:` nor a `# ` heading falls through to its filename stem.
    """
    try:
        with path.open("rb") as handle:
            chunk = handle.read(_TITLE_READ_BYTES)
    except OSError:
        return path.stem

    text = chunk.decode("utf-8", errors="replace")
    frontmatter, body = _strip_frontmatter(text)

    if frontmatter is not None:
        match = _FRONTMATTER_TITLE_RE.search(frontmatter)
        if match:
            title = match.group(1).strip().strip('"').strip("'").strip()
            if title:
                return title

    match = _FIRST_HEADING_RE.search(body)
    if match:
        heading = match.group(1).strip()
        if heading:
            return heading

    return path.stem


def _section_bodies(readme_text: str) -> dict[str, str]:
    """Split docs/README.md's body into per-`## `-section text, keyed by the
    heading name exactly as written (before any trailing whitespace).
    """
    headings = list(_SECTION_HEADING_RE.finditer(readme_text))
    bodies: dict[str, str] = {}
    for index, match in enumerate(headings):
        name = match.group(1)
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(readme_text)
        bodies[name] = readme_text[start:end]
    return bodies


def _resolve_link_target(docs_dir: Path, raw_target: str) -> Path | None:
    """Resolve a markdown link target relative to `docs_dir`.

    Returns None for anything that is not a plain, in-tree `docs/`-relative
    path: external URLs, bare anchors, and anything that resolves outside
    `docs_dir` (e.g. `../README.md`, the repo-root README linked from
    "Start here", which is a different file entirely).
    """
    target = raw_target.split("#", 1)[0].strip()
    if not target:
        return None
    if "://" in target:
        return None
    try:
        resolved = (docs_dir / target).resolve()
    except (OSError, ValueError):
        return None
    try:
        resolved.relative_to(docs_dir.resolve())
    except ValueError:
        return None
    return resolved


def _linked_names_for_section(docs_dir: Path, corpus_dir: Path, body: str) -> set[str]:
    linked: set[str] = set()
    for match in _LINK_RE.finditer(body):
        resolved = _resolve_link_target(docs_dir, match.group(1))
        if resolved is None:
            continue
        if resolved.suffix != ".md":
            continue
        if resolved.parent != corpus_dir:
            continue
        linked.add(resolved.name)
    return linked


def compute_readme_index_drift(repo_root: Path) -> ReadmeIndexDrift:
    """Compare `docs/README.md`'s links against the on-disk corpus for each
    indexed section, per `SECTION_CORPORA`.

    Raises `ReadmeIndexUnavailable` if `docs/` or `docs/README.md` is absent.
    Never builds a partial/empty-but-clean result in that case.
    """
    repo_root = Path(repo_root)
    docs_dir = repo_root / "docs"
    if not docs_dir.is_dir():
        raise ReadmeIndexUnavailable(docs_dir)

    readme_path = docs_dir / "README.md"
    if not readme_path.is_file():
        raise ReadmeIndexUnavailable(readme_path)

    readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
    bodies = _section_bodies(readme_text)

    sections: list[SectionDrift] = []
    for section_name, subdir in SECTION_CORPORA.items():
        corpus_dir = (docs_dir / subdir) if subdir else docs_dir
        if corpus_dir.is_dir():
            on_disk_paths = sorted(corpus_dir.glob("*.md"))
        else:
            on_disk_paths = []
        on_disk_names = {p.name for p in on_disk_paths}

        body = bodies.get(section_name, "")
        linked_names = _linked_names_for_section(docs_dir, corpus_dir.resolve(), body)

        missing = sorted(on_disk_names - linked_names)
        dead = sorted(linked_names - on_disk_names)

        by_name = {p.name: p for p in on_disk_paths}
        missing_titles = {name: _extract_title(by_name[name]) for name in missing}

        sections.append(
            SectionDrift(
                section=section_name,
                linked=len(linked_names),
                on_disk=len(on_disk_names),
                missing=missing,
                dead=dead,
                missing_titles=missing_titles,
            )
        )

    return ReadmeIndexDrift(sections=sections)
