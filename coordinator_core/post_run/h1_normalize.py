"""
coordinator_core.post_run.h1_normalize — one canonical H1 per markdown page.

Purpose: after a distill or lessons run every touched page carries exactly one H1, and it reads
the frontmatter `title` (falling back to the filename slug). Pure text transform plus a thin
file wrapper; no model, no git, no subprocess.

Invariant: idempotent (a second pass changes nothing); headings inside fences are never touched;
frontmatter bytes are preserved verbatim. Heading detection reuses guide_index._split_markdown;
line positions are found with the same fence rule and cross-checked against its heading list.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.distill.guide_index import _HEADING_RE, _split_markdown

_SKIP_NAMES = frozenset({"_index.md"})


def _frontmatter_end(lines: list[str]) -> int:
    """Index of the first line after the frontmatter block, 0 when there is none."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def _frontmatter_title(lines: list[str], end: int) -> str:
    if end == 0:
        return ""
    try:
        import yaml

        data = yaml.safe_load("\n".join(lines[1 : end - 1]))
    except Exception:
        return ""
    title = data.get("title") if isinstance(data, dict) else None
    return " ".join(str(title).split()) if isinstance(title, (str, int, float)) else ""


def _h1_line_indexes(lines: list[str], start: int) -> list[int]:
    out: list[int] = []
    in_fence = False
    for i in range(start, len(lines)):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            out.append(i)
    return out


def normalize_text(text: str, slug: str) -> str:
    """Return `text` with exactly one H1 equal to the frontmatter title (else `slug`).
    A missing H1 is inserted after the frontmatter; the first H1 is rewritten to the canonical
    title; a later H1 is dropped when it repeats the canonical title, otherwise demoted to H2."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    end = _frontmatter_end(lines)
    canonical = _frontmatter_title(lines, end) or slug
    h1s = _h1_line_indexes(lines, end)
    headings, _ = _split_markdown(text)
    if len(h1s) != sum(1 for level, _t in headings if level == 1):
        return text
    trailing_nl = text.endswith("\n")
    canon_line = f"# {canonical}"
    if not h1s:
        insert = [canon_line, ""]
        rest = lines[end:]
        if rest and rest[0].strip() == "":
            insert = [canon_line]
        lines = lines[:end] + insert + rest
    else:
        drop: set[int] = set()
        for n, i in enumerate(h1s):
            m = _HEADING_RE.match(lines[i])
            title = m.group(2) if m else ""
            if n == 0:
                lines[i] = canon_line
            elif title == canonical:
                drop.add(i)
            else:
                lines[i] = f"## {title}"
        lines = [ln for i, ln in enumerate(lines) if i not in drop]
    out = eol.join(lines)
    return out + eol if trailing_nl or not text else out


def normalize_file(path: Path) -> bool:
    """Normalise one page in place; True when bytes changed. `_index.md` and unreadable files are skipped."""
    path = Path(path)
    if path.name in _SKIP_NAMES:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    new = normalize_text(text, path.stem)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8", newline="")
    return True


def normalize_paths(paths: list[Path]) -> list[Path]:
    """Normalise each markdown path; return the ones whose bytes changed (input order)."""
    return [Path(p) for p in paths if Path(p).suffix == ".md" and normalize_file(Path(p))]
