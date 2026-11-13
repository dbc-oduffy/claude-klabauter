"""
coordinator_core.updatedocs.readme_wikis — derive the `docs/README.md` "Wikis and Guides" table.

Purpose: render the section's `| Wiki entry | Topic |` table from the `*.md` files in
`docs/wiki` (Topic = `readme_index._extract_title`: frontmatter title, else first
heading, else stem) and splice it into the README, leaving every other line alone.

Negative spec: only the contiguous table block under the "Wikis and Guides" heading is
rewritten; prose before or after it, and every other section, is preserved byte for byte.
Rows are sorted by filename so a second run is a no-op. Never constructs a `GateResult`;
the sweep layer (`coordinator_core.ops.updatedocs_sweeps`) maps the result to a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coordinator_core.updatedocs import _stamp_cache
from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.readme_index import (
    _SECTION_HEADING_RE,
    _extract_title,
    _section_bodies,
)

SECTION_NAME = "Wikis and Guides"
_TABLE_HEADER = ("| Wiki entry | Topic |", "|---|---|")


@dataclass(frozen=True)
class WikisSyncResult:
    changed: bool
    rows: int
    added: int
    dropped: int


def render_wikis_table(wiki_dir: Path) -> list[str]:
    """Table lines (header, separator, one row per `*.md` in `wiki_dir`, sorted by name).

    Raises `OSError` if `wiki_dir` cannot be listed.
    """
    names = sorted(name for name, _ in _stamp_cache.list_md(wiki_dir))
    lines = list(_TABLE_HEADER)
    for name in names:
        topic = _extract_title(wiki_dir / name).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| [`{name}`](wiki/{name}) | {topic} |")
    return lines


def _row_targets(lines: list[str]) -> set[str]:
    return {ln.split("](wiki/", 1)[1].split(")", 1)[0] for ln in lines if "](wiki/" in ln}


def splice_wikis_table(readme_text: str, table_lines: list[str]) -> str | None:
    """`readme_text` with the Wikis section's table block replaced by `table_lines`.

    A section with no table gets one inserted after its heading. Returns None when the
    README has no "Wikis and Guides" heading.
    """
    headings = list(_SECTION_HEADING_RE.finditer(readme_text))
    for index, match in enumerate(headings):
        if match.group(1) != SECTION_NAME:
            continue
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(readme_text)
        body_lines = readme_text[start:end].split("\n")
        table_idx = [i for i, ln in enumerate(body_lines) if ln.startswith("|")]
        if table_idx:
            first, last = table_idx[0], table_idx[-1]
            body_lines[first : last + 1] = table_lines
        else:
            at = 1 if body_lines[0] == "" else 0
            body_lines[at:at] = [*table_lines, ""]
        return readme_text[:start] + "\n".join(body_lines) + readme_text[end:]
    return None


def sync_readme_wikis(repo_root: Path) -> WikisSyncResult:
    """Rewrite the Wikis table of `<repo_root>/docs/README.md` from `docs/wiki`.

    Raises `UpdatedocsTargetMissing` when `docs/README.md`, `docs/wiki` or the section
    heading is absent; nothing is written in that case.
    """
    docs_dir = Path(repo_root) / "docs"
    readme_path = docs_dir / "README.md"
    wiki_dir = docs_dir / "wiki"
    if not readme_path.is_file():
        raise UpdatedocsTargetMissing(readme_path)
    if not wiki_dir.is_dir():
        raise UpdatedocsTargetMissing(wiki_dir)

    try:
        table = render_wikis_table(wiki_dir)
    except OSError:
        raise UpdatedocsTargetMissing(wiki_dir) from None
    original = readme_path.read_text(encoding="utf-8", errors="replace")
    updated = splice_wikis_table(original, table)
    if updated is None:
        raise UpdatedocsTargetMissing(readme_path)

    old_targets = _row_targets(_section_bodies(original).get(SECTION_NAME, "").split("\n"))
    new_targets = _row_targets(table)
    if updated != original:
        with readme_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
    return WikisSyncResult(
        changed=updated != original,
        rows=len(new_targets),
        added=len(new_targets - old_targets),
        dropped=len(old_targets - new_targets),
    )
