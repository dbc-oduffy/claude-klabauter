"""
test_no_dangling_machinery_citations — C5 of
pln-state-keeps-the-work-not-the-machinery-*: catch a NEW dangling
machinery citation without re-litigating the ones already dangling before
this plan touched anything.

Purpose: durable records outside `state/` cite `state/subagent-share/<uuid>`
session directories and `state/review-trail/...` paths. The corpus already
contained 435 dangling UUID citations and ~3,470 dangling review-trail path
citations BEFORE this plan started (session directories reaped long ago,
`state/review-trail/` itself relocated to `.coordinator-local/review-trail/`
by C6) -- fixing those by hand is explicitly out of scope (the plan's
Anti-scope bans hand-fixing citations), and asserting a clean tree would
fail on day one for reasons this plan did not cause.

So this is a RATCHET, not a clean-tree assertion (EM scoping decision,
2026-09-02, written into the C5 plan-task row after the first C5 dispatch
blocked on the literal reading). The question this test asks is: "did a
citation that resolved before this plan stop resolving because of it?"

  - The 344 UUIDs recorded live in C4's committed audit
    (`state/audits/2026-09-02-cited-subagent-share-sidecars.md`) MUST still
    resolve on disk under `state/subagent-share/`. One of those going
    dangling is exactly the regression this guard exists to catch.
  - The pre-existing dangling set (435 UUIDs, ~3,470 review-trail paths) is
    recorded ONCE in `state/audits/2026-09-02-dangling-citation-baseline.json`
    and carried as a baseline this test ignores -- it is not this test's
    job to shrink it.
  - Any dangling citation found in the corpus that is in NEITHER the live
    set NOR the baseline is new, and fails the test loudly.

Scan corpus: the same one C4 uses -- `git ls-files --cached --others
--exclude-standard`, excluding anything rooted at `state/`. This is what
correctly keeps `.claude/repomap-cache` and `.structural-index/` generated
indexes and gitignored churn out of the count where they're not
git-visible; an earlier sweep that used a raw filesystem walk over-counted
by including a gitignored generated index that enumerates every session
directory (an inventory, not a citation).

Negative-spec (RAG-bait):
    - This test does NOT fix, rewrite, or hand-repoint any citation --
      shrinking the baseline is an explicit non-goal of this chunk (the
      plan's Anti-scope bans hand-fixing citations). A future chunk that
      retires citations may shrink the baseline file; this test only
      guards against the baseline growing without a decision to grow it.
    - This test does NOT assert the tree is citation-clean. A dangling
      citation present in the baseline is a known, pre-existing fact this
      test carries forward, not a fact it is trying to hide.
    - This test does NOT treat `state/` (including `state/subagent-share/`
      and `state/audits/` themselves) as scan corpus -- same C4 scoping
      decision: the concern is citations from durable records OUTSIDE
      `state/`, since `state/` itself is the machinery under change.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session import machinery_paths
from coordinator_core.ops.extract_cited_sidecars import (
    _UUID_CITATION_RE,
    _list_candidates,
)

_REVIEW_TRAIL_RE = re.compile(r"state/review-trail/[A-Za-z0-9_.\-/]+[A-Za-z0-9_]")

_LIVE_AUDIT_PATH = "state/audits/2026-09-02-cited-subagent-share-sidecars.md"
_BASELINE_PATH = "state/audits/2026-09-02-dangling-citation-baseline.json"

_LIVE_UUID_HEADER_RE = re.compile(r"^## ([0-9a-fA-F-]{8,36})$", re.MULTILINE)


def _repo_root() -> str:
    return show_toplevel(cwd=os.getcwd()) or os.getcwd()


def _load_live_uuids(root: str) -> Set[str]:
    """UUIDs C4 recorded as cited-and-on-disk at the time it ran -- the
    known-live set this test asserts must still resolve."""
    path = os.path.join(root, _LIVE_AUDIT_PATH)
    text = Path(path).read_text(encoding="utf-8")
    return set(_LIVE_UUID_HEADER_RE.findall(text))


def _load_baseline(root: str) -> Tuple[Set[str], Set[str]]:
    """The pre-existing dangling sets this test carries and ignores."""
    path = os.path.join(root, _BASELINE_PATH)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return set(data["dangling_uuids"]), set(data["dangling_review_trail_paths"])


def _scan_corpus(root: str) -> Dict[str, str]:
    """rel_path -> file content, for every candidate file in C4's scan
    corpus (git-visible, outside `state/`). One read per file, same shape
    as C4's own walk -- no per-citation subprocess."""
    contents: Dict[str, str] = {}
    for rel_path in _list_candidates(root):
        full = os.path.join(root, rel_path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                contents[rel_path] = fh.read()
        except OSError:
            continue
    return contents


def _find_uuid_citations(contents: Dict[str, str]) -> Dict[str, List[str]]:
    hits: Dict[str, Set[str]] = {}
    for rel_path, text in contents.items():
        for match in _UUID_CITATION_RE.finditer(text):
            hits.setdefault(match.group(1), set()).add(rel_path)
    return {uid: sorted(files) for uid, files in hits.items()}


def _find_review_trail_citations(contents: Dict[str, str]) -> Dict[str, List[str]]:
    hits: Dict[str, Set[str]] = {}
    for rel_path, text in contents.items():
        for match in _REVIEW_TRAIL_RE.finditer(text):
            hits.setdefault(match.group(0), set()).add(rel_path)
    return {token: sorted(files) for token, files in hits.items()}


def _on_disk_session_ids(root: str) -> Set[str]:
    bucket = machinery_paths.share_root(root)
    if not os.path.isdir(bucket):
        return set()
    return {
        name
        for name in os.listdir(bucket)
        if os.path.isdir(os.path.join(bucket, name))
    }


def test_no_new_dangling_uuid_citation():
    root = _repo_root()
    live_uuids = _load_live_uuids(root)
    baseline_uuids, _baseline_review_trail = _load_baseline(root)
    on_disk = _on_disk_session_ids(root)

    contents = _scan_corpus(root)
    cited = _find_uuid_citations(contents)

    missing_live = sorted(live_uuids - on_disk)
    assert not missing_live, (
        "REGRESSION: the following UUIDs were recorded live in "
        f"{_LIVE_AUDIT_PATH} but no longer resolve under "
        "the machinery share root -- a citation that used to work stopped "
        f"working: {missing_live[:20]}"
        + (" ... (truncated)" if len(missing_live) > 20 else "")
    )

    new_dangling = sorted(
        uid
        for uid in cited
        if uid not in on_disk and uid not in baseline_uuids
    )
    assert not new_dangling, (
        "NEW dangling UUID citation(s) found -- not on disk, and not in "
        f"the recorded baseline ({_BASELINE_PATH}). Citing files: "
        + ", ".join(f"{uid} <- {cited[uid]}" for uid in new_dangling[:10])
    )


def test_no_new_dangling_review_trail_citation():
    root = _repo_root()
    _baseline_uuids, baseline_review_trail = _load_baseline(root)

    contents = _scan_corpus(root)
    cited = _find_review_trail_citations(contents)

    new_dangling = sorted(
        token
        for token in cited
        if not os.path.exists(os.path.join(root, *token.split("/")))
        and token not in baseline_review_trail
    )
    assert not new_dangling, (
        "NEW dangling state/review-trail/ citation(s) found -- target does "
        f"not exist on disk, and not in the recorded baseline "
        f"({_BASELINE_PATH}). Citing files: "
        + ", ".join(f"{tok} <- {cited[tok]}" for tok in new_dangling[:10])
    )
