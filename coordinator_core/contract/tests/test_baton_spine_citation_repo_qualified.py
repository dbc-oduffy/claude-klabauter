"""
test_baton_spine_citation_repo_qualified — pins that every repo-wide
citation of `docs/plans/2026-08-01-baton-spine-information-integrity.md`
names its home repo.

That plan was authored and committed in the sibling DoE-claude repo, never
in this one -- this repo's own git history has no add-commit for it under
any name (`git log --all --diff-filter=A -- '*baton-spine-information-
integrity*'` returns nothing), and the ONE commit here that names it as a
spec authority (34d39afce6) trails it `Plan: docs/plans/2026-08-01-baton-
spine-information-integrity.md (DoE-claude)`. A citing file that omits the
`DoE-claude` qualifier reads, to any local reader, as a broken local link
to a path that was "never committed" -- the exact defect this test pins
shut.

Spec backlink: state/bug-backlog/2026-08-10-a-dozen-plus-engine-files-and-the-frozen-01f385fb3020.yaml

Negative-spec:
    - Does not assert the cited plan exists anywhere on disk -- it is a
      DoE-claude authoring-repo document, structurally unreachable from
      this repo. Only that every citation of it is repo-qualified.
    - Scoped to THIS ONE plan path, not a general spec-backlink linter --
      `coordinator_core.ops.assert_no_dangling_plan_backlinks` already owns
      the general moved-plan / id-form / path-form gates, and its
      `spec.?backlink`-line-only scan does not reach most of this plan's
      citation sites (bare `Authority:`/`Source:`/comment-prefixed
      mentions, never the literal string "spec backlink").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import leaf_spawn_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_REL_PATH = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
_PLAN_PATH = "docs/plans/2026-08-01-baton-spine-information-integrity.md"
_QUALIFIER = "DoE-claude"
_QUALIFIER_WINDOW = 20


def _tracked_source_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT,
        capture_output=True,
        timeout=30,
        **leaf_spawn_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [
        p for p in raw.split("\0")
        if p and (p.endswith(".py") or p.endswith(".md"))
    ]


def _strip_whitespace_with_index_map(text: str) -> tuple[str, list[int]]:
    """Drop every whitespace char, keeping a stripped-index -> original-index
    map so a match found in the stripped text can be traced back to a line
    number. Prose line-wraps (a hard wrap mid-path, or indentation before a
    continuation line) insert only whitespace between characters that are
    contiguous in the citation itself, so stripping whitespace before
    matching reunites a citation split across lines without needing a
    wrap-tolerant regex.
    """
    kept = []
    index_map = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        kept.append(ch)
        index_map.append(i)
    return "".join(kept), index_map


def test_every_citation_of_the_baton_spine_plan_names_its_home_repo():
    unqualified = []
    for rel in _tracked_source_files():
        if rel == _TEST_REL_PATH:
            continue
        full = _REPO_ROOT / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stripped, index_map = _strip_whitespace_with_index_map(text)
        search_from = 0
        while True:
            idx = stripped.find(_PLAN_PATH, search_from)
            if idx == -1:
                break
            search_from = idx + 1
            window = stripped[max(0, idx - _QUALIFIER_WINDOW):idx]
            if _QUALIFIER in window:
                continue
            orig_idx = index_map[idx]
            line_no = text.count("\n", 0, orig_idx) + 1
            snippet = text.splitlines()[line_no - 1].strip()
            unqualified.append((rel, line_no, snippet))
    assert not unqualified, (
        "citation(s) of the DoE-claude-only plan "
        f"{_PLAN_PATH!r} missing the '{_QUALIFIER}' repo qualifier -- "
        "reads as a dangling local link to a path never committed here:\n"
        + "\n".join(f"  {rel}:{line_no}: {line}" for rel, line_no, line in unqualified)
    )
