"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_parity

Reconstructs the deleted `tests/wsc-asic/test-wsc-commit-parity.sh` golden-
oracle parity assertions (a)-(e) as native pytest against the NEW pure-Python
pipeline, plus demonstrates the spawn-collapse KPI and covers AC17/AC18 at
the full `ceremony.wsc_tail` op level. This is the C10 chunk of the
`wsc_tail` rebuild (docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md) --
the AC4/AC14 "port-or-retire the parity tests -> reconstruct" resolution.

Provenance: the deleted bash oracle recovered from
`example-doctrine-repo:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh` (the
kill commit's parent). Its fixture data (SUBJECT/PROSE_BODY/DELETED_PATHS/
KEPT_ENTRIES/COMMIT_PATHS below) is reproduced VERBATIM so the golden message
this file asserts against is byte-identical to the one the bash oracle
pinned -- this file is not a fresh design, it is the same contract ported.

Coverage:
  (a)  golden byte-identical commit message (both at the pure `compose_message`
       unit level, and end-to-end through the full `run_commit_pipeline`).
  (b)  staged-set == explicit pathspec (commit tree contains exactly
       COMMIT_PATHS + DELETED_PATHS, nothing else).
  (c)  `deletion_block_gate` passes on a well-formed message (Kept block
       only, no staged deletions).
  (c2) `deletion_block_gate` fails on a malformed message (Deleted-claimed
       path never staged for deletion).
  (d)  a concurrent sibling's own already-staged file, outside the explicit
       pathspec, is neither committed nor lost.
  (e)  a concurrent sibling's own staged DELETION, outside the explicit
       pathspec, does not false-positive the deletion-block gate's F3
       inverse check (the exact 2026-07-07 example-cockpit-repo incident shape).
  KPI: a representative single `run_commit_pipeline` pass spawns ONLY `git`
       subprocesses (AC2 mechanical assertion: never bash/node/.sh/
       coordinator-session.sh) and the spawn count is small and bounded --
       see `test_kpi_...` docstring for the measured-vs-plan-estimate note.
  KPI (2026-07-22, docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md
       DEC-2): a representative deferred-push `ceremony.wsc_tail` pass
       completes its blocking path in <2.0s wall clock (AC1) and reaches no
       `git push`/`git fetch` before the result returns (AC2) --
       `test_kpi_wsc_tail_blocking_path_under_2s`.
  AC17: the `shipped_in` stamp lands in its own pushed follow-up commit at
       full `ceremony.wsc_tail` op exit -- never an unswept dirty edit.
  AC18: crash-after-commit-before-stamp is simulated (an exception raised
       from inside `post_commit_stamp_and_ship`, after the sentinel already
       carries the real `committed_sha`); re-invocation recovers via the
       sentinel and completes the stamp/follow-up-commit/receipt-emit
       without double-committing the main ceremony commit.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C10
  (AC4, AC14, AC17, AC18).

Also covers (2026-07-22 C9 wiring-gap fix -- § C9): `_run_precommit_tail` sequences
`tail_ops.render_handoff_tracker` + `tail_ops.refresh_roadmap_callout` (STEP_2_75)
between the archive-tier ops and `coverage.gate`, mirroring the OLD `wsc_commit.py`'s
Op 4 position (STEP_2_7 stamp+archive -> STEP_2_75 render pair -> STEP_2_9C
coverage.gate) -- see `test_precommit_tail_*` below.

Also covers (2026-07-22 origin-stub-close fold -- step 5d, see wsc_tail.py module
docstring): folding the standalone `handoff.close_origin_stub` op into the
post-commit stamp phase, per cross-repo/inbox/2026-07-22-claude-central-em-
wsc-tail-cutover-contract.md ("Out of scope" § 2.7b, "please confirm whether
wsc_tail's body already folds this"):
  (f)  end-to-end origin-stub close on ship, joined via `governing_plan_slug`
       (`test_origin_stub_close_end_to_end_via_governing_plan`).
  (g)  clean no-op -- no governing plan, no consumed handoff -- no failure, no
       spurious warning (`test_origin_stub_close_noop_when_nothing_to_close`).
  (h)  a stub-close failure soft-fails (`exit_code=2`, never `failed_critical`,
       never `exit_code=1`) without unwinding the already-landed main commit
       (`test_origin_stub_close_failure_does_not_fail_the_tail`).
  (i)  AC18 sentinel-resume still closes the stub -- post-commit work, runs on
       both the fresh and resumed pass (`test_origin_stub_close_runs_on_ac18_resume`).
  (j)  the standalone `handoff.close_origin_stub` op remains independently
       registered and reachable -- fold-IN, not a move (regression guard,
       `test_close_origin_stub_standalone_op_still_registered`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Optional

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from coordinator_core.ops import coordinator_complete_entry as complete_entry_mod
from coordinator_core.ops.ceremony import commit_pipeline as commit_pipeline_mod
from coordinator_core.ops.ceremony.commit_gates import deletion_block_gate
from coordinator_core.ops.ceremony.commit_message import compose_message, format_kept_entry
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline
from ._ceremony_lock_guard import assert_no_ceremony_lock_reintroduction
from .fixtures.pipeline_result import make_pipeline_result

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_EM_DASH = " — "


# ---------------------------------------------------------------------------
# Plain-repo helpers (mirrors the deleted bash oracle's own temp-repo
# seeding, and the sibling C2/C3/C4 test files' free-function convention).
# ---------------------------------------------------------------------------


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _committed_files_at_head(repo: Path) -> list[str]:
    result = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _porcelain(repo: Path) -> list[str]:
    return [line for line in _git(["status", "--porcelain"], repo).stdout.splitlines() if line]


def _head_message(repo: Path) -> str:
    return _git(["log", "-1", "--format=%B"], repo).stdout


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Assertions (a) + (b) -- golden fixture, reproduced VERBATIM from the
# deleted bash oracle (example-doctrine-repo:85006468^:coordinator/tests/wsc-asic/
# test-wsc-commit-parity.sh).
# ---------------------------------------------------------------------------

_SUBJECT = "workstream-complete: my-feature"
_PROSE_BODY = "Closed out the my-feature workstream: shipped the refactor and updated docs."
_DELETED_PATHS = ["tasks/my-feature/scratch-notes.md", "tasks/my-feature/draft-snippet.sh"]
_KEPT_PATH = "tasks/my-feature/todo.md"
_KEPT_REASON = "still load-bearing for active sibling workstream"
_KEPT_ENTRIES = [format_kept_entry(_KEPT_PATH, _KEPT_REASON)]
_COMMIT_PATHS = [
    "state/lessons/2026-06-30-my-feature.yaml",
    "docs/plans/2026-06-30-my-feature.md",
]

_GOLDEN_MESSAGE = (
    "workstream-complete: my-feature\n"
    "\n"
    "Closed out the my-feature workstream: shipped the refactor and updated docs.\n"
    "\n"
    "Deleted (Step 2.67):\n"
    "tasks/my-feature/scratch-notes.md\n"
    "tasks/my-feature/draft-snippet.sh\n"
    "\n"
    "Kept (Step 2.67):\n"
    f"tasks/my-feature/todo.md{_EM_DASH}still load-bearing for active sibling workstream\n"
    "--- end Step 2.67 blocks ---\n"
)


def test_assertion_a_golden_message_byte_identical_pure_unit():
    """(a), pure-function level -- no git, no pipeline, just `compose_message`."""
    msg = compose_message(
        subject=_SUBJECT,
        prose=_PROSE_BODY,
        deleted_paths=_DELETED_PATHS,
        kept_entries=_KEPT_ENTRIES,
    )
    assert msg == _GOLDEN_MESSAGE


def test_assertions_a_and_b_end_to_end_through_pipeline(tmp_path):
    """(a) golden message + (b) staged-set == explicit pathspec, driven
    through the full `run_commit_pipeline` on the bash oracle's own fixture
    data -- the same scenario the deleted CLI-level test exercised.

    C7c note: this test's own red state at dispatch time was NOT the
    branch-decline defect C7c's sibling tests hit (this fixture never
    configures a remote, so no push -- and no decline -- is ever reached
    here); it was `commit()`'s unconditional `Commit-Token: <uuid4().hex>`
    trailer (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md,
    W1, already landed on this branch), appended to every commit message
    AFTER `compose_message()` builds it -- so the golden fixture, which
    predates W1, no longer matches byte-for-byte. Fixed by comparing the
    golden prefix and asserting the appended trailer's shape separately,
    rather than a golden-message repair (a)/(b) this test never needed."""
    repo = _init_repo(tmp_path)

    # Seed + commit COMMIT_PATHS (tracked at HEAD, to be modified+re-staged).
    for p in _COMMIT_PATHS:
        _seed_file(repo, p, "placeholder")
    _git(["add", "--", *_COMMIT_PATHS], repo)
    _git(["commit", "-q", "-m", "initial: seed commit_paths"], repo)

    # Seed + commit DELETED_PATHS (tracked at HEAD, to be staged-deleted).
    for p in _DELETED_PATHS:
        _seed_file(repo, p, "scratch")
    _git(["add", "--", *_DELETED_PATHS], repo)
    _git(["commit", "-q", "-m", "initial: seed deleted_paths"], repo)

    # Seed + commit the KEPT path (tracked at HEAD, satisfies Assertion-2).
    _seed_file(repo, _KEPT_PATH, "load-bearing")
    _git(["add", "--", _KEPT_PATH], repo)
    _git(["commit", "-q", "-m", "initial: seed kept_path"], repo)

    # Stage: delete DELETED_PATHS, modify COMMIT_PATHS (caller pre-stages
    # its own claimed deletions before invoking the ceremony -- the
    # deletion-block gate validates against staged reality, it does not
    # perform the deletion itself).
    for p in _DELETED_PATHS:
        _git(["rm", "-q", p], repo)
    for p in _COMMIT_PATHS:
        _seed_file(repo, p, "updated content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject=_SUBJECT,
        prose=_PROSE_BODY,
        deleted_paths=_DELETED_PATHS,
        kept_entries=_KEPT_ENTRIES,
        stage_paths=_COMMIT_PATHS,
        caller_paths=set(_COMMIT_PATHS),
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    # (a) golden byte-identical message, at the real committed HEAD. `git
    # log --format=%B` (like the bash oracle's own `$(...)` capture) strips
    # trailing newlines -- rstrip both sides to compare content, not git's
    # own trailing-newline normalization.
    #
    # W1 (already landed on this branch, see this test's own docstring)
    # appends a per-commit `Commit-Token: <uuid4().hex>` trailer AFTER
    # `compose_message()` runs, so the real HEAD message is the golden
    # fixture plus that trailer, never byte-identical to the fixture alone.
    head_message = _head_message(repo).rstrip("\n")
    golden_prefix = _GOLDEN_MESSAGE.rstrip("\n")
    assert head_message.startswith(golden_prefix)
    trailer_suffix = head_message[len(golden_prefix):]
    assert re.fullmatch(r"\n\nCommit-Token: [0-9a-f]{32}", trailer_suffix), trailer_suffix

    # (b) staged-set == explicit pathspec: exactly COMMIT_PATHS + DELETED_PATHS,
    # nothing more, nothing less.
    committed = set(_committed_files_at_head(repo))
    assert committed == set(_COMMIT_PATHS) | set(_DELETED_PATHS)


# ---------------------------------------------------------------------------
# Assertions (c) / (c2) -- deletion_block_gate pass/fail on well-formed vs
# malformed messages (bash oracle's WELLFORMED_MSG / MALFORMED_MSG fixtures).
# ---------------------------------------------------------------------------


def test_assertion_c_gate_passes_well_formed_message(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _KEPT_PATH, "content")
    _git(["add", "--", _KEPT_PATH], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    wellformed_msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Some prose.\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"{_KEPT_PATH}{_EM_DASH}{_KEPT_REASON}\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(wellformed_msg, gate_paths=[], cwd=repo)
    assert outcome.passed is True, outcome.diagnostics
    assert outcome.skipped is False


def test_assertion_c2_gate_fails_malformed_unstaged_deletion_claim(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, _KEPT_PATH, "content")
    _git(["add", "--", _KEPT_PATH], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    malformed_msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "tasks/nonexistent-path.md\n"
        "\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(malformed_msg, gate_paths=[], cwd=repo)
    assert outcome.passed is False
    assert any("tasks/nonexistent-path.md" in d for d in outcome.diagnostics)


# ---------------------------------------------------------------------------
# Assertion (d) -- sibling-staged file NOT absorbed by scoped pathspec.
# ---------------------------------------------------------------------------


def test_assertion_d_sibling_staged_file_not_absorbed(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    sibling_file = "sibling-staged-file.txt"
    legit_file = "docs/new-doc.md"

    _seed_file(repo, sibling_file, "sibling content — should never be absorbed")
    _seed_file(repo, legit_file, "legitimate new doc")
    _git(["add", "--", sibling_file, legit_file], repo)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: sibling-isolation-check",
        stage_paths=[legit_file],
        caller_paths={legit_file},
    )

    assert result.commit_failed is False, result.diagnostics
    committed = _committed_files_at_head(repo)
    assert committed == [legit_file]
    assert sibling_file not in committed

    status_lines = _porcelain(repo)
    assert any(line.startswith("A") and line.endswith(sibling_file) for line in status_lines)


# ---------------------------------------------------------------------------
# Assertion (e) -- sibling-staged DELETION does not false-positive the F3
# inverse check, end-to-end through the caller (not just the gate in
# isolation -- proves the 2026-07-07 example-cockpit-repo incident fix survives
# the new pipeline's caller wiring).
# ---------------------------------------------------------------------------


def test_assertion_e_sibling_staged_deletion_no_false_positive(tmp_path):
    repo = _init_repo(tmp_path)
    sibling_del_file = "tasks/sibling-session/scratch.md"
    e_commit_file = "state/lessons/e-fixture.yaml"

    _seed_file(repo, sibling_del_file, "sibling scratch")
    _seed_file(repo, e_commit_file, "lesson content")
    _git(["add", "--", sibling_del_file, e_commit_file], repo)
    _git(["commit", "-q", "-m", "seed: e-fixture files"], repo)

    # Sibling session stages its own deletion, entirely outside this
    # pipeline's pathspec. Our session updates + stages its own file.
    _git(["rm", "-q", sibling_del_file], repo)
    _seed_file(repo, e_commit_file, "updated lesson")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: e-fixture",
        stage_paths=[e_commit_file],
        caller_paths={e_commit_file},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.deletion_gate is not None
    assert result.deletion_gate.passed is True, result.deletion_gate.diagnostics

    committed = _committed_files_at_head(repo)
    assert committed == [e_commit_file]

    # The sibling's own staged deletion remains staged -- not swept, not resurrected.
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("D") and line.endswith(sibling_del_file) for line in status_lines
    )


# ---------------------------------------------------------------------------
# KPI -- spawn-collapse demonstration (AC2 mechanical assertion + AC14).
# ---------------------------------------------------------------------------


def test_kpi_spawn_count_git_only_and_collapsed(tmp_path, monkeypatch):
    """A representative single `run_commit_pipeline` pass -- the direct
    native replacement for the deleted `wsc-commit.sh` (339 LOC bash) plus
    its two deleted gate scripts, the highest-priority port this rebuild
    exists for (plan Substrate reality) -- spawns ONLY `git` subprocesses.

    Measured-vs-plan-estimate note: the plan's AC14 prose estimates a
    "~2-3" post-rebuild spawn band; empirically instrumenting this
    representative single-file-stage/gate/commit pass (no remote configured,
    the no-op push path) measures 18 `subprocess.run` calls (post C10/C11,
    see "Bound history" below), every one of them `git` (see the call list
    asserted below). "~2-3" was a pre-implementation approximation in the
    plan's prose; the number that actually matters for AC2/AC14 -- and the
    one asserted here -- is (1) EVERY spawn is `git`, never
    `bash`/`node`/a `.sh` script/`coordinator-session.sh` sourcing, and (2)
    the total is small and BOUNDED.

    Bound history (`< 11` -> `< 15`, 2026-07-27, docs/plans/2026-07-27-
    computed-commit-mechanism-selection.md § dedup; `0ec3ca894`,
    2026-08-03, spent that headroom to 15 with a redundant two-call
    ignored-path pre-filter, since collapsed back to one call -- see
    `git_native.check_ignore()`'s own negative-spec; `< 15` -> `< 16`,
    2026-08-04, defect A/B deletion-staging fix; `< 16` -> `< 19`,
    2026-08-07, docs/plans/2026-08-07-excise-the-ceremony-lock.md § C10/C11):
    the ORIGINAL `< 11` was "down from ~11" per the OLD external-process
    chain this pipeline replaced, which mixed bash+node+git across three
    different interpreters for the same ceremony -- that comparison is
    still true and this pipeline still never leaves `git`. The bound moved
    at 2026-07-27 because this pass now BUYS something the old chain never
    had: `git_native.commit_scoped()` computes index/worktree divergence
    from OBSERVED state and picks the commit mechanism accordingly, rather
    than trusting an operator to pick `git commit -- <paths>` vs. a bare
    `git commit` by hand (the shape of two real incidents -- claude-klabauter
    506748a0, example-doctrine-repo 726925b2). That correctness costs one
    `diverging_paths()` call (2 `git diff` subprocesses) in the AGREE case
    this test exercises -- `commit_pipeline.explicit_stage()` needs it to
    decide what is safe to `git add` without destroying a
    deliberately-staged partial hunk. A THIRD call site,
    `wsc_tail._derive_trailers()`, computes its own independent answer
    earlier in the full ceremony -- deliberately NOT deduped with the two
    above, since a peer session can mutate the tree in the gap between that
    check and this pipeline's own; that site is not exercised by this test,
    which calls `run_commit_pipeline()` directly rather than the full
    `wsc_tail` handler. The 2026-08-04 fix adds ONE more unconditional
    call, `git ls-files --deleted -- <paths>` (`git_native.ls_files_deleted`)
    -- `explicit_stage()` needs it, every call, to distinguish "this path
    was deleted from the worktree" from "this path never existed" (see that
    function's own "Deletion staging" docstring section); without it, a
    named deletion could never reach the commit set at all (the live
    incident this fix closes).

    2026-08-07 (docs/plans/2026-08-07-excise-the-ceremony-lock.md § C10/C11,
    the `ceremony_lock` excision): the dedup this docstring used to describe
    -- `explicit_stage()`'s divergence answer threaded into `commit_scoped()`
    via `known_checked`/`known_diverged`, "safe only because both calls run
    synchronously, back-to-back, inside the SAME `ceremony_lock` hold" -- no
    longer exists, because that lock no longer exists (C1 strips its sole
    acquisition site; `ceremony_lock.py` is an inert no-op shim). C10 removed
    the dedup rather than trying to re-bound its soundness without a lock:
    `commit_scoped()` now always derives divergence fresh for every path in
    `commit_paths`, costing 2 more pathspec-scoped `git diff` subprocesses
    per commit -- exactly the AGREE-case cost the 2027-07-27 dedup had
    removed, now paid again since the lock that justified removing it is
    gone (see `commit_scoped`'s own docstring, "OPTIONAL dedup seam"
    paragraph, for the corrected precondition). C11 closed a second,
    independent TOCTOU the same excision exposed: `commit()` used to capture
    `committed_sha` via a blind post-commit `git rev-parse HEAD`, safe only
    because it ran before the lock released; with no lock, that read could
    return a concurrent sibling's own commit. `commit()` now resolves the
    AGREE-branch `committed_sha` via ONE bounded, pathspec-scoped
    `git log --grep=<message> --fixed-strings --format=%H <pre-sha>..HEAD --
    <paths>` call matching this call's own commit message (failing loud on
    zero or multiple matches, rather than falling back to the racy read);
    the DIVERGED-branch sha needs no extra call at all, since
    `commit_scoped()`'s own `stdout` is already the CAS-verified new sha.
    Net for this test's AGREE-case pass: +2 (C10's fresh divergence check)
    +1 (C11's message-match lookup) = +3 over the prior 15-call baseline,
    landing at 18 measured. `wsc_tail._derive_trailers()` (the THIRD call
    site above) is untouched by this change -- it was never lock-dependent,
    just deliberately not deduped -- so its "BEFORE the lock is even
    acquired" framing is dropped above as no longer meaningful, not because
    its own behaviour changed. `< 19` is 18 measured plus the same one-call
    headroom convention every prior bound move in this history used, not a
    relaxation of the invariant below.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    calls: list[list[str]] = []
    orig_run = subprocess.run

    def _wrapper(args, *a: Any, **kw: Any):
        calls.append(list(args))
        return orig_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _wrapper)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    # AC2 mechanical assertion -- every single spawn is `git`, never
    # bash/node/.sh/coordinator-session.sh.
    non_git_calls = [c for c in calls if not c or c[0] != "git"]
    assert non_git_calls == [], f"non-git subprocess spawn(s) found: {non_git_calls}"
    for call in calls:
        joined = " ".join(call)
        assert "bash" not in joined
        assert "node" not in joined
        assert "coordinator-session.sh" not in joined
        assert not joined.endswith(".sh") or "COMMIT_EDITMSG" in joined

    # Bounded + collapsed: small headroom over the 18 measured post-C10/C11
    # (see the docstring's "Bound history" for what the divergence-check,
    # deletion-detection, and sha-verification residuals buy and why the OLD
    # `< 11`/`< 15`/`< 16` grew), and every one is `git` (see above).
    assert 1 <= len(calls) < 19, f"spawn count {len(calls)} out of expected collapsed band: {calls}"


def test_kpi_wsc_tail_blocking_path_under_2s(wsc_tail_repo, monkeypatch):
    """AC1/AC2 (docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md, DEC-2):
    a representative deferred-push `ceremony.wsc_tail` pass completes its
    BLOCKING path in under 2.0 seconds wall clock, and reaches no `git push`/
    `git fetch` call before the result returns.

    The retired 120s per-op dispatch-timeout override (ipc.py) treated this
    op's tail as inherently long-running; DEC-2 replaces that assumption with
    a measured, regression-tested performance property instead -- this is
    that regression test. Platform-neutral: no POSIX-only API is used, and the
    one real subprocess spawn point on the deferred-push path
    (`_spawn_deferred_push_skip_loud`) is monkeypatched out so the timed
    section measures the SAME work on both POSIX and Windows -- spawning the
    actual detached push child is AC3's concern, not this test's.
    """
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.delenv(wsc_tail_mod._ENV_SYNC_PUSH, raising=False)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    calls: list[list[str]] = []
    orig_run = subprocess.run

    def _wrapper(args, *a: Any, **kw: Any):
        calls.append(list(args))
        return orig_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _wrapper)

    start = time.perf_counter()
    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )
    elapsed = time.perf_counter() - start

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert result["push_status"] == "deferred"

    # AC1 -- blocking-path wall clock, both platforms.
    assert elapsed < 2.0, f"wsc_tail blocking path took {elapsed:.3f}s, budget is <2.0s"

    # AC2 -- zero synchronous network operations reachable from the handler
    # before the result returns: no `git push`/`git fetch` among the spawns.
    for call in calls:
        assert call and call[0] == "git", f"non-git subprocess spawn found: {call}"
        assert "push" not in call, f"blocking-path git push spawn found: {call}"
        assert "fetch" not in call, f"blocking-path git fetch spawn found: {call}"


# ---------------------------------------------------------------------------
# AC17 / AC18 -- full `ceremony.wsc_tail` op, end-to-end.
# ---------------------------------------------------------------------------


class WscTailRepo:
    """Real-git repo + remote + consumed-handoff seeding, for driving the
    full `ceremony.wsc_tail` op handler end-to-end (AC17/AC18). Mirrors
    `test_consumed_handoff_stamp.py`'s `StampRepo` fixture shape."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, text=True
        )

    @property
    def common_dir(self) -> Path:
        return (self.root / ".git").resolve()

    def seed_handoff(self, name: str, *, consumed_by: str, sid_is_consumer: bool = True) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = "\n".join(
            [
                f'title: "Test Handoff {name}"',
                "created: 2026-07-15",
                "branch: work/test/2026-07-15",
                "status: open",
                "category: infra",
                'summary: "Test handoff summary for schema post-cutoff compliance."',
                "predecessor: null",
                "consumed_at: 2026-07-15T10:00:00Z",
                f"claimed_by: {consumed_by}",
            ]
        )
        path.write_text(f"---\n{fm}\n---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", f"add handoff {name}")
        self._git("push", "origin", "main")
        return path

    def seed_origin_stub(
        self, name: str, *, roadmap_id: str, stub_id: str, deployment_state: str = "ready_to_fire"
    ) -> Path:
        """Seed a schema-compliant `kind: roadmap-baton` origin-stub baton.

        Mirrors `test_handoff_close_origin_stub.py::_seed_baton` (kind=spinoff-
        roadmap requires roadmap_id/stub_id/wave/blocks/blocked_by per the
        `_cf_spinoff_roadmap_requires_graph` cross-field rule) -- reproduced
        here rather than imported since that helper is keyed to the sibling
        `ops/tests/conftest.py` HandoffRepo fixture, not this file's real-git
        `WscTailRepo`.
        """
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = "\n".join(
            [
                f'title: "Origin stub {name}"',
                "created: 2026-01-01",
                "branch: work/test/2026-01-01",
                "status: open",
                'predecessor: "none"',
                "category: infra",
                'summary: "Test origin-stub summary for schema compliance."',
                "kind: roadmap-baton",
                f"roadmap_id: {roadmap_id}",
                f"stub_id: {stub_id}",
                "wave: 1",
                "blocks: []",
                "blocked_by: []",
                f"deployment_state: {deployment_state}",
            ]
        )
        path.write_text(f"---\n{fm}\n---\n\n# Origin Stub\n\nBody.\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", f"add origin stub {name}")
        self._git("push", "origin", "main")
        return path

    def write_plan(self, slug: str, *, roadmap_id: str, stub_id: str) -> Path:
        """Write + commit `docs/plans/<slug>.md` carrying roadmap_id/stub_id
        directly -- a read-only join source for `handoff.close_origin_stub`'s
        direct-frontmatter leg (plans are never schema-validated/mutated).
        Committed (not left dirty) -- an untracked plan file would otherwise
        false-positive `dirty_tree_gate`'s unattributable-path check, since
        `docs/plans/` is outside its known-concurrent-owner scope."""
        path = self.root / "docs" / "plans" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nroadmap_id: {roadmap_id}\nstub_id: {stub_id}\n---\n\n# Plan\n",
            encoding="utf-8",
        )
        self._git("add", "-A")
        self._git("commit", "-m", f"add governing plan {slug}")
        self._git("push", "origin", "main")
        return path

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def porcelain(self) -> list[str]:
        return [ln for ln in self._git("status", "--porcelain").stdout.splitlines() if ln]

    def remote_log(self, remote_branch: str = "main") -> str:
        return self._git("log", "--oneline", f"origin/{remote_branch}").stdout

    def sentinel_path(self, sid: str) -> Path:
        return self.common_dir / "coordinator-sessions" / sid / "wsc-tail-commit-landed"


@pytest.fixture
def wsc_tail_repo(tmp_path) -> WscTailRepo:
    root = tmp_path / "repo"
    root.mkdir()
    r = WscTailRepo(root)
    r._git("init", "-b", "main")
    r._git("config", "user.email", "wsc-tail-parity@claude-klabauter.test")
    r._git("config", "user.name", "WSC Tail Parity Test")
    r._git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    r._git("add", "-A")
    r._git("commit", "-m", "chore: initial skeleton")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    r._git("remote", "add", "origin", str(bare))
    push = r._git("push", "-u", "origin", "main")
    assert push.returncode == 0, push.stderr
    return r


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_ac17_shipped_in_stamp_lands_in_own_pushed_follow_up_commit(wsc_tail_repo):
    """AC17: the `shipped_in` stamp lands in its own pushed follow-up
    commit -- never as an unswept dirty working-tree edit at op exit."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert result["stamped"] == [handoff_relpath]
    assert result["follow_up_committed_sha"] is not None
    assert result["follow_up_committed_sha"] != result["committed_sha"]
    # Deferred is the default push_mode (DEC-1): the follow-up commit lands
    # locally at op exit, but its push is handed off to the detached child
    # spawned at step 5e -- never awaited synchronously here.
    assert result["push_status"] == "deferred"
    assert result["pushed"] is None
    assert result["follow_up_pushed"] is None
    assert result["integrity_breach"] is False

    # Never left as an unswept dirty working-tree edit -- the stamped
    # handoff file itself does not appear in porcelain status at op exit.
    status_lines = repo.porcelain()
    assert not any(handoff_relpath in ln for ln in status_lines), status_lines

    # The follow-up commit really landed locally, even though its push is
    # deferred to the detached child.
    assert repo.head_sha() == result["follow_up_committed_sha"]


def test_ac17_shipped_in_stamp_lands_in_own_pushed_follow_up_commit_sync_seam(
    wsc_tail_repo, monkeypatch
):
    """Sync seam (`COORDINATOR_WSC_SYNC_PUSH=1`) variant of AC17: with the
    deferred-push cutover opted out of, the follow-up commit still lands in
    its own commit AND is pushed synchronously in-op, byte-for-byte the
    pre-DEC-1 contract this test pinned before C3/C8.

    C7c (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
    both the main commit's sync push AND the follow-up (C5 stamp+ship)
    commit's own push are this test's subject -- it is pinned to the
    pre-DEC-1 "both really land" contract by name and by docstring. It used
    to run on `wsc_tail_repo`'s shared `main`; a transitional C6b-era edit
    (superseded here) had re-pinned only the MAIN push to `push_status=
    "declined"` while leaving `follow_up_pushed=True`, which was consistent
    at the time since C6e (the change that routes
    `consumed_handoff_stamp._commit_and_push_follow_up` through the same
    branch-gated `push_with_retry`) had not yet landed. With C6e now in,
    BOTH pushes decline on `main`, and no combination of "declined"/"landed"
    on `main` can satisfy this test's own stated contract -- repair (a):
    a per-test `work/*` checkout (not a `wsc_tail_repo` fixture change --
    that fixture is shared by 8 call sites across this file, most of them
    deliberately exercising the `main`-decline path) restores the branch
    both pushes need to actually land, and the main-push assertions below
    revert from the transitional "declined" pin back to this test's
    original "pushed" contract."""
    repo = wsc_tail_repo
    branch = "work/test/ac17-sync-seam"
    checkout = repo._git("checkout", "-b", branch)
    assert checkout.returncode == 0, checkout.stderr
    # Match local/remote branch names (same care as the sibling C7c fixes):
    # the follow-up leg's `push_with_retry` issues a bare `git push`, which
    # `push.default=simple` refuses when local and remote names differ, even
    # with `-u` tracking configured.
    push_branch = repo._git("push", "-u", "origin", branch)
    assert push_branch.returncode == 0, push_branch.stderr
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert result["stamped"] == [handoff_relpath]
    assert result["follow_up_committed_sha"] is not None
    assert result["follow_up_committed_sha"] != result["committed_sha"]
    # C7c: on the `work/*` branch checked out above, the real `work/*`-only
    # push-leg branch policy (commit_pipeline.py) allows the push -- this is
    # this test's original "byte-for-byte pre-DEC-1" contract (both the main
    # commit's sync push AND the follow-up's own push really land), not the
    # transitional "declined" pin a C6b-era edit gave this assertion (see
    # this test's own docstring for why that pin no longer fits once C6e
    # routed the follow-up leg through the same branch gate).
    assert result["push_status"] == "pushed"
    assert result["pushed"] is True
    # The follow-up (C5 stamp+ship) commit's own push is a SEPARATE call
    # into `scoped_git_commit`'s push leg -- it also lands here, on the same
    # allowed branch.
    assert result["follow_up_pushed"] is True

    # Never left as an unswept dirty working-tree edit -- the stamped
    # handoff file itself does not appear in porcelain status at op exit.
    status_lines = repo.porcelain()
    assert not any(handoff_relpath in ln for ln in status_lines), status_lines

    # The follow-up commit really landed AND really pushed.
    assert repo.head_sha() == result["follow_up_committed_sha"]
    assert result["follow_up_committed_sha"][:7] in repo.remote_log(remote_branch=branch)


def test_chain_terminal_stamp_all_skipped_surfaces_tail_item_not_silent_exit_0(
    wsc_tail_repo,
):
    """Regression for cross-repo/inbox/2026-07-23-claude-central-em-wsc-
    tail-stamp-ship-silent-skip.md (C2): a chain-terminal pass whose ONLY
    consumed-handoff candidate is skipped by R3 (genuinely, grossly
    future-dated here -- not the TZ false-positive C1 fixes) must NOT exit 0
    as if the stamp succeeded. It must surface a `tail_results[
    "consumed_handoff_stamp"]["failed"]` entry naming the skip reason and
    the `archive-stamp-cli ship-handoff` remediation, and report `exit_code
    == 2` (a surfaced tail item, never a commit failure -- the commit itself
    still lands, see `commit_failed`/`exit_code=1` assertions below)."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    # Grossly future-dated filename -- genuinely implausible under BOTH the
    # old and the widened (C1) bound, so this isolates C2's observability
    # fix from C1's timezone-ambiguity widening.
    handoff_relpath = "state/handoffs/2099-01-01_000000_pred.md"
    repo.seed_handoff("2099-01-01_000000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    # The commit landed -- this is a soft-fail tail item, not a commit
    # failure (module docstring: exit_code=2 never implies commit_failed).
    assert result["commit_failed"] is False
    assert result["committed_sha"] is not None
    assert result["exit_code"] == 2, result

    stamp_result = result["tail_results"]["consumed_handoff_stamp"]
    assert stamp_result["acted"] == []
    assert any(handoff_relpath in s for s in stamp_result["skipped"]), stamp_result
    failed_entries = stamp_result["failed"]
    assert any(
        handoff_relpath in e and "archive-stamp-cli ship-handoff" in e
        for e in failed_entries
    ), failed_entries

    # No follow-up commit -- nothing was stamped.
    assert result["follow_up_committed_sha"] is None
    assert result["stamped"] == []


def test_single_session_close_lands_but_names_no_flip_due_in_diagnostics(
    wsc_tail_repo,
):
    """Regression for cross-repo/inbox/2026-08-10-example-doctrine-repo-em-wsc-tail-
    silent-noop-and-gate-rewalk.md finding 1: a landed commit whose step-1
    resolve found no consumed handoff for this sid (`chain_terminal=False`,
    the ordinary single-session-close shape -- same fixture pattern as
    `test_kpi_wsc_tail_blocking_path_under_2s`) must still exit 0 (this is
    a genuinely successful close, not a defect), but `diagnostics` must
    name that no terminal flip was due -- never a bare empty list
    indistinguishable from "we never checked"."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    # A genuinely-successful single-session close: still exits 0.
    assert result["commit_failed"] is False
    assert result["committed_sha"] is not None
    assert result["exit_code"] == 0, result

    diagnostics = result["diagnostics"]
    assert any(
        "chain_terminal=False" in d and sid in d and "no terminal flip was due" in d
        for d in diagnostics
    ), diagnostics


def test_chain_terminal_commit_abort_stamps_never_evaluated_and_labels_nodes(
    wsc_tail_repo, monkeypatch
):
    """Regression for cross-repo/inbox/2026-08-04-example-retrieval-repo-em-wsc-tail-
    abort-loses-baton-terminal-flip.md: when commit_pipeline aborts on a
    chain-terminal pass, the stamp step never RAN at all (unlike
    test_chain_terminal_stamp_all_skipped_surfaces_tail_item_not_silent_exit_0
    above, where it ran and skipped every candidate) -- so every candidate
    baton in `initial_consumed` must still surface a labelled
    `consumed_handoff_stamp["failed"]` entry, and the three nodes that used
    to go empty/absent (`archive_sweeps:detached_fire`,
    `consumed_handoff_stamp`'s `skipped`, `handoff.close_origin_stub`) must
    carry labelled skips instead of vanishing/staying `[]`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    failed_outcome = make_pipeline_result(
        commit_failed=True,
        diagnostics=["forced failure for wsc-tail-abort-loses-baton regression test"],
    )
    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", lambda *_a, **_kw: failed_outcome)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["committed_sha"] is None
    assert result["commit_failed"] is True
    assert result["exit_code"] == 1, result

    stamp_result = result["tail_results"]["consumed_handoff_stamp"]
    assert stamp_result["acted"] == []
    assert any(
        "consumed_handoff_stamp:commit-failed" in s for s in stamp_result["skipped"]
    ), stamp_result
    failed_entries = stamp_result["failed"]
    assert any(
        handoff_relpath in e and "never-evaluated" in e and "in_flight" in e
        for e in failed_entries
    ), failed_entries

    sweeps_result = result["tail_results"][wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED]
    assert sweeps_result == {
        "acted": [],
        "skipped": [f"{wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED}:commit-failed"],
        "failed": [],
    }

    origin_stub_result = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub_result == {
        "acted": [],
        "skipped": [f"{wsc_tail_mod.OP_CLOSE_ORIGIN_STUB}:commit-failed"],
        "failed": [],
    }


def test_ac18_crash_after_commit_resumes_from_sentinel_without_double_commit(
    wsc_tail_repo, monkeypatch
):
    """AC18: a crash after the main commit lands but before the post-commit
    stamp/follow-up-commit/receipt-emit completes is simulated by raising
    from inside `post_commit_stamp_and_ship` (called only AFTER the
    commit-sentinel has already been updated with the real `committed_sha`
    -- see wsc_tail.py step 5b). Re-invocation must recover from the
    sentinel, resume at the post-commit stamp step, and complete without
    re-running (and so without duplicating) the main ceremony commit."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    orig_post_commit = wsc_tail_mod.consumed_handoff_stamp.post_commit_stamp_and_ship

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash after main commit landed, before stamp completed")

    monkeypatch.setattr(wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", _boom)

    params = {
        "sid": sid,
        "subject": "workstream-complete: feature",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
    }

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    sentinel = repo.sentinel_path(sid)
    assert sentinel.exists()
    real_committed_sha = sentinel.read_text(encoding="utf-8").strip()
    assert real_committed_sha  # real sha, not the empty pre-commit placeholder
    assert repo.head_sha() == real_committed_sha

    # Restore the real implementation for the resumed invocation.
    monkeypatch.setattr(
        wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", orig_post_commit
    )

    result = _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    assert result["resumed_from_sentinel"] is True
    # Keyed on the RECOVERED sha, never a fresh re-commit -- the main
    # ceremony commit is not run a second time.
    assert result["committed_sha"] == real_committed_sha
    assert result["stamped"] == [handoff_relpath]
    assert result["follow_up_committed_sha"] is not None
    # ANY resumed pass reports "unknown_resumed" regardless of push_mode
    # (module docstring push_status matrix) -- the pre-crash push outcome
    # was never persisted anywhere this pass can recover, and the default
    # deferred push_mode still leaves follow_up_pushed at None.
    assert result["push_status"] == "unknown_resumed"
    assert result["follow_up_pushed"] is None

    # Sentinel cleared only on successful receipt emit.
    assert not sentinel.exists()

    status_lines = repo.porcelain()
    assert not any(handoff_relpath in ln for ln in status_lines), status_lines


def test_ac18_crash_after_commit_resumes_from_sentinel_without_double_commit_sync_seam(
    wsc_tail_repo, monkeypatch
):
    """Sync seam (`COORDINATOR_WSC_SYNC_PUSH=1`) variant of AC18: even on a
    resumed pass, `push_status` reports "unknown_resumed" (module docstring
    matrix -- ANY resumed pass wins regardless of push_mode), but the sync
    seam still drives a real synchronous `follow_up_pushed=True` through
    `post_commit_stamp_and_ship`, byte-for-byte the pre-DEC-1 contract.

    C7c (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
    `follow_up_pushed=True` is this test's own subject -- the follow-up push
    really landing -- which C6e (routing `consumed_handoff_stamp._commit_
    and_push_follow_up` through the same branch-gated `push_with_retry`)
    made impossible on `wsc_tail_repo`'s shared `main`. Repair (a): a
    per-test `work/*` checkout (not a `wsc_tail_repo` fixture change --
    shared by 8 call sites in this file). `push_status` itself needs no
    matching change: "unknown_resumed" wins on ANY resumed pass regardless
    of branch (module docstring matrix), so it is untouched here, unlike
    the sibling AC17 sync-seam fix."""
    repo = wsc_tail_repo
    branch = "work/test/ac18-sync-seam"
    checkout = repo._git("checkout", "-b", branch)
    assert checkout.returncode == 0, checkout.stderr
    push_branch = repo._git("push", "-u", "origin", branch)
    assert push_branch.returncode == 0, push_branch.stderr
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    orig_post_commit = wsc_tail_mod.consumed_handoff_stamp.post_commit_stamp_and_ship

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash after main commit landed, before stamp completed")

    monkeypatch.setattr(wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", _boom)

    params = {
        "sid": sid,
        "subject": "workstream-complete: feature",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
    }

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    sentinel = repo.sentinel_path(sid)
    assert sentinel.exists()
    real_committed_sha = sentinel.read_text(encoding="utf-8").strip()
    assert real_committed_sha
    assert repo.head_sha() == real_committed_sha

    monkeypatch.setattr(
        wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", orig_post_commit
    )

    result = _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    assert result["resumed_from_sentinel"] is True
    assert result["committed_sha"] == real_committed_sha
    assert result["stamped"] == [handoff_relpath]
    assert result["follow_up_committed_sha"] is not None
    assert result["push_status"] == "unknown_resumed"
    assert result["follow_up_pushed"] is True

    assert not sentinel.exists()

    status_lines = repo.porcelain()
    assert not any(handoff_relpath in ln for ln in status_lines), status_lines


def test_wsc_tail_never_archives_the_live_session_dir(wsc_tail_repo, monkeypatch):
    """`ceremony.wsc_tail` must never call `tail_ops.cs_archive` itself.
    Archival belongs at SessionEnd (`coordinator/bin/wsc-close.py archive-
    session` -> `coordinator_core.session.scope.archive`), which has the
    liveness check this op's tail never had. `/workstream-complete` fires
    MID-session -- a session can close several workstreams before it ends --
    so archiving the session dir here would move `coordinator-sessions/
    <sid>/` to `.archive/` while the session is still live, resetting eight
    fire-once sentinels it backs (`.foreground-ok`, `.harness-bg-capable`,
    `unrouted-sizing-nudge.fired`, `harness-directive-nudge.fired`,
    `em-report-altitude-tally.json`, `em-report-altitude-nudged`,
    `multiwave-workflow-nudged`/`workflow-launched`,
    `exploration-tier-dispatch-offered`) out from under a still-running
    session. (`.autonomous` and `.dispatch-nudge-ok` are tempdir-homed and
    were never affected by this call -- not part of this invariant.)

    Three independent legs, per local convention
    (`test_no_blocking_archive_sweep_call_remains_on_wsc_tail_source` /
    `..._in_tail_ops_module`): a spy on the shared `tail_ops` module object
    (catches any late-bound `tail_ops.cs_archive(...)` call from anywhere in
    this op), a source-scan sibling (catches a reintroduction via `from
    ...tail_ops import cs_archive` or similar that would evade the spy's
    binding), and an on-disk assertion of the actual invariant this test is
    named for (the session dir survives, untouched)."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)
    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    session_dir = repo.common_dir / "coordinator-sessions" / sid
    archive_dir = repo.common_dir / "coordinator-sessions" / ".archive" / sid

    calls: list[tuple[Path, str]] = []

    def _spy_archive(common_dir: Path, session_id: str) -> Any:
        calls.append((common_dir, session_id))
        raise AssertionError("tail_ops.cs_archive must never be called from wsc_tail")

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "cs_archive", _spy_archive)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    # Assert the invariant first, named for the incident, so a reintroduced
    # call that soft-fails into exit_code=2 (swallowed by a broad `except`)
    # reports as THIS failure rather than being buried behind an
    # exit-code-mismatch assertion.
    assert calls == [], (
        f"e510140a regression: tail_ops.cs_archive was called mid-session: {calls}"
    )
    assert "tail_ops.cs_archive(" not in inspect.getsource(wsc_tail_mod), (
        "e510140a regression: a `tail_ops.cs_archive(` call site has returned to "
        "wsc_tail.py's source -- archival must stay SessionEnd-only"
    )
    if session_dir.exists():
        assert not archive_dir.exists(), (
            f"e510140a regression: {sid} was archived mid-session (found at {archive_dir})"
        )

    assert result["exit_code"] == 0, result


def test_ac9_crash_before_receipt_emit_resumes_without_duplicate_commit(
    wsc_tail_repo, monkeypatch
):
    """AC9 double-commit regression: a crash in the window between the main
    commit landing and receipt emit must leave the commit sentinel intact --
    proving the sentinel survives until receipt emit -- so a re-invoke
    resumes from it rather than re-running the full pipeline and landing a
    SECOND commit for the same session. (This window used to also carry the
    sentinel-sweeping `cs_archive` call, per the pre-fix step-6 ordering;
    that call site has since been removed from this op entirely -- archival
    now happens at SessionEnd, not here -- but the sentinel-survival
    guarantee this test pins remains the real duplicate-commit guard.)"""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.seed_handoff("2026-07-15_100000_pred.md", consumed_by=sid)
    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before receipt emit")

    monkeypatch.setattr(wsc_tail_mod, "emit_receipt", _boom)

    params = {
        "sid": sid,
        "subject": "workstream-complete: feature",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
    }

    with pytest.raises(RuntimeError, match="simulated crash before receipt emit"):
        _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    sentinel = repo.sentinel_path(sid)
    assert sentinel.exists(), "sentinel must survive a crash before receipt emit (AC9)"
    real_committed_sha = sentinel.read_text(encoding="utf-8").strip()
    assert real_committed_sha  # real sha, not the empty pre-commit placeholder
    # Crashing this late (post-stamp, pre-receipt) means HEAD has already
    # moved past the main commit onto the stamp follow-up commit -- the
    # recovered sha still identifies the main ceremony commit, not HEAD.
    assert real_committed_sha in repo._git("log", "--format=%H").stdout.splitlines()

    subjects_after_crash = repo._git("log", "--format=%s").stdout.splitlines()
    assert subjects_after_crash.count(params["subject"]) == 1

    monkeypatch.undo()  # restore the real emit_receipt for the resumed pass

    result = _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    assert result["resumed_from_sentinel"] is True
    # Keyed on the RECOVERED sha, never a fresh re-commit -- the main
    # ceremony commit is not run a second time.
    assert result["committed_sha"] == real_committed_sha
    assert not sentinel.exists()  # cleared once the resumed pass's receipt emit succeeds

    subjects_after_resume = repo._git("log", "--format=%s").stdout.splitlines()
    assert subjects_after_resume.count(params["subject"]) == 1, subjects_after_resume


# ---------------------------------------------------------------------------
# _run_precommit_tail -- STEP_2_75 render pair wiring (2026-07-22 C9 fix).
# ---------------------------------------------------------------------------


def test_precommit_tail_runs_render_pair_before_coverage_gate(tmp_path, monkeypatch):
    """render_handoff_tracker + refresh_roadmap_callout run, in that order,
    same as before (mirroring the OLD wsc_commit.py's Op 4 position -- see
    module docstring addendum). coverage.gate itself (C6, 2026-07-23
    wsc-tail-slim-down) is now FIRED before either render (an `asyncio.
    create_task` at the top of `_run_precommit_tail`) but only JOINED
    (awaited) after every other pre-commit step, including review_trail --
    so its result lands in `call_order` LAST even though its own git-log +
    gate-algorithm work overlapped with everything in between. This is the
    observable effect of the shed: the call itself moved off the sequential-
    blocking path, but its verdict is still captured, in-process, before
    this function returns (see `_run_precommit_tail`'s docstring "coverage.
    gate concurrency" section)."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    call_order: list[str] = []

    async def _fake_empty(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": []}

    def _fake_render(_worktree_root: Path) -> dict:
        call_order.append("render_handoff_tracker")
        return {"acted": [wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER], "skipped": [], "failed": []}

    def _fake_roadmap(_worktree_root: Path, paths: list[str]) -> dict:
        call_order.append("refresh_roadmap_callout")
        assert paths == ["state/handoffs/example.md"]
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_coverage(*_a: Any, **_kw: Any) -> dict:
        call_order.append("coverage_gate")
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_review(*_a: Any, **_kw: Any) -> dict:
        call_order.append("review_trail")
        return {"acted": [], "skipped": [], "failed": []}

    # C2 (2026-07-23): archive-sweeps fire detached now -- stub spawn_detached so
    # these unrelated precommit-tail tests never actually spawn a subprocess.
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "render_handoff_tracker", _fake_render)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "refresh_roadmap_callout", _fake_roadmap)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_coverage)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "write_review_trail", _fake_review)

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-render-pair",
            review_trail=None,
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=["state/handoffs/example.md"],
        )
    )

    # coverage.gate is FIRED first (before either render, top-of-function) but
    # only JOINED (awaited) after review_trail -- see this test's docstring.
    assert call_order == [
        "render_handoff_tracker", "refresh_roadmap_callout", "review_trail", "coverage_gate",
    ]
    assert wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER in results
    assert wsc_tail_mod.tail_ops.OP_ROADMAP_CALLOUT in results
    assert results[wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER]["acted"] == [
        wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER
    ]


def test_precommit_tail_render_failure_degrades_fail_open(tmp_path, monkeypatch):
    """A render_handoff_tracker failure lands in that step's failed[] but never
    aborts the rest of the pre-commit tail -- coverage.gate and review_trail
    still run afterward (best-effort-with-report)."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    call_order: list[str] = []

    async def _fake_empty(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": []}

    def _failing_render(_worktree_root: Path) -> dict:
        return {
            "acted": [], "skipped": [],
            "failed": [f"{wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER}: RuntimeError -- boom"],
        }

    def _fake_roadmap(_worktree_root: Path, _paths: list[str]) -> dict:
        call_order.append("refresh_roadmap_callout")
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_coverage(*_a: Any, **_kw: Any) -> dict:
        call_order.append("coverage_gate")
        return {"acted": [], "skipped": [], "failed": []}

    # C2 (2026-07-23): archive-sweeps fire detached now -- stub spawn_detached so
    # these unrelated precommit-tail tests never actually spawn a subprocess.
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "render_handoff_tracker", _failing_render)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "refresh_roadmap_callout", _fake_roadmap)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_coverage)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "write_review_trail", _fake_empty)

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-render-fail",
            review_trail=None,
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=[],
        )
    )

    assert call_order == ["refresh_roadmap_callout", "coverage_gate"]
    assert results[wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER]["failed"] != []
    assert results[wsc_tail_mod.tail_ops.OP_HANDOFF_TRACKER]["acted"] == []



# ---------------------------------------------------------------------------
# coverage.gate / review_trail.write receipt-ambiguity annotation
# (2026-07-22 incident -- see coverage_gate.py's Negative-spec note).
# ---------------------------------------------------------------------------


def test_precommit_tail_annotates_coverage_gate_when_review_metadata_absent(tmp_path, monkeypatch):
    """When review_trail.write skips with `no-review-metadata`, the coverage.gate
    receipt entry is annotated `review_metadata_supplied: False` -- so a reader
    can tell "no metadata was supplied" from "genuinely uncovered" without
    string-matching review_trail's skip reason themselves."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    async def _fake_empty(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_coverage_uncovered(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:UNCOVERED"], "skipped": [], "failed": []}

    # C2 (2026-07-23): archive-sweeps fire detached now -- stub spawn_detached so
    # these unrelated precommit-tail tests never actually spawn a subprocess.
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "render_handoff_tracker",
        lambda _w: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "refresh_roadmap_callout",
        lambda _w, _p: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_coverage_uncovered)
    # write_review_trail is NOT mocked here -- exercise the real function with
    # no review_trail supplied, so it takes the genuine no-review-metadata
    # skip path this annotation exists to surface.

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-no-metadata",
            review_trail=None,
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=[],
        )
    )

    coverage_result = results[wsc_tail_mod.tail_ops.OP_COVERAGE_GATE]
    review_result = results[wsc_tail_mod.tail_ops.OP_REVIEW_TRAIL]

    assert review_result["skipped"] == [f"{wsc_tail_mod.tail_ops.OP_REVIEW_TRAIL}:no-review-metadata"]
    assert coverage_result["review_metadata_supplied"] is False
    # The verdict itself is untouched by the annotation.
    assert coverage_result["acted"] == [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:UNCOVERED"]


def test_precommit_tail_annotates_coverage_gate_when_review_metadata_supplied(tmp_path, monkeypatch):
    """When review_trail.write does NOT skip with `no-review-metadata` (complete
    metadata was supplied, whether it succeeded or failed for some other
    reason), the coverage.gate receipt entry is annotated
    `review_metadata_supplied: True`."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    async def _fake_empty(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_coverage_uncovered(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:UNCOVERED"], "skipped": [], "failed": []}

    async def _fake_review_written(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [f"{wsc_tail_mod.tail_ops.OP_REVIEW_TRAIL}:some/path.json"], "skipped": [], "failed": []}

    # C2 (2026-07-23): archive-sweeps fire detached now -- stub spawn_detached so
    # these unrelated precommit-tail tests never actually spawn a subprocess.
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "render_handoff_tracker",
        lambda _w: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "refresh_roadmap_callout",
        lambda _w, _p: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_coverage_uncovered)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "write_review_trail", _fake_review_written)

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-metadata-supplied",
            review_trail={
                "sha_range": "a..b", "reviewer": "staff-eng", "scope": "diff",
                "verdict": "OK", "diff_loc": 10,
            },
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=[],
        )
    )

    coverage_result = results[wsc_tail_mod.tail_ops.OP_COVERAGE_GATE]
    assert coverage_result["review_metadata_supplied"] is True
    # The verdict itself is untouched by the annotation.
    assert coverage_result["acted"] == [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:UNCOVERED"]


def test_precommit_tail_coverage_gate_annotation_is_additive(tmp_path, monkeypatch):
    """Old-reader compatibility: the annotation is a single added key on the
    existing coverage.gate result dict -- acted/skipped/failed are unchanged,
    so a reader ignoring the new field sees exactly the pre-existing shape."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    async def _fake_empty(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": []}

    async def _fake_coverage(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": ["coverage.gate: INDETERMINATE -- x"]}

    # C2 (2026-07-23): archive-sweeps fire detached now -- stub spawn_detached so
    # these unrelated precommit-tail tests never actually spawn a subprocess.
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "render_handoff_tracker",
        lambda _w: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "refresh_roadmap_callout",
        lambda _w, _p: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_coverage)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "write_review_trail", _fake_empty)

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-additive",
            review_trail=None,
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=[],
        )
    )

    coverage_result = results[wsc_tail_mod.tail_ops.OP_COVERAGE_GATE]
    assert coverage_result["acted"] == []
    assert coverage_result["skipped"] == []
    assert coverage_result["failed"] == ["coverage.gate: INDETERMINATE -- x"]
    assert "review_metadata_supplied" in coverage_result  # additive key present


# ---------------------------------------------------------------------------
# C6 (2026-07-23 wsc-tail-slim-down) -- coverage.gate sheds its BLOCKING-ness
# (fired via `asyncio.create_task`, joined at the end of `_run_precommit_
# tail`) while its verdict still lands in the receipt as a D-node, and the
# ceremony never branches on that verdict. review_trail.write is NOT shed --
# it stays a plain, sequential, in-op `await` (see the module docstring's
# "coverage.gate concurrency" section and negative-spec below).
# ---------------------------------------------------------------------------


def test_precommit_tail_coverage_gate_verdict_survives_the_shed(tmp_path, monkeypatch):
    """The shed moves coverage.gate's INVOCATION off the sequential-blocking
    path (asyncio.create_task fired early, joined late) -- it must NOT move
    the VERDICT out of this call's own result. The real risk this chunk
    guards against (`coverage-gate-perf.md`'s fail-open incident) is a
    detached call silently dropping its verdict; assert the verdict is still
    present, verbatim, in `_run_precommit_tail`'s returned results dict."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    async def _fake_covered(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:COVERED"], "skipped": [], "failed": []}

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "render_handoff_tracker",
        lambda _w: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(
        wsc_tail_mod.tail_ops, "refresh_roadmap_callout",
        lambda _w, _p: {"acted": [], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_covered)

    results, _extra_stage_paths = _run(
        wsc_tail_mod._run_precommit_tail(
            common_dir,
            worktree_root,
            "sid-verdict-survives",
            review_trail=None,
            b_adjudication_present=False,
            coverage_range="",
            coverage_from_handoff="",
            coverage_scope_paths=None,
            consumed_handoff_paths=[],
        )
    )

    coverage_result = results[wsc_tail_mod.tail_ops.OP_COVERAGE_GATE]
    assert coverage_result["acted"] == [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:COVERED"]


def test_wsc_tail_handler_records_coverage_gate_verdict_as_receipt_d_node(
    wsc_tail_repo, monkeypatch
):
    """End-to-end (real `_handler`, real persisted receipt): coverage.gate's
    verdict lands in the PERSISTED receipt as a D-node even though its own
    invocation was shed off the blocking path this pass. This is the
    receipt-contract half of C6 (see finding 16's receipt contract, and
    C6's own body text: "the shed is the invocation, not the verdict")."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    async def _fake_covered(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:COVERED"], "skipped": [], "failed": []}

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_covered)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None

    receipt_path = repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    coverage_nodes = [
        n for n in receipt["nodes"] if n.get("resolving_op") == wsc_tail_mod.tail_ops.OP_COVERAGE_GATE
    ]
    assert len(coverage_nodes) == 1, receipt["nodes"]
    assert coverage_nodes[0]["evidence"]["acted"] == [
        f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}:COVERED"
    ]


def test_wsc_tail_handler_does_not_branch_on_coverage_gate_verdict(wsc_tail_repo, monkeypatch):
    """coverage.gate is advisory D-node evidence only, never a hard gate
    (tail_ops.run_coverage_gate's own docstring, verbatim). An INDETERMINATE
    verdict (exit_code=2 from the underlying op, surfaced here as a
    `failed[]` entry) must NOT stop the commit from landing -- the ceremony
    continues either way. `exit_code` on the WIRE reply is allowed to reflect
    the soft-failure (advisory D-node evidence feeding the reply's own
    failure-class discriminator) -- what must NOT happen is the commit
    itself being blocked or skipped because of this verdict."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    async def _fake_indeterminate(*_a: Any, **_kw: Any) -> dict:
        return {
            "acted": [], "skipped": [],
            "failed": [f"{wsc_tail_mod.tail_ops.OP_COVERAGE_GATE}: INDETERMINATE -- no baseline"],
        }

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "run_coverage_gate", _fake_indeterminate)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", lambda *_a, **_kw: True)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    head_before = repo.head_sha()

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    # The commit landed regardless of the INDETERMINATE verdict -- coverage.gate
    # never gated it.
    assert result["committed_sha"] is not None
    assert repo.head_sha() != head_before
    assert repo.head_sha() == result["committed_sha"]
    # exit_code is allowed to reflect the soft failure (advisory evidence) --
    # it is NOT the same thing as the commit being blocked.
    assert result["exit_code"] == 2, result


def test_review_trail_write_is_still_a_plain_sequential_await(monkeypatch):
    """review_trail.write is NOT shed by C6 -- it must remain a plain,
    synchronous, in-op `await` call (never wrapped in `asyncio.create_task`,
    never fired detached). Assert `_run_precommit_tail`'s source calls
    `tail_ops.write_review_trail` with a bare `await`, not `asyncio.
    create_task` -- a cheap, durable structural guard against a future
    "cleanup" pass silently shedding it (see this module's negative-spec)."""
    import inspect as _inspect

    source = _inspect.getsource(wsc_tail_mod._run_precommit_tail)
    call_line = next(
        line for line in source.splitlines() if "tail_ops.write_review_trail(" in line
    )
    assert "await tail_ops.write_review_trail(" in call_line, (
        f"review_trail.write must stay a plain sequential await, got: {call_line!r}"
    )


def test_precommit_tail_roadmap_callout_noops_with_no_consumed_handoffs(tmp_path):
    """refresh_roadmap_callout (the REAL function, not mocked) cleanly no-ops
    when this pass consumed no predecessor handoff -- a single-session
    ceremony must not attempt any roadmap refresh."""
    common_dir = tmp_path / "repo" / ".git"
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir(parents=True)
    common_dir.mkdir(parents=True)

    result = wsc_tail_mod.tail_ops.refresh_roadmap_callout(worktree_root, [])

    assert result == {
        "acted": [],
        "skipped": [f"{wsc_tail_mod.tail_ops.OP_ROADMAP_CALLOUT}:no-consumed-handoff"],
        "failed": [],
    }


# ---------------------------------------------------------------------------
# C2 (2026-07-23, docs/plans/2026-07-23-wsc-tail-slim-down.md) -- archive
# sweeps fire DETACHED, not in-process. The blocking archive_completed_plans
# / archive_completed_handoffs / sweep_actioned_memos calls are gone from
# this op's path entirely; tail_ops.fire_archive_sweeps_detached fires four
# per-class CLIs detached instead, never the composite sweep-boot.py.
# ---------------------------------------------------------------------------

_WSC_TAIL_SOURCE_PATH = Path(wsc_tail_mod.__file__)


def test_no_blocking_archive_sweep_call_remains_on_wsc_tail_source():
    """Grep-assert (plan § C2's own named test): none of the three retired blocking
    wrapper calls appear anywhere in wsc_tail.py's source -- a regression here would
    mean a blocking archive/sweep call crept back onto the ceremony's critical path."""
    source = _WSC_TAIL_SOURCE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "tail_ops.archive_completed_plans(",
        "tail_ops.archive_completed_handoffs(",
        "tail_ops.sweep_actioned_memos(",
    ):
        assert forbidden not in source, (
            f"blocking call {forbidden!r} must not appear in wsc_tail.py -- "
            "archive sweeps fire detached via tail_ops.fire_archive_sweeps_detached (C2)"
        )


def test_no_blocking_archive_sweep_call_remains_in_tail_ops_module():
    """The three retired blocking wrapper FUNCTIONS themselves must be gone from
    tail_ops.py, not merely uncalled -- a dead wrapper left behind is exactly the
    kind of no-other-caller helper the C2 dispatch brief named for deletion."""
    assert not hasattr(wsc_tail_mod.tail_ops, "archive_completed_plans")
    assert not hasattr(wsc_tail_mod.tail_ops, "archive_completed_handoffs")
    assert not hasattr(wsc_tail_mod.tail_ops, "sweep_actioned_memos")


def test_archive_sweeps_fire_after_commit_pipeline_not_before(wsc_tail_repo, monkeypatch):
    """Order-sensitive regression test (PM finding, 2026-07-23): the detached archive-
    sweep fire must be issued AFTER `run_commit_pipeline` completes and the commit has
    landed, never before -- firing pre-commit races the pipeline's own `git commit` for
    `.git/index.lock` and presents `dirty_tree_gate`'s whole-worktree scan with exactly
    the unattributable-dirty-file shape it hard-fails on. Presence alone is not enough;
    this pins ORDER."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    call_order: list[str] = []

    real_run_commit_pipeline = wsc_tail_mod.run_commit_pipeline

    def _spy_run_commit_pipeline(*args: Any, **kwargs: Any) -> Any:
        outcome = real_run_commit_pipeline(*args, **kwargs)
        call_order.append("commit_pipeline")
        return outcome

    def _fake_fire(_worktree_root: Path) -> dict:
        call_order.append("archive_sweeps_fire")
        return {"acted": [], "skipped": [], "failed": []}

    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", _spy_run_commit_pipeline)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "fire_archive_sweeps_detached", _fake_fire)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert call_order == ["commit_pipeline", "archive_sweeps_fire"], call_order
    assert result["tail_results"][wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED] == {
        "acted": [], "skipped": [], "failed": [],
    }


def test_archive_sweeps_do_not_fire_when_commit_pipeline_fails(wsc_tail_repo, monkeypatch):
    """When the commit pipeline fails (no commit landed), the detached archive-sweep
    fire must NOT be issued -- there is nothing newly-terminal to archive, and firing
    into a failed ceremony risks a sweep archiving state the ceremony just decided not
    to commit."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    fired = False

    def _fake_fire(_worktree_root: Path) -> dict:
        nonlocal fired
        fired = True
        return {"acted": [], "skipped": [], "failed": []}

    failed_outcome = make_pipeline_result(
        commit_failed=True,
        diagnostics=["forced failure for C2 regression test"],
    )

    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", lambda *_a, **_kw: failed_outcome)
    monkeypatch.setattr(wsc_tail_mod.tail_ops, "fire_archive_sweeps_detached", _fake_fire)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["committed_sha"] is None
    assert fired is False, "archive-sweeps fire must not run when the commit pipeline fails"
    # Labelled-skip fix (cross-repo/inbox/2026-08-04-example-retrieval-repo-em-wsc-tail-
    # abort-loses-baton-terminal-flip.md): the node must still be PRESENT,
    # with a labelled skip -- an absent node used to read identically to
    # "nothing to do here" to anyone diffing receipts.
    assert result["tail_results"][wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED] == {
        "acted": [],
        "skipped": [f"{wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED}:commit-failed"],
        "failed": [],
    }


def test_fire_archive_sweeps_detached_never_targets_composite_sweep_boot(tmp_path, monkeypatch):
    """The composite `coordinator/bin/sweep-boot.py` is NEVER what gets fired -- it also
    runs the unintegrated-findings reap (a tracked `git rm`), out of scope for a call
    fired on every WSC pass (plan § C2 anti-scope)."""
    worktree_root = tmp_path
    fired_scripts: list[str] = []

    def _fake_spawn(_repo_root, script_path, _args):
        fired_scripts.append(Path(script_path).name)
        return True

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "spawn_detached", _fake_spawn)

    wsc_tail_mod.tail_ops.fire_archive_sweeps_detached(worktree_root)

    assert "sweep-boot.py" not in fired_scripts
    assert set(fired_scripts) == {
        "sweep-terminal-plans.py",
        "sweep-shipped-handoffs.py",
        "sweep-consumed-handoffs.py",
        "sweep-actioned-memos.py",
    }


# ---------------------------------------------------------------------------
# Step 5d -- origin-stub close fold (2026-07-22, see wsc_tail.py module
# docstring). Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-
# wsc-tail-cutover-contract.md "Out of scope" § 2.7b.
# ---------------------------------------------------------------------------


def test_origin_stub_close_end_to_end_via_governing_plan(wsc_tail_repo):
    """(f) `ceremony.wsc_tail` closes the origin stub end-to-end when a
    governing plan resolves a matching non-terminal origin stub -- the
    closed stub carries `shipped_in: <committed_sha>` (stamped before ship,
    something the OLD pre-commit bash Step 2.7b could never do) and lands
    in its own pushed follow-up commit, never an unswept dirty edit."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.write_plan("my-feature", roadmap_id="rm-1", stub_id="stub-1")
    stub_path = repo.seed_origin_stub("2026-01-02_origin-stub.md", roadmap_id="rm-1", stub_id="stub-1")
    stub_relpath = "state/handoffs/2026-01-02_origin-stub.md"

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
                "governing_plan_slug": "my-feature",
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None

    origin_stub_tail = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub_tail["acted"] == [stub_relpath], origin_stub_tail
    assert origin_stub_tail["failed"] == []

    stub_text = stub_path.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in stub_text
    assert f"shipped_in: {result['committed_sha']}" in stub_text

    status_lines = repo.porcelain()
    assert not any(stub_relpath in ln for ln in status_lines), status_lines


def test_origin_stub_close_noop_when_nothing_to_close(wsc_tail_repo):
    """(g) clean no-op -- no governing plan, no consumed handoff -- is the
    normal case for the majority of workstreams that are not stub-derived.
    No failure, no spurious warning; overall ceremony exit_code unaffected."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    origin_stub_tail = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub_tail == {
        "acted": [],
        "skipped": [f"{wsc_tail_mod.OP_CLOSE_ORIGIN_STUB}:no-governing-plan-or-consumed-handoff"],
        "failed": [],
    }


def test_origin_stub_close_failure_does_not_fail_the_tail(wsc_tail_repo, monkeypatch):
    """(h) a stub-close failure soft-fails -- the already-landed main
    ceremony commit is never unwound, `exit_code` degrades to 2 (soft-fail),
    never 1, and never contributes to `failed_critical` (an unclosed stub
    after a landed commit is recoverable via the lvv-09 cadence backstop,
    not a breach)."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.write_plan("my-feature", roadmap_id="rm-1", stub_id="stub-1")
    repo.seed_origin_stub("2026-01-02_origin-stub.md", roadmap_id="rm-1", stub_id="stub-1")

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated handoff.close_origin_stub crash")

    monkeypatch.setattr(wsc_tail_mod, "_close_origin_stub_handler", _boom)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
                "governing_plan_slug": "my-feature",
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["committed_sha"] is not None
    assert result["exit_code"] == 2, result

    origin_stub_tail = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub_tail["acted"] == []
    assert any("simulated handoff.close_origin_stub crash" in f for f in origin_stub_tail["failed"])
    assert "failed_critical" not in origin_stub_tail


def test_origin_stub_close_runs_on_ac18_resume(wsc_tail_repo, monkeypatch):
    """(i) AC18 sentinel-resume: the origin-stub close is post-commit work,
    so it must still run when a crash inside `post_commit_stamp_and_ship`
    on the FRESH pass forces resumption -- the resumed pass keys off the
    RECOVERED `committed_sha`, never a fresh `git rev-parse HEAD`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.write_plan("my-feature", roadmap_id="rm-1", stub_id="stub-1")
    stub_path = repo.seed_origin_stub("2026-01-02_origin-stub.md", roadmap_id="rm-1", stub_id="stub-1")
    stub_relpath = "state/handoffs/2026-01-02_origin-stub.md"

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    orig_post_commit = wsc_tail_mod.consumed_handoff_stamp.post_commit_stamp_and_ship

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash after main commit landed, before stamp completed")

    monkeypatch.setattr(wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", _boom)

    params = {
        "sid": sid,
        "subject": "workstream-complete: feature",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
        "governing_plan_slug": "my-feature",
    }

    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    # Fresh pass crashed before origin-stub close ever ran -- stub untouched.
    assert "deployment_state: shipped" not in stub_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        wsc_tail_mod.consumed_handoff_stamp, "post_commit_stamp_and_ship", orig_post_commit
    )

    result = _run(wsc_tail_mod._handler(params, repo_root=repo.common_dir))

    assert result["resumed_from_sentinel"] is True
    origin_stub_tail = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub_tail["acted"] == [stub_relpath], origin_stub_tail

    stub_text = stub_path.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in stub_text
    assert f"shipped_in: {result['committed_sha']}" in stub_text


def test_close_origin_stub_standalone_op_still_registered():
    """(j) regression guard (constraint 1) -- `handoff.close_origin_stub`
    remains independently registered and reachable via the public op-registry
    after the step-5d fold; this is a fold-IN, not a move. See also the full
    standalone regression suite in `coordinator_core/ops/tests/
    test_handoff_close_origin_stub.py` (unmodified by this fold)."""
    handler = wsc_tail_mod.get_op_handler("handoff.close_origin_stub")
    assert handler is not None
    assert handler is wsc_tail_mod._close_origin_stub_handler


# ---------------------------------------------------------------------------
# wsc-tail-sub-2s-invoke-budget (docs/plans/2026-07-22-wsc-tail-sub-2s-
# invoke-budget.md) -- C3: deferred-push cutover + locked-unit restructure.
#
# RESOLVED (C8): test_ac17_shipped_in_stamp_lands_in_own_pushed_follow_up_commit
# and test_ac18_crash_after_commit_resumes_from_sentinel_without_double_commit
# above now assert the deferred contract (push_status="deferred"/
# "unknown_resumed", pushed/follow_up_pushed None) against wsc_tail's
# DEFERRED default push_mode; their `..._sync_seam` siblings pin the
# COORDINATOR_WSC_SYNC_PUSH=1 opt-out still driving the pre-DEC-1
# synchronous `follow_up_pushed=True` behavior byte-for-byte.
# ---------------------------------------------------------------------------


def test_push_mode_default_preserves_scoped_git_commit_contract(tmp_path):
    """`run_commit_pipeline`'s `push_mode` default ("sync") is `scoped_git_
    commit.py`'s untouched wire contract (DEC-1/F1) -- a bare call with no
    `push_mode` kwarg still pushes synchronously and returns a real `pushed`
    tri-state, byte-for-byte identical to pre-DEC-1 behavior. `scoped_git_
    commit.py` itself is untouched (out of this chunk's write-scope) -- this
    exercises the exact call shape it makes.

    C7c (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
    this test's own subject is the push MECHANISM (a bare call still pushes,
    still returns a real `pushed` tri-state) -- not the branch-policy
    contract, which is exercised elsewhere. It used to run on `main`, which
    the real `work/*`-only push-leg branch policy now declines; repair (a):
    moved onto `work/test/push-mode-default` so the push this test actually
    means to exercise still lands, rather than inverting the assertion to a
    decline that isn't this test's point."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    branch = "work/test/push-mode-default"
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["checkout", "-b", branch], repo)
    push0 = _git(["push", "-u", "origin", branch], repo)
    assert push0.returncode == 0, push0.stderr

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert result.push is not None  # the push step actually ran
    assert result.pushed is True
    assert result.integrity_breach is False


def test_wsc_tail_deferred_mode_default_result_contract_and_no_sync_push(
    wsc_tail_repo, monkeypatch
):
    """`ceremony.wsc_tail`'s default `push_mode` is "deferred" (DEC-1): the
    result ALWAYS carries `push_status="deferred"`, `pushed`/
    `follow_up_pushed` both `None`, `integrity_breach=False` (AC8/F4) -- and
    `push_with_retry` (the synchronous network round-trip) is proven NEVER
    to run on the blocking path (AC2), while the ONE detached-push spawn
    point fires exactly once."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.delenv(wsc_tail_mod._ENV_SYNC_PUSH, raising=False)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    spawn_calls: list[Path] = []
    monkeypatch.setattr(
        wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: spawn_calls.append(wt)
    )

    def _push_should_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "push_with_retry must never run on wsc_tail's blocking path in deferred mode (AC2)"
        )

    monkeypatch.setattr(commit_pipeline_mod, "push_with_retry", _push_should_not_run)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert result["push_status"] == "deferred"
    assert result["pushed"] is None
    assert result["follow_up_pushed"] is None
    assert result["integrity_breach"] is False
    assert len(spawn_calls) == 1
    assert Path(spawn_calls[0]).resolve() == repo.root.resolve()


def test_wsc_tail_sync_seam_restores_synchronous_push_byte_for_byte(wsc_tail_repo, monkeypatch):
    """`COORDINATOR_WSC_SYNC_PUSH=1` restores `push_mode="sync"` -- today's
    fully synchronous push CONTRACT (the push, if any lands, happens in-op,
    observable at result-return time via the real remote, never merely
    "eventually" via a detached child) -- and the deferred-push spawn point
    is never invoked.

    C6b (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
    this fixture repo lives on "main", which the real `work/*`-only push-leg
    branch policy (landed earlier in this same plan, commit_pipeline.py, out
    of C6b's scope) genuinely declines -- this test was pinned to
    `push_status="pushed"`/`pushed is True` only because the pre-C6b
    derivation folded that decline silently into "pushed" (the exact defect
    C6b fixes). Now correctly asserts the decline, not a push that landed;
    `remote_log()` never carries the commit for the same reason."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    spawn_calls: list[Path] = []
    monkeypatch.setattr(
        wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: spawn_calls.append(wt)
    )

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["push_status"] == "declined"
    assert result["pushed"] is None
    assert spawn_calls == []  # sync mode never spawns a detached push
    assert result["committed_sha"][:7] not in repo.remote_log()


# ---------------------------------------------------------------------------
# C6b (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
# `push_status` reads `PipelineResult.push_status` directly -- never
# re-derived from the `pushed` tri-state, which used to fold a policy
# decline silently into "pushed". Each test below drives a REAL
# `PipelineResult` (built via `dataclasses.replace` off the genuine object
# `run_commit_pipeline` returned for an actual commit) through the real
# `wsc_tail._handler` derivation -- never a stub of the derivation itself.
# ---------------------------------------------------------------------------


def _patch_pipeline_result(monkeypatch, **overrides: Any) -> None:
    orig_run_commit_pipeline = wsc_tail_mod.run_commit_pipeline

    def _patched(*args: Any, **kwargs: Any) -> Any:
        real_result = orig_run_commit_pipeline(*args, **kwargs)
        return dataclasses.replace(real_result, **overrides)

    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", _patched)


def test_c6b_policy_decline_reports_declined_never_pushed(wsc_tail_repo, monkeypatch):
    """AC12: a policy decline (`pushed=None`, `push_status="declined"` on
    the real `PipelineResult`) must report `push_status="declined"` --
    NEVER `"pushed"`, which is the exact defect C6b fixes (the old
    derivation's `else` branch folded any non-`False` `pushed` into
    `"pushed"`, silently reporting a refused push as landed)."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    _patch_pipeline_result(
        monkeypatch,
        pushed=None,
        push_status=commit_pipeline_mod.PUSH_STATUS_DECLINED,
        integrity_breach=False,
    )

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["push_status"] == "declined"
    assert result["push_status"] != "pushed"


def test_c6b_no_remote_reports_no_remote(wsc_tail_repo, monkeypatch):
    """AC12: the new `"no-remote"` member (this module had none before
    C6b) -- a genuine no-remote-configured `PipelineResult` must report
    `push_status="no-remote"`, not fold silently into `"pushed"`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    _patch_pipeline_result(
        monkeypatch,
        pushed=None,
        push_status=commit_pipeline_mod.PUSH_STATUS_NO_REMOTE,
        integrity_breach=False,
    )

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["push_status"] == "no-remote"


def test_c6b_not_attempted_stays_pushed_and_distinct_from_declined(wsc_tail_repo, monkeypatch):
    """AC12 point 5: this module's pre-existing `push_mode="none"` semantics
    (`pushed=None`, nothing ever attempted -- canonical `push_status=
    "not-attempted"`) must keep reporting `"pushed"`, same as before C6b,
    and must stay a DIFFERENT value than a real `"declined"` policy
    refusal -- two distinct "no push happened" reasons that must not
    collapse into one."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    _patch_pipeline_result(
        monkeypatch,
        pushed=None,
        push_status=commit_pipeline_mod.PUSH_STATUS_NOT_ATTEMPTED,
        integrity_breach=False,
    )

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["push_status"] == "pushed"
    assert result["push_status"] != "declined"


def test_c6b_genuine_failure_still_reports_failed(wsc_tail_repo, monkeypatch):
    """AC12: a genuine push failure (`pushed=False`, canonical
    `push_status="push-failed"`) must still report `push_status="failed"`
    -- unchanged by the C6b reconciliation."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")
    _patch_pipeline_result(
        monkeypatch,
        pushed=False,
        push_status=commit_pipeline_mod.PUSH_STATUS_FAILED,
        integrity_breach=True,
    )

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 2, result
    assert result["push_status"] == "failed"


def test_c6b_resumed_pass_still_reports_unknown_resumed(wsc_tail_repo, monkeypatch):
    """AC12 point 3: `"unknown_resumed"` has no counterpart in the canonical
    set and must survive the reconciliation -- a resumed pass wins
    regardless of the (never-consulted, since `run_commit_pipeline` is
    skipped entirely on a resumed pass) pipeline `push_status`. Reuses the
    existing AC18 sentinel-resume pattern from
    `test_ac18_crash_after_commit_resumes_from_sentinel_without_double_
    commit_sync_seam`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.setenv(wsc_tail_mod._ENV_SYNC_PUSH, "1")

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )
    assert result["exit_code"] == 0, result
    committed_sha = result["committed_sha"]

    # Simulate a crash-recovered pass: write the AC18 sentinel for a NEW
    # session id carrying the already-landed sha, then resume.
    resumed_sid = _unique_session_id()
    sentinel_path = repo.sentinel_path(resumed_sid)
    sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel_path.write_text(committed_sha, encoding="utf-8")

    resumed_result = _run(
        wsc_tail_mod._handler(
            {
                "sid": resumed_sid,
                "subject": "workstream-complete: feature",
                "stage_paths": [],
                "caller_paths": [],
            },
            repo_root=repo.common_dir,
        )
    )
    assert resumed_result["push_status"] == "unknown_resumed"


def test_ac4_no_ceremony_lock_nesting_remains_on_any_live_path():
    """AC9 (repo-wide reintroduction guard, re-pointed by C7,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md). Name pinned by AC9's
    literal text (S3 close review, finding 6) -- the body below is
    repo-wide, not scoped to nesting or to `wsc_tail`'s live path; if AC9's
    wording is amended, this name should follow.

    `ceremony_lock.py` was deleted outright by C7 -- the mutex it implemented
    was killed by PM ruling (repeated shared-worktree wedges) and its
    restoration is separately sized, explicitly out of scope for this plan.
    This is the only executable enforcement of the plan's Anti-scope "do NOT
    reimplement a mutex" -- but it enforces exactly one identifier
    (`ceremony_lock`), not the Anti-scope's full "any file, any name" text; a
    mutex reintroduced under a different name is NOT caught here and needs
    plan review to catch. See `_ceremony_lock_guard.py`'s module docstring
    for exactly what is and is not covered, and why."""
    repo_root = Path(__file__).resolve().parents[4]
    assert_no_ceremony_lock_reintroduction(repo_root)


def test_wait_for_can_fire_during_wsc_tail_critical_section(wsc_tail_repo, monkeypatch):
    """AC6: the commit-pipeline unit runs inside ONE `asyncio.to_thread` call,
    so the event loop stays live while it runs in a worker thread -- proven
    by racing a concurrent `asyncio.sleep`-driven ticker against a
    monkeypatched slow `run_commit_pipeline` and observing the ticker made
    real progress WHILE the pipeline's blocking sleep was still in flight
    (mirrors `test_dispatch_message.py::test_blocking_handler_does_not_
    wedge_loop`'s existing ipc-level pattern, at the wsc_tail op level)."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    orig_run_commit_pipeline = wsc_tail_mod.run_commit_pipeline

    def _slow_run_commit_pipeline(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return orig_run_commit_pipeline(*args, **kwargs)

    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", _slow_run_commit_pipeline)

    ticker_progress: list[float] = []

    async def _concurrent_ticker() -> None:
        start = time.monotonic()
        for _ in range(6):
            await asyncio.sleep(0.05)
            ticker_progress.append(time.monotonic() - start)

    async def _drive() -> Any:
        return await asyncio.gather(
            wsc_tail_mod._handler(
                {
                    "sid": sid,
                    "subject": "workstream-complete: feature",
                    "stage_paths": ["tasks/feature/todo.md"],
                    "caller_paths": ["tasks/feature/todo.md"],
                },
                repo_root=repo.common_dir,
            ),
            _concurrent_ticker(),
        )

    result, _ticker_none = _run(_drive())

    assert result["exit_code"] == 0, result
    # The ticker made real progress WELL BEFORE the pipeline's 0.3s sleep
    # would have returned control -- proof the event loop was never wedged.
    # If to_thread were absent, the ticker's own awaits would starve until
    # the synchronous sleep returned, and no progress would show before ~0.3s.
    assert any(t < 0.25 for t in ticker_progress), ticker_progress


def test_fired_timeout_abandons_and_lets_complete(wsc_tail_repo, monkeypatch):
    """Invariant (verbatim, module docstring/code comment): "a fired timeout
    must abandon-and-let-complete, never unwind the lock ahead of the work."
    A caller-side `asyncio.wait_for` timeout (mirroring ipc.py's own dispatch
    wrapping) firing WHILE the commit-pipeline unit is mid-`to_thread` does
    NOT kill the worker thread (impossible -- `asyncio.to_thread` cannot
    interrupt a running thread) -- it completes its commit and releases its
    own internal `ceremony_lock` on its own schedule, independent of the
    caller already having received a `TimeoutError`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    orig_run_commit_pipeline = wsc_tail_mod.run_commit_pipeline
    delay = 1.0
    # Set by the worker thread the instant it is INSIDE the patched pipeline.
    # The caller's timeout is armed off this event, never off wall clock from
    # handler entry: steps 1-3 shell out to `git` several times before step 5a
    # is ever reached, so a fixed timeout races the preamble and on a loaded
    # box fires BEFORE any worker thread exists -- there is then nothing
    # abandoned to complete, and the test fails asserting its own premise.
    entered_pipeline = threading.Event()

    def _slow_run_commit_pipeline(*args: Any, **kwargs: Any) -> Any:
        entered_pipeline.set()
        time.sleep(delay)
        return orig_run_commit_pipeline(*args, **kwargs)

    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", _slow_run_commit_pipeline)

    head_before = repo.head_sha()

    async def _drive() -> Any:
        task = asyncio.ensure_future(
            wsc_tail_mod._handler(
                {
                    "sid": sid,
                    "subject": "workstream-complete: feature",
                    "stage_paths": ["tasks/feature/todo.md"],
                    "caller_paths": ["tasks/feature/todo.md"],
                },
                repo_root=repo.common_dir,
            )
        )
        arm_deadline = time.monotonic() + 60.0
        while not entered_pipeline.is_set():
            if task.done():
                raise AssertionError(
                    "handler returned without ever reaching step 5a's "
                    f"asyncio.to_thread(run_commit_pipeline, ...): {task.result()!r}"
                )
            if time.monotonic() > arm_deadline:
                task.cancel()
                raise AssertionError(
                    "handler never reached step 5a's "
                    "asyncio.to_thread(run_commit_pipeline, ...) within 60s"
                )
            await asyncio.sleep(0.01)
        # Now provably mid-`to_thread`, with `delay` seconds of worker sleep
        # still ahead of it -- the timeout below cannot fire anywhere else.
        return await asyncio.wait_for(task, timeout=0.2)

    with pytest.raises(asyncio.TimeoutError):
        _run(_drive())

    # The abandoned worker thread is NOT killed by the caller's timeout --
    # give it real wall-clock time to finish its commit on its own.
    deadline = time.monotonic() + delay + 3.0
    while time.monotonic() < deadline:
        if repo.head_sha() != head_before:
            break
        time.sleep(0.05)

    assert repo.head_sha() != head_before, (
        "the abandoned to_thread worker must still complete its commit even "
        "though the caller already received a TimeoutError -- abandon-and-"
        "let-complete, never unwind the lock ahead of the work"
    )


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-07-23-wsc-tail-slim-down.md) -- per-step wall-clock
# timing instrumentation. Every cost claim about this op was previously
# unmeasured inference; this test pins the timing map's shape as a contract
# (a later chunk asserts on this map's step membership/count).
# ---------------------------------------------------------------------------

_EXPECTED_TIMING_STEPS = {
    "params_and_push_mode",
    "step1_resolve",
    "sentinel_read",
    "precommit_tail_total",
    "precommit.tracker_and_roadmap",
    "precommit.coverage_gate_fire",
    "precommit.coverage_gate_join",
    "precommit.review_trail",
    "trailer_derivation",
    "sentinel_write",
    "commit_pipeline",
    "stamp_and_ship",
    "origin_stub_close",
    "postcommit.archive_sweeps_detached",
    "deferred_push_spawn",
    "cs_release_artifact",
}


def test_timing_map_covers_every_instrumented_step_with_nonnegative_ms(
    wsc_tail_repo, monkeypatch
):
    """A representative fresh (non-resumed), deferred-push, no-governing-plan
    pass records exactly the C1 step set in the PERSISTED receipt's
    `wsc-tail-timing` D-node -- `dirty_tree_gate` and `trailer_derivation` are
    two of these named steps (trailer_derivation individually visible, never
    folded into `commit_pipeline` -- see wsc_tail.py's `commit_pipeline`
    timing-span comment for why `dirty_tree_gate` itself could not be
    separated without editing commit_pipeline.py, which this chunk's remit
    hard-forbids).

    Every step name is STABLE/greppable (membership + count is a contract,
    not debug output) and every `ms` value is a non-negative number -- a
    timer that ever produced a negative or non-numeric duration would be
    lying about wall-clock cost, defeating the entire point of this
    instrumentation.
    """
    repo = wsc_tail_repo
    sid = _unique_session_id()
    monkeypatch.delenv(wsc_tail_mod._ENV_SYNC_PUSH, raising=False)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["committed_sha"] is not None
    assert result["resumed_from_sentinel"] is False

    receipt_path = repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    timing_nodes = [n for n in receipt["nodes"] if n.get("id") == "wsc-tail-timing"]
    assert len(timing_nodes) == 1, "expected exactly one timing D-node in the persisted receipt"
    timing_node = timing_nodes[0]

    # Non-tail-step: must never feed op_tail's acted/skipped/failed aggregation.
    assert timing_node.get("tail_step") is False
    assert timing_node.get("type") == "D"

    steps = timing_node["evidence"]["steps"]
    step_names = {entry["step"] for entry in steps}

    assert step_names == _EXPECTED_TIMING_STEPS, (
        f"timing map step membership drifted -- got {sorted(step_names)}, "
        f"expected {sorted(_EXPECTED_TIMING_STEPS)}"
    )

    for entry in steps:
        assert isinstance(entry["ms"], (int, float)), entry
        assert entry["ms"] >= 0.0, entry

    # No duplicate step names within a single pass -- each named step ran
    # (and was measured) exactly once on this fresh/non-resumed/committed path.
    assert len(steps) == len(_EXPECTED_TIMING_STEPS)


# ---------------------------------------------------------------------------
# C7 (2026-07-23, docs/plans/2026-07-23-wsc-tail-slim-down.md) -- the step-1
# resolve's consumer enumeration found nothing genuinely dead to slim yet
# (see the comment above "Step 1: initial resolve" in wsc_tail.py), so this
# guards the ONE silent-flip risk the chunk's finding 17 named: a
# chain-terminal close via the DIRECT `find_all_consumed_handoffs` branch
# (Detector B's OWN branch is already covered by
# test_resolver_git_provenance.py::test_wsc_tail_step1_sees_detector_b_hit).
# ---------------------------------------------------------------------------


def test_disposition_chain_terminal_via_direct_consumed_handoff(wsc_tail_repo):
    """A live `claimed_by: <sid>` predecessor handoff on disk at invocation
    time (no Detector-B fallback involved -- `find_all_consumed_handoffs`
    finds it directly) resolves `disposition == "chain-terminal"`, and the
    surviving downstream consumer (the C5 stamp pass, permanent -- see C7's
    enumeration) stamps that SAME handoff, not some other/no handoff --
    proof `chain_terminal` was never silently flipped to "single-session"
    for this close."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    handoff_relpath = "state/handoffs/2026-07-15_090000_pred.md"
    repo.seed_handoff("2026-07-15_090000_pred.md", consumed_by=sid)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] in (0, 2), result
    assert result["disposition"] == "predecessor-consumed", result
    assert result["stamped"] == [handoff_relpath], result


# ---------------------------------------------------------------------------
# C8 (2026-07-23, docs/plans/2026-07-23-wsc-tail-slim-down.md, AC7) -- exit-2
# diagnostics name the actual failed tail item; the two labels that stay
# genuinely in-op after C7's slim (`commit_pipeline`, `cs_release_artifact`)
# are each named on failure rather than leaving `diagnostics` printable as a
# bare `[]` (the wart `coordinator/bin/wsc-tail.py`'s
# `result.get("diagnostics", result.get("tail_results"))` fallback can never
# reach, since the `diagnostics` key is always present on the wire payload).
# ---------------------------------------------------------------------------


def test_diagnostics_name_failing_commit_pipeline(wsc_tail_repo, monkeypatch):
    """A hard `commit_pipeline` failure (exit_code=1) is named in
    `diagnostics`, not left for the caller to dig out of `tail_results`
    alone."""
    repo = wsc_tail_repo
    sid = _unique_session_id()

    failed_outcome = make_pipeline_result(
        commit_failed=True,
        diagnostics=["forced failure for C8 regression test"],
    )
    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", lambda *_a, **_kw: failed_outcome)

    result = _run(
        wsc_tail_mod._handler(
            {"sid": sid, "subject": "workstream-complete: feature"},
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert result["diagnostics"], "diagnostics must not be empty on a commit_pipeline failure"
    assert any(
        d.startswith("commit_pipeline failed") for d in result["diagnostics"]
    ), result["diagnostics"]
    assert any("forced failure for C8 regression test" in d for d in result["diagnostics"])


def test_diagnostics_name_failing_cs_release_artifact(wsc_tail_repo, monkeypatch):
    """A `cs_release_artifact` failure -- with the main commit itself landing
    clean -- is named in `diagnostics`. Before C8, this was the exact `[]`
    wart: the op's own `diagnostics` list carried nothing (only
    `pipeline_result.diagnostics`, empty on a clean commit), so
    `wsc-tail.py`'s trampoline printed a bare `[]` while the real failure sat
    unexamined in `tail_results["cs_release_artifact"]["failed"]`."""
    repo = wsc_tail_repo
    sid = _unique_session_id()
    repo.write_plan("my-feature", roadmap_id="rm-1", stub_id="stub-1")

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    def _failing_cs_release(*_a: Any, **_kw: Any) -> dict:
        return {"acted": [], "skipped": [], "failed": ["cs_release_artifact: forced failure for C8 regression test"]}

    monkeypatch.setattr(wsc_tail_mod.tail_ops, "cs_release_artifact", _failing_cs_release)

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
                "governing_plan_slug": "my-feature",
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["committed_sha"] is not None, result
    assert result["exit_code"] == 2, result
    assert result["diagnostics"], "diagnostics must not be empty on a cs_release_artifact failure"
    assert any(
        d.startswith("cs_release_artifact failed") for d in result["diagnostics"]
    ), result["diagnostics"]
    assert any(
        "forced failure for C8 regression test" in d for d in result["diagnostics"]
    ), result["diagnostics"]


# ---------------------------------------------------------------------------
# Completion-entry scaffold-residue gate (bug 2026-07-28-workstream-complete-
# d-complete-entry-emi-f6be5553dee4): a fail-loud precommit refusal when a
# staged `archive/completed/` path still carries unfilled
# `coordinator-complete-entry` scaffold residue -- the literal `PLACEHOLDER`
# title, the `<!-- PROSE: ... -->` stub, or `nature: null` /
# `nature_inferred: true` left unset. Fixtures below are built from the REAL
# `coordinator_core.ops.coordinator_complete_entry.main` scaffold output
# (never a hand-written stand-in), so these tests cannot pass vacuously
# against a fixture that never resembled a real scaffold.
# ---------------------------------------------------------------------------


def _write_completion_scaffold(repo: "WscTailRepo", *, nature: str = "") -> str:
    """Scaffold a real `archive/completed/` entry via the production
    `coordinator-complete-entry` path (never hand-authored), and return its
    path relative to the repo root (posix-normalized, for use as a
    `stage_paths` entry)."""
    old_cwd = os.getcwd()
    os.chdir(str(repo.root))
    try:
        argv = ["--sid", _unique_session_id(), "--disposition", "single-session"]
        if nature:
            argv += ["--nature", nature]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = complete_entry_mod.main(argv)
        assert rc == 0, buf.getvalue()
        entry_path = Path(buf.getvalue().strip().splitlines()[0])
    finally:
        os.chdir(old_cwd)
    return str(entry_path.relative_to(repo.root)).replace(os.sep, "/")


def _fix_title(text: str) -> str:
    return "\n".join(
        (
            'title: "Ported the wsc_tail scaffold-residue gate"'
            if line.startswith("title:")
            else line
        )
        for line in text.splitlines()
    ) + "\n"


def _fix_prose(text: str) -> str:
    return text.replace(
        "<!-- PROSE: Replace this with a ≤8-sentence past-tense description "
        "of what shipped and why it matters. -->",
        "Shipped the completion-entry scaffold-residue gate; it now refuses "
        "a commit that still carries unfilled scaffold placeholders.",
    )


def _fix_nature(text: str) -> str:
    return text.replace("nature: null", "nature: bugfix").replace(
        "nature_inferred: true", "nature_inferred: false"
    )


def test_completion_scaffold_title_residue_alone_refuses_commit(wsc_tail_repo):
    """Title placeholder present, prose and nature already resolved ->
    refused, and the diagnostic names the file and the title residue."""
    repo = wsc_tail_repo
    relpath = _write_completion_scaffold(repo, nature="bugfix")
    full = repo.root / relpath
    full.write_text(_fix_prose(full.read_text(encoding="utf-8")), encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": _unique_session_id(),
                "subject": "workstream-complete: fixture",
                "stage_paths": [relpath],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert relpath in result["error"] or any(relpath in d for d in result["diagnostics"]), result
    assert any("title" in d and "placeholder" in d for d in result["diagnostics"]), result["diagnostics"]
    assert repo.porcelain(), "refused commit must leave the scaffold uncommitted"


def test_completion_scaffold_prose_residue_alone_refuses_commit(wsc_tail_repo):
    """Prose stub present, title and nature already resolved -> refused, and
    the diagnostic names the file and the prose residue."""
    repo = wsc_tail_repo
    relpath = _write_completion_scaffold(repo, nature="bugfix")
    full = repo.root / relpath
    full.write_text(_fix_title(full.read_text(encoding="utf-8")), encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": _unique_session_id(),
                "subject": "workstream-complete: fixture",
                "stage_paths": [relpath],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert any("PROSE" in d for d in result["diagnostics"]), result["diagnostics"]


def test_completion_scaffold_nature_residue_alone_refuses_commit(wsc_tail_repo):
    """`nature: null` / `nature_inferred: true` present, title and prose
    already resolved -> refused, and the diagnostic names the file and the
    nature residue."""
    repo = wsc_tail_repo
    relpath = _write_completion_scaffold(repo)  # no --nature -> residue left
    full = repo.root / relpath
    text = full.read_text(encoding="utf-8")
    text = _fix_title(text)
    text = _fix_prose(text)
    full.write_text(text, encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": _unique_session_id(),
                "subject": "workstream-complete: fixture",
                "stage_paths": [relpath],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert any("nature" in d for d in result["diagnostics"]), result["diagnostics"]


def test_completion_scaffold_fully_filled_in_commits_clean(wsc_tail_repo):
    """A fully hand-filled entry (title, prose, and nature all resolved) is
    NOT refused -- the gate is residue-specific, not a blanket refusal of
    every `archive/completed/` path."""
    repo = wsc_tail_repo
    relpath = _write_completion_scaffold(repo)  # no --nature -> starts with residue
    full = repo.root / relpath
    text = full.read_text(encoding="utf-8")
    text = _fix_title(text)
    text = _fix_prose(text)
    text = _fix_nature(text)
    full.write_text(text, encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": _unique_session_id(),
                "subject": "workstream-complete: fixture",
                "stage_paths": [relpath],
                "caller_paths": [relpath],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] in (0, 2), result
    assert result["committed_sha"] is not None, result


def test_completion_scaffold_gate_unaffected_when_no_archive_completed_path_staged(
    wsc_tail_repo,
):
    """A run whose staged paths never touch `archive/completed/` at all is
    entirely unaffected by the gate, even though the file scanner would find
    residue if pointed at the (untouched) scaffold living elsewhere in the
    tree."""
    repo = wsc_tail_repo
    # Scaffold a real entry but do NOT stage it -- only an unrelated file is staged.
    _write_completion_scaffold(repo)

    (repo.root / "tasks" / "feature").mkdir(parents=True)
    (repo.root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": _unique_session_id(),
                "subject": "workstream-complete: fixture",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=repo.common_dir,
        )
    )

    assert result["exit_code"] in (0, 2), result
    assert result["committed_sha"] is not None, result
