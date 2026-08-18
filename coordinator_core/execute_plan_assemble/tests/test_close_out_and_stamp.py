"""
coordinator_core.execute_plan_assemble.tests.test_close_out_and_stamp — direct
coverage for `close_out_and_stamp.py`.

Why this file exists: `close_out_and_stamp` had NO dedicated test file (grep
confirmed none, and `execute_plan_assemble/tests/` did not exist at all) and
was covered only transitively, through the shared `locate_fenced_block`
locator seam. That gap was not academic -- example-retrieval-repo-em reported a real
defect (commit `08cbf4bd`) where the locator's two missing hardening fixes
made this op refuse on every plan still carrying `coordinator-doc-new`'s
scaffolded template comment. The op's refusal drove EMs to hand-stamp
`status:` instead, which skips the chunk-completion cross-reference entirely
-- a *partially shipped* plan could be stamped `implemented` with no
mechanical check at all. That second-order effect (not just the locator bug
itself) is what this file pins down directly against the op.

Spec backlinks:
  coordinator_core/execute_plan_assemble/close_out_and_stamp.py (module under test)
  cross-repo/archive/2026-07-26-example-retrieval-repo-em-spine-locator-parity-gap.md (originating report)
  commit 08cbf4bd (the locator fix this op's refusal-on-template-comment regression is pinned against)

Fixture reuse (negative-spec, see fixture_expectations.py's own docstring):
this file does NOT mint a parallel plan-tasks-spine fixture corpus. Every
plan-body shape used below is the CANONICAL corpus at
coordinator/bin/tests/fixtures/plan-tasks-spine/ (consolidated in
b63b922a) -- reused as-is because every fixture needed here (a LOCATED
spine with mixed deferred/non-deferred rows, the two MALFORMED shapes, the
ABSENT shape, and the template-comment regression shape) already exists
there, each already carrying a `status: draft` frontmatter field this op's
stamp step needs. No fixture in this file's own directory.

Isolation: every test that exercises the op's own `_run_git`/git-log path or
its stamp step runs inside a freshly `git init`-ed `tmp_path` (never this
repo's own working tree), and any test that reaches
`archive_stamp.cs_stamp_plan_implemented` (which resolves its `--plan` path
relative to the process's OWN cwd, not the `repo_root=` the op was given --
see `_run_close_out` below) chdirs into that tmp_path first via
`monkeypatch.chdir`.

Commit-leg tests (Defect 3, 2026-07-27): the op's commit leg now runs
in-process through `run_commit_pipeline` (see `close_out_and_stamp.py`'s
own docstring), never a `coordinator-safe-commit` subprocess -- so nothing
here mocks the commit step. Every test below that reaches the commit leg
lands a REAL commit against its own throwaway `tmp_path` repo (never this
repo's working tree) and asserts on real git state (`_head_subject`,
`_committed_files_at_head`) rather than a faked subprocess call. This is a
deliberate strengthening over the prior mocked-commit coverage: it is also
the seam that proves the multi-live-session regression this defect fixes
(`TestCommitLegConcurrency` below) -- a real `coordinator-safe-commit`
shell-out would refuse under concurrency; `run_commit_pipeline`'s
explicit-pathspec stage never trips that gate at all.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

# Declared, not excused: this file exercises the op's own `_run_git`/git-log path
# and its real, in-process commit leg (`run_commit_pipeline`, per this file's own
# module docstring "Commit-leg tests" section) -- deliberately never mocked, since
# proving the commit leg lands a real commit under concurrency is exactly what
# distinguishes it from the prior mocked-commit coverage and the shell-out
# `coordinator-safe-commit` regression it replaced. Every test runs inside a
# freshly `git init`-ed `tmp_path`, never this repo's own working tree, so no
# shared-state hoist is possible or attempted. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = (
    _REPO_ROOT / "coordinator" / "bin" / "tests" / "fixtures" / "plan-tasks-spine"
)

_FIXTURE_VALID_SPINE = _FIXTURES_DIR / "valid-spine-with-deferrals.md"
_FIXTURE_ZERO_BLOCKS = _FIXTURES_DIR / "zero-fenced-blocks.md"
_FIXTURE_MULTI_BLOCKS = _FIXTURES_DIR / "multiple-fenced-blocks.md"
_FIXTURE_HEADING_NO_FENCE = _FIXTURES_DIR / "heading-without-fence.md"
_FIXTURE_TEMPLATE_COMMENT = _FIXTURES_DIR / "template-comment-with-deferral.md"

# Each fixture plan's own `deliverable_id:` frontmatter value (Defect fix,
# 2026-07-27 -- see close_out_and_stamp.py's own docstring § Deliverable
# scoping): `_commit_chunk` below now needs the CORRECT id per fixture to
# land an ATTRIBUTABLE commit -- the wrong id (or none) exercises the fix's
# own false-negative-by-design behavior instead.
_DLV_VALID_SPINE = "dlv-fixture-valid-spine-000001"
_DLV_TEMPLATE_COMMENT = "dlv-fixture-template-comment-000001"
_DLV_DISPOSITION = "dlv-fixture-disposition-000001"


# ---------------------------------------------------------------------------
# Git/repo test-isolation helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t"], root)
    _run_git(["config", "user.name", "test"], root)


def _set_origin_main(root: Path, sha: Optional[str] = None) -> None:
    """Points `refs/remotes/origin/main` at `sha` (HEAD by default) without
    an actual remote/fetch -- `_chunk_evidence_log_range`'s `git merge-base
    origin/main HEAD` only needs the REF to resolve, not a configured
    remote, so this is sufficient to exercise the branch-divergence range
    bound in an isolated `tmp_path` repo with no network."""
    target = sha if sha is not None else _head_sha(root)
    _run_git(["update-ref", "refs/remotes/origin/main", target], root)


def _seed_plan(root: Path, fixture_path: Path, dest_name: str = "plan.md") -> Path:
    """Copies `fixture_path`'s content into `root/dest_name`, git-adds and
    commits it (subject "seed" -- deliberately not chunk-id-shaped, so it
    never matches `_CHUNK_SUBJECT_RE` and never counts as a shipped chunk).
    Returns the seeded plan's path.
    """
    dest = root / dest_name
    dest.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


def _commit_chunk(
    root: Path,
    plan_rel: str,
    chunk_id: str,
    *,
    deliverable_id: Optional[str] = None,
) -> None:
    """Lands a commit touching `plan_rel` whose subject starts with
    `<chunk_id>: `, per the DEC-2 recovery-triple convention
    `_committed_chunk_ids` greps for. A trivial trailing-comment append is
    required -- `git log -- <path>` only lists commits that actually change
    the tree entry at that path, so a same-content commit would never show
    up in the op's own git-log query.

    `deliverable_id`, when given, lands a `Deliverable-Id: <value>` git
    trailer on the commit (Defect fix, 2026-07-27 -- see
    close_out_and_stamp.py's own docstring § Deliverable scoping). This is
    what makes the commit ATTRIBUTABLE to a specific plan's own
    completeness query -- pass the CLOSING plan's own `deliverable_id:`
    frontmatter value to land a matching, countable commit. Omitted
    (`None`, the default) deliberately lands an UNTRAILERED commit -- the
    shape every pre-trailer-convention commit on the branch actually has,
    and the shape `TestDeliverableScoping.
    test_untrailered_commit_with_matching_subject_does_not_count` exercises
    directly to prove such a commit is never counted as evidence.
    """
    plan_file = root / plan_rel
    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", plan_rel], root)
    message_args = ["-m", f"{chunk_id}: land chunk"]
    if deliverable_id:
        message_args += ["-m", f"Deliverable-Id: {deliverable_id}"]
    _run_git(["commit", "-q", *message_args], root)


def _head_sha(root: Path) -> str:
    result = _run_git(["rev-parse", "HEAD"], root)
    return result.stdout.strip()


def _head_subject(root: Path) -> str:
    result = _run_git(["log", "-1", "--format=%s", "HEAD"], root)
    return result.stdout.strip()


def _committed_files_at_head(root: Path) -> list[str]:
    result = _run_git(["show", "--name-only", "--pretty=format:", "HEAD"], root)
    return [line for line in result.stdout.splitlines() if line]


def _run_close_out(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    plan_rel: str,
    *,
    dry_run: bool = False,
) -> tuple[int, dict, str]:
    """Runs `close_out_and_stamp(plan_rel, repo_root=root)` with cwd set to
    `root`, then returns `(exit_code, result, pre_call_head_sha)`.

    The chdir is load-bearing, not cosmetic: `archive_stamp.
    cs_stamp_plan_implemented` -> `plan_status_transition.main` resolves its
    `--plan <path>` argument via a plain `os.path.exists`/`open` against the
    process's OWN cwd -- it does not accept or consult the `repo_root=` this
    op was given. Without the chdir, a `plan_path_rel` like "plan.md" would
    resolve against whatever directory pytest happened to be invoked from,
    not `root`, and the stamp step would spuriously fail with "plan not
    found" on every full-shipped test.

    The commit leg is NOT mocked (Defect 3 fix, 2026-07-27) -- it runs a
    REAL `run_commit_pipeline` commit against `root`'s own git history. The
    `pre_call_head_sha` return lets callers assert whether a new commit
    landed (`_head_sha(root) != pre_call_head_sha`) without depending on any
    faked call-recording.

    `dry_run` (2026-08-04) is forwarded verbatim to `close_out_and_stamp` --
    see `TestDryRun` below for the coverage this parameter exists for."""
    pre_call_head_sha = _head_sha(root)
    monkeypatch.chdir(root)
    exit_code, result = coas.close_out_and_stamp(plan_rel, repo_root=root, dry_run=dry_run)
    return exit_code, result, pre_call_head_sha


def _read_status(plan_file: Path) -> Optional[str]:
    text = plan_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def _spine_rows(plan_file: Path) -> list[dict]:
    """Parses the plan's current `## Tasks` spine off disk -- used by the
    AC7/AC8/AC9 tests to assert on `disposition`/`disposition_ref` after a
    `close_out_and_stamp` call has (or has not) auto-resolved a row."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import locate_fenced_block

    text = plan_file.read_text(encoding="utf-8")
    located = locate_fenced_block(text)
    assert located.status == LocateStatus.LOCATED
    return yaml.safe_load(located.body) or []


_PLAN_TEMPLATE = """---
title: "Fixture plan — disposition resolution model"
created: 2026-07-27
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-07-27"
plan_id: "pln-fixture-disposition-000001"
deliverable_id: "dlv-fixture-disposition-000001"
---

# Fixture plan — disposition resolution model

## Tasks

```yaml plan-tasks
{rows}
```
"""


def _seed_disposition_plan(
    root: Path, rows_yaml: str, status: str = "executing", dest_name: str = "plan.md"
) -> Path:
    """Seeds a plan whose `## Tasks` spine carries `disposition`-shaped
    rows (C7, plan-line-item-resolution-model) -- the canonical
    `plan-tasks-spine` fixture corpus this file otherwise reuses predates
    D1's `disposition` field entirely (every row there is legacy
    `deferred`-shaped only), so AC7/AC8/AC9 coverage needs its own
    minimal, inline plan text rather than a fixture file."""
    text = _PLAN_TEMPLATE.format(status=status, rows=rows_yaml)
    dest = root / dest_name
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


# ===========================================================================
# _determine_shipped -- the chunk-completion cross-reference itself
# ===========================================================================


class TestDetermineShipped:
    def test_full_shipped_spine_reports_shipped_with_no_missing(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert shipped is True
        assert missing == []
        assert error is None

    def test_partially_shipped_spine_is_refused_and_names_missing_chunks(self, tmp_path):
        """The highest-value assertion in this file: hand-stamping `status:`
        skips exactly this check, letting a partially-shipped plan be marked
        `implemented` with no mechanical verification at all -- this pins
        that the op's own oracle correctly refuses that outcome.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        # C2a and C2b deliberately left uncommitted.

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert shipped is False
        assert sorted(missing) == ["C2a", "C2b"]
        assert error is None

    def test_deferred_rows_never_count_toward_missing(self, tmp_path):
        """valid-spine-with-deferrals.md carries D1/D2/D3 (all `deferred:
        true`) alongside C1/C2a/C2b. Committing only the non-deferred rows
        must still report full-shipped -- deferred rows are excluded from
        the oracle entirely, not merely satisfied some other way.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert shipped is True
        assert missing == []
        assert "D1" not in missing and "D2" not in missing and "D3" not in missing

    def test_absent_spine_is_treated_as_full_shipped(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_ZERO_BLOCKS)

        text = plan_file.read_text(encoding="utf-8")
        # Confirm the fixture is genuinely ABSENT under the shared locator
        # before asserting on the op's own downstream handling of it.
        from coordinator_core.frontmatter.body_blocks import locate_fenced_block

        assert locate_fenced_block(text).status == LocateStatus.ABSENT

        shipped, missing, join_provenance, error = coas._determine_shipped(text, "plan.md", root)
        assert shipped is True
        assert missing == []
        assert error is None

    @pytest.mark.parametrize(
        "fixture_path",
        [_FIXTURE_MULTI_BLOCKS, _FIXTURE_HEADING_NO_FENCE],
        ids=["multiple-fenced-blocks", "heading-without-fence"],
    )
    def test_malformed_spine_fails_loud_and_names_the_spine(self, tmp_path, fixture_path):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, fixture_path)

        text = plan_file.read_text(encoding="utf-8")
        from coordinator_core.frontmatter.body_blocks import locate_fenced_block

        assert locate_fenced_block(text).status == LocateStatus.MALFORMED

        shipped, missing, join_provenance, error = coas._determine_shipped(text, "plan.md", root)
        assert shipped is False
        assert missing == []
        assert error is not None
        # "loud" -- the message names the offending spine (its own relative
        # path), not a generic "something went wrong".
        assert "plan.md" in error
        assert "malformed" in error.lower()
        assert "## Tasks spine" in error


# ===========================================================================
# close_out_and_stamp -- the full orchestration (locate + stamp + commit)
# ===========================================================================


class TestCloseOutAndStamp:
    def test_full_shipped_stamps_and_commits(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is True
        assert result["missing_chunk_ids"] == []
        assert _read_status(plan_file) == "implemented"

        # A real commit landed (Defect 3 fix): the stamp changed plan.md, so
        # the explicit-pathspec stage_paths=["plan.md"] had something to
        # commit.
        #
        # DR-272 interaction (2026-08-05/06): `plan_status_transition.
        # _commit_plan_flip` now commits its own real status flip directly,
        # under its own subject line ("plan-status-transition: stamp status
        # ... on plan.md"), BEFORE this op's own commit leg gets a chance to
        # stage anything -- see `_stage_paths_committed_already`'s docstring.
        # This op's composed "shipped end-to-end, stamped implemented"
        # subject is therefore never the one that actually lands here; the
        # commit this op REPORTS (`result["commit"]["committed_sha"]`) is
        # the already-landed HEAD sha, whichever op's name is on its
        # subject -- assert on that reporting contract, not on a subject
        # line this op no longer authors for the stamped path.
        assert _head_sha(root) != pre_head
        assert result["commit"]["committed_sha"] == _head_sha(root)
        assert "plan-status-transition" in _head_subject(root)
        assert _committed_files_at_head(root) == ["plan.md"]


class TestCloseOutReachesSharedCascadeEntrypoint:
    """docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6
    Addendum Q4: `close_out_and_stamp` composes over `archive_stamp.
    cs_stamp_plan_implemented` -> `plan_status_transition.main()` ->
    `plan_status_transition._stamp_implemented` -> `plan_status_transition.
    _run_cascade` for its `implemented` stamp, rather than hand-rolling a
    second cascade trigger of its own (this module's own "Composition, not
    duplication" docstring section) -- so this close-out path and the
    DoE-side polyglot trampoline's direct `plan-status-transition
    stamp-implemented` invocation both fire the SAME `deliverable.
    cascade_terminal` op, never two independent implementations that could
    disagree (mirrors `test_cockpit_ground_truth_regression.
    test_both_triggers_resolve_the_identical_entrypoint_object`'s own
    object-identity style of assertion for the other two triggers --
    outcome-agreement alone would also pass against two independently-
    written cascades that happen to concur, which is not what this
    proves).

    Asserts the SHARED entrypoint (`plan_status_transition._run_cascade`)
    is actually reached, via a counting/recording monkeypatch -- not merely
    that the plan's own `status:` field ends up `implemented` (already
    covered by `TestCloseOutAndStamp.test_full_shipped_stamps_and_commits`
    above)."""

    @staticmethod
    def _patch_cascade_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Optional[str]]]:
        """Patches `plan_status_transition._run_cascade` itself (not the
        `deliverable_cascade._handler` it wraps) -- `_stamp_implemented`
        calls `_run_cascade(...)` as a bare name inside
        `coordinator_core.ops.plan_status_transition`'s own module
        namespace, so patching that module's attribute is what a real
        in-process call actually resolves against at call time, mirroring
        `test_cockpit_ground_truth_regression.py`'s own
        `plan_status_transition_mod, "_run_cascade"` patch target.
        Returns the `calls` list the spy appends
        `(plan_path, deliverable_id)` to, in call order -- never raises or
        forwards to the real cascade, so no real `deliverable.
        cascade_terminal` write happens under this spy (the shared-entrypoint
        REACH is what's being proved here, not the cascade op's own
        per-target predicate/provenance behavior -- that is
        `test_deliverable_cascade.py`'s job, per this class's own
        docstring)."""
        import coordinator_core.ops.plan_status_transition as pst_mod

        calls: list[tuple[str, Optional[str]]] = []

        def _spy(plan_path: str, deliverable_id: Optional[str]) -> int:
            calls.append((plan_path, deliverable_id))
            return 0

        monkeypatch.setattr(pst_mod, "_run_cascade", _spy)
        return calls

    def test_implemented_transition_reaches_shared_cascade_entrypoint(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        calls = self._patch_cascade_spy(monkeypatch)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"
        assert len(calls) == 1, (
            "close_out_and_stamp's implemented transition must reach "
            "plan_status_transition._run_cascade exactly once, the SAME "
            "shared entrypoint the other two cascade triggers use"
        )
        called_plan_path, called_deliverable_id = calls[0]
        assert called_plan_path.endswith("plan.md")
        # `_run_cascade` receives `_state["deliverable_id"]` in its
        # COMPARISON-SAFE form -- `_plan_deliverable_id` reads it through
        # `read_fm_field_unquoted`, so surrounding quotes and any trailing
        # comment are already stripped by the time it reaches this call.
        # The fixture's quoted `deliverable_id:` value therefore arrives
        # bare, NOT quote-preserved: every downstream consumer joins on this
        # value (the `Deliverable-Id` trailer equality the chunk-evidence
        # join depends on), and a quoted id would match no trailer at all.
        # This assertion previously expected the raw quoted form, which the
        # unquoted read superseded without the expectation following it.
        assert called_deliverable_id == _DLV_VALID_SPINE

    def test_landed_transition_does_not_reach_the_cascade_entrypoint(
        self, tmp_path, monkeypatch
    ):
        """`landed` is not `implemented` (constraint 3): a plan that stamps
        `landed` (code in, a row still `open`) must never reach the cascade
        entrypoint at all -- only the terminal `implemented` transition
        does. Mirrors `TestResolutionModel.
        test_shipped_with_a_row_still_open_stamps_landed_not_implemented`'s
        own forced-`shipped` setup."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Never actually committed under this op's own heuristic.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        monkeypatch.setattr(
            coas,
            "_determine_shipped",
            lambda *args, **kwargs: (True, [], coas.JOIN_PROVENANCE_JOINED, None),
        )
        calls = self._patch_cascade_spy(monkeypatch)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "landed"
        assert _read_status(plan_file) == "landed"
        assert calls == [], (
            "a `landed` stamp must never reach the terminal-state cascade "
            "entrypoint -- only `implemented` does"
        )

    def test_repeat_invocation_against_already_implemented_plan_does_not_refire(
        self, tmp_path, monkeypatch
    ):
        """AC6i (mirrored here for the close-out trigger specifically):
        re-invoking close-out against an already-`implemented` plan is a
        genuine end-to-end no-op -- the idempotent no-op branch of
        `_stamp_implemented` never calls `_run_cascade` at all (see that
        module's own docstring), so a second `close_out_and_stamp` call
        must not add a second entry to the spy's call list."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        calls = self._patch_cascade_spy(monkeypatch)

        first_exit, first_result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")
        assert first_exit == coas.EXIT_OK, first_result
        assert _read_status(plan_file) == "implemented"
        assert len(calls) == 1

        second_exit, second_result, pre_second_head = _run_close_out(
            monkeypatch, root, "plan.md"
        )

        assert second_exit == coas.EXIT_OK, second_result
        assert second_result["stamped"] is False
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) == pre_second_head, (
            "a repeat invocation against an already-implemented plan must "
            "be a genuine end-to-end no-op -- no second commit either"
        )
        assert len(calls) == 1, (
            "the idempotent no-op path must never re-fire the cascade "
            "entrypoint (AC6i)"
        )


class TestPostCommitTailStubCloseReach:
    """AC4 (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md
    § C5): `/execute-plan`'s close-out and `/mise-en-place`'s per-baton tail
    both land through `coordinator/bin/close-out-and-stamp.py` ->
    `close_out_and_stamp()` -- the SAME call path -- so exercising that one
    function proves reach for both ceremonies at once. (The third ceremony,
    `ceremony.wsc_tail`, already has its own real-stub coverage in
    `test_wsc_tail_parity.py::test_origin_stub_close_runs_on_ac18_resume`
    et al.)

    Mirrors this repo's own established reach-proving convention (see
    `test_origin_stub_close_failure_does_not_fail_the_tail` in
    `test_wsc_tail_parity.py`): monkeypatch the injected
    `_close_origin_stub_handler` module-global to a call-recording fake and
    assert the CALL happens on a successful commit -- proving reach into
    `post_commit_tail`'s composition, never re-deriving the join/scan/guard
    logic itself (that is `handoff_close_origin_stub.py`'s own coverage).
    """

    def test_full_shipped_close_out_reaches_stub_close_leg(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        calls: list[dict] = []

        async def _fake_close_origin_stub_handler(params: dict, common_dir: Path) -> dict:
            calls.append(params)
            return {
                "exit_code": 0,
                "closed": [],
                "skipped": [
                    {"roadmap_id": "rm-1", "stub_id": "stub-1", "reason": "test-reach"}
                ],
            }

        monkeypatch.setattr(
            coas, "_close_origin_stub_handler", _fake_close_origin_stub_handler
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["commit"]["committed_sha"] is not None

        # The composed leg was actually invoked, with the join inputs
        # `_run_origin_stub_close` derives from this ceremony's own plan
        # path and the just-landed commit sha -- not merely a result key
        # that happens to be present.
        assert len(calls) == 1
        assert calls[0]["plan_path"] == "docs/plans/plan.md"
        assert calls[0]["sha"] == result["commit"]["committed_sha"]
        assert result["origin_stub_close"]["skipped"] == ["rm-1:stub-1:test-reach"]

    def test_no_commit_never_reaches_stub_close_leg(self, tmp_path, monkeypatch):
        """A REPEATED close-out against an already-fully-resolved plan
        writes nothing on its second pass (`wrote_anything is False` --
        both the stamp and AC8's auto-resolve are documented no-ops once
        already applied) and skips the commit leg entirely -- reach into
        `post_commit_tail` is gated on a REAL `committed_sha`, never
        attempted speculatively."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        # First pass: real run, no mock -- stamps, auto-resolves, and
        # commits for real, fully resolving the plan.
        first_exit_code, first_result, _pre = _run_close_out(monkeypatch, root, "plan.md")
        assert first_exit_code == coas.EXIT_OK
        assert first_result["commit"]["committed_sha"] is not None

        calls: list[dict] = []

        async def _fake_close_origin_stub_handler(params: dict, common_dir: Path) -> dict:
            calls.append(params)
            return {"exit_code": 0, "closed": [], "skipped": []}

        monkeypatch.setattr(
            coas, "_close_origin_stub_handler", _fake_close_origin_stub_handler
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["commit"]["committed_sha"] is None
        assert calls == []
        assert result["origin_stub_close"] == {"acted": [], "skipped": [], "failed": []}

    def test_landed_sha_unverified_does_not_raise_and_records_skip(
        self, tmp_path, monkeypatch
    ):
        """W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md):
        a commit that LANDED but whose sha could not be resolved
        (`commit_pipeline.PipelineResult.sha_unverified=True`,
        `commit_failed=False`, `committed_sha=None`) must NOT hit the
        `if pipeline_result.commit_failed:` raise branch -- that would
        report durable history as a failure, the exact bug this plan
        closes. `origin_stub_close` must record the reach was skipped
        (needs a real sha), not silently stay at its empty default and not
        crash attempting the reach with `committed_sha=None`.

        Mechanism check (red-proof): reverting the `elif pipeline_result.
        sha_unverified:` branch back out (so only `if pipeline_result.
        committed_sha:` remains) makes `origin_stub_close` fall through to
        the bare `{"acted": [], "skipped": [], "failed": []}` default
        instead of naming the skip reason -- this test's `skipped`
        assertion then fails. Verified by hand: temporarily removing that
        elif and re-running reproduces exactly that failure (an empty
        `skipped` list); restored immediately after.
        """
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        from types import SimpleNamespace

        fake_result = SimpleNamespace(
            committed_sha=None,
            pushed=None,
            push_status=coas.PUSH_STATUS_NOT_ATTEMPTED,
            pushed_range=None,
            pushed_count=None,
            commit_failed=False,
            sha_unverified=True,
            diagnostics=[
                "commit: landed but sha verification failed -- HEAD unresolvable",
            ],
        )
        monkeypatch.setattr(coas, "run_commit_pipeline", lambda *a, **k: fake_result)
        # Force the REAL `run_commit_pipeline` call site (rather than the
        # DR-272 `_stage_paths_committed_already` shortcut, which reports
        # the stamp step's OWN already-landed HEAD sha and never calls
        # `run_commit_pipeline` at all -- see that function's own docstring)
        # so this test actually exercises the branch under test.
        monkeypatch.setattr(coas, "_stage_paths_committed_already", lambda *a, **k: False)

        calls: list[dict] = []

        async def _fake_close_origin_stub_handler(params: dict, common_dir: Path) -> dict:
            calls.append(params)
            return {"exit_code": 0, "closed": [], "skipped": []}

        monkeypatch.setattr(
            coas, "_close_origin_stub_handler", _fake_close_origin_stub_handler
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        # Never the raise/EXIT_BUSINESS_FAIL branch -- the commit landed.
        assert exit_code == coas.EXIT_OK
        assert "error" not in result
        assert result["commit"]["committed_sha"] is None
        assert result["commit"]["commit_failed"] is False
        assert result["commit"]["sha_unverified"] is True

        # The stub-close reach was never attempted (no real sha to join on)
        # -- but the gap is NAMED, not a silent empty default.
        assert calls == []
        assert result["origin_stub_close"]["acted"] == []
        assert result["origin_stub_close"]["failed"] == []
        assert len(result["origin_stub_close"]["skipped"]) == 1
        assert "sha-unverified" in result["origin_stub_close"]["skipped"][0]

        # Review: coordinator:code-reviewer -- folded from the deleted
        # test_pipeline_result_missing_push_status_degrades_safely (C7b),
        # whose name still claimed a missing-push_status degradation path
        # that C2 removed; this fixture already builds an equivalent
        # sha_unverified=True double, it only lacked these assertions.
        assert result["commit"]["push_status"] == coas.PUSH_STATUS_NOT_ATTEMPTED
        assert result["commit"]["pushed_range"] is None
        assert result["commit"]["pushed_count"] is None


class TestCloseOutAndStampContinued:
    """Continuation of `TestCloseOutAndStamp` above -- split into a second
    class (rather than reopened under the same name, which Python would
    silently drop from collection) so `TestPostCommitTailStubCloseReach`
    (AC4) could land adjacent to the reach it proves without renaming every
    pre-existing method below."""

    def test_partially_shipped_skips_stamp_but_auto_resolve_still_commits(
        self, tmp_path, monkeypatch
    ):
        """On the halted path the STATUS stamp step never runs (status stays
        `draft`) -- but AC8's auto-resolve of the one committed-but-open row
        (`C1`) is independent of overall `shipped` status (C7,
        plan-line-item-resolution-model, 2026-07-27): `C1`'s row is still
        `open` and IS committed, so it gets auto-resolved to `coded` with its
        commit sha in `disposition_ref`, and that write IS something this op
        made, so the commit leg does land it -- superseding the prior
        "this op made NO change of its own" premise, which predates AC8.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        # C2a and C2b deliberately left uncommitted.

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is False
        assert result["stamped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C2a", "C2b"]
        # status: NOT flipped -- the stamp step never runs on the halted path.
        assert _read_status(plan_file) == "draft"

        # AC8: C1's row was auto-resolved even though the plan overall halted.
        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        assert c1_row["disposition"] == "coded"
        assert c1_row["disposition_ref"]
        c2a_row = next(row for row in rows if row.get("id") == "C2a")
        assert c2a_row.get("disposition", "open") == "open"

        assert result["commit"]["commit_failed"] is False
        assert result["commit"]["committed_sha"] is not None
        assert _head_sha(root) != pre_head
        assert _committed_files_at_head(root) == ["plan.md"]

    def test_partially_shipped_with_nothing_committed_at_all_is_a_genuine_noop(
        self, tmp_path, monkeypatch
    ):
        """Pins the OTHER branch of the `wrote_anything = stamped or
        auto_resolved` gate the sibling test above exercises: a halted plan
        where NO spine row has a matching chunk-commit at all has nothing
        for AC8's auto-resolve step to find, so `auto_resolved` is also
        `False` -- this op made NO change of its own to commit at all, and
        the commit leg is skipped entirely (`committed_sha=None`,
        `commit_failed=False`), never attempted-and-caught. This is the
        original Defect-3 no-op guarantee (2026-07-27): it must survive
        AC8's later widening of the gate to also cover the auto-resolve
        write, and this test is what pins that survival now that the
        gate has two live branches instead of one. Deliberately the mirror
        of `test_partially_shipped_skips_stamp_but_auto_resolve_still_commits`
        above -- same halted `shipped=False`/`stamped=False` shape, but no
        commit fires here because neither disjunct of `wrote_anything` is
        `True`.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # No chunk commits landed at all -- C1/C2a/C2b all deliberately
        # left uncommitted, so there is no committed-but-open row for AC8's
        # auto-resolve step to find either.

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is False
        assert result["stamped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C1", "C2a", "C2b"]
        # status: NOT flipped -- the stamp step never runs on the halted path.
        assert _read_status(plan_file) == "draft"

        # C2 fix (2026-08-06): this run's own `close_out_last_partial:`
        # evaluation stamp (see `_stamp_close_out_partial_evaluation`) IS a
        # real write this op made, so `wrote_anything` is now `True` here
        # and a commit fires -- the "nothing of this op's own to commit"
        # no-op this test used to pin no longer applies to the FIRST
        # evaluation of a halted plan; it still applies to every REPEAT
        # evaluation (idempotent-by-presence), which is not exercised here.
        assert result["partial_evaluation_stamped"] is True
        assert result["commit"]["commit_failed"] is False
        assert result["commit"]["committed_sha"] is not None
        assert _head_sha(root) != pre_head

    @pytest.mark.parametrize(
        "fixture_path",
        [_FIXTURE_MULTI_BLOCKS, _FIXTURE_HEADING_NO_FENCE],
        ids=["multiple-fenced-blocks", "heading-without-fence"],
    )
    def test_malformed_spine_refuses_before_any_stamp_or_commit(
        self, tmp_path, monkeypatch, fixture_path
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, fixture_path)
        original_text = plan_file.read_text(encoding="utf-8")

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL
        assert "plan.md" in result["error"]
        assert "malformed" in result["error"].lower()
        # Neither the stamp step nor the commit step ever ran.
        assert plan_file.read_text(encoding="utf-8") == original_text
        assert _head_sha(root) == pre_head

    def test_absent_spine_is_treated_as_full_shipped_but_not_stamped(
        self, tmp_path, monkeypatch
    ):
        """False-positive-stamp incident fix: `shipped=True` on the
        no-spine/no-ledger branch no longer implies an evidence-backed
        stamp -- `join_provenance` reports `JOIN_PROVENANCE_NO_EVIDENCE_
        SOURCE` (no spine parse, no git-log query, no ledger read ever ran)
        and the stamping gate refuses to act on it, leaving the plan's own
        status field untouched."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_ZERO_BLOCKS)
        original_status = _read_status(plan_file)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is False
        assert result["missing_chunk_ids"] == []
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_NO_EVIDENCE_SOURCE
        assert _read_status(plan_file) == original_status
        assert _head_sha(root) == pre_head


# ===========================================================================
# --dry-run (2026-08-04): a caller with no way to observe this ceremony's
# verdict short of MUTATING had no choice but to run the mutating path
# purely to read it -- and did, stamping/committing/pushing to a shared
# branch as a side effect of a read (see close_out_and_stamp.py's own
# docstring and coordinator/bin/close-out-and-stamp.py's usage text for the
# incident this closes). This suite pins: the dry verdict equals the live
# verdict, and dry-run writes NOTHING (no stamp, no plan-body disposition
# backfill, no commit, no push).
# ===========================================================================


class TestDryRun:
    def test_dry_run_on_full_shipped_plan_reports_shipped_and_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)
        original_bytes = plan_file.read_bytes()

        exit_code, result, pre_head = _run_close_out(
            monkeypatch, root, "plan.md", dry_run=True
        )

        assert exit_code == coas.EXIT_OK
        assert result["dry_run"] is True
        assert result["shipped"] is True
        assert result["stamped"] is True
        assert result["status_target"] == "implemented"
        assert result["missing_chunk_ids"] == []

        # Byte-identical plan file, and HEAD did not move -- "no exception
        # raised" proves nothing here; this is the actual contract.
        assert plan_file.read_bytes() == original_bytes
        assert _read_status(plan_file) == "draft"
        assert _head_sha(root) == pre_head

        # No commit was attempted at all.
        assert result["commit"]["committed_sha"] is None
        assert result["commit"]["commit_failed"] is False

    def test_dry_run_on_partial_plan_reports_same_missing_ids_and_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        # C2a and C2b deliberately left uncommitted -- same halted shape as
        # test_partially_shipped_skips_stamp_but_auto_resolve_still_commits,
        # whose LIVE run auto-resolves C1's row and commits it.
        original_bytes = plan_file.read_bytes()

        exit_code, result, pre_head = _run_close_out(
            monkeypatch, root, "plan.md", dry_run=True
        )

        assert exit_code == coas.EXIT_OK
        assert result["dry_run"] is True
        assert result["shipped"] is False
        assert result["stamped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C2a", "C2b"]

        # AC8's auto-resolve backfill did NOT persist: C1's row is still
        # `open` on disk, and the plan file is byte-identical to before the
        # call -- unlike the live counterpart test, which DOES write this.
        assert plan_file.read_bytes() == original_bytes
        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        assert c1_row.get("disposition", "open") == "open"
        assert _read_status(plan_file) == "draft"
        assert _head_sha(root) == pre_head

        assert result["commit"]["committed_sha"] is None
        assert result["commit"]["commit_failed"] is False

    def test_dry_and_live_verdicts_agree_on_the_same_fixture(self, tmp_path, monkeypatch):
        """Same fixture, same chunk commits, seeded into TWO independent
        repos (one dry-run, one live) -- pins requirement 2: "a dry run
        that computes a different answer than the real one is worse than
        no dry run at all"."""
        dry_root = tmp_path / "dry"
        live_root = tmp_path / "live"
        dry_root.mkdir()
        live_root.mkdir()

        for root in (dry_root, live_root):
            _init_repo(root)
            _seed_plan(root, _FIXTURE_VALID_SPINE)
            _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
            # C2a/C2b deliberately left uncommitted in both -- a partial,
            # not full-shipped, verdict exercises more of the shared
            # decision surface (status_target=None, missing_chunk_ids,
            # AND the AC8 auto-resolve computation) than the full-shipped
            # case alone would.

        dry_exit, dry_result, _ = _run_close_out(monkeypatch, dry_root, "plan.md", dry_run=True)
        live_exit, live_result, _ = _run_close_out(monkeypatch, live_root, "plan.md", dry_run=False)

        assert dry_exit == live_exit == coas.EXIT_OK
        # Every verdict field the caller actually reads must agree --
        # `dry_run` and `commit` are the only two fields excluded from this
        # comparison (a dry run never attempts a commit, by design; a
        # commit's own sha is inherently run-specific either way).
        compared_keys = [
            "shipped",
            "stamped",
            "status_target",
            "missing_chunk_ids",
            "deliverable_id_mismatch",
            "disposition_ref_rejections",
            "open_chunk_ids",
            "skipped_sibling_repos",
        ]
        for key in compared_keys:
            assert dry_result[key] == live_result[key], key
        # `message` differs only by the dry-run suffix this op appends.
        _DRY_SUFFIX = " [dry-run: no write/commit performed]"
        assert dry_result["message"].endswith(_DRY_SUFFIX)
        assert dry_result["message"][: -len(_DRY_SUFFIX)] == live_result["message"]

    def test_dry_run_does_not_regress_the_existing_live_path(self, tmp_path, monkeypatch):
        """The bare positional call (no `dry_run` kwarg at all) must still
        mutate exactly as before this fix -- pins AC5/requirement 4's "no
        existing caller silently stops stamping"."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["dry_run"] is False
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head


class TestMainCliDryRunFlag:
    """`main(argv)` argument-parsing coverage for the `--dry-run` flag
    itself -- the CLI's pre-existing "strictly positional, errors on extra
    arguments" contract (requirement 6) must survive extending it."""

    def test_dry_run_flag_is_accepted_alongside_the_positional_plan_path(
        self, tmp_path, monkeypatch, capsys
    ):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)
        monkeypatch.chdir(root)
        pre_head = _head_sha(root)

        exit_code = coas.main(["plan.md", "--dry-run"])

        assert exit_code == coas.EXIT_OK
        assert _head_sha(root) == pre_head
        out = capsys.readouterr().out
        assert '"dry_run": true' in out

    def test_a_second_positional_argument_is_still_a_usage_error(self, tmp_path):
        assert coas.main(["plan.md", "extra-arg"]) == coas.EXIT_USAGE

    def test_an_unrecognized_flag_is_a_usage_error(self, tmp_path):
        assert coas.main(["plan.md", "--not-a-real-flag"]) == coas.EXIT_USAGE

    def test_missing_plan_path_is_a_usage_error(self, tmp_path):
        assert coas.main(["--dry-run"]) == coas.EXIT_USAGE


# ===========================================================================
# Regression: Defect 3 -- the close-out commit leg cannot complete under
# ordinary concurrency (cross-repo/example-cockpit-repo-em report,
# 2026-07-27, independently reproduced with 15 live sessions)
# ===========================================================================


class TestCommitLegConcurrency:
    """Before this fix, `close_out_and_stamp`'s commit leg shelled out to
    `coordinator-safe-commit` in its liveness-auto-detecting default mode
    with no scope at all -- that binary correctly refuses outright when more
    than one session is live on the branch, which is this repo's NORMAL
    concurrent-EM state. The fix routes the commit through
    `run_commit_pipeline` with an explicit `stage_paths` pathspec instead,
    which is never gated on session count at all (there is no liveness check
    in this code path to trip) and never sweeps a peer session's own dirty
    files into the commit (the explicit pathspec IS the whole scope).
    """

    def test_commit_succeeds_under_many_live_sessions(self, tmp_path, monkeypatch):
        """Pins the reported symptom directly: `resolve_live_session_ids()`
        reporting many concurrently-live sessions (the exact condition that
        made `coordinator-safe-commit`'s default mode refuse) must have NO
        effect on this op's outcome -- the new commit leg never consults
        session liveness at all.
        """
        import coordinator_core.liveness as liveness

        monkeypatch.setattr(
            liveness,
            "resolve_live_session_ids",
            lambda: frozenset(f"fake-live-session-{i}" for i in range(15)),
        )

        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"
        assert result["commit"]["commit_failed"] is False
        assert result["commit"]["committed_sha"] is not None
        assert _head_sha(root) != pre_head

    def test_commit_never_absorbs_an_unrelated_dirty_peer_path(self, tmp_path, monkeypatch):
        """Simulates a live peer session's own uncommitted WIP file sitting
        dirty in the SAME shared worktree -- the routine concurrent-EM case.
        The close-out commit must land ONLY `plan.md` (its own explicit
        pathspec); the peer's file must be neither staged nor swept into the
        commit tree, and must remain exactly as dirty as it was before this
        op ran.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        peer_file = root / "peer-session-wip.txt"
        peer_file.write_text("a live peer session's own uncommitted work\n", encoding="utf-8")

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["commit"]["committed_sha"] is not None
        assert _head_sha(root) != pre_head
        assert _committed_files_at_head(root) == ["plan.md"]

        # The peer's file is untouched: still present, still untracked/dirty,
        # never absorbed into the commit this op just made.
        assert peer_file.exists()
        porcelain = _run_git(["status", "--porcelain", "--", "peer-session-wip.txt"], root)
        assert porcelain.stdout.strip().startswith("??")


# ===========================================================================
# Regression: the reported defect itself (08cbf4bd)
# ===========================================================================


class TestTemplateCommentRegression:
    """Before `08cbf4bd`, a plan carrying `coordinator-doc-new`'s unedited
    scaffolded HTML comment above the real `## Tasks` fence was MALFORMED
    under `locate_fenced_block` -- `close_out_and_stamp` refused it entirely
    (`EXIT_BUSINESS_FAIL`, a "malformed ## Tasks spine" error), which drove
    EMs to hand-stamp `status:` instead and silently skip the
    chunk-completion cross-reference. These tests pin the FIXED behavior:
    the fixture is processed as an ordinary LOCATED spine with a determinate
    shipped/halted verdict, never refused as malformed.
    """

    def test_template_comment_plan_is_not_refused_as_malformed(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        # C1 (non-deferred) deliberately left uncommitted -- this asserts
        # the fixture is no longer MALFORMED, independent of ship status.
        _seed_plan(root, _FIXTURE_TEMPLATE_COMMENT)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert "error" not in result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C1"]
        # C2 fix (2026-08-06): the first halted evaluation of a plan now
        # stamps a durable `close_out_last_partial:` frontmatter field (see
        # `_stamp_close_out_partial_evaluation`'s own docstring) and commits
        # it -- so HEAD DOES move here, unlike the pre-fix "genuinely
        # nothing to commit" halted case this test used to pin.
        assert result["partial_evaluation_stamped"] is True
        assert _head_sha(root) != pre_head

    def test_template_comment_plan_closes_out_successfully_once_shipped(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_TEMPLATE_COMMENT)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_TEMPLATE_COMMENT)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is True
        assert result["missing_chunk_ids"] == []
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head


# ===========================================================================
# Regression: stamp must land on the CORRECT plan file when cwd != repo_root
# ===========================================================================


class TestStampIndependentOfCwd:
    """`_determine_shipped` reads `live_path` (already resolved absolute
    against `repo_root=`) directly, so the shipped/halted verdict has never
    depended on cwd. But the stamp step used to hand `cs_stamp_plan_implemented`
    a repo-RELATIVE path, and `cs_stamp_plan_implemented` forwards that
    straight to `plan_status_transition.main`, which resolves `--plan`
    against the process's OWN cwd with no repo-root anchoring. Every other
    test in this file chdirs into `root` before calling the op (see
    `_run_close_out`'s docstring) -- which is exactly why this cwd-dependent
    bug survived undetected: the op is engine-dispatched and its cwd is not
    guaranteed to be the repo root at all.

    Negative-spec: this test deliberately does NOT chdir into `root` (or add
    a chdir to `_run_close_out`, which every other test in this file relies
    on) -- the whole point is to exercise the op from a cwd that is neither
    the repo root nor any ancestor/descendant of the plan file, so a
    cwd-relative `--plan` resolution would land on the wrong path (or fail
    outright with "plan not found") while the fixed, absolute-path resolution
    still finds and stamps the right file.
    """

    def test_stamp_lands_on_correct_plan_when_cwd_is_elsewhere(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        exit_code, result = coas.close_out_and_stamp("plan.md", repo_root=root)

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is True
        assert result["missing_chunk_ids"] == []
        assert _read_status(plan_file) == "implemented"
        # And no stray "plan.md" was ever created relative to the wrong cwd.
        assert not (elsewhere / "plan.md").exists()


# ===========================================================================
# Regression: Defect 2 (a)-(d) -- chunk-shipped detection reports every
# chunk missing on fully-shipped plans (cross-repo/example-cockpit-repo-em report)
# ===========================================================================


class TestDefect2ChunkCommitDetection:
    def test_chunk_commit_that_never_touches_the_plan_doc_still_counts(self, tmp_path):
        """2(a): a chunk commit that touches only WORK files (never the plan
        doc itself) must still register as shipped -- the prior
        `git log -- <plan-path>` path-scoping could never see it, which is
        exactly the reported symptom (every chunk reported missing on a
        fully-shipped plan). Deliberately does NOT use `_commit_chunk`
        (this file's own helper, which touches the plan file) -- the whole
        point is a commit that touches a DIFFERENT file.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)

        for chunk_id in ("C1", "C2a", "C2b"):
            work_file = root / f"{chunk_id}.txt"
            work_file.write_text(f"{chunk_id} work\n", encoding="utf-8")
            _run_git(["add", f"{chunk_id}.txt"], root)
            _run_git(
                [
                    "commit",
                    "-q",
                    "-m",
                    f"{chunk_id}: land chunk (work file only)",
                    "-m",
                    f"Deliverable-Id: {_DLV_VALID_SPINE}",
                ],
                root,
            )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert missing == []
        assert shipped is True

    def test_multi_id_wave_commit_subject_registers_every_id(self, tmp_path):
        """2(b): a per-wave commit subject `C1,C2a,C2b: ...` must register
        ALL THREE ids, not zero (the prior regex's character class had no
        `,`, so the whole subject failed to match and contributed nothing).
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)

        work_file = root / "wave.txt"
        work_file.write_text("wave work\n", encoding="utf-8")
        _run_git(["add", "wave.txt"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C1,C2a,C2b: land the whole wave in one commit",
                "-m",
                f"Deliverable-Id: {_DLV_VALID_SPINE}",
            ],
            root,
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert missing == []
        assert shipped is True

    def test_prose_subject_with_colon_does_not_register_as_a_chunk_id(self, tmp_path):
        """Bounding check for 2(b)'s widened regex: an ordinary prose
        subject like `fix: whatever was broken` must not cause `fix` (or
        any other leading token) to spuriously satisfy a spine chunk-id.
        """
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)

        work_file = root / "unrelated.txt"
        work_file.write_text("unrelated\n", encoding="utf-8")
        _run_git(["add", "unrelated.txt"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "fix: whatever was broken",
                "-m",
                f"Deliverable-Id: {_DLV_VALID_SPINE}",
            ],
            root,
        )

        query_ok, committed = coas._committed_chunk_ids(root, _DLV_VALID_SPINE)
        assert query_ok is True
        assert "fix" in committed
        # "fix" registering as a committed id is expected (this op has no
        # way to distinguish a genuine 1-token chunk-id subject from a
        # prose subject that happens to start with a colon-terminated
        # word) -- but it must never SPURIOUSLY satisfy an unrelated
        # spine chunk-id like "C1".
        assert coas._committed_id_covers_spine_id("fix", "C1") is False

    def test_sub_chunk_suffixed_commit_satisfies_its_parent_spine_id(self, tmp_path):
        """2(c): a spine id `C1` must be satisfied by a commit subjected
        `C1a: ...` (the disjoint-write-target sub-chunk expansion shape),
        but a spine id `C1` must NOT be satisfied by `C11` -- a distinct
        chunk id, not a sub-chunk of `C1`.
        """
        assert coas._committed_id_covers_spine_id("C1a", "C1") is True
        assert coas._committed_id_covers_spine_id("C3r", "C3") is True
        assert coas._committed_id_covers_spine_id("C11", "C1") is False
        assert coas._committed_id_covers_spine_id("C1", "C1") is True

        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # Land the spine's C1 as a sub-chunk-suffixed subject, and C2a/C2b
        # verbatim -- full-shipped should still be reported.
        _commit_chunk(root, "plan.md", "C1z", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert missing == []
        assert shipped is True

    def test_adjacency_dash_tag_does_not_cover_its_spine_id(self, tmp_path):
        """Defect fix, 2026-08-14 (example-retrieval-repo cross-repo memo
        `2026-08-14-example-retrieval-repo-em-close-out-adjacency-suffix-covers-spine-row`):
        rule 2's dash-tag shape read `C8a-pre` as covering spine `C8a`,
        so a landed PREREQUISITE chunk satisfied the row it was a
        prerequisite FOR. Live repro: `b58edc057 "C8a-pre: green the
        posix-exec baseline ..."` on plan
        `2026-08-11-installer-body-port-to-python`, reporting
        `missing_chunk_ids: ["C8b"]` when C8a was equally open.

        Variant tags must keep covering -- the exclusion is a closed set
        of ADJACENCY tags, not a retreat from rule 2.
        """
        for tag in ("pre", "prep", "post"):
            assert coas._committed_id_covers_spine_id(f"C8a-{tag}", "C8a") is False
        assert coas._committed_id_covers_spine_id("C8a-doe", "C8a") is True
        assert coas._committed_id_covers_spine_id("C8a-mak", "C8a") is True
        assert coas._committed_id_covers_spine_id("C1-fix2", "C1") is True
        # `-press`/`-poster` are ordinary variant tags: the exclusion is on
        # the whole tag, never a prefix of it.
        assert coas._committed_id_covers_spine_id("C8a-press", "C8a") is True

        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1-pre", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert "C1" in missing
        assert shipped is False

    def test_apostrophe_chunk_id_registers_and_covers_its_own_spine_id(self):
        """Defect fix, 2026-08-06: a review-time chunk split can mint an
        apostrophe-bearing id (`C9'`, from splitting `C9`). Live repro:
        commit `9ffbaa505b54`'s exact subject `C9': deliver the manifest
        to DoE ...` was invisible to `missing_chunk_ids` before this fix --
        `_CHUNK_SUBJECT_RE`'s character class never admitted `'`, so the
        id group before `:` could not match at all and `_extract_chunk_ids`
        returned `[]` for the whole subject.
        """
        ids = coas._extract_chunk_ids(
            "C9': deliver the manifest to DoE", spine_ids=["C9'"]
        )
        assert ids == ["C9'"]
        assert coas._committed_id_covers_spine_id("C9'", "C9'") is True

    def test_apostrophe_chunk_id_commit_scan_end_to_end(self, tmp_path):
        """Same defect, exercised through the real `_committed_chunk_ids`
        git-log scan this op actually calls -- the live repro's exact
        subject shape (`C9': deliver the manifest to DoE ...`) must be
        found as evidence, mirroring commit `9ffbaa505b54` on plan
        `2026-08-06-writer-declared-write-surface-manifest`.
        """
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        work_file = root / "manifest.txt"
        work_file.write_text("manifest\n", encoding="utf-8")
        _run_git(["add", "manifest.txt"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C9': deliver the manifest to DoE with the correspondence "
                "property restated",
                "-m",
                f"Deliverable-Id: {_DLV_VALID_SPINE}",
            ],
            root,
        )
        query_ok, committed = coas._committed_chunk_ids(
            root, _DLV_VALID_SPINE, spine_ids=["C9'"]
        )
        assert query_ok is True
        assert "C9'" in committed

    def test_dot_chunk_id_does_not_falsely_cover_an_unrelated_id(self):
        """Negative-spec companion to the apostrophe fix: `.` was already
        admitted literally by the id character class (a character class
        has no `.`-as-wildcard meaning), so a subject `C9.: ...` must
        register the literal id `C9.` and must NOT be treated as covering
        an unrelated spine id `C9x` -- confirming this module's static
        character class never behaved like an unescaped dynamic regex
        would (no `C9.` == `C9x` false-positive collapse either before or
        after this fix).
        """
        ids = coas._extract_chunk_ids("C9.: land the dotted chunk", spine_ids=["C9."])
        assert ids == ["C9."]
        assert coas._committed_id_covers_spine_id("C9.", "C9x") is False
        assert coas._committed_id_covers_spine_id("C9x", "C9.") is False

    def test_plain_chunk_id_unaffected_by_apostrophe_widening(self):
        """Control case (the live run's own C0): an ordinary punctuation-
        free chunk id must resolve exactly as before this fix -- byte-
        identical behavior for the common case is the hard requirement
        the widened character class must not regress.
        """
        assert coas._extract_chunk_ids(
            "C0: record the stop-signal as discharged", spine_ids=["C0"]
        ) == ["C0"]
        assert coas._extract_chunk_ids("C13: land chunk", spine_ids=["C13"]) == ["C13"]
        assert coas._extract_chunk_ids("C3a: land chunk", spine_ids=["C3a"]) == ["C3a"]

    def test_git_log_query_failure_is_distinguishable_from_zero_commits(
        self, tmp_path, monkeypatch
    ):
        """2(d): a broken git-log query (git-not-on-PATH, non-zero exit)
        must be reported as an ERROR distinct from "genuinely nothing
        committed" -- prior behavior silently collapsed both into an empty
        set, which is exactly how this defect presented ("none of your
        work exists" instead of a loud query-failure error).
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        real_run_git = coas._run_git

        def _broken_git_log(args, cwd):
            if args and args[0] == "log":
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: broken")
            return real_run_git(args, cwd)

        monkeypatch.setattr(coas, "_run_git", _broken_git_log)

        query_ok, committed = coas._committed_chunk_ids(root, _DLV_VALID_SPINE)
        assert query_ok is False
        assert committed == set()

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert shipped is False
        assert missing == []
        assert error is not None
        assert "git-log query" in error
        assert "BROKEN" in error


# ===========================================================================
# Regression: paren-slug chunk-id suffix (Defect fix, 2026-08-15) -- the house
# `Cn(slug):` commit-subject convention (`C16(composition-invocation-
# budgets): ...`) registered ZERO chunk-ids: `_CHUNK_SUBJECT_RE`'s id
# character class excluded `(`/`)` outright, so the leading-token match
# failed before the separator/spine-bounding logic ever ran. 221 subjects
# match this shape in this repo's own `git log` (2026-08-15), including
# three of HEAD's own most recent commits at the time of the fix. Reported
# cross-repo by example-retrieval-repo-em (memo dated 2026-08-15), where it presented
# as a false `key_mismatch` on a fully-shipped plan.
# ===========================================================================


class TestParenSlugChunkIdSuffix:
    def test_single_id_paren_slug_strips_to_bare_id(self):
        assert coas._extract_chunk_ids(
            "C1(path-scope): typed path_scope kwarg", spine_ids=["C1"]
        ) == ["C1"]
        assert coas._extract_chunk_ids(
            "C16(composition-invocation-budgets): compute_scope's agent-claim "
            "scan stops scaling with peer count",
            spine_ids=["C16"],
        ) == ["C16"]

    def test_compound_paren_slug_strips_each_token_independently(self):
        """A compound subject may mix a plain and a parenthesized id
        freely -- only the parenthesized token's suffix is stripped, the
        plain token is untouched."""
        assert coas._extract_chunk_ids(
            "C1+C3(path-scope): typed path_scope kwarg", spine_ids=["C1", "C3"]
        ) == ["C1", "C3"]
        assert coas._extract_chunk_ids(
            "C1(path-scope)+C3(other-scope): typed", spine_ids=["C1", "C3"]
        ) == ["C1", "C3"]

    def test_spine_bounded_paren_slug_still_bounds_on_the_bare_id(self):
        """The paren suffix is stripped BEFORE spine-id bounding, so a
        parenthesized token that does not cover any real spine id is still
        dropped, exactly like an unparenthesized stranger token would be."""
        assert coas._extract_chunk_ids(
            "C1(path-scope)+C9(unrelated): typed", spine_ids=["C1"]
        ) == ["C1"]

    def test_unparenthesized_id_list_unaffected_by_the_fix(self):
        """Control case: `C1+C3: typed` (no parens at all) is byte-identical
        to pre-fix behavior."""
        assert coas._extract_chunk_ids(
            "C1+C3: typed", spine_ids=["C1", "C3"]
        ) == ["C1", "C3"]

    def test_empty_parens_strip_to_the_bare_id(self):
        assert coas._extract_chunk_ids("C1(): typed", spine_ids=["C1"]) == ["C1"]

    def test_unbalanced_parens_do_not_crash_and_register_nothing(self):
        assert coas._extract_chunk_ids("C1(oops: unterminated paren") == []
        assert coas._extract_chunk_ids(
            "C1(oops: unterminated paren", spine_ids=["C1"]
        ) == []

    def test_nested_parens_do_not_crash_and_register_nothing(self):
        assert coas._extract_chunk_ids("C1(a(b)): nested paren") == []
        assert coas._extract_chunk_ids(
            "C1(a(b)): nested paren", spine_ids=["C1"]
        ) == []

    def test_pinned_counter_examples_unchanged(self):
        """The three counter-examples this defect fix must not regress,
        pinned by the dispatching brief -- none of these involve parens at
        all, so none of their outputs should move."""
        assert coas._extract_chunk_ids(
            "coordinator/bin/stitch-observer-sidecar.py: add --scan standalone leak sweep"
        ) == []
        assert coas._extract_chunk_ids(
            "g4-M1/M3a/M3b/M4/M4b: commit-authorization teeth"
        ) == []
        assert coas._extract_chunk_ids("fix: whatever was broken") == ["fix"]
        assert coas._extract_chunk_ids(
            "mise: wave 5 -- xwin-03+04 C12 ... + xwin-05 C3"
        ) == ["mise"]
        assert coas._extract_chunk_ids(
            "mise: wave 5 -- xwin-03+04 C12 ... + xwin-05 C3",
            spine_ids=["C12", "C3"],
        ) == []

    def test_paren_slug_commit_registers_end_to_end(self, tmp_path):
        """Same defect, exercised through the real `_committed_chunk_ids`
        git-log scan and `_determine_shipped` -- a plan whose every chunk
        landed under the `Cn(slug):` convention must be reported fully
        shipped, not `key_mismatch`."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root, "plan.md", "C1(path-scope): typed path_scope kwarg",
            deliverable_id=_DLV_VALID_SPINE,
        )
        _commit_with_subject(
            root, "plan.md", "C2a+C2b(second-scope): land both remaining chunks",
            deliverable_id=_DLV_VALID_SPINE,
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert missing == []
        assert shipped is True
        assert join_provenance == coas.JOIN_PROVENANCE_JOINED


# ===========================================================================
# Defect fix, 2026-08-15 -- prefix-then-ids commit-subject form
# (`retrieval-audit C0/C1/C7: ...`, `retrieval-audit C2: ...`). A SECOND,
# DISTINCT defect from the paren-slug fix above: `_CHUNK_SUBJECT_RE` requires
# the id-list to be the LEADING token, so a scope word ahead of it fails the
# whole match and `_extract_chunk_ids` returns `[]`. Reported cross-repo via
# `cross-repo/inbox/2026-08-15-example-retrieval-repo-em-close-out-and-stamp-key-
# mismatch.md`: ten correctly-trailered commits on a fully-shipped
# example-retrieval-repo plan all refused stamping. See `_CHUNK_SUBJECT_PREFIXED_RE`'s
# own comment block for the exact contiguity bound admitted.
# ===========================================================================


class TestPrefixThenIdsChunkSubject:
    def test_single_id_with_prefix_registers(self):
        assert coas._extract_chunk_ids(
            "retrieval-audit C2: schema v21 adds verdict, result_count, event_class",
            spine_ids=["C2"],
        ) == ["C2"]

    def test_slash_joined_multi_id_with_prefix_registers(self):
        assert coas._extract_chunk_ids(
            "retrieval-audit C0/C1/C7: migration-ladder tripwire, RED outcome "
            "test, consumer-dimension ruling",
            spine_ids=["C0", "C1", "C7"],
        ) == ["C0", "C1", "C7"]

    def test_prefixed_token_naming_no_real_spine_id_is_dropped(self):
        """Spine-bounded rejection: a prefixed token still has to cover a
        real spine id, exactly like the unprefixed multi-id path."""
        assert coas._extract_chunk_ids(
            "retrieval-audit C9: nothing real here", spine_ids=["C1"]
        ) == []

    def test_pinned_counter_examples_still_register_nothing_extra(self):
        """Every counter-example the dispatching brief pins: each has its
        real chunk-id mention strictly AFTER the subject's only `: `, not
        immediately before it, so `_CHUNK_SUBJECT_RE` resolves the single
        leading token first and `_CHUNK_SUBJECT_PREFIXED_RE` is never
        reached -- none of these move from pre-fix behavior."""
        assert coas._extract_chunk_ids(
            "close: mark C8 shipped, and record why this plan cannot stamp "
            "implemented",
            spine_ids=["C8"],
        ) == []
        assert coas._extract_chunk_ids(
            "cross-repo: deliver ... C7 sweep deny was inverted memo from ...",
            spine_ids=["C7"],
        ) == []
        assert coas._extract_chunk_ids(
            "doctrine: stage the resolves-trailer zero-join amendment ahead "
            "of claude-klabauter C4",
            spine_ids=["C4"],
        ) == []
        assert coas._extract_chunk_ids(
            "mise: wave 5 -- xwin-03+04 C12 ... + xwin-05 C3",
            spine_ids=["C12", "C3"],
        ) == []
        assert coas._extract_chunk_ids(
            "mise: wave 1 -- DOCTRINE-C7a admission gate ...; RESIDUE-C9 "
            "named-dispatch strip guard ...",
            spine_ids=["C7a", "C9"],
        ) == []

    def test_still_unaffected_control_cases(self):
        """Unrelated pinned counter-examples from the paren-slug fix, still
        untouched by this fix."""
        assert coas._extract_chunk_ids(
            "coordinator/bin/stitch-observer-sidecar.py: add --scan "
            "standalone leak sweep",
            spine_ids=["C1"],
        ) == []
        assert coas._extract_chunk_ids(
            "g4-M1/M3a/M3b/M4/M4b: ...",
            spine_ids=["M1", "M3a", "M3b", "M4", "M4b"],
        ) == ["M3a", "M3b", "M4", "M4b"]
        assert coas._extract_chunk_ids("fix: whatever was broken") == ["fix"]
        assert coas._extract_chunk_ids(
            "C1+C3(path-scope): typed", spine_ids=["C1", "C3"]
        ) == ["C1", "C3"]

    def test_prefix_then_ids_commit_registers_end_to_end(self, tmp_path):
        """Same defect, exercised through the real `_committed_chunk_ids`
        git-log scan and `_determine_shipped` -- a plan whose every chunk
        landed under the `<scope> Cn: ...`/`<scope> Cn/Cm: ...` convention
        must be reported fully shipped, not `key_mismatch`."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root, "plan.md", "retrieval-audit C1: typed path_scope kwarg",
            deliverable_id=_DLV_VALID_SPINE,
        )
        _commit_with_subject(
            root, "plan.md",
            "retrieval-audit C2a/C2b: land both remaining chunks",
            deliverable_id=_DLV_VALID_SPINE,
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert missing == []
        assert shipped is True
        assert join_provenance == coas.JOIN_PROVENANCE_JOINED


class TestTrailerMatchedNoChunkIdMessaging:
    """Chunk B: a commit whose `Deliverable-Id` trailer MATCHES but whose
    subject registers zero chunk-ids (e.g. a plan-authoring/ceremony
    commit) used to be reported through the exact same `key_mismatch`
    reason string as a commit whose trailer VALUE genuinely differed --
    `_JOIN_PROVENANCE_REASON[JOIN_PROVENANCE_KEY_MISMATCH]` asserts "never
    one equal to this plan's own frontmatter value", which is false in this
    state and sends the reader to re-inspect an already-correct trailer.
    `DeliverableJoinStats.trailer_matched_no_chunk_id_count` (2026-08-15)
    lets the message name the real cause instead."""

    def test_committed_chunk_shas_counts_trailer_matched_no_chunk_id_commits(
        self, tmp_path
    ):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root, "plan.md", "docs: author more of the plan document",
            deliverable_id=_DLV_VALID_SPINE,
        )

        query_ok, committed, committed_shas, join_stats = coas._committed_chunk_shas(
            root, _DLV_VALID_SPINE, spine_ids=["C1", "C2a", "C2b"]
        )
        assert query_ok is True
        assert committed == set()
        assert committed_shas == {}
        assert join_stats.matched_commit_count == 0
        assert join_stats.trailer_matched_no_chunk_id_count == 1

    def test_broken_query_placeholder_zeroes_the_new_field(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)

        def _broken_git_log(args, cwd):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: broken")

        monkeypatch.setattr(coas, "_run_git", _broken_git_log)
        query_ok, _committed, _shas, join_stats = coas._committed_chunk_shas(
            root, _DLV_VALID_SPINE, spine_ids=["C1", "C2a", "C2b"]
        )
        assert query_ok is False
        assert join_stats.trailer_matched_no_chunk_id_count == 0

    def test_message_names_no_chunk_id_cause_not_value_differed(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root, "plan.md", "docs: author more of the plan document",
            deliverable_id=_DLV_VALID_SPINE,
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_KEY_MISMATCH
        assert "registered no chunk-id" in result["message"]
        assert "inspect the commit subject, not the trailer" in result["message"]
        assert "never one equal to this plan's own frontmatter value" not in result["message"]

    def test_genuine_value_mismatch_keeps_the_static_reason(
        self, tmp_path, monkeypatch
    ):
        """Control case: when NO commit's trailer matches at all (a genuine
        value mismatch, not a no-chunk-id one), the static reason string is
        unchanged."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root, "plan.md", "C1: land the chunk",
            deliverable_id="dlv-a-totally-different-plan",
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_KEY_MISMATCH
        assert "never one equal to this plan's own frontmatter value" in result["message"]
        assert "registered no chunk-id" not in result["message"]


# ===========================================================================
# Regression: C7, plan-line-item-resolution-model (2026-07-27) -- AC7/AC8/AC9
# ===========================================================================


class TestResolutionModel:
    """The widened completeness oracle (AC9), the `landed` intermediate
    status (AC7), and committed-but-open row auto-resolution (AC8) -- see
    `close_out_and_stamp.py`'s own "Resolution-model widening" docstring
    section for the full design rationale.
    """

    def test_wont_do_row_excluded_from_commit_oracle_stamps_implemented(
        self, tmp_path, monkeypatch
    ):
        """AC9: a plan with one `wont_do` row and every other row `coded`
        stamps `implemented`, not halted -- `wont_do` was never
        commit-required (it carries no code to land), and `coded` rows are
        already resolved, so there is nothing left `open` to block the
        stamp."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_ref: 1234567\n"
            "  body: |\n"
            "    Already resolved from a prior close-out pass.\n"
            "- id: C2\n"
            "  title: Declined widget polish\n"
            "  change_kind: doc-edit\n"
            "  surface: docs/wiki/widget.md\n"
            "  deferred: false\n"
            "  disposition: wont_do\n"
            "  disposition_detail: Not worth the churn.\n"
            "  pm_approved: true\n"
            "  body: |\n"
            "    Declined -- never carried a commit of its own.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        # C1 is already `disposition: coded` (as if a PRIOR close-out pass
        # auto-resolved it), but `coded` STAYS commit-required (AC9 only
        # excludes spun_off/backlogged/wont_do) -- land the real matching
        # commit too, or the oracle correctly reports it missing.
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["open_chunk_ids"] == []
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head

    def test_committed_open_row_auto_resolves_to_coded_with_sha(
        self, tmp_path, monkeypatch
    ):
        """AC8: a committed chunk-id whose row is still `open` is
        auto-resolved to `disposition: coded` with `disposition_ref` set to
        the covering commit's own sha -- verified directly against the
        rewritten spine on disk, not just the shipped/implemented verdict."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)
        expected_sha = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"

        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        assert c1_row["disposition"] == "coded"
        # The auto-resolved sha comes from the SAME `_committed_chunk_shas`
        # git-log query this op's own completeness oracle uses -- assert it
        # is a real, resolvable commit, not merely non-empty.
        assert c1_row["disposition_ref"]
        resolved = _run_git(
            ["rev-parse", "--short", c1_row["disposition_ref"]], root
        ).stdout.strip()
        assert resolved == expected_sha

    def test_shipped_with_a_row_still_open_stamps_landed_not_implemented(
        self, tmp_path, monkeypatch
    ):
        """AC7: `implemented` requires BOTH the code oracle (every
        commit-required id has a matching commit) AND full resolution (no
        row still `open`). This pins the CONTRACT directly against the
        stamp-selection step: with `_determine_shipped` forced to report
        `shipped=True` while a row genuinely has no matching commit (so
        AC8's auto-resolve finds nothing to do), the op must stamp `landed`,
        never `implemented` -- the defensive branch AC7 requires even
        though the ordinary auto-resolved happy path (see the two tests
        above) does not exercise it directly.
        """
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Never actually committed under this op's own heuristic.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        monkeypatch.setattr(
            coas,
            "_determine_shipped",
            lambda *args, **kwargs: (True, [], coas.JOIN_PROVENANCE_JOINED, None),
        )

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["open_chunk_ids"] == ["C1"]
        assert result["status_target"] == "landed"
        assert result["stamped"] is True
        assert _read_status(plan_file) == "landed"
        assert "landed" in result["message"]
        assert _head_sha(root) != pre_head

        # C1's row is untouched -- there was no matching commit for
        # auto-resolve to find, so it stays exactly 'open'.
        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        assert c1_row.get("disposition", "open") == "open"

    def test_stamp_plan_landed_is_idempotent_and_respects_terminal_statuses(
        self, tmp_path
    ):
        """`_stamp_plan_landed` mirrors `_stamp_implemented`'s own gating:
        a TERMINAL/deferred status is a no-op (never resurrected into
        `landed`), and a plan already `landed` is an idempotent no-op --
        it never regresses a plan that has already progressed."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )

        landed_plan = _seed_disposition_plan(root, rows_yaml, status="landed", dest_name="landed.md")
        assert coas._stamp_plan_landed(str(landed_plan)) == 0
        assert _read_status(landed_plan) == "landed"

        implemented_plan = _seed_disposition_plan(
            root, rows_yaml, status="implemented", dest_name="implemented.md"
        )
        assert coas._stamp_plan_landed(str(implemented_plan)) == 0
        assert _read_status(implemented_plan) == "implemented"

        executing_plan = _seed_disposition_plan(
            root, rows_yaml, status="executing", dest_name="executing.md"
        )
        assert coas._stamp_plan_landed(str(executing_plan)) == 0
        assert _read_status(executing_plan) == "landed"

    def test_stamp_plan_landed_write_path_holds_the_cross_process_lock(self, tmp_path):
        """Review: code-reviewer (P2 #1) -- C1 (`plan_tasks_mutate.resolve`)
        newly reaches `_stamp_plan_landed` from a hot path, on a machine
        whose own doctrine names 50-70 concurrent LLM sessions as average
        load (repo CLAUDE.md § Load norm). A second writer holding the
        SAME cross-process lock `locked_rmw` would take must block this
        call until released -- proving the write path is actually gated
        by the lock, not merely calling a plain open()/write() that
        happens to sit near lock-using code. A genuine cross-process
        concurrency test (two real OS processes racing) is impractical in
        this suite; holding the lock via `locked_write.held_lock` from
        this same process and asserting `LockTimeout` is the acceptable
        substitute the review names."""
        from coordinator_core.locked_write import held_lock

        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan = _seed_disposition_plan(root, rows_yaml, status="executing", dest_name="plan.md")

        with held_lock(Path(plan), anchor_root=root, timeout=1.0):
            # `_stamp_plan_landed` catches `LockTimeout` itself and reports
            # it as an ordinary rc=1 failure (matching every other error
            # branch in this function) rather than letting it propagate --
            # rc=1 plus the status remaining untouched is the externally
            # observable proof the write never got past the lock.
            assert coas._stamp_plan_landed(str(plan), timeout=0.2) == 1
        assert _read_status(plan) == "executing"

        # Released once the external holder above exits -- proves the
        # timeout above was purely lock contention, not a genuine failure.
        assert coas._stamp_plan_landed(str(plan)) == 0
        assert _read_status(plan) == "landed"

    def test_stamp_preserves_comments_and_block_scalars_verbatim(
        self, tmp_path, monkeypatch
    ):
        """Regression for the destructive-close-out defect (2026-07-27): the
        prior implementation re-parsed the fence body with `yaml.safe_load`
        and re-emitted it with `yaml.safe_dump`, which silently drops every
        YAML-level comment (a `#` comment, an `<!-- Review: ... -->` HTML
        comment sitting between two rows) and never re-selects a `|`
        literal block scalar (PyYAML re-serializes it as a single-quoted
        scalar, doubling any embedded apostrophe). This test would have
        caught the original bug -- it asserts on the raw on-disk TEXT, not
        a re-parsed semantic view, which is exactly why the prior test
        suite (all of which re-parses and asserts on semantic fields) let
        the defect through."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    It's the widget's first shipment.\n"
            "# a loose YAML comment sitting between two rows\n"
            "# <!-- Review: the Director of Engineering -- looks fine -->\n"
            "- id: C2\n"
            "  title: Polish the widget\n"
            "  change_kind: doc-edit\n"
            "  surface: docs/wiki/widget.md\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Not committed yet -- stays open.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)
        # C2 deliberately left uncommitted -- stays `open`, untouched.
        # Capture the pre-close-out on-disk state AFTER the chunk commit
        # (which itself appends a trailer line to make the commit
        # non-empty) -- that trailer is part of the "original" text the
        # stamp step must otherwise leave untouched.
        original_text = plan_file.read_text(encoding="utf-8")
        original_lines = original_text.splitlines(keepends=True)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")
        assert exit_code == coas.EXIT_OK, result

        new_text = plan_file.read_text(encoding="utf-8")
        new_lines = new_text.splitlines(keepends=True)

        # Comment lines survive verbatim.
        assert "# a loose YAML comment sitting between two rows\n" in new_lines
        assert "# <!-- Review: the Director of Engineering -- looks fine -->\n" in new_lines

        # The block scalar is still a `|` block, and no apostrophe got
        # doubled (the PyYAML-round-trip failure mode this fix removes).
        assert "  body: |\n" in new_lines
        assert "    It's the widget's first shipment.\n" in new_lines
        assert "    Not committed yet -- stays open.\n" in new_lines
        assert "It''s" not in new_text
        assert "widget''s" not in new_text

        # Exactly the expected four stamp lines were inserted: three for C1
        # (disposition: + disposition_ref: + disposition_detail:), plus ONE
        # `close_out_last_partial:` line (C2 fix, 2026-08-06 -- this run is
        # still halted, since C2's own row stays open/uncommitted, so
        # `_stamp_close_out_partial_evaluation` fires too); C2's row body
        # is completely untouched otherwise (still uncommitted/open).
        assert len(new_lines) == len(original_lines) + 4
        assert any(l.startswith("close_out_last_partial: ") for l in new_lines)
        assert "  disposition: coded\n" in new_lines
        disposition_ref_lines = [l for l in new_lines if l.startswith("  disposition_ref: ")]
        assert len(disposition_ref_lines) == 1
        disposition_detail_lines = [
            l for l in new_lines if l.startswith("  disposition_detail: ")
        ]
        assert len(disposition_detail_lines) == 1

        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        c2_row = next(row for row in rows if row.get("id") == "C2")
        assert c1_row["disposition"] == "coded"
        assert c1_row["disposition_ref"]
        # disposition_detail carries the covering commit's own subject line
        # (DR-103) -- _commit_chunk lands a commit subject of the form
        # "<chunk_id>: land chunk".
        assert c1_row["disposition_detail"] == "C1: land chunk"
        assert c2_row.get("disposition", "open") == "open"
        assert "disposition_ref" not in c2_row
        assert "disposition_detail" not in c2_row

        assert _head_sha(root) != pre_head

    def test_stamp_replaces_existing_disposition_lines_in_place(self, tmp_path):
        """A row that already carries `disposition:`/`disposition_ref:`
        lines (e.g. from a prior close-out pass) gets those lines REPLACED
        in place, never duplicated -- pinned directly against
        `_stamp_rows_in_body`, independent of the full orchestration."""
        body = (
            "- id: C1\n"
            "  title: Ship it\n"
            "  deferred: false\n"
            "  disposition: open\n"
            "  disposition_ref: 0000000\n"
            "  body: |\n"
            "    Already stamped once before.\n"
        )
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "abc1234"})
        assert error is None
        lines = new_body.splitlines(keepends=True)
        assert lines.count("  disposition: coded\n") == 1
        assert lines.count("  disposition_ref: abc1234\n") == 1
        assert "  disposition: open\n" not in lines
        assert "  disposition_ref: 0000000\n" not in lines
        # No duplicate keys introduced.
        assert sum(1 for l in lines if l.strip().startswith("disposition:")) == 1
        assert sum(1 for l in lines if l.strip().startswith("disposition_ref:")) == 1

    def test_fidelity_gate_refuses_on_unstampable_body_without_writing_or_committing(
        self, tmp_path, monkeypatch
    ):
        """Step 2: if the row-level stamp somehow produced a change outside
        the disposition/disposition_ref fields, the op must refuse --
        never write, never commit, never push. Forces exactly that shape
        by monkeypatching `_stamp_rows_in_body` to return a body with an
        unrelated line corrupted, then asserts the whole
        `close_out_and_stamp` orchestration surfaces the refusal as a
        business failure and leaves the plan file and git history
        untouched."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)
        # Capture the plan's on-disk state AFTER the chunk commit (which
        # itself appends a trailer line to make the commit non-empty) --
        # this is the actual pre-close-out state the refusal must leave
        # untouched.
        original_text = plan_file.read_text(encoding="utf-8")

        def _corrupting_stamp(body, updates, details=None):
            # Deliberately drop an unrelated line -- not a
            # disposition/disposition_ref/disposition_detail field -- to
            # simulate a stamper bug the fidelity gate must catch.
            lines = body.splitlines(keepends=True)
            corrupted = [l for l in lines if "title" not in l]
            return "".join(corrupted), None

        monkeypatch.setattr(coas, "_stamp_rows_in_body", _corrupting_stamp)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL
        assert "error" in result
        assert "plan.md" in result["error"]
        assert "refusing" in result["error"].lower()

        # No write, no commit, no push.
        assert plan_file.read_text(encoding="utf-8") == original_text
        assert _head_sha(root) == pre_head

    def test_stamp_lands_at_a_non_default_child_key_indent(self, tmp_path):
        """Review: code-reviewer -- F4: `_stamp_rows_in_body` previously
        hardcoded `content_indent = dash_indent + 2` (`yaml.safe_dump`'s
        own default list-of-dicts formatting), which this fix exists to
        stop imposing on the file. A row whose child keys sit at a
        DIFFERENT indent (here: 4 spaces after the dash, not 2) must still
        have its existing `deferred:` key correctly detected -- an
        in-place replace, never a duplicate insertion -- and the new
        `disposition:`/`disposition_ref:` lines must land at that row's
        OWN measured indent, not the hardcoded default. The resulting body
        must still parse as valid YAML with the expected values."""
        body = (
            "-   id: C1\n"
            "    title: Ship it\n"
            "    deferred: false\n"
            "    body: |\n"
            "      Shipped at a non-default indent.\n"
        )
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "abc1234"})
        assert error is None
        lines = new_body.splitlines(keepends=True)

        # Stamp lines landed at the row's OWN indent (4 spaces), not the
        # hardcoded `dash_indent + 2` (2 spaces) default.
        assert "    disposition: coded\n" in lines
        assert "    disposition_ref: abc1234\n" in lines
        assert "  disposition: coded\n" not in lines
        assert "  disposition_ref: abc1234\n" not in lines

        # No duplicate keys, and the pre-existing `deferred:` key survives
        # untouched at its own indent.
        assert lines.count("    deferred: false\n") == 1
        assert sum(1 for l in lines if l.strip().startswith("disposition:")) == 1
        assert (
            sum(1 for l in lines if l.strip().startswith("disposition_ref:")) == 1
        )

        import yaml

        parsed = yaml.safe_load(new_body)
        assert parsed[0]["id"] == "C1"
        assert parsed[0]["disposition"] == "coded"
        assert parsed[0]["disposition_ref"] == "abc1234"
        assert parsed[0]["deferred"] is False

    def test_stamp_replaces_existing_disposition_at_a_non_default_indent(
        self, tmp_path
    ):
        """Companion to the insertion case above: a row that already
        carries `disposition:`/`disposition_ref:` lines at a non-default
        indent (3 spaces after the dash) must have those lines REPLACED in
        place at that SAME indent -- proving `_row_key_line_indices`
        (which matches at exactly `content_indent`) actually finds the
        pre-existing keys once `content_indent` is measured rather than
        assumed."""
        body = (
            "-  id: C1\n"
            "   title: Ship it\n"
            "   disposition: open\n"
            "   disposition_ref: 0000000\n"
            "   body: |\n"
            "     Already stamped once, at a non-default indent.\n"
        )
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "def5678"})
        assert error is None
        lines = new_body.splitlines(keepends=True)

        assert lines.count("   disposition: coded\n") == 1
        assert lines.count("   disposition_ref: def5678\n") == 1
        assert "   disposition: open\n" not in lines
        assert "   disposition_ref: 0000000\n" not in lines
        # No duplicate keys, no lines lost or gained (replace, not insert).
        assert sum(1 for l in lines if l.strip().startswith("disposition:")) == 1
        assert (
            sum(1 for l in lines if l.strip().startswith("disposition_ref:")) == 1
        )
        assert len(lines) == len(body.splitlines(keepends=True))

    def test_stamp_quotes_an_all_digit_sha_so_yaml_keeps_it_a_string(self):
        """An abbreviated sha that happens to be all digits must be emitted
        QUOTED, or YAML reads it back as an int and the row fails
        plan-tasks.schema.json's `type: string` on disposition_ref.

        Not a hypothetical: shas are hex, so ~2.3% of abbreviated ones
        ((10/16)**8, about one commit in 43) are all-digit. A real
        auto-resolve run (1576648b) wrote `disposition_ref: 17519732` into
        docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md, and the
        write-time spine guard flagged it until this fix.
        """
        import yaml

        body = (
            "- id: C1\n"
            "  title: Ship it\n"
            "  body: |\n"
            "    A chunk whose covering commit abbreviates to all digits.\n"
        )
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "17519732"})
        assert error is None

        parsed = yaml.safe_load(new_body)
        assert parsed[0]["disposition_ref"] == "17519732"
        assert isinstance(parsed[0]["disposition_ref"], str), (
            "an all-digit sha round-tripped as a non-string — YAML parsed it "
            "as a number, which is the exact defect this test pins"
        )

    def test_stamp_leaves_a_non_numeric_sha_unquoted(self):
        """The quoting above is conditional, not blanket: a sha containing
        any hex letter cannot parse as a number, so it stays bare and the
        stamped line's shape is unchanged from before that fix. Pins the
        blast radius of the numeric-quoting change to the numeric case
        only -- a blanket quote would churn every existing plan's
        disposition_ref line on its next stamp."""
        body = "- id: C1\n  title: Ship it\n"
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "7876a31d"})
        assert error is None
        assert "  disposition_ref: 7876a31d\n" in new_body.splitlines(keepends=True)

    def test_fidelity_gate_refuses_a_mis_indented_stamp(self, tmp_path, monkeypatch):
        """Review: code-reviewer -- F3: proves the fidelity gate now
        REFUSES a stamp landed at the wrong indent, rather than passing it
        vacuously. Forces exactly that shape by monkeypatching
        `_stamp_rows_in_body` to emit its `disposition:`/`disposition_ref:`
        lines at an indent that does NOT match the row's own measured
        content indent, then asserts the whole `close_out_and_stamp`
        orchestration refuses -- no write, no commit, no push."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)
        original_text = plan_file.read_text(encoding="utf-8")

        def _mis_indented_stamp(body, updates, details=None):
            # Row's own content indent is 2 spaces; deliberately stamp at
            # 4 spaces instead -- textually a valid `disposition:` line,
            # but at the WRONG indent for this row.
            lines = body.splitlines(keepends=True)
            lines.append("    disposition: coded\n")
            lines.append("    disposition_ref: abc1234\n")
            return "".join(lines), None

        monkeypatch.setattr(coas, "_stamp_rows_in_body", _mis_indented_stamp)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL
        assert "error" in result
        assert "plan.md" in result["error"]
        assert "refusing" in result["error"].lower()

        # No write, no commit, no push.
        assert plan_file.read_text(encoding="utf-8") == original_text
        assert _head_sha(root) == pre_head


# ===========================================================================
# Regression: cross-repo memo, example-cockpit-repo-em 2026-08-01 --
# close-out-and-stamp deterministically refused to stamp a completed plan
# with "found a change outside the disposition/disposition_ref/
# disposition_detail fields (first diverging line: '\\n')"
# ===========================================================================


def _stamp_whole_plan(plan_text: str, updates: dict, details: dict) -> tuple[str, str]:
    """Drives the real stamp path over a whole plan's text the same way
    `_auto_resolve_committed_open_rows` does -- locate the fence, stamp the
    body, reassemble around the span -- and returns `(new_text,
    fidelity_error)`. Kept local to this regression block: the defect it
    pins lives entirely in the span-reassembly seam between
    `_stamp_rows_in_body` and its caller, which a body-only unit test
    cannot see."""
    located = locate_fenced_block(plan_text)
    assert located.status == LocateStatus.LOCATED
    start, end = located.span
    new_body, stamp_error = coas._stamp_rows_in_body(
        plan_text[start:end], updates, details
    )
    assert stamp_error is None, stamp_error
    new_text = plan_text[:start] + new_body + plan_text[end:]
    return new_text, coas._assert_stamp_fidelity(plan_text, new_text, "plan.md")


class TestFinalRowStampDoesNotPlantABlankLineBeforeTheFence:
    def test_body_without_a_trailing_newline_keeps_its_own_shape(self):
        """`locate_fenced_block(...).span` hands `_stamp_rows_in_body` a
        body whose FINAL line terminator lives OUTSIDE the span. Appending
        stamp lines at the very end of such a body forces the function's
        own newline fixup on the previously-final line; without a
        compensating strip at the return, the caller's reassembly emits
        that newline AND the span-external one. Pinned at the
        `_stamp_rows_in_body` level: a body that arrived without a trailing
        newline must leave without one."""
        body = "- id: C1\n  title: Ship it\n  deferred: false"
        new_body, error = coas._stamp_rows_in_body(
            body, {"C1": "abc1234"}, {"C1": "C1: land chunk"}
        )
        assert error is None
        assert not new_body.endswith("\n")
        assert new_body.endswith("  disposition_detail: 'C1: land chunk'")

    def test_body_with_a_trailing_newline_is_unaffected(self):
        """The strip above is conditional, not blanket -- a body that
        already ended with a newline keeps it, so no pre-existing caller's
        output shape changes."""
        body = "- id: C1\n  title: Ship it\n  deferred: false\n"
        new_body, error = coas._stamp_rows_in_body(body, {"C1": "abc1234"})
        assert error is None
        assert new_body.endswith("\n")

    def test_stamping_the_last_row_of_the_fence_passes_the_fidelity_gate(self):
        """The memo's exact reported failure: a plan whose final spine row
        is the last line of the fence body stamped clean, but the
        reassembled text carried a bare blank line between the last
        `disposition_detail:` line and the closing fence -- which
        `_assert_stamp_fidelity` correctly refused, deterministically, on
        every retry (the gate was right; the stamper was wrong)."""
        # No trailing newline on the last row: `_PLAN_TEMPLATE` supplies the
        # one before the closing fence, so this is the real on-disk shape
        # (the last row's own line terminator lives OUTSIDE the located
        # body's span). A trailing newline here would instead leave a blank
        # line in the ORIGINAL, which is why the rest of this file's
        # fixtures never hit the defect.
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the first thing\n"
            "  deferred: false\n"
            "- id: C2\n"
            "  title: Ship the last thing\n"
            "  deferred: false"
        )
        plan_text = _PLAN_TEMPLATE.format(status="executing", rows=rows_yaml)
        assert "\n\n```\n" not in plan_text
        new_text, fidelity_error = _stamp_whole_plan(
            plan_text,
            {"C1": "abc1234", "C2": "def5678"},
            {"C1": "C1: land it", "C2": "C2: land it"},
        )

        assert fidelity_error is None, fidelity_error
        assert "  disposition_detail: 'C2: land it'\n```\n" in new_text
        assert "\n\n```\n" not in new_text

        import yaml

        parsed = yaml.safe_load(locate_fenced_block(new_text).body)
        assert parsed[-1]["id"] == "C2"
        assert parsed[-1]["disposition"] == "coded"
        assert parsed[-1]["disposition_ref"] == "def5678"


class TestFidelityGateBoundsRowSpansToTheFenceBody:
    def test_prose_after_the_fence_does_not_skew_the_last_row_indent(self):
        """`_assert_stamp_fidelity` used to run `_find_row_spans` over the
        WHOLE plan text, so the final row's span ran to end-of-document --
        past the closing fence, across every following section. Any
        ordinary markdown line shaped like an indented `key: value` could
        then win `_measure_row_content_indent`'s `min()` and produce a
        bogus `expected_indent`, false-rejecting a correct stamp on the
        last row. Here the row's own content indent is 4 while the prose
        below the fence carries a 2-space `key:`-shaped line."""
        rows_yaml = (
            "-   id: C1\n"
            "    title: Ship the last thing\n"
            "    deferred: false"
        )
        plan_text = _PLAN_TEMPLATE.format(status="executing", rows=rows_yaml) + (
            "\n"
            "## Notes\n"
            "\n"
            "Example config the reader is meant to copy:\n"
            "\n"
            "  note: this line is `key:`-shaped and shallower than the row\n"
        )
        new_text, fidelity_error = _stamp_whole_plan(
            plan_text, {"C1": "abc1234"}, {"C1": "C1: land it"}
        )

        assert fidelity_error is None, fidelity_error
        assert "    disposition: coded\n" in new_text
        assert "    disposition_ref: abc1234\n" in new_text

    def test_span_bounding_degrades_to_whole_file_when_no_fence_is_present(self):
        """Defensive: a text with no locatable `## Tasks` fence must fall
        back to today's whole-file scan rather than crashing the gate."""
        no_fence = "# Just prose\n\nNothing spine-shaped here at all.\n"
        assert coas._find_row_spans_in_plan(
            no_fence.splitlines(keepends=True), no_fence
        ) == []


# ===========================================================================
# Regression: code-review finding, 2026-07-27 -- `stamped=True` on a
# genuine no-op (idempotent re-run against an already-terminal plan)
# ===========================================================================


class TestIdempotentRerunDoesNotAttemptAZeroDiffCommit:
    """`_stamp_plan_landed` / `plan_status_transition._stamp_implemented`
    (via `cs_stamp_plan_implemented`) both return `rc == 0` on a
    documented no-op branch (status already terminal / already at
    target) with NO on-disk write. Before the fix, the caller set
    `stamped = True` on any `rc == 0` regardless of which branch fired --
    so a SECOND `close_out_and_stamp` call against a plan this op already
    stamped `implemented` would read `stamped=True` against a
    byte-clean `plan.md`, then attempt (and fail loud on) a zero-diff
    `git commit`. This class exercises exactly that shape: a full
    close-out call, immediately followed by a second call against the
    now-`implemented` plan.

    Uses the valid-spine, fully-committed fixture deliberately -- NOT the
    absent-spine fixture this test used before the false-positive-stamp
    incident fix: an absent spine with no Dispatch Ledger now reports
    `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE` and is never stamped at all (see
    `TestCloseOutAndStampContinued.
    test_absent_spine_is_treated_as_full_shipped_but_not_stamped`), so it
    can no longer reach an `implemented` status to idempotently re-run
    against. This fixture's join IS evidence-backed (a real
    Deliverable-Id trailer join over real commits), which is what this
    class's `stamped`/`wrote_anything` no-op seam actually needs."""

    def test_second_call_against_an_already_implemented_plan_is_a_genuine_noop(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        # First call: genuinely full-shipped (evidence-backed join), stamps
        # "implemented" and lands a real commit.
        exit_code_1, result_1, pre_head_1 = _run_close_out(monkeypatch, root, "plan.md")
        assert exit_code_1 == coas.EXIT_OK, result_1
        assert result_1["stamped"] is True
        assert _read_status(plan_file) == "implemented"
        head_after_first_call = _head_sha(root)
        assert head_after_first_call != pre_head_1

        # Second call: the plan is now "implemented" -- a TERMINAL status
        # -- so both `cs_stamp_plan_implemented` and (were the target
        # `landed`) `_stamp_plan_landed` would no-op with rc=0. This call
        # must recognize that as "nothing written" and skip the commit
        # leg entirely, rather than attempting (and failing loud on) a
        # zero-diff commit against `plan.md`.
        exit_code_2, result_2, pre_head_2 = _run_close_out(monkeypatch, root, "plan.md")
        assert exit_code_2 == coas.EXIT_OK, result_2
        assert result_2["stamped"] is False
        assert result_2["commit"]["commit_failed"] is False
        assert result_2["commit"]["committed_sha"] is None
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) == pre_head_2
        assert _head_sha(root) == head_after_first_call


# ===========================================================================
# Regression: chunk-ids collide ACROSS plans -- Deliverable-Id trailer
# scoping (Defect fix, 2026-07-27; see close_out_and_stamp.py's own
# docstring § Deliverable scoping for the full defect narrative and the
# chosen false-negative-over-false-positive tradeoff).
# ===========================================================================


class TestDeliverableScoping:
    """Pins the fix for the live false-positive bug: chunk-ids (`C1`,
    `C2a`, `C8b`, ...) are only unique WITHIN a single plan's own spine, and
    the shared workstream branch carries commits from many concurrent plans
    reusing the same ids. An UNRELATED plan's chunk-id-shaped commit
    subject must never satisfy THIS plan's spine row of the same id --
    scoping is via the `Deliverable-Id:` git trailer cross-referenced
    against the closing plan's own frontmatter `deliverable_id:` field;
    subject match alone is no longer sufficient.
    """

    def test_matching_chunk_id_but_different_deliverable_id_does_not_count(
        self, tmp_path
    ):
        """THE LIVE BUG, reproduced directly: a commit whose subject
        matches the spine's chunk-id but whose `Deliverable-Id` trailer
        belongs to a DIFFERENT plan must not count as evidence -- this is
        exactly the false-positive observed live on
        work/machine-b/2026-07-21to26, 2026-07-27 (plan
        2026-07-27-plan-line-item-resolution-model's `C8b` row read as
        shipped via two unrelated plans' own `C8b` commits).

        Against the PRE-FIX code (raw subject matching, no trailer check)
        this assertion would FAIL -- `shipped` would be `True` and `missing`
        would be `[]`, because the C1 commit's subject alone was sufficient
        evidence. That is the exact defect this fix closes.
        """
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(
            root, "plan.md", "C1", deliverable_id="dlv-some-other-plan-000002"
        )
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]
        # A DIFFERENT plan's Deliverable-Id trailer exists in range (the C1
        # commit above) alongside C2a/C2b's own matching trailers -- the
        # join itself succeeded (C2a/C2b matched), so this is "joined", not
        # "key_mismatch": the still-missing C1 is a genuinely separate row
        # this plan's own join correctly excluded, not a join failure.
        assert join_provenance == "joined"

    def test_matching_chunk_id_and_matching_deliverable_id_counts(self, tmp_path):
        """The companion positive case: a commit whose `Deliverable-Id`
        trailer DOES match the closing plan's own `deliverable_id:` counts
        as evidence exactly as before this fix."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []
        assert join_provenance == "joined"

    def test_untrailered_commit_with_matching_subject_does_not_count(self, tmp_path):
        """A commit that predates the `Deliverable-Id:` trailer convention
        -- matching subject, no trailer at all -- is UNATTRIBUTABLE and
        must never count, even though its subject matches. This is the
        deliberate false-negative-over-false-positive tradeoff this fix
        makes on purpose (see the module docstring): a false negative just
        makes a human look twice, a false positive silently ships an
        incomplete plan as done."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1")  # no deliverable_id -- untrailered
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]
        # C2a/C2b both carry the correctly-matching trailer -- the join
        # succeeded overall ("joined"); C1's own commit simply carries no
        # trailer at all and is correctly excluded as unattributable, which
        # is a per-row exclusion, not a join-provenance failure.
        assert join_provenance == "joined"

    def test_plan_with_no_deliverable_id_reports_every_chunk_uncommitted(
        self, tmp_path
    ):
        """A plan with no `deliverable_id:` frontmatter field at all
        cannot be scoped -- the conservative choice this fix makes (see
        the module docstring's "No `deliverable_id`..." section) is to
        report every commit-required chunk-id as missing, NEVER to fall
        back to the old unscoped subject-matching (which would silently
        reinstate the defect this fix closes). The git-log query itself
        must still succeed (`error is None`) -- this is "cannot
        attribute", not "the git query broke" (Defect 2(d)'s distinction
        stays intact). `join_provenance` is `"no_join_key"` here -- the
        join was never ATTEMPTED at all (no `deliverable_id:` to key off
        of), which is a distinct fact from "the join ran and found nothing"
        (`no_join_candidates`/`key_mismatch`) -- see close_out_and_stamp.py's
        own join-provenance widening for why the two must not be
        conflated."""
        root = tmp_path
        _init_repo(root)
        text = _FIXTURE_VALID_SPINE.read_text(encoding="utf-8").replace(
            '\ndeliverable_id: "dlv-fixture-valid-spine-000001"\n', "\n"
        )
        assert "deliverable_id" not in text
        plan_file = root / "plan.md"
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        assert coas._plan_deliverable_id(text) is None

        shipped, missing, join_provenance, error = coas._determine_shipped(text, "plan.md", root)
        assert error is None
        assert shipped is False
        assert sorted(missing) == ["C1", "C2a", "C2b"]
        assert join_provenance == "no_join_key"

    def test_sub_chunk_suffix_still_matches_with_deliverable_id_scoping(
        self, tmp_path
    ):
        """Sub-chunk-suffix matching (`C1a` covers spine id `C1`, Defect
        2(c)) still works once the commit ALSO carries the matching
        `Deliverable-Id` trailer -- deliverable scoping and sub-chunk
        matching compose; neither disables the other. (This is a
        deliverable-scoped restatement of
        `TestDefect2ChunkCommitDetection.
        test_sub_chunk_suffixed_commit_satisfies_its_parent_spine_id`
        above, which already covers this under the new scoping -- kept
        here too as an explicit, self-contained pin per this defect's own
        test plan.)"""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1z", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True


# ===========================================================================
# Deliverable-Id near-miss diagnostic (2026-08-01) -- makes a zero/
# under-counted `missing_chunk_ids` verdict LEGIBLE when the real cause is a
# `Deliverable-Id` VALUE mismatch between the plan's own frontmatter
# `deliverable_id:` and the trailer its own commits actually carry (two
# independent producers of the same FK -- see close_out_and_stamp.py's own
# `_deliverable_id_near_miss_diagnostics` docstring). Diagnostic-only: the
# join semantics/verdict in `_committed_chunk_shas` are UNCHANGED by this
# fix -- these tests pin that this is purely additive explanation, never a
# second path to "count" a mismatched commit as evidence.
# ===========================================================================


class TestDeliverableIdMismatchDiagnostic:
    def test_mismatch_present_names_both_values_with_count(self, tmp_path):
        """A near-miss trailer value on a chunk-shaped commit is reported
        with its covering-commit count, and the plan's own `deliverable_id`
        is excluded from the candidate set (it isn't a "near miss" -- it
        already counts as real evidence when present)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        other_id = "dlv-percolate-root-rung-ordering-the-doe-roo-a8c947"
        _commit_chunk(root, "plan.md", "C1", deliverable_id=other_id)
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=other_id)
        # An untrailered commit (no Deliverable-Id at all) must never be
        # reported as a near-miss candidate -- there is no value to name.
        _commit_chunk(root, "plan.md", "C2b")

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1", "C2a", "C2b"]
        )
        assert candidates == [{"deliverable_id": other_id, "commit_count": 2}]

    def test_no_near_miss_returns_empty(self, tmp_path):
        """Genuinely-uncommitted chunks, with no trailer at all on any
        chunk-shaped commit -- there is no near-miss candidate to name."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1")  # untrailered

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1"]
        )
        assert candidates == []

    def test_no_colon_subject_is_never_a_candidate(self, tmp_path):
        """A commit subject with no leading `<id>: ` shape at all (no colon
        to anchor `_CHUNK_SUBJECT_RE`) yields zero ids from
        `_extract_chunk_ids` and must never contribute a near-miss
        candidate, even if it carries a differing `Deliverable-Id` trailer
        -- mirrors `_committed_chunk_shas`'s own "subject yields at least
        one chunk-id" gate exactly."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        assert coas._extract_chunk_ids("merge branch updates") == []
        _commit_with_subject(
            root, "plan.md", "merge branch updates", deliverable_id="dlv-other-000009"
        )

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1"]
        )
        assert candidates == []

    def test_prose_subject_token_not_covering_a_missing_id_is_not_a_candidate(
        self, tmp_path
    ):
        """A `<token>: <prose>` commit whose leading token is NOT one of the
        ids still missing must never be named as the near-miss cause.

        `_extract_chunk_ids` deliberately registers the single leading token
        of ANY colon-prefixed subject, so `fix: ...` contributes the id
        `fix`. Gating on subject SHAPE alone would therefore let an ordinary
        housekeeping commit that happens to carry a different Deliverable-Id
        be reported as the reason a plan's chunks did not count -- naming a
        wholly unrelated deliverable with full confidence. The intersection
        against `missing_chunk_ids` is what forecloses that; this test is
        the assertion that goes red if the gate is ever loosened back to
        shape-only. Observed live: claude-klabauter's own history carries `fix:`,
        `ceremony:` and `memo:` subjects under many distinct deliverable
        ids."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        unrelated = "dlv-some-unrelated-deliverable-000077"
        assert coas._extract_chunk_ids("fix: tidy an unrelated guard") == ["fix"]
        _commit_with_subject(
            root, "plan.md", "fix: tidy an unrelated guard", deliverable_id=unrelated
        )

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1", "C2a"]
        )
        assert candidates == []

    def test_absent_deliverable_id_returns_empty_without_git_call(self, tmp_path, monkeypatch):
        """`deliverable_id=None`/falsy short-circuits before any git-log
        call at all -- nothing to compare a candidate against."""
        root = tmp_path
        _init_repo(root)

        call_count = {"n": 0}
        real_run_git = coas._run_git

        def counting_run_git(args, cwd):
            call_count["n"] += 1
            return real_run_git(args, cwd)

        monkeypatch.setattr(coas, "_run_git", counting_run_git)

        assert coas._deliverable_id_near_miss_diagnostics(root, None, ["C1"]) == []
        assert coas._deliverable_id_near_miss_diagnostics(root, "", ["C1"]) == []
        assert call_count["n"] == 0

    def test_zero_trailered_commits_in_range_never_recommends_an_equivalence(
        self, tmp_path, monkeypatch
    ):
        """Second-defect regression (range-fix, 2026-08-07 -- see this
        module's own bug-backlog entry `2026-08-07-close-out-and-stamp-s-
        chunk-evidence-joi-8b6a7a32d833.yaml`): when the searched range
        carries ZERO commits with any `Deliverable-Id` trailer at all
        (`JOIN_PROVENANCE_NO_JOIN_CANDIDATES`), this is a range/visibility
        failure, not a key mismatch -- the caller must never be steered
        toward declaring a `state/deliverable-equivalence.yaml` equivalence
        for it, since doing so on a genuine range bug would record a FALSE
        equivalence between two unrelated workstreams (the exact live
        incident this fix closes). `deliverable_id_mismatch` must stay
        empty and the message must carry the `no_join_candidates` reason,
        never an equivalence NOTE."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # Nothing else committed at all -- the only commit in this repo's
        # history is the untrailered "seed" commit `_seed_plan` itself
        # lands, so the searched range carries zero Deliverable-Id-trailered
        # commits of any kind.

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_NO_JOIN_CANDIDATES
        assert result["deliverable_id_mismatch"] == []
        assert "no_join_candidates" in result["message"]
        assert "equivalence" not in result["message"]
        assert "NOTE" not in result["message"]

    def test_close_out_and_stamp_message_and_result_carry_the_mismatch(
        self, tmp_path, monkeypatch
    ):
        """Integration: `close_out_and_stamp`'s own `message` string gets a
        generic, register-matched NOTE (2026-08-14, key_mismatch stops
        naming strangers) -- the structured `deliverable_id_mismatch` result
        key still carries the candidate(s) (unaffected, `shipped`/AC7
        invariant), but the human-facing message no longer names a specific
        foreign id or advises declaring a `state/deliverable-equivalence.
        yaml` equivalence: the search behind this diagnostic is unscoped by
        author/session, so naming a stranger's commit as evidence against
        THIS plan and inviting the reader to declare an equivalence against
        it is exactly the shared-worktree false-positive this fix closes."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        other_id = "dlv-some-other-value-000042"
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=other_id)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C1", "C2a", "C2b"]
        assert result["deliverable_id_mismatch"] == [
            {"deliverable_id": other_id, "commit_count": 3}
        ]
        assert other_id not in result["message"]
        assert "equivalence" not in result["message"]
        assert "earliest artifact wins" not in result["message"]
        assert (
            "3 commit(s) in range belong to other deliverables (expected on "
            "a shared tree)" in result["message"]
        )

    def test_genuinely_uncommitted_with_no_near_miss_message_unchanged(
        self, tmp_path, monkeypatch
    ):
        """Today's message/behavior is UNCHANGED when chunks are genuinely
        uncommitted and no near-miss `Deliverable-Id` candidate exists --
        no bogus NOTE clause is ever appended, and `deliverable_id_mismatch`
        is empty."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        # C1 committed correctly, C2a/C2b never committed at all -- no
        # commit anywhere carries a differing Deliverable-Id trailer.
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C2a", "C2b"]
        assert result["deliverable_id_mismatch"] == []
        assert result["message"] == (
            f"plan.md: {len(result['missing_chunk_ids'])} chunk(s) still "
            "uncommitted, committed partial state"
        )
        assert "NOTE" not in result["message"]

    def test_happy_path_full_shipped_unaffected_no_diagnostic_call(
        self, tmp_path, monkeypatch
    ):
        """The happy path (fully shipped, `missing_chunk_ids == []`) never
        even CALLS `_deliverable_id_near_miss_diagnostics` -- pinned
        directly against a spy on that function, not just the result
        shape, per this fix's own "never touch the happy path" constraint
        (the caller must gate the call itself, not merely discard an
        empty result)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        call_count = {"n": 0}
        real_diagnostic = coas._deliverable_id_near_miss_diagnostics

        def counting_diagnostic(repo_root, deliverable_id, missing_chunk_ids):
            call_count["n"] += 1
            return real_diagnostic(repo_root, deliverable_id, missing_chunk_ids)

        monkeypatch.setattr(
            coas, "_deliverable_id_near_miss_diagnostics", counting_diagnostic
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["deliverable_id_mismatch"] == []
        assert "NOTE" not in result["message"]
        assert call_count["n"] == 0

    def test_plan_with_no_deliverable_id_no_crash_no_bogus_diagnostic(
        self, tmp_path, monkeypatch
    ):
        """A plan with no `deliverable_id:` frontmatter field at all (or an
        explicit `null`) must not crash the diagnostic, and must never
        manufacture a bogus near-miss candidate out of thin air.

        This is also `_determine_shipped`'s `"no_join_key"` join-provenance
        state -- the join was never attempted at all (no key to attempt it
        with), so `close_out_and_stamp`'s own `message` must say the chunks
        could not be ATTRIBUTED, never that they are "still uncommitted" --
        that wording asserts a substantive delivery finding the join was
        never in a position to make (this is "cannot attribute", not "the
        git query broke" -- Defect 2(d)'s distinction stays intact, and this
        is the exact conflation the cross-repo memo this fix closes flagged
        against 7 correctly-committed chunks)."""
        root = tmp_path
        _init_repo(root)
        text = _FIXTURE_VALID_SPINE.read_text(encoding="utf-8").replace(
            '\ndeliverable_id: "dlv-fixture-valid-spine-000001"\n', "\n"
        )
        assert "deliverable_id" not in text
        plan_file = root / "plan.md"
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["deliverable_id_mismatch"] == []
        assert "NOTE" not in result["message"]
        assert "could not be attributed" in result["message"]
        assert "no_join_key" in result["message"]
        assert "still uncommitted" not in result["message"]


class TestKeyMismatchStopsNamingStrangers:
    """docs/plans/2026-08-14-excise-cut-reaches-the-divergence-check.md C4:
    promoted from `repro_claim_b.py`. `_deliverable_log_records` is the
    sole `git log` site behind every chunk-evidence reader and bounds only
    by commit range -- no author, committer, `Session-Id`, or deliverable
    restriction. On a shared worktree with dozens of concurrent sessions,
    an unrelated peer's landed commit therefore enters the candidate set
    and surfaced (pre-fix) in the `key_mismatch` diagnostic as though it
    were evidence about THIS plan, complete with advice to declare a
    (false) equivalence against a total stranger."""

    def test_ac6_peers_commit_not_named_and_no_equivalence_advice(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # A single "stranger" commit -- simulating a concurrent peer
        # session's landed commit on the shared tree -- with a
        # chunk-shaped subject covering a still-missing spine row (C1) and
        # trailers naming a completely different Deliverable-Id and a
        # foreign Session-Id. Nothing else is committed at all -- C1, C2a,
        # C2b are all still open, all attributable only to this one
        # foreign commit.
        stranger_id = "dlv-stranger-foreign999"
        (root / "stranger.txt").write_text("stranger content\n", encoding="utf-8")
        _run_git(["add", "stranger.txt"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C1: stranger's own unrelated chunk\n\n"
                f"Deliverable-Id: {stranger_id}\n"
                "Session-Id: some-other-concurrent-session\n",
            ],
            root,
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_KEY_MISMATCH
        assert result["deliverable_id_mismatch"] == [
            {"deliverable_id": stranger_id, "commit_count": 1}
        ]
        assert stranger_id not in result["message"]
        assert "equivalence" not in result["message"]
        assert "earliest artifact wins" not in result["message"]
        assert (
            "1 commit(s) in range belong to other deliverables (expected on "
            "a shared tree)" in result["message"]
        )

    def test_ac7_shipped_unchanged_from_pre_fix_behaviour(self, tmp_path, monkeypatch):
        """Same fixture as AC6 -- `shipped` (and the rest of the structured
        verdict) is IDENTICAL to what pre-fix behaviour computed; only the
        message/reason presentation moved."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        stranger_id = "dlv-stranger-foreign999"
        (root / "stranger.txt").write_text("stranger content\n", encoding="utf-8")
        _run_git(["add", "stranger.txt"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C1: stranger's own unrelated chunk\n\n"
                f"Deliverable-Id: {stranger_id}\n"
                "Session-Id: some-other-concurrent-session\n",
            ],
            root,
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C1", "C2a", "C2b"]


# ===========================================================================
# Regression: single-chunk-id-only subject parser (Defect fix, 2026-07-27) --
# multi-chunk commit subjects (`,`/`+`/`/`-joined id-lists) are an
# established convention on the shared workstream branch; the prior parser
# only recognized `,` and mis-split nothing at all on a `+`/`/`-joined
# subject, silently crediting zero of the named ids. Corpus evidence for the
# separator set (`git log --format='%s'` over both DoE-claude and
# claude-klabauter, 2026-07-27): `,`, `, ` (comma with trailing space, seen
# live -- `C2, C7b: ...`), `+` (`C3+C2b: ...`), `/` (`C8a-doe/C8p: ...`).
# ===========================================================================


def _commit_with_subject(
    root: Path,
    plan_rel: str,
    subject: str,
    *,
    deliverable_id: Optional[str] = None,
    touch_file: Optional[str] = None,
) -> None:
    """Lands a commit with an EXACT, caller-chosen subject (unlike
    `_commit_chunk`, which hardcodes `<chunk_id>: land chunk`) -- needed for
    multi-chunk-subject coverage, where the subject itself names more than
    one id or is deliberately NOT chunk-id-shaped at all. Touches
    `plan_rel` by default (a trivial trailing-comment append, same reason
    `_commit_chunk` does this -- `git log -- <path>` only lists commits that
    actually change the tree entry at that path); pass `touch_file` to touch
    a different path instead.
    """
    target_rel = touch_file if touch_file is not None else plan_rel
    target_file = root / target_rel
    if touch_file is not None and not target_file.exists():
        target_file.write_text("", encoding="utf-8")
    with target_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {subject!r} landed -->\n")
    _run_git(["add", target_rel], root)
    message_args = ["-m", subject]
    if deliverable_id:
        message_args += ["-m", f"Deliverable-Id: {deliverable_id}"]
    _run_git(["commit", "-q", *message_args], root)


class TestMultiChunkSubjectSeparators:
    """Each of the three corpus-derived separators (`,`, `+`, `/`), plus the
    comma-with-trailing-space spacing variant, registers every id it names."""

    @pytest.mark.parametrize(
        "subject,expected_ids",
        [
            ("C1,C2a,C2b: land the whole wave in one commit", {"C1", "C2a", "C2b"}),
            ("C1, C2a: land two chunks, comma-space form", {"C1", "C2a"}),
            ("C1+C2a: land two chunks, plus-joined", {"C1", "C2a"}),
            ("C1/C2a: land two chunks, slash-joined", {"C1", "C2a"}),
            (
                "C1 + C2a: land two chunks, space-padded plus-joined",
                {"C1", "C2a"},
            ),
            (
                "C1 / C2a: land two chunks, space-padded slash-joined",
                {"C1", "C2a"},
            ),
        ],
        ids=["comma", "comma-space", "plus", "slash", "plus-spaced", "slash-spaced"],
    )
    def test_each_corpus_separator_registers_every_named_id(
        self, tmp_path, subject, expected_ids
    ):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(root, "plan.md", subject, deliverable_id=_DLV_VALID_SPINE)

        query_ok, committed = coas._committed_chunk_ids(root, _DLV_VALID_SPINE)
        assert query_ok is True
        assert expected_ids <= committed

    def test_space_padded_plus_joined_subject_reproduces_cross_repo_memo(self):
        """Live repro from the 2026-08-06 cross-repo memo (`close-out-and-
        stamp-compound-subject-space-separator`): `C4 + C3b + C5a: ...` and
        `C4b + C5b: ...` must NOT fail the whole leading-token match --
        `_CHUNK_SUBJECT_RE`'s `,` branch already tolerates surrounding
        whitespace; `+`/`/` did not, so the ENTIRE match failed (zero ids,
        not a partial miss)."""
        assert coas._CHUNK_SUBJECT_RE.match(
            "C4 + C3b + C5a: delete the misc bucket, surface the drop set, repair the tests"
        ) is not None
        assert coas._CHUNK_SUBJECT_RE.match(
            "C4b + C5b: minting policy becomes a threshold we derive"
        ) is not None

    def test_multi_chunk_subject_with_partial_spine_coverage_reports_only_the_gap(
        self, tmp_path
    ):
        """A `/`-joined multi-chunk subject naming `C1` and `C2a` (both real
        spine ids) leaves `C2b` (the spine's third non-deferred row)
        genuinely uncommitted -- `_determine_shipped` must report exactly
        that one gap, not treat the whole commit as all-or-nothing."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1/C2a: land two of the three spine chunks",
            deliverable_id=_DLV_VALID_SPINE,
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C2b"]

    def test_multi_chunk_subject_with_non_matching_deliverable_id_credits_nothing(
        self, tmp_path
    ):
        """The Deliverable-Id gate (preceding fix) still applies to a
        multi-chunk subject: a `,`-joined subject naming every spine id, but
        trailered to a DIFFERENT plan's `deliverable_id`, must credit NONE
        of the named ids -- the trailer check gates the whole commit, and
        multi-id splitting only ever applies to commits that already
        cleared that gate."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1,C2a,C2b: land the whole wave in one commit",
            deliverable_id="dlv-some-other-plan-000099",
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert sorted(missing) == ["C1", "C2a", "C2b"]

        query_ok, committed = coas._committed_chunk_ids(root, _DLV_VALID_SPINE)
        assert query_ok is True
        assert committed == set()

    def test_sub_chunk_suffix_inside_multi_chunk_subject_covers_both_spine_ids(
        self, tmp_path
    ):
        """The real observed-live shape (2026-07-27): `C8a-doe/C8p: ...`
        must credit BOTH spine `C8a` (via the dash-tag sub-chunk-suffix
        `-doe`) AND spine `C8p` (exact match) -- this is the exact commit
        subject the reported defect ("chunk `C8p` shipped inside
        `C8a-doe/C8p: ...` and the oracle reported it uncommitted") is
        drawn from, generalized to also assert `C8a`'s own coverage via the
        dash-tag suffix widening."""
        assert coas._committed_id_covers_spine_id("C8a-doe", "C8a") is True
        assert coas._committed_id_covers_spine_id("C8a-mak", "C8a") is True
        assert coas._committed_id_covers_spine_id("C1-fix2", "C1") is True
        # The widened dash-tag suffix must not resurrect the C11-vs-C1
        # false-positive Defect 2(c) was written to prevent.
        assert coas._committed_id_covers_spine_id("C11", "C1") is False

        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C8a-doe/C8p: add `landed` to the plan status enum",
            deliverable_id=_DLV_VALID_SPINE,
        )

        query_ok, committed = coas._committed_chunk_ids(root, _DLV_VALID_SPINE)
        assert query_ok is True
        assert committed == {"C8a-doe", "C8p"}
        assert coas._committed_id_covers_spine_id("C8a-doe", "C8a") is True
        assert any(
            coas._committed_id_covers_spine_id(cid, "C8p") for cid in committed
        )

    def test_numeric_sub_dispatch_suffix_covers_letter_ending_spine_id(
        self, tmp_path
    ):
        """Defect fix, 2026-08-07 (bug-backlog
        `2026-08-07-close-out-and-stamp-reports-key-mismatch-dc4072b44474
        .yaml`): a wave-map fanout that numbers its sub-dispatches
        (`C6a` -> `C6a1`..`C6a7`) must satisfy its parent spine id `C6a`,
        the same way a lettered sub-chunk suffix (`C1a`) already does --
        but ONLY when the base spine id does not itself end in a digit,
        preserving `C11` must-not-cover-`C1` verbatim."""
        assert coas._committed_id_covers_spine_id("C6a1", "C6a") is True
        assert coas._committed_id_covers_spine_id("C6a7", "C6a") is True
        assert coas._committed_id_covers_spine_id("C6b2", "C6b") is True
        # Digit-ending base id: the existing C11-vs-C1 exclusion must not
        # regress -- a trailing digit on a digit-ending base is still a
        # distinct, unrelated spine id, never a sub-dispatch of it.
        assert coas._committed_id_covers_spine_id("C11", "C1") is False

        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C6a1: numbered sub-dispatch of C6a",
            deliverable_id=_DLV_VALID_SPINE,
        )
        _commit_with_subject(
            root,
            "widget.py",
            "C6a1: numbered sub-dispatch commit for a foreign plan",
            deliverable_id="dlv-a-completely-different-plan-abc123",
        )

        query_ok, committed = coas._committed_chunk_ids(
            root, _DLV_VALID_SPINE, spine_ids=["C6a", "C1"]
        )
        assert query_ok is True
        assert "C6a1" in committed
        assert any(
            coas._committed_id_covers_spine_id(cid, "C6a") for cid in committed
        )
        # Deliverable-Id scoping still resolves the SAME-subject, SAME-id
        # commit for the foreign plan when queried under its OWN
        # deliverable id -- a colliding subject never leaks the wrong
        # plan's evidence into the other's committed set.
        query_ok_foreign, committed_foreign = coas._committed_chunk_ids(
            root, "dlv-a-completely-different-plan-abc123", spine_ids=["C6a"]
        )
        assert query_ok_foreign is True
        assert "C6a1" in committed_foreign

    def test_path_shaped_subject_before_colon_is_not_mis_split_into_chunk_ids(
        self, tmp_path
    ):
        """Bounding check on the widened `+`/`/` separator support: a REAL
        subject convention on this branch prefixes the subject with a file
        path, not a chunk-id list (`coordinator/bin/stitch-observer-
        sidecar.py: add --scan standalone leak sweep`). This must not be
        mis-split into bogus ids `coordinator`/`bin`/`stitch-observer-
        sidecar.py` -- none of which are chunk-id-shaped (`_CHUNK_ID_SHAPE_
        RE` requires a leading `C` + digit) -- and in particular must never
        spuriously satisfy a real spine id.
        """
        assert coas._extract_chunk_ids(
            "coordinator/bin/stitch-observer-sidecar.py: add --scan standalone leak sweep"
        ) == []
        assert coas._extract_chunk_ids(
            "docs/wiki: repoint review-brightline-gate canonical invocation"
        ) == []
        # A milestone/wave-tagged subject (real corpus shape, not this
        # oracle's spine-id convention) also contributes nothing.
        assert coas._extract_chunk_ids(
            "g4-M1/M3a/M3b/M4/M4b: commit-authorization teeth"
        ) == []

        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "coordinator/bin/stitch-observer-sidecar.py: add --scan standalone leak sweep",
            deliverable_id=_DLV_VALID_SPINE,
        )
        # C1/C2a/C2b all deliberately left uncommitted -- the path-shaped
        # subject above must not accidentally satisfy any of them.

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert sorted(missing) == ["C1", "C2a", "C2b"]

    def test_prose_subject_after_colon_never_reaches_the_splitter(self, tmp_path):
        """Negative-spec for the id-list/description boundary itself: a
        genuine multi-chunk subject's DESCRIPTION half (everything after
        the first `: `) is never fed to the separator splitter, even when
        it contains slashes and commas that would otherwise look
        id-list-shaped -- `_CHUNK_SUBJECT_RE` only ever captures group(1),
        the leading id-list, so `C1,C2a: touched path/to/file, and
        other/thing too` must register exactly `C1` and `C2a`, never split
        anything out of the description."""
        ids = coas._extract_chunk_ids(
            "C1,C2a: touched path/to/file, and other/thing too"
        )
        assert ids == ["C1", "C2a"]

    def test_leading_token_only_bound_documents_non_leading_chunk_id_miss(self):
        """Pins the KNOWN, DELIBERATE false negative documented in
        `_extract_chunk_ids`'s own docstring (Defect fix, 2026-08-04): a
        subject whose real chunk ids are not the leading token contributes
        NOTHING, even when a spine id is supplied and appears later in the
        subject text. `mise: wave 5 -- xwin-03+04 C12 ... + xwin-05 C3` is a
        live-corpus-shaped example (see the real `mise: wave N --
        DOCTRINE-C7a ...; RESIDUE-C9 ...` / `RESIDUE-C1..C7 ... C8 ...`
        subjects the docstring cites) -- `C12` and `C3` never register even
        though both are named `spine_ids`.

        Also pins WHY this function must not be widened to a full-subject
        scan instead: these three real corpus subjects mention a spine id
        without landing it, and a bare token scan cannot distinguish them
        from a genuine landing commit -- widening would silently OVER-
        credit a chunk that never shipped, the dangerous direction this
        function's docstring already forbids."""
        assert (
            coas._extract_chunk_ids(
                "mise: wave 5 — xwin-03+04 C12 … + xwin-05 C3",
                spine_ids=["C12", "C3"],
            )
            == []
        )
        assert (
            coas._extract_chunk_ids(
                "close: mark C8 shipped, and record why this plan cannot "
                "stamp implemented",
                spine_ids=["C8"],
            )
            == []
        )
        assert (
            coas._extract_chunk_ids(
                "cross-repo: deliver ... C7 sweep deny was inverted memo "
                "from claude-klabauter-em",
                spine_ids=["C7"],
            )
            == []
        )
        assert (
            coas._extract_chunk_ids(
                "doctrine: stage the resolves-trailer zero-join amendment "
                "ahead of claude-klabauter C4",
                spine_ids=["C4"],
            )
            == []
        )


# ===========================================================================
# Non-`C`-prefixed spine ids (Defect fix, 2026-08-01): the multi-id split's
# bounding gate used to be a static `^C\d` shape assumption
# (`_CHUNK_ID_SHAPE_RE`), so a compound subject naming only `A`/`B`/`V`-
# prefixed ids registered ZERO chunk-ids -- observed live, plan
# `docs/plans/2026-08-01-baton-spine-information-integrity.md` (spine ids
# `A1`-`A6`/`B1`-`B4`/`V1`), whose own chunk commits (`7614fb7ad`:
# `A1+A2+A3+A5+B1+B2+B3: ...`; `3dc5b71cd`: `A4+A6+B4: ...`) were fully
# invisible to the oracle, which reported all 11 chunks open on a
# fully-shipped plan. The fix bounds the multi-id split to the plan's own
# spine ids (`spine_ids`, threaded through `_extract_chunk_ids` from
# `_all_spine_ids`) instead of a static shape regex -- see that function's
# and `_CHUNK_ID_SHAPE_RE`'s own docstrings.
# ===========================================================================

_DISPOSITION_ROWS_TEMPLATE = (
    "- id: {a1}\n"
    "  title: baton producer, part 1\n"
    "  deferred: false\n"
    "  body: |\n"
    "    give Resolves: a producer.\n"
    "- id: {a2}\n"
    "  title: baton producer, part 2\n"
    "  deferred: false\n"
    "  body: |\n"
    "    open a peer-delivery door into frozen batons.\n"
    "- id: {b1}\n"
    "  title: peer-delivery ruling\n"
    "  deferred: false\n"
    "  body: |\n"
    "    record the peer-delivery ruling.\n"
    "- id: {b2}\n"
    "  title: ship-state trailer sweep\n"
    "  deferred: false\n"
    "  body: |\n"
    "    sweep the ship-state trailer consumers on the token axis.\n"
    "- id: {v1}\n"
    "  title: verification chunk\n"
    "  deferred: false\n"
    "  body: |\n"
    "    verify the whole wave end to end.\n"
)


class TestNonCPrefixedSpineIds:
    def test_compound_subject_resolves_non_c_prefixed_ids(self, tmp_path):
        """Unit-level pin, direct against the two REAL live-repro subjects
        named in the defect report: `_committed_chunk_ids`, given the
        plan's own `A`/`B`-prefixed spine ids as `spine_ids`, resolves
        every id out of both compound subjects -- the exact resolution
        the static `^C\\d`-only gate could never produce."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = _DISPOSITION_ROWS_TEMPLATE.format(
            a1="A1", a2="A2", b1="B1", b2="B2", v1="V1"
        )
        _seed_disposition_plan(root, rows_yaml)
        _commit_with_subject(
            root,
            "plan.md",
            "A1+A2+A3+A5+B1+B2+B3: give Resolves: a producer, and open a peer-delivery door into frozen batons",
            deliverable_id=_DLV_DISPOSITION,
        )
        _commit_with_subject(
            root,
            "plan.md",
            "A4+A6+B4: record the peer-delivery ruling, and sweep the ship-state trailer consumers on the token axis",
            deliverable_id=_DLV_DISPOSITION,
        )

        spine_ids = ["A1", "A2", "A3", "A4", "A5", "A6", "B1", "B2", "B3", "B4", "V1"]
        query_ok, committed = coas._committed_chunk_ids(
            root, _DLV_DISPOSITION, spine_ids
        )
        assert query_ok is True
        assert committed == {
            "A1", "A2", "A3", "A5", "B1", "B2", "B3", "A4", "A6", "B4",
        }
        # The static fallback gate (no `spine_ids`) is the OLD, defective
        # behavior -- pinned here so a future edit cannot silently widen it
        # back to admitting these ids unconditionally.
        assert coas._extract_chunk_ids("A1+A2+A3+A5+B1+B2+B3: ...") == []

    def test_fully_shipped_plan_with_code_only_commits_stamps_implemented(
        self, tmp_path, monkeypatch
    ):
        """Requirement: a fully-shipped plan whose chunk commits touch ONLY
        code (never the plan file itself) reports zero open chunks and
        stamps `implemented` -- every commit below uses `touch_file` to
        land against a throwaway code path, exactly the shape a real
        background-Workflow executor produces (see this module's own
        docstring § Range choice: executors are barred from writing plan
        bodies at all)."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = _DISPOSITION_ROWS_TEMPLATE.format(
            a1="A1", a2="A2", b1="B1", b2="B2", v1="V1"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_with_subject(
            root,
            "plan.md",
            "A1+A2: land the first pair of chunks",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="a.py",
        )
        _commit_with_subject(
            root,
            "plan.md",
            "B1+B2: land the second pair of chunks",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="b.py",
        )
        _commit_with_subject(
            root,
            "plan.md",
            "V1: verify the whole wave",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="v.py",
        )

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["open_chunk_ids"] == []
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head

    def test_partial_plan_reports_missing_and_skips_stamp(self, tmp_path, monkeypatch):
        """Regression guard (must NOT change): a genuinely partial plan --
        one non-`C`-prefixed chunk (`V1`) never committed at all -- still
        reports it missing and the stamp is correctly skipped. An oracle
        that over-detects (crediting `V1` when it never shipped) would be
        worse than the original under-detection defect, since it would
        silently close out unfinished work."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = _DISPOSITION_ROWS_TEMPLATE.format(
            a1="A1", a2="A2", b1="B1", b2="B2", v1="V1"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_with_subject(
            root,
            "plan.md",
            "A1+A2+B1+B2: land everything except the verification chunk",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="everything-but-v1.py",
        )
        # V1 deliberately left uncommitted.

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["V1"]
        assert result["status_target"] is None
        assert _read_status(plan_file) == "executing"
        # Nothing this op does not own was written or committed beyond the
        # halted-state ceremony commit itself; in particular, no `status:`
        # flip landed.
        assert _head_sha(root) != pre_head

    def test_subject_match_before_merge_base_range_is_excluded_when_plan_text_absent(
        self, tmp_path
    ):
        """Pre-range-fix behavior, preserved as a rung-3-only regression
        (`_chunk_evidence_log_range`'s § Range choice, widened again):
        without `plan_text` (a caller with no plan text at hand, e.g. a
        direct unit-test call), the range still falls back to the plain
        `merge-base origin/main HEAD`..`HEAD` bound, so a commit AT the
        merge-base itself is still excluded. See
        `test_chunk_commits_behind_a_later_advanced_origin_main_are_still_attributed`
        below for the WIDENED (plan-text-supplied) behavior this fix adds."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = _DISPOSITION_ROWS_TEMPLATE.format(
            a1="A1", a2="A2", b1="B1", b2="B2", v1="V1"
        )
        _seed_disposition_plan(root, rows_yaml)
        _commit_with_subject(
            root,
            "plan.md",
            "A1: shipped before this branch's own divergence point",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="pre-divergence.py",
        )
        _set_origin_main(root)
        _commit_with_subject(
            root,
            "plan.md",
            "A2: shipped after this branch's own divergence point",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="post-divergence.py",
        )

        spine_ids = ["A1", "A2", "B1", "B2", "V1"]
        query_ok, committed = coas._committed_chunk_ids(
            root, _DLV_DISPOSITION, spine_ids
        )
        assert query_ok is True
        assert "A1" not in committed
        assert "A2" in committed

    def test_chunk_commits_behind_a_later_advanced_origin_main_are_still_attributed(
        self, tmp_path
    ):
        """Range-fix regression (2026-08-07 -- see this module's own
        bug-backlog entry `2026-08-07-close-out-and-stamp-s-chunk-evidence-
        joi-8b6a7a32d833.yaml`, two independent sightings): supersedes this
        class's own prior single assertion, which pinned the CONFIRMED BUG
        this fix closes -- a chunk commit that had already landed before
        `origin/main` advanced past it read as "outside the evidence
        window" and was silently excluded, even though it genuinely
        belonged to THIS plan's own deliverable.

        On a shared-main workflow `origin/main` advances as PEERS push,
        independently of this plan's own chunk work -- interleaving peer
        commits between this plan's own chunk commits, then advancing
        `origin/main` past ALL of them (the exact shape both live
        incidents in the backlog entry record), must not cause
        `merge-base(origin/main, HEAD)` to swallow this plan's own
        already-landed chunk commits, PROVIDED the caller supplies
        `plan_text` so `_chunk_evidence_log_range` can widen past the
        (now-degenerate) merge-base bound -- see `_chunk_evidence_log_range`'s
        own docstring for the full rung ladder."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = _DISPOSITION_ROWS_TEMPLATE.format(
            a1="A1", a2="A2", b1="B1", b2="B2", v1="V1"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        _commit_with_subject(
            root,
            "plan.md",
            "A1: first chunk lands",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="a1.py",
        )
        # A peer commit, unrelated to this plan, interleaved between chunk
        # commits -- the ordinary shape of a shared-main worktree.
        _commit_with_subject(
            root, "plan.md", "peer: unrelated work", touch_file="peer1.py"
        )
        _commit_with_subject(
            root,
            "plan.md",
            "A2: second chunk lands",
            deliverable_id=_DLV_DISPOSITION,
            touch_file="a2.py",
        )
        _commit_with_subject(
            root, "plan.md", "peer: more unrelated work", touch_file="peer2.py"
        )
        # origin/main now advances PAST every commit landed so far --
        # simulating peers pushing/mirroring this branch's own history
        # forward, the root cause the pre-fix `merge-base origin/main HEAD`
        # bound was blind to.
        _set_origin_main(root)

        spine_ids = ["A1", "A2", "B1", "B2", "V1"]
        plan_text = plan_file.read_text(encoding="utf-8")
        query_ok, committed = coas._committed_chunk_ids(
            root, _DLV_DISPOSITION, spine_ids, plan_text=plan_text
        )
        assert query_ok is True
        assert "A1" in committed
        assert "A2" in committed


# ===========================================================================
# Cross-repo scope scanning (Defect fix, 2026-07-27 -- the last
# false-negative in this oracle): the completeness scan used to look only
# at `repo_root`, so a chunk that legitimately shipped as a commit in a
# SIBLING repo the plan's own `scope:` names could never be seen. These
# tests pin the fix directly against `close_out_and_stamp.py`'s new
# `_plan_sibling_repo_ids` / `_resolve_sibling_repo_root` /
# `_sibling_committed_chunk_ids` trio, composed with BOTH pre-existing
# fixes this defect must not weaken: Deliverable-Id trailer scoping and
# multi-chunk subject parsing, both of which apply identically to a
# sibling repo's own commits.
# ===========================================================================

_DLV_SIBLING = "dlv-fixture-sibling-000001"

_SIBLING_PLAN_TEMPLATE = """---
title: "Fixture plan — cross-repo sibling scan"
created: 2026-07-27
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-07-27"
plan_id: "pln-fixture-sibling-000001"
deliverable_id: "dlv-fixture-sibling-000001"
scope:
  - plan.md
  - sibling-repo: some/path/in/sibling.py
---

# Fixture plan — cross-repo sibling scan

## Tasks

```yaml plan-tasks
{rows}
```
"""

_NO_SIBLING_PLAN_TEMPLATE = """---
title: "Fixture plan — no sibling scope prefixes"
created: 2026-07-27
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-07-27"
plan_id: "pln-fixture-no-sibling-000001"
deliverable_id: "dlv-fixture-sibling-000001"
scope:
  - plan.md
  - coordinator_core/execute_plan_assemble/close_out_and_stamp.py
---

# Fixture plan — no sibling scope prefixes

## Tasks

```yaml plan-tasks
{rows}
```
"""


def _seed_sibling_scope_plan(
    root: Path,
    rows_yaml: str,
    *,
    status: str = "executing",
    template: str = _SIBLING_PLAN_TEMPLATE,
    dest_name: str = "plan.md",
) -> Path:
    """Seeds a plan whose `scope:` frontmatter names a sibling repo via the
    documented `<repo-id>: <path>` prefix grammar -- mirrors
    `_seed_disposition_plan` (same inline-template shape, same disposition-
    ready `## Tasks` spine) but adds the `scope:` block this file's other
    fixtures never carry, since cross-repo sibling scanning is exactly the
    behavior under test here."""
    text = template.format(status=status, rows=rows_yaml)
    dest = root / dest_name
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


_SIBLING_ROW_C1_OPEN = "- id: C1\n  title: land in sibling repo\n  disposition: open\n"


class TestCrossRepoSiblingScan:
    def test_chunk_credited_from_sibling_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """THE FIX, reproduced directly: `C1` is committed ONLY in the
        sibling repo the plan's own `scope:` names (`sibling-repo:`), never
        in the home repo -- against the PRE-FIX code (single-repo scan)
        this would report `shipped=False`, `missing=["C1"]`; the fix must
        report `shipped=True`, `missing=[]`."""
        root = tmp_path / "home"
        root.mkdir()
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        _init_repo(root)
        _init_repo(sibling_root)
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SIBLING_REPO", str(sibling_root))

        plan_file = _seed_sibling_scope_plan(root, _SIBLING_ROW_C1_OPEN)
        _commit_chunk(sibling_root, "dummy.txt", "C1", deliverable_id=_DLV_SIBLING)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []

    def test_sibling_commit_with_non_matching_deliverable_id_credits_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """The Deliverable-Id gate applies to a sibling repo's own commits
        exactly as it does to the home repo's -- a sibling commit whose
        subject matches `C1` but whose `Deliverable-Id` trailer belongs to
        a DIFFERENT plan must never count as evidence."""
        root = tmp_path / "home"
        root.mkdir()
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        _init_repo(root)
        _init_repo(sibling_root)
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SIBLING_REPO", str(sibling_root))

        plan_file = _seed_sibling_scope_plan(root, _SIBLING_ROW_C1_OPEN)
        _commit_chunk(
            sibling_root, "dummy.txt", "C1", deliverable_id="dlv-some-other-plan-000099"
        )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]

    def test_unresolvable_sibling_is_skipped_not_crashed_and_surfaced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A sibling repo that is not registered on this machine (never
        cloned here / no registry entry) must NOT crash the close-out, and
        must NOT be silently swallowed either -- it is skipped, and the
        skip is surfaced in `close_out_and_stamp`'s own result dict via
        `skipped_sibling_repos`. Since the skipped sibling's evidence is
        the ONLY thing that could satisfy `C1`, this plan correctly does
        NOT claim full-shipped on that basis alone."""
        root = tmp_path
        # Deliberately do NOT set MACHINE_LOCAL_REPOS_SIBLING_REPO -- this
        # sibling is unresolvable on this machine.
        monkeypatch.delenv("MACHINE_LOCAL_REPOS_SIBLING_REPO", raising=False)
        _init_repo(root)
        plan_file = _seed_sibling_scope_plan(root, _SIBLING_ROW_C1_OPEN)

        exit_code, result, pre_call_head_sha = _run_close_out(
            monkeypatch, root, "plan.md"
        )

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C1"]
        assert len(result["skipped_sibling_repos"]) == 1
        assert result["skipped_sibling_repos"][0].startswith("sibling-repo: ")
        # No crash, and the op still made forward progress on its own
        # write (status: executing -> landed is NOT reached here since
        # shipped is False; only auto-resolve/stamp bookkeeping, if any,
        # is exercised elsewhere -- this test's own assertion surface is
        # the skip list and the non-crash).
        assert _head_sha(root) != pre_call_head_sha or _head_sha(root) == pre_call_head_sha

    def test_no_sibling_prefixes_in_scope_is_unchanged_from_today(self, tmp_path):
        """The common case: a plan's `scope:` names only local-repo paths,
        no `<repo-id>:` prefix at all. `_plan_sibling_repo_ids` must return
        `[]`, `_sibling_committed_chunk_ids` must return `(set(), [])`
        unconditionally, and `_determine_shipped`'s verdict must be
        byte-identical to its pre-fix behavior (already exercised by every
        other test in this file, none of which declare a sibling-prefixed
        `scope:` at all) -- this is the explicit regression guard."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_sibling_scope_plan(
            root, _SIBLING_ROW_C1_OPEN, template=_NO_SIBLING_PLAN_TEMPLATE
        )
        text = plan_file.read_text(encoding="utf-8")

        assert coas._plan_sibling_repo_ids(text) == []
        committed, skipped = coas._sibling_committed_chunk_ids(text, _DLV_SIBLING)
        assert committed == set()
        assert skipped == []

        # No commit anywhere -- C1 stays genuinely uncommitted.
        shipped, missing, join_provenance, error = coas._determine_shipped(text, "plan.md", root)
        assert error is None
        assert shipped is False
        assert missing == ["C1"]

    def test_sibling_shipped_chunk_is_not_auto_resolved_row_stays_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Sibling-SHA auto-resolve decision (see this module's own
        docstring § Cross-repo scope scanning): a chunk shipped ONLY via a
        sibling repo's commit is credited for the shipped/missing verdict,
        but its spine row is deliberately NOT auto-resolved to `disposition:
        coded` (a bare sha would be ambiguous without knowing which repo it
        came from) -- it stays `open`, and the plan's `status:` lands on
        the intermediate `landed` target (D9), never `implemented`, until a
        human runs `resolve --coded` manually."""
        root = tmp_path / "home"
        root.mkdir()
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        _init_repo(root)
        _init_repo(sibling_root)
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SIBLING_REPO", str(sibling_root))

        plan_file = _seed_sibling_scope_plan(root, _SIBLING_ROW_C1_OPEN)
        _commit_chunk(sibling_root, "dummy.txt", "C1", deliverable_id=_DLV_SIBLING)

        exit_code, result, _pre_call_head_sha = _run_close_out(
            monkeypatch, root, "plan.md"
        )

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["skipped_sibling_repos"] == []
        assert result["status_target"] == coas._LANDED_STATUS
        assert _read_status(plan_file) == coas._LANDED_STATUS

        rows = _spine_rows(plan_file)
        c1_row = next(r for r in rows if r.get("id") == "C1")
        assert c1_row.get("disposition", "open") == "open"
        assert "disposition_ref" not in c1_row


# ===========================================================================
# `_SCOPE_SIBLING_PREFIX_RE` grammar (Defect fix, 2026-07-27): the pattern
# used to require MANDATORY whitespace after the colon (`\s+`), a form no
# real plan author ever writes -- YAML parses `- repo: path` as a mapping,
# so authors write the space-free `- repo:path` form to keep the scope
# entry a plain string. The mandatory-whitespace grammar made every
# sibling-repo scope entry ever written invisible to
# `_plan_sibling_repo_ids`, silently disabling the cross-repo scan these
# tests exercise above. These tests pin the fix (whitespace now OPTIONAL)
# together with the two safety properties the fix must not regress:
# Windows drive letters and URLs must never parse as a sibling-repo
# prefix.
# ===========================================================================


class TestScopeSiblingPrefixRegexGrammar:
    def test_zero_space_form_matches(self):
        """THE FIX, reproduced directly: `<repo-id>:<path>` with NO space
        after the colon -- the form every real plan actually writes (see
        `grep -rhoE '^\\s+- [a-z0-9-]+:[^ ]+' docs/plans/*.md` in
        DoE-claude) -- must match."""
        match = coas._SCOPE_SIBLING_PREFIX_RE.match(
            "claude-klabauter:coordinator_core/dag.py"
        )
        assert match is not None
        assert match.group(1) == "claude-klabauter"
        assert match.group(2) == "coordinator_core/dag.py"

    def test_single_space_form_still_matches(self):
        """The pre-fix documented form (`<repo-id>: <path>`, one space)
        must keep matching -- this is the exact form the existing
        `_SIBLING_PLAN_TEMPLATE` fixture above uses, and is not itself the
        defect (whitespace being ALLOWED was always fine; whitespace being
        REQUIRED was the bug)."""
        match = coas._SCOPE_SIBLING_PREFIX_RE.match(
            "sibling-repo: some/path/in/sibling.py"
        )
        assert match is not None
        assert match.group(1) == "sibling-repo"
        assert match.group(2) == "some/path/in/sibling.py"

    def test_multi_space_form_still_matches(self):
        """Multiple spaces after the colon (accidental extra whitespace,
        or a hand-aligned YAML block) must also still match -- `\\s*` is
        zero-OR-MORE, not zero-or-one."""
        match = coas._SCOPE_SIBLING_PREFIX_RE.match(
            "claude-klabauter:   coordinator_core/dag.py"
        )
        assert match is not None
        assert match.group(1) == "claude-klabauter"
        assert match.group(2) == "coordinator_core/dag.py"

    def test_windows_drive_letter_backslash_does_not_match(self):
        """`C:\\Users\\foo\\bar` must NEVER parse as a sibling-repo prefix
        -- a single-character drive letter can never satisfy the
        `[A-Za-z][A-Za-z0-9_-]+` repo-id group, which requires a MINIMUM
        of two characters (one mandatory leading letter, one-or-more
        additional characters). This property is unchanged by the
        whitespace fix -- the two-char minimum was never the broken
        part."""
        assert coas._SCOPE_SIBLING_PREFIX_RE.match(r"C:\Users\foo\bar") is None

    def test_windows_drive_letter_forward_slash_does_not_match(self):
        """`D:/foo/bar` -- the forward-slash-style Windows path some tools
        emit -- must also never match, for the same single-character
        reason as the backslash form above."""
        assert coas._SCOPE_SIBLING_PREFIX_RE.match("D:/foo/bar") is None

    def test_https_url_does_not_match(self):
        """`https://example.com/x` must NEVER parse as a sibling-repo
        prefix. Unlike a drive letter, `https` is 5 characters and WOULD
        satisfy the two-char repo-id minimum on its own -- what excludes
        it is the `(?!//)` negative lookahead placed immediately after
        the colon (BEFORE `\\s*` consumes anything): a URL's `://` means
        the two characters right after the colon are always `//`, so the
        lookahead fails and the match cannot anchor here at all."""
        assert (
            coas._SCOPE_SIBLING_PREFIX_RE.match("https://example.com/x") is None
        )

    def test_http_url_does_not_match(self):
        """Same as the `https` case, for the shorter `http` scheme --
        confirms the `(?!//)` guard, not scheme-length, is doing the
        work."""
        assert coas._SCOPE_SIBLING_PREFIX_RE.match("http://example.com/x") is None

    def test_bare_path_without_colon_does_not_match(self):
        """A bare local-repo scope path with no colon at all (the common
        case -- most `scope:` entries name a path in the home repo, never
        a sibling) must not match; the regex requires a colon."""
        assert (
            coas._SCOPE_SIBLING_PREFIX_RE.match(
                "coordinator_core/execute_plan_assemble/close_out_and_stamp.py"
            )
            is None
        )

    def test_prose_line_with_colon_and_space_is_rejected_downstream(self):
        """A prose line shaped like `Note: see below` DOES match the bare
        regex (`Note` is a valid-shaped repo-id token) -- the regex alone
        cannot distinguish it from a real sibling-repo prefix. The
        rejection is `_plan_sibling_repo_ids`'s own downstream ` " " in
        rest` check (see that function's docstring), which this test
        pins explicitly so the regex-only view above isn't mistaken for
        the full picture."""
        match = coas._SCOPE_SIBLING_PREFIX_RE.match("Note: see below")
        assert match is not None
        rest = match.group(2).strip()
        assert " " in rest  # `_plan_sibling_repo_ids` rejects on this

    def test_plan_sibling_repo_ids_recognizes_zero_space_scope_entry(self):
        """End-to-end through `_plan_sibling_repo_ids` (not just the bare
        regex): a `scope:` list entry written in the real-world zero-space
        form must now be recognized as naming a sibling repo -- this is
        the exact shape that was previously silently invisible."""
        plan_text = """---
title: "Fixture"
scope:
  - plan.md
  - claude-klabauter:coordinator_core/dag.py
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == ["claude-klabauter"]

    def test_plan_sibling_repo_ids_still_ignores_drive_letter_and_url_entries(self):
        """End-to-end: a `scope:` list containing a Windows path and a URL
        (neither of which is a real sibling-repo prefix) must not be
        mistaken for one, even after the whitespace fix."""
        plan_text = r"""---
title: "Fixture"
scope:
  - plan.md
  - C:\Users\foo\bar
  - https://example.com/x
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == []


# ===========================================================================
# `_SCOPE_SIBLING_SLASH_RE` / registry-gated slash-form sibling recognition
# (Defect fix, 2026-08-01): the colon grammar above never covered the
# `<repo-id>/<path>` shape a real plan (`docs/plans/2026-08-01-baton-spine-
# information-integrity.md`) used exclusively, so its cross-repo chunks
# read as permanently open. The fix's own docstring is explicit that a
# slash-form match is trusted ONLY when `_resolve_sibling_repo_root`
# actually resolves it against the machine-local registry -- unlike the
# colon form, an unresolvable slash-form entry is NOT surfaced in
# `skipped_sibling_repos` at all (a stated, deliberate asymmetry, not an
# oversight). These tests pin the fix plus that known limitation.
# ===========================================================================


class TestScopeSiblingSlashRegexGrammar:
    def test_slash_form_regex_matches(self):
        """The bare grammar: `<repo-id>/<path>`, no colon at all."""
        match = coas._SCOPE_SIBLING_SLASH_RE.match(
            "claude-klabauter/coordinator_core/ops/rollup_derive.py"
        )
        assert match is not None
        assert match.group(1) == "claude-klabauter"
        assert match.group(2) == "coordinator_core/ops/rollup_derive.py"

    def test_registered_slash_form_sibling_is_recognized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """THE FIX, reproduced against the live case: a `scope:` entry
        shaped `claude-klabauter/coordinator_core/ops/rollup_derive.py`,
        for a repo-id that IS registered on this machine, resolves to
        `['claude-klabauter']` -- pre-fix this registered zero siblings at
        all, since only the colon grammar was ever tried."""
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_CLAUDE_KLABAUTER_REPO", str(tmp_path))
        plan_text = """---
title: "Fixture"
scope:
  - plan.md
  - claude-klabauter/coordinator_core/ops/rollup_derive.py
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == ["claude-klabauter"]

    def test_registered_repo_id_that_is_also_a_local_dir_is_not_a_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Review: code-reviewer -- Finding 1, 2026-08-02: a registry match
        alone is NOT sufficient to accept a slash-form entry as a sibling
        reference. Here `some-dir` is BOTH a registered `repos.<id>` on
        this machine AND a real local directory in the plan's own
        `repo_root` -- the over-detection direction the containment check
        exists to close. Without it, `_plan_sibling_repo_ids` would
        misclassify `some-dir/file.py` as a sibling reference and trigger a
        scan of the wrong repo, unioning its committed chunk-ids into this
        plan's evidence and risking an unfinished plan stamped
        `implemented`. This test DOES fail against the pre-fix baseline
        (pre-fix it returns `['some-dir']`; post-fix it returns `[]`)."""
        home_root = tmp_path / "home"
        home_root.mkdir()
        (home_root / "some-dir").mkdir()
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SOME_DIR", str(sibling_root))
        plan_text = """---
title: "Fixture"
scope:
  - plan.md
  - some-dir/file.py
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text, home_root) == []
        # Without a repo_root to check containment against, the registry
        # match alone still governs (documented degrade-safe posture).
        assert coas._plan_sibling_repo_ids(plan_text) == ["some-dir"]

    def test_local_paths_do_not_misfire_as_slash_form_siblings(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The property the registry gate exists to protect: an ordinary
        `scope:` list of repo-relative local paths, none of whose leading
        segments are registered sibling repo-ids, must resolve to `[]`.
        Without the registry gate, `coordinator/bin/widget.py` would be
        misread as naming a sibling repo `coordinator` purely by shape --
        this is the exact ambiguity `_plan_sibling_repo_ids`'s own
        docstring names between a local path and a sibling reference.

        Review: code-reviewer -- Finding 2: this test does NOT demonstrate
        `ee30c5a7c`'s slash-grammar fix and would pass identically against
        the pre-fix baseline -- pre-fix, `_plan_sibling_repo_ids` never
        attempted the slash grammar at all, so any slash-form-only `scope:`
        list resolved to `[]` regardless of registry state. What this test
        actually guards is a DIFFERENT, hypothetical future regression: a
        shape-only widening of slash-form recognition that skips the
        registry gate entirely. See
        `test_registered_slash_form_sibling_is_recognized` above for the
        test that DOES fail pre-fix and validates the fix itself."""
        monkeypatch.delenv("MACHINE_LOCAL_REPOS_COORDINATOR", raising=False)
        monkeypatch.delenv("MACHINE_LOCAL_REPOS_DOCS", raising=False)
        plan_text = """---
title: "Fixture"
scope:
  - coordinator/bin/widget.py
  - docs/wiki/foo.md
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == []

    def test_colon_form_still_works_alongside_slash_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Regression guard: adding the slash grammar must not disturb the
        pre-existing colon grammar -- both forms recognized in the same
        `scope:` list, each resolving its own repo id in first-seen
        order."""
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_CLAUDE_KLABAUTER_REPO", str(tmp_path))
        plan_text = """---
title: "Fixture"
scope:
  - plan.md
  - sibling-repo: some/path/in/sibling.py
  - claude-klabauter/coordinator_core/ops/rollup_derive.py
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == [
            "sibling-repo",
            "claude-klabauter",
        ]

    def test_unregistered_slash_form_repo_is_not_recognized_known_limitation(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """KNOWN LIMITATION, pinned deliberately rather than papered over
        (see `_plan_sibling_repo_ids`'s own docstring § "KNOWN LIMITATION,
        stated rather than papered over"): a slash-form entry naming a
        repo NOT registered on this machine is indistinguishable from an
        ordinary local path and is silently NOT recognized as
        sibling-shaped at all. Unlike the colon form's own unresolvable-
        sibling handling, this does NOT surface in `skipped_sibling_repos`
        either -- the scanner never learns a sibling was named in the
        first place. This is a deliberate asymmetry the docstring commits
        to (gating slash-form recognition on shape alone would instead
        misfire on every ordinary plan with a slash-shaped local scope
        entry), not a gap to close.

        Review: code-reviewer -- Finding 2: this test does NOT demonstrate
        `ee30c5a7c`'s slash-grammar fix and would pass identically against
        the pre-fix baseline -- pre-fix, the slash grammar didn't exist at
        all, so an unregistered (or any) slash-form entry already resolved
        to `[]` unconditionally. It guards a different, hypothetical future
        regression: this KNOWN LIMITATION being silently narrowed or
        widened later without a docstring update to match. It does not
        validate that the fix itself works.
        """
        monkeypatch.delenv("MACHINE_LOCAL_REPOS_UNREGISTERED_SIBLING", raising=False)
        plan_text = """---
title: "Fixture"
scope:
  - plan.md
  - unregistered-sibling/some/path.py
---

# Fixture
"""
        assert coas._plan_sibling_repo_ids(plan_text) == []
        committed, skipped = coas._sibling_committed_chunk_ids(plan_text, _DLV_SIBLING)
        assert committed == set()
        assert skipped == []  # not even surfaced as skipped -- see docstring above


_SLASH_SIBLING_PLAN_TEMPLATE = """---
title: "Fixture plan — slash-form cross-repo sibling scan"
created: 2026-08-01
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-08-01"
plan_id: "pln-fixture-slash-sibling-000001"
deliverable_id: "dlv-fixture-sibling-000001"
scope:
  - plan.md
  - sibling-repo/some/path/in/sibling.py
---

# Fixture plan — slash-form cross-repo sibling scan

## Tasks

```yaml plan-tasks
{rows}
```
"""


class TestCrossRepoSlashFormSiblingScan:
    def test_chunk_credited_from_sibling_repo_via_slash_form_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """End-to-end: a plan whose `scope:` names a sibling repo via the
        SLASH form, whose chunk commit landed only in that sibling repo
        (never in the home repo), must be detected as committed -- mirrors
        `TestCrossRepoSiblingScan.test_chunk_credited_from_sibling_repo`
        but for the slash grammar this fix adds rather than the
        pre-existing colon grammar. Against the pre-fix code (colon-only
        recognition) this would report `shipped=False`, `missing=["C1"]`."""
        root = tmp_path / "home"
        root.mkdir()
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        _init_repo(root)
        _init_repo(sibling_root)
        monkeypatch.setenv("MACHINE_LOCAL_REPOS_SIBLING_REPO", str(sibling_root))

        plan_file = _seed_sibling_scope_plan(
            root, _SIBLING_ROW_C1_OPEN, template=_SLASH_SIBLING_PLAN_TEMPLATE
        )
        _commit_chunk(sibling_root, "dummy.txt", "C1", deliverable_id=_DLV_SIBLING)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []


# ---------------------------------------------------------------------------
# A5 -- `plan_path_rel` posix output (2026-07-28)
# ---------------------------------------------------------------------------


def test_plan_path_rel_uses_rel_id_not_bare_str_relative_to():
    # A5 fix: `close_out_and_stamp` now computes `plan_path_rel` via
    # `coordinator_core.wire_paths.rel_id(live_path, root)`, not
    # `str(live_path.relative_to(root))`. `plan_path_rel` is matched below
    # against git-derived paths (`_determine_shipped`), which are ALWAYS
    # forward-slash -- `str()` renders `os.sep`, which is `\` on Windows and
    # would silently misclassify shipped/missing chunks at plan close-out on
    # that host.
    assert coas.rel_id is not None
    from pathlib import PureWindowsPath

    root = PureWindowsPath("C:/repo")
    plan = PureWindowsPath("C:/repo/docs/plans/2026-07-20-x.md")
    rel = plan.relative_to(root)
    assert str(rel) == "docs\\plans\\2026-07-20-x.md"
    assert rel.as_posix() == "docs/plans/2026-07-20-x.md"


def test_close_out_and_stamp_nested_plan_path_rel_is_forward_slash(tmp_path, monkeypatch):
    # Regression/functional check with a real (POSIX) filesystem: a plan
    # nested two directories deep must still resolve to a clean
    # forward-slash-joined relative path for git-comparison purposes.
    root = tmp_path
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "test@example.com"], root)
    _run_git(["config", "user.name", "Test"], root)

    plan_dir = root / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_rel = "docs/plans/2026-07-20-nested-plan.md"
    plan_file = plan_dir / "2026-07-20-nested-plan.md"
    plan_file.write_text(
        "---\nstatus: reviewed\nplan_id: nested-plan\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- {id: C1, title: chunk one, pm_approved: true}\n```\n",
        encoding="utf-8",
    )
    _run_git(["add", plan_rel], root)
    _run_git(["commit", "-q", "-m", "seed plan"], root)

    exit_code, result, _pre_sha = _run_close_out(monkeypatch, root, plan_rel)

    # Not asserting success (no chunk commits exist) -- only that any
    # plan-path-derived text in the result is forward-slash-joined, never
    # backslash-joined.
    rendered = str(result)
    assert "docs\\plans" not in rendered


# ===========================================================================
# Deliverable-Id equivalence join (2026-08-04, `state/deliverable-
# equivalence.yaml` wiring) -- pins that `_committed_chunk_shas` and
# `_deliverable_id_near_miss_diagnostics` join a declared fork pair (the C7
# shape, `docs/plans/2026-08-03-scope-guard-peer-claim-release.md`), that a
# pair ABSENT from the map behaves exactly as before, that the other three
# join-provenance verdicts are unaffected, and that no frontmatter is ever
# mutated by the canonicalization itself. See close_out_and_stamp.py's own
# docstring § "Deliverable-Id equivalence join" for the join-key-only
# negative-spec this class exercises.
# ===========================================================================


class TestDeliverableEquivalenceJoin:
    @pytest.fixture(autouse=True)
    def _reset_equivalence_memo(self):
        from coordinator_core.ops.deliverable_equivalence import (
            _reset_equivalence_map_cache,
        )

        _reset_equivalence_map_cache()
        yield
        _reset_equivalence_map_cache()

    @staticmethod
    def _write_equivalence_map(root: Path, loser: str, winner: str) -> None:
        state_dir = root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "deliverable-equivalence.yaml").write_text(
            "entries:\n"
            f"  - loser: {loser}\n"
            f"    winner: {winner}\n"
            "    evidence: 'test fixture'\n",
            encoding="utf-8",
        )

    def test_forked_pair_present_in_map_joins(self, tmp_path):
        """The C7 shape: the plan's own frontmatter `deliverable_id:`
        carries the LOSING leg of a declared fork; the covering commits
        actually carry the WINNING leg's `Deliverable-Id:` trailer. With
        the equivalence entry declared, `_committed_chunk_shas` now joins
        them -- against the PRE-FIX code (raw equality, no canonicalize())
        this would report every chunk missing and `join_provenance ==
        "key_mismatch"`."""
        root = tmp_path
        _init_repo(root)
        winner_id = "dlv-fixture-valid-spine-winner-000002"
        self._write_equivalence_map(root, _DLV_VALID_SPINE, winner_id)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=winner_id)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []
        assert join_provenance == "joined"

    def test_pair_absent_from_map_behaves_exactly_as_today(self, tmp_path):
        """An equivalence map IS present on disk, but carries no entry for
        this plan's own `deliverable_id`/trailer pair -- must behave
        identically to no map at all (`key_mismatch`, nothing joined).
        This is the "grants nothing on its own" guarantee: presence of the
        artifact must never be conflated with presence of a matching
        entry."""
        root = tmp_path
        _init_repo(root)
        unrelated_winner = "dlv-some-unrelated-fork-winner-000003"
        self._write_equivalence_map(
            root, "dlv-some-unrelated-fork-loser-000004", unrelated_winner
        )
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        other_id = "dlv-some-other-plan-000002"
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=other_id)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert sorted(missing) == ["C1", "C2a", "C2b"]
        assert join_provenance == "key_mismatch"

    def test_no_join_key_provenance_unaffected_by_map_presence(self, tmp_path):
        """A plan with no `deliverable_id:` frontmatter at all still
        reports `no_join_key` even when an (irrelevant) equivalence map is
        present -- the join is never attempted regardless of the map."""
        root = tmp_path
        _init_repo(root)
        self._write_equivalence_map(
            root, "dlv-some-unrelated-fork-loser-000005", "dlv-some-unrelated-fork-winner-000006"
        )
        text = _FIXTURE_VALID_SPINE.read_text(encoding="utf-8").replace(
            '\ndeliverable_id: "dlv-fixture-valid-spine-000001"\n', "\n"
        )
        assert "deliverable_id" not in text
        plan_file = root / "plan.md"
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            text, "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert join_provenance == "no_join_key"

    def test_no_join_candidates_provenance_unaffected_by_map_presence(self, tmp_path):
        """`deliverable_id:` is present, but zero commits in range carry
        ANY `Deliverable-Id` trailer -- `no_join_candidates`, unchanged by
        an (irrelevant) equivalence map being present on disk."""
        root = tmp_path
        _init_repo(root)
        self._write_equivalence_map(
            root, "dlv-some-unrelated-fork-loser-000007", "dlv-some-unrelated-fork-winner-000008"
        )
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id)  # untrailered

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert join_provenance == "no_join_candidates"

    def test_near_miss_diagnostic_excludes_a_now_joined_pair(self, tmp_path):
        """Once a fork pair is declared in the equivalence map,
        `_deliverable_id_near_miss_diagnostics` must no longer report the
        winner-trailered commit as a near-miss candidate for the loser
        `deliverable_id` -- that pair now joins as real evidence
        (`_committed_chunk_shas`), so reporting it here too would be
        stale/contradictory advice."""
        root = tmp_path
        _init_repo(root)
        winner_id = "dlv-fixture-valid-spine-winner-000002"
        self._write_equivalence_map(root, _DLV_VALID_SPINE, winner_id)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=winner_id)

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1"]
        )
        assert candidates == []

    def test_near_miss_diagnostic_still_fires_for_an_undeclared_pair(self, tmp_path):
        """The companion negative case: an equivalence map is present, but
        carries no entry for this specific pair -- the near-miss diagnostic
        must still fire exactly as before this fix."""
        root = tmp_path
        _init_repo(root)
        self._write_equivalence_map(
            root, "dlv-some-unrelated-fork-loser-000009", "dlv-some-unrelated-fork-winner-000010"
        )
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        other_id = "dlv-percolate-root-rung-ordering-the-doe-roo-a8c947"
        _commit_chunk(root, "plan.md", "C1", deliverable_id=other_id)

        candidates = coas._deliverable_id_near_miss_diagnostics(
            root, _DLV_VALID_SPINE, ["C1"]
        )
        assert candidates == [{"deliverable_id": other_id, "commit_count": 1}]

    def test_no_frontmatter_mutated_by_the_join(self, tmp_path, monkeypatch):
        """Full end-to-end `close_out_and_stamp` run over a joined fork
        pair: the plan's own `deliverable_id:` frontmatter value stays
        EXACTLY as it was on disk (the loser id) -- canonicalize() is a
        join-key transform used only in-memory for the comparison, never
        written back to the plan (`deliverable_equivalence.py`'s own
        negative-spec)."""
        root = tmp_path
        _init_repo(root)
        winner_id = "dlv-fixture-valid-spine-winner-000002"
        self._write_equivalence_map(root, _DLV_VALID_SPINE, winner_id)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=winner_id)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["shipped"] is True
        assert result["stamped"] is True
        # status: flipped (this op's own stamp step) -- but deliverable_id:
        # itself, the join key this fix touches, must be untouched.
        assert _read_status(plan_file) == "implemented"
        final_text = plan_file.read_text(encoding="utf-8")
        assert f'deliverable_id: "{_DLV_VALID_SPINE}"' in final_text
        assert winner_id not in final_text


# ===========================================================================
# Plan-side disposition_ref evidence -- the SECOND, independently-verified
# evidence path for `docs/plans/2026-08-03-klabauter-rows-relocate-into-
# claude-klabauter.md` C5/C6 (a chunk whose commit subject named the acceptance
# criterion or artifact, e.g. "AC6 MET: ..."/"DR-261: ...", rather than the
# chunk-id -- invisible to the commit-subject join forever, per
# `_extract_chunk_ids`'s own docstring). See close_out_and_stamp.py's own
# docstring § Plan-side disposition_ref evidence for the full design and the
# Deliverable-Id question this decides (a disposition_ref commit does NOT
# also need a matching Deliverable-Id trailer -- the ref's own placement
# inside this plan's own spine row is the scoping mechanism).
# ===========================================================================


def _make_non_ancestor_commit(root: Path) -> str:
    """A REAL commit object in `root`'s own object store that is NOT
    reachable from current `HEAD` -- built with a plain `commit-tree` write
    (reusing HEAD's own tree, no parent) rather than checkout/branch
    gymnastics. `git rev-parse --verify <sha>^{commit}` resolves it (the
    object genuinely exists); `git merge-base --is-ancestor <sha> HEAD`
    reports false (no ref, including HEAD, was ever built from it) -- this
    is exactly the shape `_verify_disposition_ref`'s `DISPOSITION_REF_
    NOT_ANCESTOR` case must reject: a real commit HEAD never reached."""
    head_tree = _run_git(["rev-parse", "HEAD^{tree}"], root).stdout.strip()
    result = _run_git(
        ["commit-tree", head_tree, "-m", "unreachable commit for non-ancestor test"],
        root,
    )
    return result.stdout.strip()


class TestVerifyDispositionRef:
    def test_valid_ancestor_sha_verifies(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "AC6 MET: unrelated subject, no chunk-id"], root)
        real_sha = _head_sha(root)

        sha, reason = coas._verify_disposition_ref(root, real_sha)

        assert sha == real_sha
        assert reason is None

    def test_absent_ref_is_rejected(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        for absent in (None, "", "   "):
            sha, reason = coas._verify_disposition_ref(root, absent)
            assert sha is None
            assert reason == coas.DISPOSITION_REF_ABSENT

    def test_malformed_ref_is_rejected(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        for malformed in ("HEAD~1", "not-a-sha!", "main"):
            sha, reason = coas._verify_disposition_ref(root, malformed)
            assert sha is None
            assert reason == coas.DISPOSITION_REF_MALFORMED

    def test_unresolvable_hex_ref_is_rejected(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        sha, reason = coas._verify_disposition_ref(
            root, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        )

        assert sha is None
        assert reason == coas.DISPOSITION_REF_UNRESOLVABLE

    def test_non_ancestor_commit_is_rejected(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        non_ancestor_sha = _make_non_ancestor_commit(root)

        sha, reason = coas._verify_disposition_ref(root, non_ancestor_sha)

        assert sha is None
        assert reason == coas.DISPOSITION_REF_NOT_ANCESTOR


class TestDispositionRefEvidenceInDetermineShipped:
    def test_disposition_ref_to_valid_ancestor_counts_as_shipped(self, tmp_path):
        """The motivating case: a chunk's commit subject names the
        acceptance criterion (`AC6 MET: ...`), never the chunk-id -- the
        commit-subject join can NEVER see it (per `_extract_chunk_ids`'s own
        docstring), yet the chunk genuinely shipped. `disposition_ref`
        pointing at that real, ancestor commit is what makes it visible."""
        root = tmp_path
        _init_repo(root)
        (root / "widget.py").write_text("v1")
        _run_git(["add", "widget.py"], root)
        _run_git(["commit", "-q", "-m", "AC6 MET: a claude-klabauter-driven publish is byte-identical"], root)
        landing_sha = _head_sha(root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            f"  disposition: coded\n"
            f"  disposition_ref: {landing_sha}\n"
            "  disposition_detail: 'AC6 met on measured bytes'\n"
            "  body: |\n"
            "    Verification chunk, subject never named the chunk-id.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == []
        assert shipped is True

    def test_disposition_ref_need_not_carry_matching_deliverable_id_trailer(
        self, tmp_path
    ):
        """Pins this fix's own design decision (see close_out_and_stamp.py's
        docstring § Plan-side disposition_ref evidence): the covering commit
        carries NO Deliverable-Id trailer at all (an untrailered, pre-
        convention-shaped commit), and the ref still counts -- the ref's own
        placement inside this plan's spine row is the scoping mechanism,
        not the trailer join."""
        root = tmp_path
        _init_repo(root)
        (root / "widget.py").write_text("v1")
        _run_git(["add", "widget.py"], root)
        _run_git(["commit", "-q", "-m", "DR-261: ownership confirmation, no trailer at all"], root)
        landing_sha = _head_sha(root)

        rows_yaml = (
            "- id: C6\n"
            "  title: Notify sibling\n"
            "  change_kind: doc-edit\n"
            "  surface: cross-repo/outbox/\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            f"  disposition_ref: {landing_sha}\n"
            "  disposition_detail: 'landed'\n"
            "  body: |\n"
            "    No Deliverable-Id trailer on the covering commit.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == []
        assert shipped is True

    def test_bogus_disposition_ref_stays_missing(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_ref: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            "  disposition_detail: 'bogus ref'\n"
            "  body: |\n"
            "    A disposition_ref that does not resolve to any commit.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == ["C5"]
        assert shipped is False

    def test_non_ancestor_disposition_ref_stays_missing(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        non_ancestor_sha = _make_non_ancestor_commit(root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            f"  disposition_ref: {non_ancestor_sha}\n"
            "  disposition_detail: 'not an ancestor of HEAD'\n"
            "  body: |\n"
            "    A disposition_ref pointing at a real but unreachable commit.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == ["C5"]
        assert shipped is False

    def test_coded_row_with_no_disposition_ref_stays_missing(self, tmp_path):
        """A `disposition: coded` row that carries NO `disposition_ref` at
        all still requires ordinary commit-subject evidence -- it must not
        be treated as self-attesting shipped merely by having a
        `disposition` value."""
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_detail: 'no ref recorded at all'\n"
            "  body: |\n"
            "    A coded row with no disposition_ref field.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == ["C5"]
        assert shipped is False

    def test_commit_subject_path_still_works_unchanged_alongside_disposition_ref(
        self, tmp_path
    ):
        """The pre-existing commit-subject join (C1, ordinary chunk-id-
        shaped subject + matching Deliverable-Id trailer) still shippes a
        row exactly as before -- this fix ADDS a second evidence path, it
        does not touch the first."""
        root = tmp_path
        _init_repo(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ordinary chunk\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ships via the ordinary commit-subject join.\n"
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_ref: PLACEHOLDER\n"
            "  disposition_detail: 'AC6 met on measured bytes'\n"
            "  body: |\n"
            "    Ships via the new disposition_ref path only.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)

        with open(root / "widget2.py", "w", encoding="utf-8") as fh:
            fh.write("v1")
        _run_git(["add", "widget2.py"], root)
        _run_git(["commit", "-q", "-m", "AC6 MET: never named as C5"], root)
        landing_sha = _head_sha(root)
        plan_file.write_text(
            plan_file.read_text(encoding="utf-8").replace("PLACEHOLDER", landing_sha),
            encoding="utf-8",
        )
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "plan: pin disposition_ref"], root)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )

        assert error is None
        assert missing == []
        assert shipped is True


class TestCloseOutAndStampDispositionRefRejections:
    def test_full_run_stamps_implemented_from_disposition_ref_evidence_alone(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: a plan whose only committed chunk's subject NEVER
        named the chunk-id (an AC/artifact-named subject, the klabauter C5/
        C6 shape) still stamps `implemented`, because its row's own
        disposition_ref resolves to that real, ancestor commit."""
        root = tmp_path
        _init_repo(root)
        (root / "widget.py").write_text("v1")
        _run_git(["add", "widget.py"], root)
        _run_git(["commit", "-q", "-m", "AC6 MET: a claude-klabauter-driven publish is byte-identical"], root)
        landing_sha = _head_sha(root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            f"  disposition: coded\n"
            f"  disposition_ref: {landing_sha}\n"
            "  disposition_detail: 'AC6 met on measured bytes'\n"
            "  body: |\n"
            "    Verification chunk, subject never named the chunk-id.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["disposition_ref_rejections"] == {}
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head

    def test_rejected_disposition_ref_is_named_with_its_own_reason(
        self, tmp_path, monkeypatch
    ):
        """`disposition_ref_rejections` names the SPECIFIC reason a
        still-missing row's disposition_ref did not count -- not merely
        that the chunk is missing."""
        root = tmp_path
        _init_repo(root)
        (root / "seed.txt").write_text("seed")
        _run_git(["add", "seed.txt"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        non_ancestor_sha = _make_non_ancestor_commit(root)

        rows_yaml = (
            "- id: C5\n"
            "  title: Verify byte identity\n"
            "  change_kind: verification\n"
            "  surface: coordinator/bin/publish.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            f"  disposition_ref: {non_ancestor_sha}\n"
            "  disposition_detail: 'not an ancestor of HEAD'\n"
            "  body: |\n"
            "    A disposition_ref pointing at a real but unreachable commit.\n"
        )
        plan_file = _seed_disposition_plan(root, rows_yaml)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C5"]
        assert result["disposition_ref_rejections"] == {
            "C5": coas.DISPOSITION_REF_NOT_ANCESTOR
        }
        assert "disposition_ref did not count as evidence" in result["message"]
        assert "C5 (non-ancestor)" in result["message"]


# ===========================================================================
# docs/project-tracker.md `N of M` reconciliation (AC7, C8)
# ===========================================================================


def _seed_tracker_plan(root: Path, rows_yaml: str, deliverable_id: str) -> str:
    """Seeds `docs/plans/tracker-fixture.md` (a real, on-disk, committed
    plan carrying `deliverable_id`) for the tracker-reconciliation tests
    below, and returns its repo-relative path. Reuses `_PLAN_TEMPLATE`
    (same fixture the rest of this file already relies on) rather than a
    parallel template."""
    dest_rel = "docs/plans/tracker-fixture.md"
    dest = root / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = _PLAN_TEMPLATE.format(status="executing", rows=rows_yaml).replace(
        'deliverable_id: "dlv-fixture-disposition-000001"',
        f'deliverable_id: "{deliverable_id}"',
    )
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_rel], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest_rel


_TRACKER_FIXTURE_ROWS = (
    "- id: C1\n"
    "  title: First chunk\n"
    "  change_kind: code-edit\n"
    "  surface: coordinator/bin/one.py\n"
    "  deferred: false\n"
    "  disposition: open\n"
    "  body: |\n"
    "    First chunk.\n"
    "- id: C2\n"
    "  title: Second chunk\n"
    "  change_kind: code-edit\n"
    "  surface: coordinator/bin/two.py\n"
    "  deferred: false\n"
    "  disposition: open\n"
    "  body: |\n"
    "    Second chunk.\n"
)

_TRACKER_FIXTURE_DELIVERABLE_ID = "dlv-tracker-fixture-000001"


def _tracker_text_with_claim(plan_rel: str, claim: str) -> str:
    return (
        "# Project Tracker\n"
        "**Last updated:** 2026-08-04\n\n"
        "## Active Workstreams\n\n"
        "### 1. Tracker fixture workstream\n"
        f"**Status:** In progress — {claim}, PM-ratified boundary and gate "
        "narrative untouched by this reconciler.\n"
        f"**Specs:** `{plan_rel}`\n\n"
        "- [ ] C1\n"
        "- [ ] C2\n\n"
        "## Backlog\n"
    )


class TestTrackerReconciliation:
    """`reconcile_tracker_shipped_counts` / `apply_tracker_reconciliation`
    (AC7) -- the bounded `N of M` edit against `docs/project-tracker.md`.
    Reuses this module's own `_determine_shipped`/`_commit_required_chunk_
    ids` machinery (never a second implementation) and Specs:-line/plan-
    file fixtures, never a parallel tracker-format oracle."""

    def test_stale_claim_is_corrected_and_narrative_is_byte_identical(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_rel = _seed_tracker_plan(
            root, _TRACKER_FIXTURE_ROWS, _TRACKER_FIXTURE_DELIVERABLE_ID
        )
        _commit_chunk(
            root, plan_rel, "C1", deliverable_id=_TRACKER_FIXTURE_DELIVERABLE_ID
        )

        stale_text = _tracker_text_with_claim(plan_rel, "0 of 2 chunks landed")
        new_text, edits = coas.reconcile_tracker_shipped_counts(stale_text, root)

        assert edits == [
            {
                "section": "Tracker fixture workstream",
                "plan_path": plan_rel,
                "old": "0 of 2",
                "new": "1 of 2",
            }
        ]
        expected_text = stale_text.replace("0 of 2 chunks", "1 of 2 chunks")
        assert new_text == expected_text
        # HARD CONSTRAINT (C8): every byte outside the digit span itself is
        # untouched -- assert it directly, not merely "the result looks
        # right".
        assert new_text.replace("1 of 2", "0 of 2", 1) == stale_text

    def test_already_agreeing_claim_is_left_byte_identical(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_rel = _seed_tracker_plan(
            root, _TRACKER_FIXTURE_ROWS, _TRACKER_FIXTURE_DELIVERABLE_ID
        )
        _commit_chunk(
            root, plan_rel, "C1", deliverable_id=_TRACKER_FIXTURE_DELIVERABLE_ID
        )

        agreeing_text = _tracker_text_with_claim(plan_rel, "1 of 2 chunks landed")
        new_text, edits = coas.reconcile_tracker_shipped_counts(agreeing_text, root)

        assert edits == []
        assert new_text == agreeing_text

    def test_no_specs_plan_reference_is_left_untouched(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        text = (
            "# Project Tracker\n\n"
            "### 1. No specs at all\n"
            "**Status:** 0 of 2 chunks landed, no Specs: line at all.\n\n"
        )
        new_text, edits = coas.reconcile_tracker_shipped_counts(text, root)
        assert edits == []
        assert new_text == text

    def test_unresolvable_plan_path_is_left_untouched(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        text = _tracker_text_with_claim(
            "docs/plans/does-not-exist.md", "0 of 2 chunks landed"
        )
        new_text, edits = coas.reconcile_tracker_shipped_counts(text, root)
        assert edits == []
        assert new_text == text

    def test_no_n_of_m_claim_present_is_a_no_op(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_rel = _seed_tracker_plan(
            root, _TRACKER_FIXTURE_ROWS, _TRACKER_FIXTURE_DELIVERABLE_ID
        )
        text = (
            "### 1. Tracker fixture workstream\n"
            "**Status:** In progress, no digit claim here.\n"
            f"**Specs:** `{plan_rel}`\n\n"
        )
        new_text, edits = coas.reconcile_tracker_shipped_counts(text, root)
        assert edits == []
        assert new_text == text

    def test_apply_writes_only_when_an_edit_fires(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        plan_rel = _seed_tracker_plan(
            root, _TRACKER_FIXTURE_ROWS, _TRACKER_FIXTURE_DELIVERABLE_ID
        )
        _commit_chunk(
            root, plan_rel, "C1", deliverable_id=_TRACKER_FIXTURE_DELIVERABLE_ID
        )
        tracker_path = root / "docs" / "project-tracker.md"
        stale_text = _tracker_text_with_claim(plan_rel, "0 of 2 chunks landed")
        tracker_path.write_text(stale_text, encoding="utf-8")
        before_mtime = tracker_path.stat().st_mtime_ns

        edits = coas.apply_tracker_reconciliation(tracker_path, root)
        assert edits
        reconciled_text = tracker_path.read_text(encoding="utf-8")
        assert "1 of 2 chunks" in reconciled_text

        # A second run over the now-correct tracker is a true no-op: no
        # edits, the surrounding narrative stays byte-identical, and the
        # file's mtime is left untouched (never rewritten when
        # reconcile_tracker_shipped_counts finds nothing to change).
        # Review: coordinator:code-reviewer — prior assertions were
        # tautological (x == x) and couldn't fail on a real rewrite;
        # capture mtime immediately before the no-op call and assert
        # equality immediately after.
        mtime_before_second_call = tracker_path.stat().st_mtime_ns
        edits_again = coas.apply_tracker_reconciliation(tracker_path, root)
        assert edits_again == []
        assert tracker_path.read_text(encoding="utf-8") == reconciled_text
        assert tracker_path.stat().st_mtime_ns == mtime_before_second_call

    def test_reconciliation_against_this_repos_own_tracker_is_a_no_op(self):
        """Runs the reconciler read-only against THIS repo's actual
        `docs/project-tracker.md` (never writes it) -- pins the observed,
        verified-live fact that no current workstream row carries a
        literal `N of M chunks` claim, so a real run finds nothing to
        reconcile. A future row written in that shape becomes exercised
        coverage for free; this test's job is only to prove today's file
        round-trips byte-identical through the reconciler."""
        repo_root = _REPO_ROOT
        tracker_path = repo_root / "docs" / "project-tracker.md"
        if not tracker_path.exists():
            # `e4467eb6c chore: delete docs/project-tracker.md (sweep
            # completion)` retired the file this test pins a property of.
            # Skipped rather than deleted: the assertion is still the one
            # wanted if a tracker is ever reintroduced, and a hard failure
            # here breaks the suite for every session over an artifact the
            # repo deliberately no longer has.
            pytest.skip("docs/project-tracker.md was retired by e4467eb6c")
        text = tracker_path.read_text(encoding="utf-8")
        new_text, edits = coas.reconcile_tracker_shipped_counts(text, repo_root)
        assert edits == []


# ===========================================================================
# Hyphen-range-subject diagnostic (2026-08-05) -- makes a `missing_chunk_ids`
# false negative LEGIBLE when the real cause is a `C1-C4: ...`-style commit
# subject: `-` is deliberately NOT a recognized multi-id separator
# (`_extract_chunk_ids` only splits on `,`/`+`/`/`), so such a subject goes
# down the single-id path and registers nothing at all, even though every
# named chunk genuinely shipped. See close_out_and_stamp.py's own
# `_hyphen_range_subject_diagnostics` docstring for the live incident and the
# explicit "do not add `-` to the separator set" negative-spec this pins.
#
# Diagnostic-only: these tests confirm the join semantics/verdict in
# `_committed_chunk_shas`/`_determine_shipped` are UNCHANGED by this fix --
# `C1-C4:` must keep registering zero ids under the real oracle; only the
# NEW diagnostic explains why.
# ===========================================================================


class TestHyphenRangeSubjectDiagnostic:
    def test_hyphen_range_subject_fires_over_a_covering_spine(self, tmp_path):
        """`C1-C4: ...`, trailered to the plan's own deliverable_id, with
        every one of C1..C4 still reported missing -- fires, naming the sha,
        subject, and the spine ids the range appears to span."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1-C4: land four chunks in one commit",
            deliverable_id=_DLV_VALID_SPINE,
        )
        head_sha = _head_sha(root)[:7]

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C1", "C2", "C3", "C4"]
        )
        assert len(offenders) == 1
        offender = offenders[0]
        assert head_sha == offender["sha"][: len(head_sha)]
        assert offender["subject"] == "C1-C4: land four chunks in one commit"
        assert sorted(offender["spanned_chunk_ids"]) == ["C1", "C4"]

    def test_doctrine_dash_tag_does_not_fire(self, tmp_path):
        """`DOCTRINE-C7a: ...` splits to `["DOCTRINE", "C7a"]` -- `DOCTRINE`
        covers no spine id, so this must NOT be confidently reported as a
        hyphen-range subject. Load-bearing: this is the exact shape the
        real `_extract_chunk_ids`/`_committed_id_covers_spine_id` gate
        exists to leave alone (a compound dash-tag, not an id-list)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "DOCTRINE-C7a: admission gate",
            deliverable_id=_DLV_VALID_SPINE,
        )

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C7a"]
        )
        assert offenders == []

    def test_residue_dash_tag_does_not_fire(self, tmp_path):
        """`RESIDUE-C9: ...` splits to `["RESIDUE", "C9"]` -- `RESIDUE`
        covers no spine id, so this must NOT fire either, mirroring the
        `DOCTRINE-C7a` case above."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "RESIDUE-C9: named-dispatch strip guard",
            deliverable_id=_DLV_VALID_SPINE,
        )

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C9"]
        )
        assert offenders == []

    def test_dash_tag_suffix_shape_does_not_fire(self, tmp_path):
        """`C8a-mak: ...` is a REAL corpus shape (`_committed_id_covers_
        spine_id`'s own docstring names it: a repo-variant dash-tag
        suffix, e.g. Claude-klabauter's own tag on a sub-chunk landed
        elsewhere too -- NOT a hyphen-joined range) and it must not be
        mistaken for one. It splits to `["C8a", "mak"]`; `C8a` covers
        itself but `mak` does not cover any spine id, so this must not
        fire -- and, unlike the `DOCTRINE`/`RESIDUE` cases above, this
        currently only holds because dash-tag suffixes are always
        lowercase while spine ids always start uppercase
        (`_committed_id_covers_spine_id`'s own case-sensitive match).
        This test pins that case-sensitivity invariant directly, since it
        is incidental to a naming convention, not asserted anywhere else
        this diagnostic itself relies on it (Review: code-reviewer --
        Finding 1)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C8a-mak: land C8a via the claude-klabauter variant tag",
            deliverable_id=_DLV_VALID_SPINE,
        )

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C8a"]
        )
        assert offenders == []

    def test_exact_match_preferred_over_suffix_derived_match(self, tmp_path):
        """Bipartite-matching edge case (Review: code-reviewer -- Finding
        2): `C1a-C1` splits to components `["C1a", "C1"]`. `C1a` exactly
        matches missing spine id `C1a` but ALSO covers base spine id `C1`
        via the single-lowercase-letter sub-chunk suffix rule; `C1` exactly
        matches missing spine id `C1` and covers nothing else. A single
        greedy first-match pass could assign `C1a`'s exact match away to
        `C1` (since `covered[0]` is whichever candidate appears first in
        `missing_chunk_ids`, not necessarily the exact one), leaving the
        second component with no candidate left and aborting detection
        entirely even though a valid one-to-one assignment exists:
        `C1a`->`C1a`, `C1`->`C1`. The two-pass exact-match-first
        assignment must find that assignment and fire."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1a-C1: land both the sub-chunk and its base row",
            deliverable_id=_DLV_VALID_SPINE,
        )

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C1", "C1a"]
        )
        assert len(offenders) == 1
        assert sorted(offenders[0]["spanned_chunk_ids"]) == ["C1", "C1a"]

    def test_hyphen_range_subject_scoped_to_a_different_plan_does_not_report(
        self, tmp_path
    ):
        """A `C1-C4:`-shaped subject trailered to a DIFFERENT plan's
        `deliverable_id` must never be reported against THIS plan just
        because its subject shape happens to match -- the same
        Deliverable-Id-scoping bound `_committed_chunk_shas`'s own join
        enforces, applied identically here."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1-C4: land four chunks belonging to a different plan",
            deliverable_id="dlv-some-other-plan-000099",
        )

        offenders = coas._hyphen_range_subject_diagnostics(
            root, _DLV_VALID_SPINE, ["C1", "C2", "C3", "C4"]
        )
        assert offenders == []

    def test_absent_deliverable_id_returns_empty_without_git_call(
        self, tmp_path, monkeypatch
    ):
        """`deliverable_id=None`/falsy short-circuits before any git-log
        call at all -- nothing to scope the search against."""
        root = tmp_path
        _init_repo(root)

        call_count = {"n": 0}
        real_run_git = coas._run_git

        def counting_run_git(args, cwd):
            call_count["n"] += 1
            return real_run_git(args, cwd)

        monkeypatch.setattr(coas, "_run_git", counting_run_git)

        assert (
            coas._hyphen_range_subject_diagnostics(root, None, ["C1", "C4"]) == []
        )
        assert (
            coas._hyphen_range_subject_diagnostics(root, "", ["C1", "C4"]) == []
        )
        assert call_count["n"] == 0

    def test_close_out_and_stamp_reports_the_hyphen_range_subject_and_note(
        self, tmp_path, monkeypatch
    ):
        """Integration: `close_out_and_stamp`'s own `message` gets the
        CAUSE-pointed NOTE clause naming the recognized separators, the
        structured `hyphen_range_subjects` result key carries the offending
        commit -- and, critically, `shipped`/`missing_chunk_ids` are
        UNCHANGED by this diagnostic firing (the regression this fix must
        never introduce): the plan's spine (C1/C2a/C2b) is genuinely NOT
        satisfied by a `C1-C4:`-style subject that names none of those
        exact ids via the recognized-separator path, so it must still read
        as fully missing."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1-C2a: land two chunks as a hyphen range",
            deliverable_id=_DLV_VALID_SPINE,
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C1", "C2a", "C2b"]
        assert len(result["hyphen_range_subjects"]) == 1
        offender = result["hyphen_range_subjects"][0]
        assert offender["subject"] == "C1-C2a: land two chunks as a hyphen range"
        assert sorted(offender["spanned_chunk_ids"]) == ["C1", "C2a"]
        assert (
            "commit subject(s) used '-' to join a chunk-id list" in result["message"]
        )
        assert "',', '+', '/'" in result["message"]
        assert "C1-C2a: land two chunks as a hyphen range" in result["message"]

    def test_genuinely_uncommitted_with_no_hyphen_range_subject_message_unchanged(
        self, tmp_path, monkeypatch
    ):
        """No commit subject uses `-` as a separator at all -- the
        diagnostic reports nothing, and today's message/behavior for a
        genuinely-uncommitted plan is completely unaffected."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C2a", "C2b"]
        assert result["hyphen_range_subjects"] == []
        assert result["message"] == (
            f"plan.md: {len(result['missing_chunk_ids'])} chunk(s) still "
            "uncommitted, committed partial state"
        )

    def test_happy_path_full_shipped_unaffected_no_diagnostic_call(
        self, tmp_path, monkeypatch
    ):
        """The happy path (fully shipped, `missing_chunk_ids == []`) never
        even CALLS `_hyphen_range_subject_diagnostics` -- pinned directly
        against a spy on that function, the same "never touch the happy
        path" constraint `_deliverable_id_near_miss_diagnostics` already
        enforces (the caller must gate the call itself, not merely discard
        an empty result)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        call_count = {"n": 0}
        real_diagnostic = coas._hyphen_range_subject_diagnostics

        def counting_diagnostic(repo_root, deliverable_id, missing_chunk_ids):
            call_count["n"] += 1
            return real_diagnostic(repo_root, deliverable_id, missing_chunk_ids)

        monkeypatch.setattr(
            coas, "_hyphen_range_subject_diagnostics", counting_diagnostic
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["hyphen_range_subjects"] == []
        assert call_count["n"] == 0


# ===========================================================================
# Dispatch Ledger fallback (C1) + partial-evaluation stamp (C2)
# Spec backlink: dispatch brief 2026-08-06, "teach _determine_shipped the
# legacy Dispatch-Ledger format" / "give the partial outcome a durable,
# legible record".
# ===========================================================================

_LEDGER_PLAN_TEMPLATE = """---
title: "Fixture plan — legacy Dispatch Ledger"
created: 2026-07-02
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-07-02"
plan_id: "pln-fixture-ledger-000001"
deliverable_id: "dlv-fixture-ledger-000001"
---

# Fixture plan — legacy Dispatch Ledger

## Dispatch Ledger

| # | chunk-id | one-line brief | write-files | gate-kind | runs | est-min | status |
|---|---|---|---|---|---|---|---|
{rows}
"""


def _seed_ledger_plan(root: Path, rows_md: str, status: str = "draft", dest_name: str = "plan.md") -> Path:
    """Seeds a plan with NO `## Tasks` spine (ABSENT under
    `locate_fenced_block`) but a real `## Dispatch Ledger` table -- the
    legacy pre-spine delivery-record shape `_dispatch_ledger_delivered`
    exists to read (see close_out_and_stamp.py's own docstring § Dispatch
    Ledger fallback)."""
    text = _LEDGER_PLAN_TEMPLATE.format(status=status, rows=rows_md)
    dest = root / dest_name
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


class TestDispatchLedgerFallback:
    def test_spine_present_still_wins_over_ledger(self, tmp_path, monkeypatch):
        """A LOCATED spine (even a trivial one, even alongside a `##
        Dispatch Ledger` heading naming rows that would otherwise mislead)
        must never reach the ledger fallback at all -- spine-present always
        wins (see `_determine_shipped`'s own routing comment)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_disposition_plan(
            root,
            "- id: C1\n"
            "  title: only row\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n",
        )
        # Append a `## Dispatch Ledger` heading whose only row claims a
        # completely different, fabricated chunk-id "delivered" -- if this
        # were ever consulted it would corrupt the verdict; the spine's own
        # C1 (deliberately left uncommitted) must be the only thing that
        # decides the outcome.
        with plan_file.open("a", encoding="utf-8") as fh:
            fh.write(
                "\n## Dispatch Ledger\n\n"
                "| # | chunk-id | brief | files | gate | runs | est | status |\n"
                "|---|---|---|---|---|---|---|---|\n"
                "| 1 | ZZZ | bogus | none | none | now | 1 | committed 0000000000000000000000000000000000000000 |\n"
            )
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "add bogus ledger"], root)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C1"]
        assert result["join_provenance"] != coas.JOIN_PROVENANCE_LEDGER_FALLBACK

    def test_ledger_fallback_classifies_fully_committed_legacy_plan_as_shipped(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ledger_plan(
            root,
            "| 1 | C1 | do the thing | none | none | now | 1 | committed PLACEHOLDER1 |\n"
            "| 2 | C2 | do another thing | none | none | now | 1 | committed PLACEHOLDER2 (EM-inline) |\n",
        )
        # Land two real commits in this throwaway repo and splice their
        # actual short shas into the ledger rows -- `_dispatch_ledger_
        # delivered` verifies via `git cat-file -e`, so a fabricated sha
        # must never pass; only a REAL, resolvable sha does.
        sha1_full = _run_git(["rev-parse", "HEAD"], root).stdout.strip()
        (root / "other-file-1.txt").write_text("a\n", encoding="utf-8")
        _run_git(["add", "other-file-1.txt"], root)
        _run_git(["commit", "-q", "-m", "unrelated work 1"], root)
        sha1 = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()
        (root / "other-file-2.txt").write_text("b\n", encoding="utf-8")
        _run_git(["add", "other-file-2.txt"], root)
        _run_git(["commit", "-q", "-m", "unrelated work 2"], root)
        sha2 = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()

        text = plan_file.read_text(encoding="utf-8")
        text = text.replace("PLACEHOLDER1", sha1).replace("PLACEHOLDER2", sha2)
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "splice real shas into ledger"], root)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_LEDGER_FALLBACK
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"

    def test_ledger_citing_nonexistent_sha_is_not_shipped(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ledger_plan(
            root,
            "| 1 | C1 | do the thing | none | none | now | 1 | "
            "committed 0000000000000000000000000000000000000000 |\n",
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C1"]
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_LEDGER_FALLBACK
        assert _read_status(plan_file) == "draft"

    def test_ledger_citing_non_ancestor_sha_is_not_shipped(self, tmp_path, monkeypatch):
        """A sha that resolves to a real commit object -- present in the
        repo's object database -- but was never landed on this branch (a
        rebased-away / abandoned-branch / fetched-but-unmerged commit) must
        NOT count as delivered. `git cat-file -e` alone would pass this
        (Review: coordinator:code-reviewer P1 -- existence-only check was
        not the same anti-self-attestation posture `_verify_disposition_ref`
        applies); `_dispatch_ledger_delivered` must additionally require
        `git merge-base --is-ancestor <sha> HEAD`."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ledger_plan(
            root,
            "| 1 | C1 | do the thing | none | none | now | 1 | committed PLACEHOLDER |\n",
        )
        base_sha = _run_git(["rev-parse", "HEAD"], root).stdout.strip()
        # Land a commit on a throwaway branch, then rewind the current
        # branch back to base -- the commit object still exists (reachable
        # via the throwaway branch ref) but HEAD never reached it.
        _run_git(["checkout", "-q", "-b", "throwaway"], root)
        (root / "off-branch.txt").write_text("x\n", encoding="utf-8")
        _run_git(["add", "off-branch.txt"], root)
        _run_git(["commit", "-q", "-m", "never-landed work"], root)
        off_branch_sha = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()
        _run_git(["checkout", "-q", "-B", "main", base_sha], root)

        text = plan_file.read_text(encoding="utf-8")
        text = text.replace("PLACEHOLDER", off_branch_sha)
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "splice off-branch sha into ledger"], root)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["missing_chunk_ids"] == ["C1"]
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_LEDGER_FALLBACK
        assert _read_status(plan_file) == "draft"

    def test_malformed_ledger_table_is_not_shipped(self, tmp_path, monkeypatch):
        """A `## Dispatch Ledger` heading whose table has no recognizable
        `status` column at all -- `_dispatch_ledger_delivered` cannot
        classify any row, so this must fail loud (mirrors the spine's own
        MALFORMED posture) rather than guess shipped."""
        root = tmp_path
        _init_repo(root)
        _seed_ledger_plan(
            root,
            "| 1 | C1 | do the thing |\n",
            dest_name="plan.md",
        )
        # Overwrite with a header that has no "status" column.
        plan_file = root / "plan.md"
        text = plan_file.read_text(encoding="utf-8")
        text = text.replace(
            "| # | chunk-id | one-line brief | write-files | gate-kind | runs | est-min | status |\n"
            "|---|---|---|---|---|---|---|---|\n",
            "| # | chunk-id | one-line brief |\n|---|---|---|\n",
        )
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "drop status column"], root)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL, result
        assert "error" in result
        assert "Dispatch Ledger" in result["error"]

    def test_suffixed_dispatch_ledger_heading_is_found_not_bypassed(
        self, tmp_path, monkeypatch
    ):
        """False-positive-stamp incident fix: `## Dispatch Ledger — claude-klabauter
        [M] slice` (a real corpus heading with a trailing suffix) must be
        located by `_DISPATCH_LEDGER_HEADING_RE`, not silently bypassed
        into the no-evidence-source branch despite having a full,
        committed ledger table right there."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ledger_plan(
            root,
            "| 1 | C1 | do the thing | none | none | now | 1 | committed PLACEHOLDER1 |\n",
        )
        text = plan_file.read_text(encoding="utf-8")
        text = text.replace(
            "## Dispatch Ledger\n", "## Dispatch Ledger — claude-klabauter [M] slice\n"
        )
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "suffix the ledger heading"], root)

        (root / "other-file-1.txt").write_text("a\n", encoding="utf-8")
        _run_git(["add", "other-file-1.txt"], root)
        _run_git(["commit", "-q", "-m", "unrelated work 1"], root)
        sha1 = _run_git(["rev-parse", "--short", "HEAD"], root).stdout.strip()

        text = plan_file.read_text(encoding="utf-8").replace("PLACEHOLDER1", sha1)
        plan_file.write_text(text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "splice real sha into suffixed ledger"], root)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_LEDGER_FALLBACK
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"


# ===========================================================================
# False-positive-stamp incident (2026-08-06): the no-spine/no-ledger branch
# of `_determine_shipped` performs ZERO evidence lookups yet used to report
# `JOIN_PROVENANCE_JOINED`, byte-identical to a genuine evidence-backed
# join -- `workstream_complete` callers branched on that string BY VALUE and
# treated it as attributed. This class pins the fix: `shipped` stays True
# (D7's own posture, unchanged), but `join_provenance` now reports
# `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE` and the stamping gate refuses to act
# on it -- see `_determine_shipped`'s own routing comment and
# `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE`'s own docstring.
# ===========================================================================


class TestNoEvidenceSourceProvenance:
    def test_no_spine_no_ledger_reports_no_evidence_source_not_joined(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_ZERO_BLOCKS)
        plan_text = plan_file.read_text(encoding="utf-8")

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_text, "plan.md", root
        )

        assert error is None
        assert shipped is True
        assert missing == []
        assert join_provenance == coas.JOIN_PROVENANCE_NO_EVIDENCE_SOURCE
        assert join_provenance != coas.JOIN_PROVENANCE_JOINED

    def test_real_deliverable_id_join_still_reports_joined(self, tmp_path, monkeypatch):
        """Sibling pin: a plan with a real `## Tasks` spine and a genuine
        Deliverable-Id-trailer join over committed chunks still reports the
        ordinary `JOIN_PROVENANCE_JOINED` -- this fix narrows ONLY the
        no-spine/no-ledger branch, nothing about the real join path."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)
        plan_text = plan_file.read_text(encoding="utf-8")

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_text, "plan.md", root
        )

        assert error is None
        assert shipped is True
        assert missing == []
        assert join_provenance == coas.JOIN_PROVENANCE_JOINED

    def test_no_evidence_source_plan_is_not_stamped_end_to_end(self, tmp_path, monkeypatch):
        """Downstream refusal-to-stamp, exercised via the real
        `close_out_and_stamp` entrypoint (not a direct `_determine_shipped`
        call) -- the actual stamping caller this fix protects."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_ZERO_BLOCKS)
        original_status = _read_status(plan_file)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["join_provenance"] == coas.JOIN_PROVENANCE_NO_EVIDENCE_SOURCE
        assert result["stamped"] is False
        assert _read_status(plan_file) == original_status
        assert _head_sha(root) == pre_head


class TestPartialEvaluationStamp:
    def test_halted_plan_gets_a_durable_partial_stamp_on_first_evaluation(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # No chunk commits landed -- genuinely halted, nothing for AC8 to
        # auto-resolve either (mirrors
        # test_partially_shipped_with_nothing_committed_at_all_is_a_genuine_noop).

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["partial_evaluation_stamped"] is True
        text = plan_file.read_text(encoding="utf-8")
        assert "close_out_last_partial:" in text
        assert _head_sha(root) != pre_head

        # A second, unchanged call must NOT rewrite the stamp -- idempotent
        # by presence, preserving the pre-existing halted-plan no-op
        # guarantee for every call after the first.
        second_head = _head_sha(root)
        exit_code_2, result_2, pre_head_2 = _run_close_out(monkeypatch, root, "plan.md")
        assert exit_code_2 == coas.EXIT_OK, result_2
        assert result_2["partial_evaluation_stamped"] is False
        assert _head_sha(root) == pre_head_2
        assert _head_sha(root) == second_head


# ===========================================================================
# C1 (2026-08-08, `docs/plans/2026-08-08-a-status-field-cannot-vouch-for-
# itself.md`): the certified-ship path clears `close_out_last_partial:`
# rather than leaving it as a stale, unremarked marker on an `implemented`
# plan (AC1, AC3).
# ===========================================================================


class TestCertifiedShipClearsPartialMarker:
    def test_certified_ship_clears_marker_and_stamps_implemented(
        self, tmp_path, monkeypatch
    ):
        """Named reviewer finding: a plan that previously halted (and so
        already carries a `close_out_last_partial:` marker) must have that
        marker CLEARED, and the `implemented` stamp must actually land, once
        every chunk ships. This asserts against the LIVE FILE re-read from
        disk after the call, never the in-memory return value -- a
        return-value-only assertion cannot see the status-clobber this
        chunk exists to avoid."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        # C1 alone lands, C2a/C2b uncommitted -- halted -- to plant a real
        # marker via the existing halted-path stamp mechanism.
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        first_exit, first_result, _pre = _run_close_out(monkeypatch, root, "plan.md")
        assert first_exit == coas.EXIT_OK, first_result
        assert first_result["partial_evaluation_stamped"] is True
        assert "close_out_last_partial:" in plan_file.read_text(encoding="utf-8")

        # Now the remaining chunks ship -- certified-ship path.
        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["stamped"] is True

        # Re-read from disk -- the load-bearing assertion this chunk exists
        # for.
        live_text = plan_file.read_text(encoding="utf-8")
        assert "close_out_last_partial:" not in live_text
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head

    def test_stamp_failure_after_clear_restores_the_marker_on_disk(
        self, tmp_path, monkeypatch
    ):
        """Review: code-reviewer -- P2 finding, 2026-08-08. Reproduces the
        reachable hazard traced against `plan_status_transition._stamp_
        implemented`: that function can flip `status:` to `implemented` on
        disk via its own locked_rmw and STILL return a failure rc (its own
        commit-the-flip attempt fails, or a resumed-stranded-flip commit on
        a later run also fails) -- `stamp_rc not in (0, 2)` then propagates
        as this op's own `EXIT_BUSINESS_FAIL`. Faking `cs_stamp_plan_
        implemented` to do exactly that (write `status: implemented` to the
        live file, return rc=1) reproduces the on-disk shape without
        depending on `plan_status_transition`'s own internal commit-failure
        path. Asserts the marker this op cleared pre-stamp is restored on
        disk -- the fix for the false-clean read `workstream_complete` leg A
        would otherwise take (terminal `status:` next to an absent marker)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        first_exit, first_result, _pre = _run_close_out(monkeypatch, root, "plan.md")
        assert first_exit == coas.EXIT_OK, first_result
        assert first_result["partial_evaluation_stamped"] is True
        assert "close_out_last_partial:" in plan_file.read_text(encoding="utf-8")

        _commit_chunk(root, "plan.md", "C2a", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)

        def _fake_stamp_writes_then_fails(plan_path: str) -> int:
            # Simulates plan_status_transition._stamp_implemented's own
            # locked_rmw landing a real status flip on disk, followed by its
            # own commit-the-flip attempt failing -- rc=1, the only fatal rc
            # this call site treats as a genuine stamp failure.
            path = Path(plan_path)
            text = path.read_text(encoding="utf-8")
            text = text.replace("status: draft", "status: implemented", 1)
            path.write_text(text, encoding="utf-8")
            return 1

        monkeypatch.setattr(
            coas.archive_stamp,
            "cs_stamp_plan_implemented",
            _fake_stamp_writes_then_fails,
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL, result
        live_text = plan_file.read_text(encoding="utf-8")
        # The load-bearing assertion: the marker cleared before the (failed)
        # stamp call must be back on disk, so a reader of this plan's own
        # frontmatter -- workstream_complete leg A included -- does not see
        # a terminal `status:` next to a false-clean, absent marker.
        assert "close_out_last_partial:" in live_text
        assert _read_status(plan_file) == "implemented"

    def test_certified_ship_with_no_marker_is_byte_clean(self, tmp_path, monkeypatch):
        """A plan that never halted (no `close_out_last_partial:` marker at
        all) must not gain one, and stamping must still work identically --
        the clear is a no-op when there is nothing to clear."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        assert "close_out_last_partial:" not in plan_file.read_text(encoding="utf-8")
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["stamped"] is True
        live_text = plan_file.read_text(encoding="utf-8")
        assert "close_out_last_partial:" not in live_text
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head


# ===========================================================================
# AC-table/spine desync check (C1/C2 -- eng-director review finding,
# 2026-08-06): advisory-only detection of a spine-fully-resolved plan whose
# own '## Acceptance Criteria' table still reads unresolved. See
# `close_out_and_stamp.py`'s own `_ac_table_desync_finding` docstring.
# ===========================================================================

_AC_PLAN_TEMPLATE = """---
title: "Fixture plan — AC-table desync"
created: 2026-08-06
author: test-fixture
status: {status}
branch: "work/test-fixture/2026-08-06"
plan_id: "pln-fixture-ac-desync-000001"
deliverable_id: "dlv-fixture-ac-desync-000001"
---

# Fixture plan — AC-table desync

{ac_section}

## Tasks

```yaml plan-tasks
{rows}
```
"""

_DLV_AC_DESYNC = "dlv-fixture-ac-desync-000001"

_AC_TABLE_PENDING = """## Acceptance Criteria

| # | Criterion | Status |
|---|---|---|
| AC1 | The widget ships | pending |
| AC2 | The widget is documented | pending |
"""

_AC_TABLE_RESOLVED = """## Acceptance Criteria

| # | Criterion | Status |
|---|---|---|
| AC1 | The widget ships | green (abc1234) |
| AC2 | The widget is documented | ✅ |
"""

_AC_TABLE_MALFORMED = """## Acceptance Criteria

Some prose with no table at all -- AC1 is done, AC2 is pending.
"""

_AC_TABLE_STRUCK_THROUGH = """## Acceptance Criteria

| # | Criterion | Status |
|---|---|---|
| AC1 | The widget ships | ~~pending~~ |
| AC2 | The widget is documented | ~~open~~ |
"""

_AC_TABLE_LOWERCASE_HEADING = """## acceptance criteria

| # | Criterion | Status |
|---|---|---|
| AC1 | The widget ships | pending |
"""

_AC_TABLE_TWO_COLUMN_NO_ID = """## Acceptance Criteria

| Criterion | Status |
|---|---|
| The widget ships | pending |
| The widget is documented | pending |
"""

_AC_TABLE_STATUS_NOT_LAST = """## Acceptance Criteria

| # | Status | Criterion |
|---|---|---|
| AC1 | pending | The widget ships |
| AC2 | pending | The widget is documented |
"""

_ONE_CODED_ROW = (
    "- id: C1\n"
    "  title: Ship the widget\n"
    "  change_kind: script-edit\n"
    "  surface: coordinator/bin/widget.py\n"
    "  deferred: false\n"
    "  disposition: coded\n"
    "  disposition_ref: 1234567\n"
    "  body: |\n"
    "    Already resolved from a prior close-out pass.\n"
)


def _seed_ac_plan(
    root: Path, ac_section: str, rows_yaml: str = _ONE_CODED_ROW, status: str = "draft"
) -> Path:
    text = _AC_PLAN_TEMPLATE.format(status=status, ac_section=ac_section, rows=rows_yaml)
    dest = root / "plan.md"
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", "plan.md"], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


class TestAcTableDesync:
    def test_desync_fires_when_spine_fully_coded_but_ac_table_pending(
        self, tmp_path, monkeypatch
    ):
        """C1's live repro: every commit-required spine row is `coded` with
        a matching commit, but the AC table still reads `pending` on every
        row -- the finding must fire, and it must NOT block the stamp
        (C2)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_PENDING)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert result["ac_table_desync"] == {
            "unresolved_ac_ids": ["AC1", "AC2"],
            "total_ac_rows": 2,
        }
        assert "ADVISORY" in result["message"]
        assert "AC1" in result["message"]

    def test_no_finding_when_ac_table_already_agrees(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_RESOLVED)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert result["ac_table_desync"] is None
        assert "ADVISORY" not in result["message"]

    def test_no_finding_when_no_ac_table_at_all(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_disposition_plan(root, _ONE_CODED_ROW, status="draft")
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_DISPOSITION)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert result["ac_table_desync"] is None

    def test_no_finding_on_malformed_ac_table(self, tmp_path, monkeypatch):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_MALFORMED)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert result["ac_table_desync"] is None

    def test_no_finding_when_spine_still_has_an_open_row(self, tmp_path, monkeypatch):
        """The desync check only applies once the spine itself is fully
        resolved -- an ordinary halted/landed plan with a stale-looking AC
        table (still `pending`, matching the still-open spine) is not this
        check's concern."""
        root = tmp_path
        _init_repo(root)
        open_row = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Not yet landed.\n"
        )
        plan_file = _seed_ac_plan(root, _AC_TABLE_PENDING, rows_yaml=open_row)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert result["ac_table_desync"] is None

    def test_no_finding_when_ac_status_is_struck_through(self, tmp_path, monkeypatch):
        """A struck-through Status cell (`~~pending~~`) reads as the author
        deliberately settling that row -- RESOLVED, per
        `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s own docstring -- not unresolved
        (Review: code-reviewer -- Finding [P2], 2026-08-06). Stripping the
        `~~` delimiters before matching would collapse the cell down to the
        bare vocabulary word and misclassify it; this pins the fix."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_STRUCK_THROUGH)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert result["ac_table_desync"] is None
        assert "ADVISORY" not in result["message"]

    def test_desync_fires_on_lowercase_ac_heading(self, tmp_path, monkeypatch):
        """`## acceptance criteria` (lowercase) must still be recognized --
        a case-sensitive heading match silently reads a real plan's own AC
        section as absent, which is exactly the "check goes quiet" failure
        this desync check exists to avoid (Review: code-reviewer --
        Finding [P3], 2026-08-06)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_LOWERCASE_HEADING)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert result["ac_table_desync"] == {
            "unresolved_ac_ids": ["AC1"],
            "total_ac_rows": 1,
        }

    def test_desync_fires_on_two_column_table_with_no_id_column(self, tmp_path, monkeypatch):
        """The docstring-claimed 2-column, no-ID/`#` shape must actually
        parse -- `Criterion`/`Status` only, Status located by header name
        (Review: code-reviewer -- Finding [P3], 2026-08-06, coverage gap)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_TWO_COLUMN_NO_ID)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert result["ac_table_desync"] == {
            "unresolved_ac_ids": ["The widget ships", "The widget is documented"],
            "total_ac_rows": 2,
        }

    def test_desync_fires_when_status_column_is_not_last(self, tmp_path, monkeypatch):
        """The Status column must be located by header name, not by fixed
        position -- a table with `Status` before `Criterion` must still
        correctly key off the `Status` cell, not the trailing `Criterion`
        cell (Review: code-reviewer -- Finding [P3], 2026-08-06, coverage
        gap)."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_ac_plan(root, _AC_TABLE_STATUS_NOT_LAST)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_AC_DESYNC)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["status_target"] == "implemented"
        assert result["ac_table_desync"] == {
            "unresolved_ac_ids": ["AC1", "AC2"],
            "total_ac_rows": 2,
        }


class TestCommitResultPushStatus:
    """C6a (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-
    branch.md): `commit_result` must surface `push_status` (the
    fully-disambiguated companion to the legacy tristate `pushed` field)
    plus the AC7 pushed-extent fields `pushed_range`/`pushed_count`, and
    must not collapse this op's own "no push attempted" cases (dry-run,
    already-committed-by-the-stamp-write, nothing-to-commit) into the same
    `push_status` a real policy decline reports."""

    def test_declined_push_surfaces_push_status_declined(self, tmp_path, monkeypatch):
        """A landed commit whose push leg was declined by policy (a real
        `run_commit_pipeline` outcome, e.g. branch-policy decline) must
        report `push_status="declined"` -- distinct from this op's own
        `push_mode`-less "no attempt made" cases below, even though both
        read `pushed=None`."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        from types import SimpleNamespace

        fake_result = SimpleNamespace(
            committed_sha="deadbeef",
            pushed=None,
            push_status="declined",
            pushed_range=None,
            pushed_count=None,
            commit_failed=False,
            sha_unverified=False,
            diagnostics=["push: declined by policy"],
        )
        monkeypatch.setattr(coas, "run_commit_pipeline", lambda *a, **k: fake_result)
        monkeypatch.setattr(coas, "_stage_paths_committed_already", lambda *a, **k: False)
        monkeypatch.setattr(
            coas,
            "_reach_post_commit_tail_stub_close",
            lambda *a, **k: {"acted": [], "skipped": [], "failed": []},
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["commit"]["pushed"] is None
        assert result["commit"]["push_status"] == "declined"
        assert result["commit"]["pushed_range"] is None
        assert result["commit"]["pushed_count"] is None

    def test_landed_push_surfaces_pushed_range_and_count(self, tmp_path, monkeypatch):
        """A landed, successfully-pushed commit surfaces the resolved
        `pushed_range`/`pushed_count` extent alongside `pushed=True` and
        `push_status="pushed"` -- the whole point of AC7 reaching this
        payload (the original memo's "pushed: true while origin/main
        advanced by three commits that were not its own" incident)."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        from types import SimpleNamespace

        fake_result = SimpleNamespace(
            committed_sha="deadbeef",
            pushed=True,
            push_status="pushed",
            pushed_range="abc123..deadbeef",
            pushed_count=3,
            commit_failed=False,
            sha_unverified=False,
            diagnostics=["push: landed range abc123..deadbeef (3 commits)"],
        )
        monkeypatch.setattr(coas, "run_commit_pipeline", lambda *a, **k: fake_result)
        monkeypatch.setattr(coas, "_stage_paths_committed_already", lambda *a, **k: False)
        monkeypatch.setattr(
            coas,
            "_reach_post_commit_tail_stub_close",
            lambda *a, **k: {"acted": [], "skipped": [], "failed": []},
        )

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["commit"]["pushed"] is True
        assert result["commit"]["push_status"] == "pushed"
        assert result["commit"]["pushed_range"] == "abc123..deadbeef"
        assert result["commit"]["pushed_count"] == 3

    def test_no_attempt_synthetic_paths_report_not_attempted_not_declined(
        self, tmp_path, monkeypatch
    ):
        """This op's own "no push attempted" cases (DR-272's
        already-committed-by-the-stamp-write shortcut here) must report
        `push_status="not-attempted"`, never `"declined"` -- a decline is a
        real policy outcome from `run_commit_pipeline`, distinct from this
        op never having tried at all. Exercises the DR-272 shortcut branch
        (`_stage_paths_committed_already` True), which never reaches
        `run_commit_pipeline`."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        monkeypatch.setattr(coas, "_stage_paths_committed_already", lambda *a, **k: True)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["commit"]["pushed"] is None
        assert result["commit"]["push_status"] == coas.PUSH_STATUS_NOT_ATTEMPTED
        assert result["commit"]["pushed_range"] is None
        assert result["commit"]["pushed_count"] is None

    def test_dry_run_reports_not_attempted_push_status(self, tmp_path, monkeypatch):
        """The dry-run synthetic `commit_result` (no `run_commit_pipeline`
        call at all) must also report `push_status="not-attempted"`, not
        `None`/absent."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)

        exit_code, result, _pre_head = _run_close_out(monkeypatch, root, "plan.md", dry_run=True)

        assert exit_code == coas.EXIT_OK, result
        assert result["commit"]["pushed"] is None
        assert result["commit"]["push_status"] == coas.PUSH_STATUS_NOT_ATTEMPTED
        assert result["commit"]["pushed_range"] is None
        assert result["commit"]["pushed_count"] is None


def _commit_chunk_with_demoted_trailer(
    root: Path,
    plan_rel: str,
    chunk_id: str,
    deliverable_id: str,
    *,
    tail_trailers: str = "Commit-Token: 0123456789abcdef0123456789abcdef",
) -> None:
    """Lands a chunk commit shaped exactly like the ones the 2026-08-10
    trailer-join defect produced: the caller's `Deliverable-Id:` sits in its
    OWN paragraph, with a blank line before the pipeline's `Commit-Token:`/
    `Session-Id:` block. Git recognises only the LAST paragraph as trailers,
    so `%(trailers:key=Deliverable-Id,valueonly)` comes back EMPTY for a
    commit that visibly carries the line.

    Successive `-m` arguments are exactly how git builds that shape (it joins
    them with blank lines), so this reproduces the on-disk commits
    `b1e0881d39a7`/`3301a8d1f68c` without depending on the pipeline that
    emitted them.
    """
    plan_file = root / plan_rel
    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", plan_rel], root)
    _run_git(
        [
            "commit",
            "-q",
            "-m",
            f"{chunk_id}: land chunk",
            "-m",
            f"Deliverable-Id: {deliverable_id}",
            "-m",
            tail_trailers,
        ],
        root,
    )


class TestDemotedDeliverableIdTrailerFallback:
    """Pins the message-line fallback in `_resolve_deliverable_id`.

    A defect in `commit()`'s trailer-join branch (fixed 2026-08-10 at
    `5fcbb42696e5`) left a blank line between a caller's `Deliverable-Id:`
    and the pipeline's own trailer block, so git demoted the caller's line to
    prose and the exact-equality trailer join could not see it. Every `-F`
    caller was exposed for as long as that path has existed, which is the
    commit practice `/execute-plan` doctrine prescribes -- so the affected
    set is every plan whose chunks landed before the fix, not just the two
    commits that surfaced it. Rewriting shared-branch history is not the
    remedy, so the READER adapts.

    Each test below fails against the pre-fallback reader: the demoted
    commits produce an empty trailer atom, so the join attributes nothing and
    the plan reports its chunks missing at exit 0 over a range that provably
    contains them.
    """

    def test_demoted_trailer_still_attributes_the_chunk(self, tmp_path):
        """THE LIVE BUG, reproduced: every chunk landed, every commit carries
        a visible `Deliverable-Id:` line, and the plan must read shipped."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk_with_demoted_trailer(
                root, "plan.md", chunk_id, _DLV_VALID_SPINE
            )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []
        assert join_provenance == "joined"

    def test_demoted_trailer_for_another_plan_still_does_not_count(self, tmp_path):
        """The fallback recovers attribution; it must not widen it. A DEMOTED
        line naming a DIFFERENT plan's deliverable is now visible to the join
        and must be excluded by the same exact-equality test that excludes a
        properly-parsed foreign trailer -- otherwise this fix would reopen
        the 2026-07-27 false-positive incident from the other side."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk_with_demoted_trailer(
            root, "plan.md", "C1", "dlv-some-other-plan-000002"
        )
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2a", _DLV_VALID_SPINE)
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2b", _DLV_VALID_SPINE)

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]
        assert join_provenance == "joined"

    def test_parsed_trailer_wins_over_a_conflicting_body_line(self, tmp_path):
        """Precedence is trailer-first and never the reverse. A commit whose
        PARSED trailer names another plan, while its body quotes THIS plan's
        id at line start, must still not count -- the fallback is consulted
        only when git parsed nothing at all, so no already-correct verdict
        can change."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        plan_target = root / "plan.md"
        with plan_target.open("a", encoding="utf-8") as fh:
            fh.write("\n<!-- C1 landed -->\n")
        _run_git(["add", "plan.md"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C1: land chunk",
                "-m",
                f"Deliverable-Id: {_DLV_VALID_SPINE}",
                "-m",
                "Deliverable-Id: dlv-some-other-plan-000002",
            ],
            root,
        )
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2a", _DLV_VALID_SPINE)
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2b", _DLV_VALID_SPINE)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]

    def test_mid_sentence_mention_is_not_an_attribution(self, tmp_path):
        """The fallback is line-anchored on purpose: prose that mentions
        `Deliverable-Id: <id>` mid-sentence is not an attribution and must
        not join, or every commit message discussing this defect would
        attribute itself."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        plan_target = root / "plan.md"
        with plan_target.open("a", encoding="utf-8") as fh:
            fh.write("\n<!-- C1 landed -->\n")
        _run_git(["add", "plan.md"], root)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                "C1: land chunk",
                "-m",
                f"The join reads Deliverable-Id: {_DLV_VALID_SPINE} from a trailer.",
            ],
            root,
        )
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2a", _DLV_VALID_SPINE)
        _commit_chunk_with_demoted_trailer(root, "plan.md", "C2b", _DLV_VALID_SPINE)

        shipped, missing, _join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1"]


def _commit_chunk_session_only(root: Path, plan_rel: str, chunk_id: str, session_id: str) -> None:
    """Lands a chunk commit carrying ONLY a `Session-Id:` trailer -- no
    `Deliverable-Id:` at all -- the shape `_committed_chunk_shas`'s C6
    Session-Id fallback exists to recover: a genuinely-shipped commit whose
    Deliverable-Id leg has nothing to join against. Mirrors `_commit_chunk`'s
    own append-then-commit shape so `git log -- <path>` sees a real tree
    change at `plan_rel`, exactly as that helper's own docstring explains."""
    plan_file = root / plan_rel
    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", plan_rel], root)
    _run_git(
        ["commit", "-q", "-m", f"{chunk_id}: land chunk", "-m", f"Session-Id: {session_id}"],
        root,
    )


def _write_plan_claim(root: Path, plan_rel: str, session_id: str) -> None:
    """Writes a plan-claim dir's `session_id` file directly at the exact
    on-disk path `_plan_claim_holder_session_id` reads
    (`coordinator_core.ops.fleet._common.plan_claim_dir`) -- deliberately
    bypassing the full `session.claims.claim_plan` machinery (session-id
    resolution, pid liveness, EEXIST/takeover handling), none of which this
    fallback's own read path depends on or needs exercised here."""
    claim_dir = coas.plan_claim_dir(coas.git_common_dir(root), Path(plan_rel))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")


class TestSessionIdFallbackEvidence:
    """Pins C6 (`docs/plans/2026-08-10-a-commit-trailer-that-names-the-
    session.md`, AC10, finding 0): `_committed_chunk_shas` degrades to a
    Session-Id-scoped chunk-subject match, bounded to this plan's own claim
    holder and spine ids, ONLY when its own `Deliverable-Id:` join finds
    ZERO evidence for this plan (`DeliverableJoinStats.matched_commit_count
    == 0`). See that function's own docstring for the full zero-evidence-
    gated argument this class exists to pin -- a general Session-Id
    widening, applied regardless of Deliverable-Id evidence, would re-admit
    the 2026-07-27 `C8b` cross-plan false-positive incident § Deliverable
    scoping already closed."""

    def test_zero_evidence_session_id_fallback_fires_for_a_covering_commit(
        self, tmp_path
    ):
        """THE RECOVERY CASE: zero Deliverable-Id evidence anywhere in
        range, but every chunk's own commit carries a `Session-Id:` trailer
        naming the session this plan's own claim dir records as the
        current holder, and a subject that covers a real spine id -- the
        fallback must fire and the plan must read fully shipped."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _write_plan_claim(root, "plan.md", "sess-fallback-000001")
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk_session_only(
                root, "plan.md", chunk_id, "sess-fallback-000001"
            )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is True
        assert missing == []
        # No commit anywhere in range ever carried a Deliverable-Id trailer
        # at all -- the join itself never had a candidate to compare
        # against, so this is "no_join_candidates", not "joined". The
        # Session-Id fallback resolving every chunk-id is what makes
        # `shipped` True despite that -- exactly the case this fix exists
        # to recover.
        assert join_provenance == "no_join_candidates"

    def test_zero_evidence_unresolvable_session_claim_stays_missing(self, tmp_path):
        """The negative twin: same zero-Deliverable-Id-evidence
        precondition, but the commit's `Session-Id:` trailer does NOT
        resolve to a claim on THIS plan (the plan's own claim dir records a
        DIFFERENT session as holder). No fallback evidence must be added --
        the chunk stays missing, never a crash, never a guess."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _write_plan_claim(root, "plan.md", "sess-real-holder-000001")
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk_session_only(
                root, "plan.md", chunk_id, "sess-impostor-000002"
            )

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C1", "C2a", "C2b"]
        assert join_provenance == "no_join_candidates"

    def test_non_zero_evidence_gate_blocks_the_fallback(self, tmp_path):
        """THE GATE ITSELF: at least one commit in range already carries a
        matching `Deliverable-Id:` trailer (non-zero evidence), and a SECOND
        commit exists that would otherwise satisfy the fallback shape
        (correct `Session-Id:` claim holder, covering subject) for a
        DIFFERENT still-missing chunk. The fallback must NOT fire at all --
        that second commit contributes nothing, proving the gate is
        zero-evidence-only and not a general widening."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _write_plan_claim(root, "plan.md", "sess-fallback-000001")
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C2b", deliverable_id=_DLV_VALID_SPINE)
        # Fallback-shaped, but the Deliverable-Id join above already found
        # evidence for this plan -- this must contribute nothing.
        _commit_chunk_session_only(root, "plan.md", "C2a", "sess-fallback-000001")

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C2a"]
        assert join_provenance == "joined"

    def test_partial_fallback_reports_session_fallback_partial(self, tmp_path):
        """Review: code-reviewer -- Finding P2, 2026-08-10, slice D. Zero
        Deliverable-Id evidence anywhere in range (same precondition as
        `test_zero_evidence_session_id_fallback_fires_for_a_covering_commit`),
        but the Session-Id fallback only resolves SOME of the plan's
        chunk-ids (one commit names the correct claim holder; one still has
        no covering commit at all) -- `join_provenance` must name the
        partial-fallback state, not "no_join_candidates" ("nothing existed
        to compare against" is no longer true once the fallback found
        evidence for another chunk in the same range), and the still-
        uncovered chunk must remain in `missing`."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _write_plan_claim(root, "plan.md", "sess-fallback-000001")
        for chunk_id in ("C1", "C2a"):
            _commit_chunk_session_only(
                root, "plan.md", chunk_id, "sess-fallback-000001"
            )
        # C2b gets no commit at all -- the fallback resolves 2 of 3.

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert missing == ["C2b"]
        assert join_provenance == "session_fallback_partial"


# ===========================================================================
# C4a -- repo-identity gate wiring (compute_repo_identity_gate)
# ===========================================================================


import json as _json
import time as _time

from coordinator_core.pickup_assemble import (
    _REPO_IDENTITY_MATCH,
    _REPO_IDENTITY_MISMATCH,
    _REPO_IDENTITY_UNRESOLVED,
)
from coordinator_core.session import harness_registry as _hr


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + _hr._FILETIME_EPOCH_OFFSET_SEC) * _hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = _time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(_json.dumps(payload), encoding="utf-8")
    return epoch


def _patch_pid_env(monkeypatch, pid, create_time=0.0, hit=True):
    if hit:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((pid, create_time), "env-hit"),
        )
    else:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )


class TestRepoIdentityGateWiring:
    """C4a -- wires `compute_repo_identity_gate` where `close_out_and_stamp`
    resolves its `root`. Every verdict is constructed with REAL registry
    records on disk (AC6) -- never by monkeypatching the gate's own return
    value, mirroring `pickup_assemble/tests/test_repo_identity_gate.py`'s
    own construction pattern."""

    def _setup_registry(self, monkeypatch, sessions_dir, pid, sid, cwd):
        monkeypatch.setattr(_hr, "registry_dir", lambda: sessions_dir)
        _write_registry_record(sessions_dir, f"{pid}.json", sid, pid, cwd)
        _patch_pid_env(monkeypatch, pid)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )
        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    def test_mismatch_refuses_on_cwd_derived_root(self, tmp_path, monkeypatch):
        """No explicit `repo_root` -- `close_out_and_stamp` must resolve its
        own root via cwd, so a real anchor/root divergence must refuse via
        this module's own `EXIT_BUSINESS_FAIL`/`{"error": ...}` vocabulary,
        carrying the gate's own message."""
        root = tmp_path / "repo"
        foreign = tmp_path / "foreign"
        root.mkdir()
        _init_repo(root)
        foreign.mkdir()
        (foreign / ".git").mkdir()
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        sessions_dir = tmp_path / "sessions"
        self._setup_registry(monkeypatch, sessions_dir, 4101, "sess-c4a-1", foreign)

        monkeypatch.chdir(root)
        exit_code, result = coas.close_out_and_stamp("plan.md")

        assert exit_code == coas.EXIT_BUSINESS_FAIL
        assert "error" in result
        assert str(foreign) in result["error"] or "sess-c4a-1" in result["error"]
        assert result["dry_run"] is False

    def test_mismatch_does_not_refuse_when_repo_root_explicit(self, tmp_path, monkeypatch):
        """The same real divergence, but the caller supplied `repo_root`
        explicitly (as `_run_close_out` always does) -- must proceed
        normally, carrying the MISMATCH verdict informationally."""
        root = tmp_path / "repo"
        foreign = tmp_path / "foreign"
        root.mkdir()
        _init_repo(root)
        foreign.mkdir()
        (foreign / ".git").mkdir()
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        sessions_dir = tmp_path / "sessions"
        self._setup_registry(monkeypatch, sessions_dir, 4102, "sess-c4a-2", foreign)

        exit_code, result, _ = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["gates"]["repo_identity"]["verdict"] == _REPO_IDENTITY_MISMATCH

    def test_match_emits_informational_gate_entry(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        sessions_dir = tmp_path / "sessions"
        self._setup_registry(monkeypatch, sessions_dir, 4103, "sess-c4a-3", root)

        exit_code, result, _ = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["gates"]["repo_identity"]["verdict"] == _REPO_IDENTITY_MATCH

    def test_unresolved_never_refuses(self, tmp_path, monkeypatch):
        """No registry record at all -- UNRESOLVED, never a refusal, even
        on the cwd-derived (no explicit `repo_root`) path (DR-277)."""
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(_hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 4104, hit=False)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-c4a-4")

        monkeypatch.chdir(root)
        exit_code, result = coas.close_out_and_stamp("plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["gates"]["repo_identity"]["verdict"] == _REPO_IDENTITY_UNRESOLVED


# ===========================================================================
# join-provenance non-chunk-commit inflation (cross-repo memo, 2026-08-08,
# `trailer-confirmed-and-reporting-shape-accepted`, §2): `matched_commit_
# count`/`JOIN_PROVENANCE_JOINED` used to count ANY commit in range carrying
# a `Deliverable-Id:` trailer equal to the plan's own -- including plan-
# authoring/ceremony commits with a non-chunk-shaped subject that never
# registers a chunk-id via `_extract_chunk_ids` at all. Ceremony commits
# always trailer, so a plan with genuinely zero shipped chunks (and zero
# chunk-shaped commits in range) still reported `joined` off its own
# authoring commits -- reading as a substantive, evidence-backed "nothing
# shipped" verdict rather than the true "no chunk-shaped evidence exists"
# state.
# ===========================================================================


class TestJoinProvenanceExcludesNonChunkCommits:
    def test_trailered_non_chunk_shaped_commit_alone_does_not_report_joined(
        self, tmp_path
    ):
        """A commit whose subject registers zero chunk-ids (a plain
        ceremony/authoring subject, e.g. `docs: author the plan`) but
        carries a matching `Deliverable-Id:` trailer must NOT, by itself,
        satisfy `matched_commit_count`/`JOIN_PROVENANCE_JOINED` -- the join
        stat is a fact about attributable CHUNK evidence, not about any
        trailered commit in range."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "docs: author the plan document",
            deliverable_id=_DLV_VALID_SPINE,
        )

        query_ok, committed, committed_shas, join_stats = coas._committed_chunk_shas(
            root, _DLV_VALID_SPINE, spine_ids=["C1", "C2a", "C2b"]
        )
        assert query_ok is True
        assert committed == set()
        assert committed_shas == {}
        assert join_stats.matched_commit_count == 0

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert shipped is False
        assert join_provenance != coas.JOIN_PROVENANCE_JOINED

    def test_trailered_chunk_shaped_commit_still_reports_joined(self, tmp_path):
        """Sibling pin: a genuine chunk-shaped, trailered commit still
        reports `matched_commit_count > 0`/`JOIN_PROVENANCE_JOINED` -- this
        fix narrows matched-commit counting to chunk-shaped commits only,
        it does not touch the real join path."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_with_subject(
            root,
            "plan.md",
            "C1: land chunk one",
            deliverable_id=_DLV_VALID_SPINE,
        )

        query_ok, committed, committed_shas, join_stats = coas._committed_chunk_shas(
            root, _DLV_VALID_SPINE
        )
        assert query_ok is True
        assert "C1" in committed
        assert join_stats.matched_commit_count >= 1

        shipped, missing, join_provenance, error = coas._determine_shipped(
            plan_file.read_text(encoding="utf-8"), "plan.md", root
        )
        assert error is None
        assert join_provenance == coas.JOIN_PROVENANCE_JOINED
