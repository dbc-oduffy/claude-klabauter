"""coordinator_core.ops.reachability_docs — markdown cross-reference resolver for review.reachability.

A new doc is reachable when another non-test, non-record file's added line names it, or an index
file in one of its ancestor directories does. Inputs are the diff's `added` map, the shared
`read_index` list and the ancestor index files; never a whole-tree read, never a spawn.

Negative-spec:
  - `SKILL.md`, `commands/*.md`, `agents/*.md` are discovery-resolved and produce no entry.
  - Records (state, tasks, scratch, archive, docs/{plans,research,decisions,problems,delivery}) produce
    no entry and are never a referrer: a plan citing a path does not wire it.
  - A markdown path outside `docs/wiki/`, `docs/reference/` and skill pages is `undecidable`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_RECORD_PREFIXES = (
    "state/", "tasks/", "scratch/", "archive/", "docs/plans/", "docs/research/", "docs/decisions/",
    "docs/problems/", "docs/delivery/",
)
_DISCOVERED = re.compile(r"(^|/)(SKILL\.md|(commands|agents)/[^/]+\.md)$")
_SKILL_PAGE = re.compile(r"(^|/)skills/[^/]+/.+\.md$")
_MODELLED_PREFIXES = ("docs/wiki/", "docs/reference/")
_INDEX_NAMES = ("README.md", "index.md", "DIRECTORY.md", "SKILL.md", "CLAUDE.md", "_index.md")
_GENERIC_NAMES = frozenset(_INDEX_NAMES)
_TEST_RE = re.compile(r"(\.test\.|\.spec\.|(^|/)tests?/|(^|/)__tests__/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)fixtures?/)")


def _is_record(rel: str) -> bool:
    return rel.startswith(_RECORD_PREFIXES)


def _modelled(rel: str) -> bool:
    return rel.startswith(_MODELLED_PREFIXES) or bool(_SKILL_PAGE.search(rel))


def doc_entries(rel: str) -> list[tuple[str, str]]:
    """[("doc", "")] for a markdown path the op models, else []."""
    if not rel.endswith(".md") or _DISCOVERED.search(rel) or _is_record(rel):
        return []
    return [("doc", "")]


def _patterns(rel: str, referrer: str) -> list[re.Pattern]:
    """Spellings of `rel` as `referrer` would write it: repo path, referrer-relative path, filename."""
    p = PurePosixPath(rel)
    names = {rel, p.name}
    if p.name in _GENERIC_NAMES:
        names = {rel, "/".join(p.parts[-2:])} if len(p.parts) > 1 else {rel}
    parent = PurePosixPath(referrer).parent
    try:
        names.add(p.relative_to(parent).as_posix() if p.is_relative_to(parent) else _up(p, parent))
    except ValueError:
        pass
    pats = [re.compile(r"(?<![\w./-])" + re.escape(n) + r"(?![\w-])") for n in names if "/" in n or p.name not in _GENERIC_NAMES]
    if p.name not in _GENERIC_NAMES:
        pats.append(re.compile(r"\[\[" + re.escape(p.stem) + r"(?:[|#\]])"))
    return pats


def _up(p: PurePosixPath, parent: PurePosixPath) -> str:
    """`p` relative to `parent` with `../` hops."""
    up, base = 0, parent
    while base.parts and not p.is_relative_to(base):
        base, up = base.parent, up + 1
    return "../" * up + p.relative_to(base).as_posix()


def _ancestor_indexes(root: Path, rel: str, index: set[str]) -> list[str]:
    out = []
    for d in [*PurePosixPath(rel).parents]:
        for n in _INDEX_NAMES:
            cand = (d / n).as_posix()
            if cand != rel and (cand in index or (root / cand).is_file()):
                out.append(cand)
    return out


def _named_in(rel: str, referrer: str, text: str) -> bool:
    return any(p.search(text) for p in _patterns(rel, referrer))


def doc_verdicts(
    root: Path, rels: list[str], added: dict[str, list[str]], index: list[str]
) -> dict[str, str]:
    """{rel: "reachable" | "unreachable" | "undecidable"}."""
    referrers = {
        r: "\n".join(lines) for r, lines in added.items()
        if not _TEST_RE.search(r) and not _is_record(r)
    }
    idx = set(index)
    out: dict[str, str] = {}
    for rel in rels:
        if not _modelled(rel):
            out[rel] = "undecidable"
            continue
        if any(_named_in(rel, r, t) for r, t in referrers.items() if r != rel):
            out[rel] = "reachable"
            continue
        wired = False
        for ix in _ancestor_indexes(root, rel, idx):
            try:
                text = (root / ix).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _named_in(rel, ix, text):
                wired = True
                break
        out[rel] = "reachable" if wired else "unreachable"
    return out
