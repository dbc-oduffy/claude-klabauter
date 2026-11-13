"""coordinator_core.post_run.nav_index -- rebuild `_index.md` for the family dirs a run touched.

Purpose: after a distill or lessons run, every wiki directory that holds a touched page gets a
current `_index.md` (breadcrumb, H1, parent-page line, subdirectories, then direct pages), in the
format example-retrieval-repo's `wiki-index` generator emits, so a later `wiki-index --check` stays green.
Git-free: one `os.scandir` walk per root (names only), reads only the pages it lists. Output is
sorted and LF-only; an `_index.md` whose rebuilt bytes equal the disk is not rewritten.

Invariants: only a directory containing a touched page, a touched page's own `X/` family dir, and
ancestors that lack an `_index.md` (a new family must be listed by its parent) are rebuilt.
Dotfiles and `_index.md` are never listed. No hand-written `_index.md` is preserved.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_SUMMARY_CHARS = 120

_MD_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SENTENCE_RE = re.compile(r"^(.*?[.!?])(\s|$)")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|`)(?=\S)(.+?)(?<=\S)\1")
_NEEDS_ESCAPE_RE = re.compile(r"[ ()]")


@dataclass(frozen=True)
class NavIndexResult:
    """`written` lists `_index.md` files created or changed; `unchanged` those rebuilt byte-identically."""

    written: tuple[Path, ...]
    unchanged: tuple[Path, ...]


def _strip_links(text: str) -> str:
    return _MD_LINK_TEXT_RE.sub(lambda m: m.group(1), _MD_IMAGE_RE.sub("", text))


def _parse_page(text: str) -> tuple[str | None, str]:
    """(title, body after the first H1); skips frontmatter or leading HTML-comment lines."""
    lines = text.split("\n")
    idx = 0
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        idx = i + 1
    else:
        while idx < len(lines) and lines[idx].strip().startswith("<!--"):
            idx += 1
    for j in range(idx, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("# "):
            return _strip_links(stripped[2:].strip()), "\n".join(lines[j + 1 :])
    return None, "\n".join(lines[idx:])


def _first_sentence(body: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    para = ""
    for cand in body.split("\n\n"):
        cand = cand.strip()
        if cand and not cand.startswith(("#", "<!--", "```", "|")):
            para = cand
            break
    para = " ".join(_strip_links(_BLOCKQUOTE_RE.sub("", para)).split())
    para = _EMPHASIS_RE.sub(lambda m: m.group(2), para)
    m = _SENTENCE_RE.match(para)
    sentence = m.group(1) if m else para
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-—") + "…"


def _load_page(root: Path, rel: str) -> tuple[str, str]:
    text = (root / rel).read_bytes().decode("utf-8").replace("\r\n", "\n")
    title, body = _parse_page(text)
    return title or Path(rel).stem, _first_sentence(body)


def _rel_link(from_dir: str, target: str) -> str:
    rel = posixpath.relpath(target, from_dir or ".")
    return f"<{rel}>" if _NEEDS_ESCAPE_RE.search(rel) else rel


def _scan(root: Path) -> dict[str, list[str]]:
    """dir (posix, relative to root, "" = root) -> sorted .md names, for every dir holding any
    `.md` at or below it; dotfiles and dot-dirs skipped."""
    pages: dict[str, list[str]] = {}
    stack = [""]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(root / d if d else root) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(f"{d}/{e.name}" if d else e.name)
            elif e.name.endswith(".md") and e.name != "_index.md":
                pages.setdefault(d, []).append(e.name)
    dirs: dict[str, list[str]] = {"": []}
    for d, names in pages.items():
        parts = d.split("/") if d else []
        for i in range(len(parts) + 1):
            dirs.setdefault("/".join(parts[:i]), [])
        dirs[d] = sorted(names)
    return dirs


def _render(root: Path, name: str, d: str, dirs: dict[str, list[str]], cache: dict[str, tuple[str, str]]) -> str:
    def page(rel: str) -> tuple[str, str]:
        if rel not in cache:
            cache[rel] = _load_page(root, rel)
        return cache[rel]

    def join(a: str, b: str) -> str:
        return f"{a}/{b}" if a else b

    title = posixpath.basename(d) if d else name
    parent_line = None
    if d and f"{d}.md" in dirs.get(posixpath.dirname(d), []):
        p_title, p_summary = page(f"{d}.md")
        title = p_title
        parent_line = f"Parent page: [{p_title}]({_rel_link(d, f'{d}.md')})"
        if p_summary:
            parent_line += f" -- {p_summary}"
    lines: list[str] = []
    if d:
        parts = d.split("/")
        crumbs = [f"[{name}]({_rel_link(d, '_index.md')})"]
        for i, label in enumerate(parts, 1):
            crumbs.append(label if i == len(parts) else f"[{label}]({_rel_link(d, '/'.join(parts[:i]) + '/_index.md')})")
        lines += [" > ".join(crumbs), ""]
    lines += [f"# {title}", ""]
    if parent_line:
        lines += [parent_line, ""]
    subdirs = sorted(s for s in dirs if s and posixpath.dirname(s) == d)
    pages = [f for f in dirs.get(d, []) if join(d, f[:-3]) not in dirs]
    for s in subdirs:
        link = _rel_link(d, f"{s}/_index.md")
        if f"{s}.md" in dirs.get(d, []):
            s_title, s_summary = page(f"{s}.md")
            line = f"- [{s_title}/]({link})" + (f" -- {s_summary}" if s_summary else "")
        else:
            line = f"- [{posixpath.basename(s)}/]({link})"
        lines.append(line)
    if subdirs and pages:
        lines.append("")
    for f in pages:
        p_title, p_summary = page(join(d, f))
        lines.append(f"- [{p_title}]({_rel_link(d, join(d, f))})" + (f" -- {p_summary}" if p_summary else ""))
    return "\n".join(lines).rstrip("\n") + "\n"


def _targets(root: Path, touched: list[Path], dirs: dict[str, list[str]]) -> list[str]:
    out: set[str] = set()
    for p in touched:
        if p.suffix != ".md" or p.name == "_index.md":
            continue
        rel = p.relative_to(root).as_posix()
        d = posixpath.dirname(rel)
        if d not in dirs:
            continue
        if rel[:-3] in dirs:
            out.add(rel[:-3])
        while True:
            out.add(d)
            if not d or (root / d / "_index.md").is_file():
                break
            d = posixpath.dirname(d)
    return sorted(out)


def rebuild(touched: Iterable[Path], wiki_roots: Iterable[Path]) -> NavIndexResult:
    """Rebuild `_index.md` for the family dirs containing a touched page under each wiki root."""
    resolved = [Path(t).resolve() for t in touched]
    written: list[Path] = []
    unchanged: list[Path] = []
    for root in sorted({Path(r).resolve() for r in wiki_roots}):
        mine = [p for p in resolved if root in p.parents]
        if not mine:
            continue
        dirs = _scan(root)
        cache: dict[str, tuple[str, str]] = {}
        for d in _targets(root, mine, dirs):
            path = root / d / "_index.md" if d else root / "_index.md"
            data = _render(root, root.name, d, dirs, cache).encode("utf-8")
            if path.is_file() and path.read_bytes() == data:
                unchanged.append(path)
                continue
            path.write_bytes(data)
            written.append(path)
    return NavIndexResult(tuple(written), tuple(unchanged))
