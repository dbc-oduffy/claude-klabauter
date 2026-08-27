"""C6, docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md:
pins the index-walk count of a real, end-to-end `run_commit_pipeline()`
call, asserted at `_parse_index_bytes` in BOTH parsers -- never at
`read_index`/`parse_index_identity`, whose call counts (11+) do not track
the underlying disk-walk count once a caching seam (`index_read_cache_
scope()`, or this chunk's own `commit_context.build_commit_context()`)
starts serving some of those calls from an already-parsed snapshot instead
of re-reading `.git/index`.

Two independent parsers, two independent counters:
  `coordinator_core.git.git_state._parse_index_bytes`  -- the FULL parser
      (`read_index()`'s own back-end; every `(mode, sha, stage)` entry).
  `coordinator_core.git.git_index._parse_index_bytes`  -- the SCOPED parser
      (`parse_index_identity()`'s own back-end, and therefore `commit_
      context.build_commit_context()`'s own back-end too -- see that
      module's docstring).

AC4's "RED at today's baseline, GREEN after" is pinned against THIS test's
own fixture scenario (one modified, previously-tracked path, agree branch,
`push_mode="never"` -- no push, no archive sweep) -- reproduced empirically
against the pre-C6 tree (`git show HEAD~:...` before this chunk landed):
7 full-parser calls / 2 scoped-parser calls. C6 (part 1) collapsed two of
`explicit_stage()`'s three separate generation-A full walks
(`_worktree_deleted_paths()`'s own `read_index()`, and the ignore-index
pre-filter's own `read_index()`) into ONE shared `build_commit_context()`
call -- a scoped walk, not a full one -- and retired `_swept_rename_delete_
paths()`'s full `read_index()` for the (common) nothing-swept case by
threading it through the same context for a cheaper pre-check, landing at
4 full / 3 scoped.

C6 (this pass) closes one more full walk: `_commit_via_head_spine`'s
AC11(b) index `stat_identity` re-check called `read_index(root,
fresh=True)` -- a full `_parse_index_bytes` walk of the whole index --
and used ONLY `.stat_identity`, three integers `IndexSnapshot.stat_
identity` builds purely from `index_path.stat()` (see `git_state.py`'s own
docstring). `git_state.read_index_stat_identity()` obtains the same value
via a single `Path.stat()`, no `read_bytes()`/`_parse_index_bytes()` at
all. `_agree_branch_cas_refusal`'s own `fresh=True` re-observation is NOT
converted: it goes through `_index_blobs(..., fresh=True)`, which needs
the FULL per-path `{path: blob-sha}` map for its own comparison (not just
stat identity) -- there is no stat-only substitute for that read, so it
stays a full walk, deliberately preserved.

Net for THIS fixture (this pass): full-parser calls -1 (4 -> 3, the
`_commit_via_head_spine` re-check), scoped-parser calls unchanged (3 -> 3).

The residual 3 full walks are, by enclosing function: `divergence.py ::
diverging_paths` (R4, generation A -- outside this chunk's `writes:`
scope), `commit_gates.py :: _staged_deletions_and_renames_in_process` (R7,
generation B -- outside this chunk's `writes:` scope), and the one
remaining `fresh=True` CAS re-observation genuinely needing full entries
(`_agree_branch_cas_refusal`, via `_index_blobs`). AC1's plan-level "<=1
full walk, and it IS the CAS re-observation" is now reachable in shape (one
CAS-caused full walk, not two) but not yet in count, since R4/R7 sit
outside this chunk's `writes:` scope -- tracked as a plan-level follow-up,
not solved by threading harder here.

This does not assert against DR-368's own "6 full + 3 scoped" prose figure
-- that figure was measured on a DIFFERENT commit scenario (a bulk/
percolate-shaped batch) before C1's CAS-freshness fix landed, and C1's own
fix makes `_commit_via_head_spine`'s re-observation genuinely fresh (a real
walk it could previously dodge via the defect C1 closes) -- so the two
numbers are not directly comparable. This test's own before/after pair is
measured against its OWN fixture, both sides, and that is the comparison
AC4 binds.

Counts, never times (AC3, DR-368,
state/lessons/2026-08-27-below-the-timer-tick-count-dont-time-and-name-the-
arm.md).

Spec backlink: docs/plans/2026-08-27-the-commit-op-resolves-one-pass-
context.md, chunk C6; dispatch brief state/dispatch-briefs/2026-08-27-the-
commit-op-resolves-one-pass-context/C6.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_index, git_state
from coordinator_core.ops.ceremony import commit_pipeline

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _seeded_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.md").write_text("one\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


class _WalkCounter:
    """Wraps `_parse_index_bytes` in `module`, counting real calls -- the
    ONLY oracle for "was `.git/index` actually re-parsed", since a cache
    hit (`index_read_cache_scope()`) or a scoped-context reuse never
    reaches this function at all."""

    def __init__(self, module, monkeypatch) -> None:
        self.count = 0
        original = module._parse_index_bytes

        def _wrapped(*args, **kwargs):
            self.count += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "_parse_index_bytes", _wrapped)


def test_ordinary_commit_walk_count_at_parse_index_bytes(tmp_path, monkeypatch):
    """One modified, previously-tracked path, agree branch, no push, no
    archive sweep -- the ordinary commit this chunk's own reduction targets.

    Post-C6 (this pass): <=3 full-parser calls (`git_state._parse_index_
    bytes`) and <=3 scoped-parser calls (`git_index._parse_index_bytes`).
    Pre-C6 this scenario measured 7 full / 2 scoped; C6 part 1 reached 4
    full / 3 scoped; this pass retires `_commit_via_head_spine`'s AC11(b)
    stat-identity re-check's full walk, landing at 3 full / 3 scoped -- see
    this module's own docstring for the `read_index_stat_identity()`
    reduction, and why the two parsers are counted, never `read_index`/
    `parse_index_identity` themselves.
    """
    repo = _seeded_repo(tmp_path)
    (repo / "a.md").write_text("two\n", encoding="utf-8")

    full_walks = _WalkCounter(git_state, monkeypatch)
    scoped_walks = _WalkCounter(git_index, monkeypatch)

    result = commit_pipeline.run_commit_pipeline(
        repo,
        session_id="s1",
        subject="chore: walk count probe",
        stage_paths=["a.md"],
        caller_paths={"a.md"},
        push_mode="never",
    )

    assert result.committed_sha is not None, result.diagnostics

    assert full_walks.count <= 3, (
        f"full-index (`git_state._parse_index_bytes`) walk count regressed: "
        f"{full_walks.count} > 3 -- see this module's docstring for the "
        f"pre-C6 baseline (7) this bounds against"
    )
    assert scoped_walks.count <= 3, (
        f"scoped-index (`git_index._parse_index_bytes`) walk count "
        f"regressed: {scoped_walks.count} > 3 -- see this module's "
        f"docstring for the pre-C6 baseline (2) this bounds against"
    )
