"""coordinator_core.post_run.linkfix -- rewrite broken relative `.md` links, decisions/ included.

Purpose: after a distill or lessons run, every touched wiki page's relative markdown links are
re-pointed at the current location of their target, repeated until a pass changes nothing.
One directory walk (names only, no file reads) builds the basename -> paths map; only touched
pages are read and rewritten. Pure over paths and text; no git, no LLM, no import of the
sibling `scope` module.

Invariants: a link whose target exists is never touched; an unresolvable link and a link whose
basename maps to more than one path are reported in `residual`, never deleted and never
guessed; links inside fenced code are left alone; line endings and anchors are preserved.
A `decisions/` target renamed to a new slug under the same `DR-<n>` number resolves by that number.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_MD_LINK_RE = re.compile(r"(\[[^\]\n]*\])\(([^)\n#]+\.md)((?:#[^)\n]*)?)\)")
_DR_RE = re.compile(r"^DR-(\d+)-")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class Residual:
    """One link this step could not fix: `reason` is `unresolvable` or `ambiguous`."""

    page: str
    target: str
    reason: str


@dataclass(frozen=True)
class LinkfixResult:
    """`changed` lists the pages rewritten, `residual` the links still broken after the final pass."""

    changed: tuple[Path, ...]
    residual: tuple[Residual, ...]
    passes: int


@dataclass(frozen=True)
class PageMap:
    """Names-only index of every `.md` under the roots; `by_name` and `by_dr` are path lists."""

    by_name: dict[str, tuple[Path, ...]]
    by_dr: dict[str, tuple[Path, ...]]


def _walk_md(root: Path, out: set[Path]) -> None:
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
            elif e.name.endswith(".md"):
                out.add(Path(e.path).resolve())


def build_page_map(wiki_roots: Iterable[Path], decisions_dir: Path | None = None) -> PageMap:
    """One walk of `wiki_roots` plus `decisions_dir`; overlapping roots dedupe by resolved path."""
    pages: set[Path] = set()
    for root in wiki_roots:
        _walk_md(Path(root), pages)
    decisions: set[Path] = set()
    if decisions_dir is not None:
        _walk_md(Path(decisions_dir), decisions)
        pages |= decisions
    by_name: dict[str, list[Path]] = {}
    for p in sorted(pages):
        by_name.setdefault(p.name, []).append(p)
    by_dr: dict[str, list[Path]] = {}
    for p in sorted(decisions):
        m = _DR_RE.match(p.name)
        if m:
            by_dr.setdefault(m.group(1), []).append(p)
    return PageMap(
        {k: tuple(v) for k, v in by_name.items()},
        {k: tuple(v) for k, v in by_dr.items()},
    )


def _relative(target: Path, page: Path) -> str:
    rel = Path(os.path.relpath(target, page.parent)).as_posix()
    return rel if rel.startswith(".") else "./" + rel


def fix_links_in_text(
    text: str, page: Path, page_map: PageMap
) -> tuple[str, list[Residual]]:
    """Rewrite broken links in `text` (the content of `page`); returns (new_text, residual)."""
    page = Path(page).resolve()
    residual: list[Residual] = []

    def _sub(m: "re.Match[str]") -> str:
        label, target, anchor = m.group(1), m.group(2), m.group(3)
        if _SCHEME_RE.match(target) or target.startswith("/"):
            return m.group(0)
        try:
            if (page.parent / target).resolve().is_file():
                return m.group(0)
        except OSError:
            return m.group(0)
        name = Path(target.replace("\\", "/")).name
        candidates = page_map.by_name.get(name)
        if not candidates:
            dr = _DR_RE.match(name)
            candidates = page_map.by_dr.get(dr.group(1)) if dr else None
        if not candidates:
            residual.append(Residual(page.as_posix(), target, "unresolvable"))
            return m.group(0)
        if len(candidates) > 1:
            residual.append(Residual(page.as_posix(), target, "ambiguous"))
            return m.group(0)
        return f"{label}({_relative(candidates[0], page)}{anchor})"

    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(_MD_LINK_RE.sub(_sub, line))
    return "".join(out), residual


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def fix_to_fixpoint(
    touched: Iterable[Path],
    wiki_roots: Iterable[Path],
    decisions_dir: Path | None = None,
) -> LinkfixResult:
    """Fix links on every touched `.md` page under `wiki_roots`, looping until a pass changes nothing.

    Non-markdown, missing, and out-of-root touched paths are skipped. The loop is bounded by
    the page count plus one; `residual` is taken from the final, change-free pass.
    """
    roots = [Path(r).resolve() for r in wiki_roots]
    page_map = build_page_map(roots, decisions_dir)
    pages = sorted(
        {
            p
            for p in (Path(t).resolve() for t in touched)
            if p.suffix == ".md" and _under(p, roots) and p.is_file()
        }
    )
    texts: dict[Path, str] = {}
    for p in pages:
        texts[p] = p.read_bytes().decode("utf-8")
    original = dict(texts)

    passes = 0
    residual: list[Residual] = []
    for _ in range(len(pages) + 1):
        passes += 1
        changed_any = False
        residual = []
        for p in pages:
            new, res = fix_links_in_text(texts[p], p, page_map)
            residual.extend(res)
            if new != texts[p]:
                texts[p] = new
                changed_any = True
        if not changed_any:
            break

    changed = tuple(p for p in pages if texts[p] != original[p])
    for p in changed:
        p.write_bytes(texts[p].encode("utf-8"))
    return LinkfixResult(changed, tuple(residual), passes)
