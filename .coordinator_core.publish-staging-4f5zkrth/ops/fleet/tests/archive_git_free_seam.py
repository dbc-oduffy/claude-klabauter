"""
coordinator_core.ops.fleet.tests.archive_git_free_seam

C1 of docs/plans/2026-08-13-archive-family-coverage-restoration.md (AC1,
AC3, AC11). A plain, explicitly-imported helper module — NEVER a
conftest.py, NEVER autouse. The 2026-08-07 incident (405 tests deleted,
commit 1d4e686a9) happened because a `fleet_repo` conftest fixture ran a
real `git init -b main` per test, ambiently, on a machine running 50-70
concurrent LLM sessions. Nothing in this module may be discovered or
applied without a test explicitly writing
`from coordinator_core.ops.fleet.tests.archive_git_free_seam import ...`.

Read this module's docstring before porting or writing ANY archive-family
test — it states the one question every such test must answer, and gives
the two-line patch that answers it "no" for the ~380 tests where it is
possible to.

AC3 — the written discriminator
--------------------------------
Derived from the two worked examples already in the tree, not asserted:

- test_archive_dest_conflict_wedge_detector.py exercises `prune_bugs`'s
  real three-way disposition through its production `_handler`, with only
  `archive_and_commit`, `check_repo_root`, and `main_worktree_root`
  monkeypatched. It is git-free because the assertion under test —
  differing-bytes dest conflict vs byte-identical convergence — is decided
  entirely by `_is_identical_duplicate`, a pure byte comparison over two
  `Path`s, ABOVE the mover seam. The mover is never even reached for the
  conflict case.
- test_archive_dest_conflict_heir_stamp_restore.py needed real git because
  `archive_handoffs`' heir-eligibility gate (H4) resolves a `shipped_in`
  commit SHA via `git cat-file -e` — an assertion about whether a specific
  object exists in a specific repository's object database. No mock has an
  object database to resolve against; the fact under test IS git's own
  state.

The rule an author applies per test, stated as a question:

    Does this assertion depend on git's own state — the index, commit
    history, or blob content of a real repository — or does it depend only
    on the filesystem (bytes at a path, a path's existence) and the op's
    return envelope (what `_handler`/`_handle_act_*` returned)?

The second class — filesystem + return envelope — never needs a repo. It
is reachable through this module's `patched_disposition_seam`, which
patches exactly the three names `test_archive_dest_conflict_wedge_
detector.py` patched by hand: `archive_and_commit` (the mover — replaced
with a fake that records what it was asked to move and returns a
production-shaped envelope, never spawning git), `check_repo_root`, and
`main_worktree_root` (the D3 consistency check and worktree-root
derivation, both of which otherwise require a real `.git` to resolve
against). Everything else in the op module — the disposition logic, the
dest-collision predicate, the terminality checks, the frontmatter reads —
runs as shipped production code against real files under `tmp_path`.

This module does NOT contradict test_archive_and_commit_disk_head_drift.py's
governed real-git pattern (own scoped module, ONE throwaway repo, ONE
test, `popup-intentional-last-resort` marker, spec backlink): that module
tests `archive_and_commit` ITSELF — the mover's own index/HEAD-drift
detection is the fact under test, so it is squarely in the first class
above and correctly stays real-git, uncovered by this helper. A test that
patches `archive_and_commit` can never observe what `archive_and_commit`
does internally; it is only ever the right choice for a test whose
assertion is about the CALLER's disposition decision, not the mover's own
git mechanics.

AC12 — the envelope contract this helper's default stub honours
------------------------------------------------------------------
`archive_and_commit`'s real return type (coordinator_core/ops/fleet/
_common.py) is `Tuple[List[dict], List[dict]]` — `(acted, failed)`, NOT a
three-key dict. `acted` items are `{"id": str, "archived": True}`
(optionally carrying an additive `index_resync_failed` key on a resync
failure, stripped again before the op's own wire envelope is built).
`failed` items are `{"id": str, "reason": str}`. There is no `skipped[]`
from `archive_and_commit` itself — dest-collision/idempotent-replay
disposition is decided by the CALLER, above the mover seam, per staff-eng
finding 6 in the plan's § Problem; a caller that decides "skip" never
builds a `Move` and never calls the mover at all.
`make_recording_mover`'s default response mirrors this exactly:
`([{"id": m.candidate_id, "archived": True} for m in moves], [])`. See
test_archive_and_commit_envelope_contract.py (this package) for the
real-git test pinning that this shape has not drifted from production —
cite state/lessons/2026-08-05-coverage-that-builds-its-own-input-never-
tests-the-door-production-uses.yaml and state/lessons/2026-08-07-a-
verifier-that-builds-its-own-inputs-ca-c93937894991.yaml: a stub whose
shape is asserted once, against the real door, is what keeps the ~380
stub-driven tests honest rather than merely self-consistent.

AC11 — three numeric predictions, made before C2 runs
---------------------------------------------------------
Derived from a scan of the candidate modules named in C3/C4's bodies (git
call-site density, the plan's own already-archived/vocabulary counts, and
the two worked C2 pilot modules' stated site counts), not from running C2:

1. **Predicted real-git fraction: ~6% (25/405).** AC5's hard cap (25 of
   405 archive-family tests) is not itself a prediction, but it is a
   deliberately-set ceiling that only makes sense if the true rate is
   close to it — C1/C2's two worked examples (test_prune_bugs: 1 git call
   site across 19 tests; test_archive_actioned_memos: 4 sites across 26
   tests) both sit well under a 6% real-git rate for their OWN module, and
   the C4 candidate list (archive_and_commit call-site coverage, index-
   resync residue, rm-and-commit, the three ops/tests handoff-transition
   modules) is a short, named, enumerable set rather than a broad
   percentage of the whole family — consistent with a low single-digit
   overall fraction, not a double-digit one.
2. **Predicted rewrite-not-port fraction: ~35%.** The 112 known-stale
   sites (54 already-archived + 58 vocabulary) alone are ~28% of 405 if
   each maps to one distinct test (they will not all be 1:1, but they
   cluster in exactly the highest-count modules — test_archive_handoffs,
   test_archive_plans — so the effective test-level rate tracks close to
   the site-level rate rather than diluting it). Layered on top: the
   disk/HEAD-drift class (84 post-seed `.write_text(` sites across 21 of
   23 modules) is invisible to the vocabulary grep and adds a further
   population of tests whose seeded-state assumption ("clean at seed
   time") no longer holds — not every write_text site precedes an op call,
   but enough do to push the combined rewrite rate several points above
   the vocabulary-only estimate. 35% is the sharp number this predicts;
   C2's combined measured rate across its two modules is the check.
3. **Predicted minutes/test: ~3.0.** 14,929 LOC of culled test source
   across 405 tests is ~37 LOC/test to read, classify (git-free vs
   real-git), and decide port/rewrite/drop — reading and classifying a
   ~35-40 line test function against current source is a 1-2 minute task
   for the git-free majority, with the real-git minority and the drift-
   rewrite tests running longer (rewriting an assertion, or standing up
   the governed one-repo-one-test pattern, is closer to 8-10 minutes each).
   Blended across the predicted 6%/35% splits above, 3.0 minutes/test is
   the sharp per-test average this predicts for C2 to check.

C2's STOP condition (AC11) is arithmetic against these three numbers, not
judgment-in-the-moment: any measured rate from C2's two pilot modules
exceeding its prediction here by more than 50%, OR the combined-rate
extrapolation to the full family exceeding the plan's L ceiling, fires
STOP.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest.mock import patch


def run(coro):
    """Thin `asyncio.run` wrapper so call sites read `run(_handler(...))`."""
    return asyncio.run(coro)


def make_recording_mover(
    *, response: Optional[Tuple[List[dict], List[dict]]] = None,
) -> Callable:
    """Build a fake `archive_and_commit` replacement.

    Never spawns git. Records every `Move` it was asked to act on (on
    `.captured`, as a plain list) and the commit `subject` it was given
    (on `.subject`), for a test to assert against.

    `response`, when given, is returned verbatim on every call — use this
    to exercise a caller's `failed[]`-handling path without needing a real
    git failure. Absent, the default mirrors `archive_and_commit`'s real
    success shape (see this module's docstring, AC12 section):
    `([{"id": m.candidate_id, "archived": True} for m in moves], [])`.
    """

    async def _mover(worktree_root, moves, subject):
        _mover.captured = list(moves)
        _mover.subject = subject
        if response is not None:
            return response
        acted = [{"id": m.candidate_id, "archived": True} for m in moves]
        return acted, []

    _mover.captured = None
    _mover.subject = None
    return _mover


@contextmanager
def patched_disposition_seam(op_module, *, worktree: Path, mover=None):
    """Patch `check_repo_root`, `main_worktree_root`, and `archive_and_
    commit` on `op_module` so its real disposition code runs against
    `worktree` (a plain `tmp_path`-derived directory — never a real repo)
    with no git subprocess anywhere in the call.

    `op_module` must be one of the `coordinator_core.ops.fleet.*` (or
    `coordinator_core.ops.*`) handler modules that imports all three names
    from `_common` at module scope (see this module's docstring) — e.g.
    `coordinator_core.ops.fleet.prune_bugs`.

    `mover`, when given, replaces the default `make_recording_mover()` —
    pass a `make_recording_mover(response=...)` to exercise a specific
    acted/failed shape.

    Yields the mover callable in use (so a test can read `.captured` /
    `.subject` after the `with` block), mirroring
    test_archive_dest_conflict_wedge_detector.py's `_fake_archive_and_
    commit.captured` pattern, generalised so ~20 modules can use it
    without each re-deriving this exact three-name patch set.
    """
    if mover is None:
        mover = make_recording_mover()
    with patch.object(
        op_module, "check_repo_root", lambda param_root, common_dir_arg: None
    ), patch.object(
        op_module, "main_worktree_root", lambda common_dir: worktree
    ), patch.object(
        op_module, "archive_and_commit", mover
    ):
        yield mover
