"""
coordinator_core.execute_plan_assemble.tests.test_close_out_and_stamp — direct
coverage for `close_out_and_stamp.py`.

Why this file exists: `close_out_and_stamp` had NO dedicated test file (grep
confirmed none, and `execute_plan_assemble/tests/` did not exist at all) and
was covered only transitively, through the shared `locate_fenced_block`
locator seam. That gap was not academic -- project-rag-em reported a real
defect (commit `08cbf4bd`) where the locator's two missing hardening fixes
made this op refuse on every plan still carrying `coordinator-doc-new`'s
scaffolded template comment. The op's refusal drove EMs to hand-stamp
`status:` instead, which skips the chunk-completion cross-reference entirely
-- a *partially shipped* plan could be stamped `implemented` with no
mechanical check at all. That second-order effect (not just the locator bug
itself) is what this file pins down directly against the op.

Spec backlinks:
  coordinator_core/execute_plan_assemble/close_out_and_stamp.py (module under test)
  cross-repo/archive/2026-07-26-project-rag-em-spine-locator-parity-gap.md (originating report)
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
    landing_sha = _head_sha(root)
    # C3 (2026-08-21, "the close ceremony stops paying for the join"): the
    # commit-subject/Deliverable-Id join this helper's own subject
    # convention was built for is deleted -- the surviving evidence path
    # is a `## Tasks` spine row's own verified `disposition_ref`. Stamp
    # the just-landed sha onto `chunk_id`'s own row (a second, real commit
    # -- never a synthetic sha) when the plan carries a LOCATED spine with
    # a row for it, so this helper's many existing callers keep meaning
    # "this chunk shipped" under the current oracle. A plan whose spine is
    # absent, or has no row for `chunk_id` (the legacy Dispatch Ledger
    # fixtures), is left untouched -- `_mark_chunk_disposition_ref` itself
    # degrades to a no-op there.
    _mark_chunk_disposition_ref(root, plan_rel, chunk_id, landing_sha)
    return landing_sha


def _mark_chunk_disposition_ref(
    root: Path, plan_rel: str, chunk_id: str, sha: str
) -> None:
    """Stamps `disposition: coded` / `disposition_ref: <sha>` onto
    `chunk_id`'s own `## Tasks` spine row and lands a second real commit --
    reuses `close_out_and_stamp`'s own surviving `_stamp_rows_in_body`
    splice (the SAME writer `cascade_baton_rows.py` now uses), never a
    hand-rolled YAML edit. No-op (no write, no commit) when the plan has
    no LOCATED spine, or no row named `chunk_id` at all."""
    plan_file = root / plan_rel
    text = plan_file.read_text(encoding="utf-8")
    located = coas.locate_fenced_block(text)
    if located.status != coas.LocateStatus.LOCATED:
        return
    start, end = located.span
    new_body, stamp_error = coas._stamp_rows_in_body(
        text[start:end], {chunk_id: sha}, {chunk_id: f"landed at {sha}"}
    )
    if stamp_error is not None:
        # The row may not exist on this fixture's spine at all (e.g. a
        # commit landed for a chunk-id this plan never declared) -- no
        # stamp to make, same as the "no row" case above.
        return
    plan_file.write_text(text[:start] + new_body + text[end:], encoding="utf-8", newline="\n")
    _run_git(["add", plan_rel], root)
    _run_git(["commit", "-q", "-m", f"resolve {chunk_id}"], root)


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
    disagree (mirrors `test_opticon_ground_truth_regression.
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
        `test_opticon_ground_truth_regression.py`'s own
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
            lambda *args, **kwargs: (True, [], True, None),
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
        stamp -- no spine parse, no git-log query, no ledger read ever ran,
        so the stamping gate refuses to act on it, leaving the plan's own
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

        # The dry run wrote nothing beyond what `_commit_chunk` itself
        # already landed (C3: `_commit_chunk` stamps `disposition: coded`/
        # `disposition_ref` onto a committed chunk's own spine row as part
        # of landing its commit -- the surviving evidence path is now
        # PLAN-side, not a post-hoc join `close_out_and_stamp` computes) --
        # the plan file is byte-identical to before this call.
        assert plan_file.read_bytes() == original_bytes
        rows = _spine_rows(plan_file)
        c1_row = next(row for row in rows if row.get("id") == "C1")
        assert c1_row.get("disposition") == "coded"
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
            "disposition_ref_rejections",
            "open_chunk_ids",
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
# ordinary concurrency (cross-repo/project-opticon-em report,
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
# chunk missing on fully-shipped plans (cross-repo/project-opticon-em report)
# ===========================================================================


class TestResolutionModel:
    """The widened completeness oracle (AC9) and the `landed` intermediate
    status (AC7) -- see `close_out_and_stamp.py`'s own evidence-sources
    docstring section for the full design rationale.

    C3 (2026-08-21, "the close ceremony stops paying for the join"):
    committed-but-open row auto-resolution (AC8) is DELETED along with the
    commit-subject join it depended on -- there is no longer an automatic
    "the tree already has this, promote it for me" inference. The former
    `test_committed_open_row_auto_resolves_to_coded_with_sha` pin is
    removed with it; the remaining tests below use a `disposition_ref`
    fixture (the surviving evidence path) in place of `_commit_chunk`'s
    subject-join fixture.
    """

    def test_wont_do_row_excluded_from_commit_oracle_stamps_implemented(
        self, tmp_path, monkeypatch
    ):
        """AC9: a plan with one `wont_do` row and every other row `coded`
        (with a verified `disposition_ref`) stamps `implemented`, not
        halted -- `wont_do` was never commit-required (it carries no code
        to land), and `coded` rows with real evidence are already
        resolved, so there is nothing left `open` to block the stamp."""
        root = tmp_path
        _init_repo(root)
        (root / "widget.py").write_text("v1")
        _run_git(["add", "widget.py"], root)
        _run_git(["commit", "-q", "-m", "ship the widget"], root)
        landing_sha = _head_sha(root)
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            f"  disposition_ref: {landing_sha}\n"
            "  disposition_detail: 'Shipped and verified.'\n"
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

        exit_code, result, pre_head = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is True
        assert result["missing_chunk_ids"] == []
        assert result["open_chunk_ids"] == []
        assert result["status_target"] == "implemented"
        assert _read_status(plan_file) == "implemented"
        assert _head_sha(root) != pre_head

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
            lambda *args, **kwargs: (True, [], True, None),
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
        the defect through.

        C3 (2026-08-21, "the close ceremony stops paying for the join"):
        `close_out_and_stamp()` no longer stamps a row's own `disposition:`/
        `disposition_ref:` itself -- that AC8 auto-resolve write path was
        deleted along with the commit-subject join it depended on, and
        `_commit_chunk` (this file's own test helper) now lands that stamp
        directly, as a second real commit, at chunk-commit time. So by the
        time `close_out_and_stamp` runs here, C1's row is already stamped;
        the only write this call itself makes is the `close_out_last_
        partial:` frontmatter line, since C2 is still open. The comment/
        block-scalar-preservation assertions below stay meaningful as a
        "this call touches nothing it should not" pin, even though the
        fence-body round-trip they originally guarded now lives in `_stamp_
        rows_in_body`'s own direct unit tests."""
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
            "# <!-- Review: Zoli -- looks fine -->\n"
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
        assert "# <!-- Review: Zoli -- looks fine -->\n" in new_lines

        # The block scalar is still a `|` block, and no apostrophe got
        # doubled (the PyYAML-round-trip failure mode this fix removes).
        assert "  body: |\n" in new_lines
        assert "    It's the widget's first shipment.\n" in new_lines
        assert "    Not committed yet -- stays open.\n" in new_lines
        assert "It''s" not in new_text
        assert "widget''s" not in new_text

        # C1's own disposition/disposition_ref/disposition_detail lines
        # were already written by `_commit_chunk`'s own `_mark_chunk_
        # disposition_ref` call, BEFORE this `close_out_and_stamp` call --
        # so the only line this call itself adds is the ONE `close_out_
        # last_partial:` frontmatter line (C2 fix, 2026-08-06 -- this run is
        # still halted, since C2's own row stays open/uncommitted).
        assert len(new_lines) == len(original_lines) + 1
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
        # disposition_detail carries the prose `_mark_chunk_disposition_ref`
        # (this file's own helper, not close_out_and_stamp) passes through
        # `_stamp_rows_in_body` at chunk-commit time.
        assert c1_row["disposition_detail"] == f"landed at {c1_row['disposition_ref']}"
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
        self, monkeypatch
    ):
        """Step 2: if the row-level stamp somehow produced a change outside
        the disposition/disposition_ref fields, the caller must refuse --
        never write, never commit, never push. Forces exactly that shape by
        monkeypatching `_stamp_rows_in_body` to return a body with an
        unrelated line corrupted, then asserts `_assert_stamp_fidelity`
        (driven the same way `_stamp_whole_plan`'s own caller drives it)
        surfaces the refusal.

        C3 (2026-08-21): pinned directly against `_stamp_whole_plan` /
        `_assert_stamp_fidelity`, not through `close_out_and_stamp()` --
        that function no longer calls `_stamp_rows_in_body` itself at all
        (the AC8 auto-resolve write path that used to wire them together
        was deleted along with the commit-subject join it depended on;
        `cascade_baton_rows.py`, C4's own surface, is this function's
        current live caller)."""
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan_text = _PLAN_TEMPLATE.format(status="executing", rows=rows_yaml)

        def _corrupting_stamp(body, updates, details=None):
            # Deliberately drop an unrelated line -- not a
            # disposition/disposition_ref/disposition_detail field -- to
            # simulate a stamper bug the fidelity gate must catch.
            lines = body.splitlines(keepends=True)
            corrupted = [l for l in lines if "title" not in l]
            return "".join(corrupted), None

        monkeypatch.setattr(coas, "_stamp_rows_in_body", _corrupting_stamp)

        new_text, fidelity_error = _stamp_whole_plan(
            plan_text, {"C1": "abc1234"}, {"C1": "C1: land chunk"}
        )

        assert fidelity_error is not None
        assert "plan.md" in fidelity_error
        assert "refusing" in fidelity_error.lower()

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

    def test_fidelity_gate_refuses_a_mis_indented_stamp(self, monkeypatch):
        """Review: code-reviewer -- F3: proves the fidelity gate now
        REFUSES a stamp landed at the wrong indent, rather than passing it
        vacuously. Forces exactly that shape by monkeypatching
        `_stamp_rows_in_body` to emit its `disposition:`/`disposition_ref:`
        lines at an indent that does NOT match the row's own measured
        content indent, then asserts `_assert_stamp_fidelity` refuses.

        C3 (2026-08-21): pinned directly against `_stamp_whole_plan` /
        `_assert_stamp_fidelity`, not through `close_out_and_stamp()` --
        see the sibling test above for why that orchestration no longer
        wires them together."""
        rows_yaml = (
            "- id: C1\n"
            "  title: Ship the widget\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/widget.py\n"
            "  deferred: false\n"
            "  body: |\n"
            "    Ship the widget end to end.\n"
        )
        plan_text = _PLAN_TEMPLATE.format(status="executing", rows=rows_yaml)

        def _mis_indented_stamp(body, updates, details=None):
            # Row's own content indent is 2 spaces; deliberately stamp at
            # 4 spaces instead -- textually a valid `disposition:` line,
            # but at the WRONG indent for this row.
            lines = body.splitlines(keepends=True)
            lines.append("    disposition: coded\n")
            lines.append("    disposition_ref: abc1234\n")
            return "".join(lines), None

        monkeypatch.setattr(coas, "_stamp_rows_in_body", _mis_indented_stamp)

        new_text, fidelity_error = _stamp_whole_plan(
            plan_text, {"C1": "abc1234"}, {"C1": "C1: land chunk"}
        )

        assert fidelity_error is not None
        assert "plan.md" in fidelity_error
        assert "refusing" in fidelity_error.lower()


# ===========================================================================
# Regression: cross-repo memo, project-opticon-em 2026-08-01 --
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
# Plan-side disposition_ref evidence -- the surviving, pure sha-ancestry
# evidence path (C3, 2026-08-21, "the close ceremony stops paying for the
# join"): a `disposition: coded` row's own `disposition_ref` verified as a
# real, ancestor commit. See close_out_and_stamp.py's own docstring §
# Plan-side disposition_ref evidence.
# ===========================================================================


def _make_non_ancestor_commit(root: Path) -> str:
    """A REAL commit object in `root`'s own object store that is NOT
    reachable from current `HEAD` -- built with a plain `commit-tree` write
    (reusing HEAD's own tree, no parent) rather than checkout/branch
    gymnastics. `git rev-parse --verify <sha>^{commit}` resolves it (the
    object genuinely exists); `git merge-base --is-ancestor <sha> HEAD`
    reports false (no ref, including HEAD, was ever built from it) -- this
    is exactly the shape `_verify_disposition_ref`'s non-ancestor rejection
    case must reject: a real commit HEAD never reached."""
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
        _run_git(["commit", "-q", "-m", "AC6 MET: a makima-driven publish is byte-identical"], root)
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
        _run_git(["commit", "-q", "-m", "AC6 MET: a makima-driven publish is byte-identical"], root)
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
        """False-positive-stamp incident fix: `## Dispatch Ledger — makima
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
            "## Dispatch Ledger\n", "## Dispatch Ledger — makima [M] slice\n"
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
        assert result["stamped"] is True
        assert _read_status(plan_file) == "implemented"


# ===========================================================================
# False-positive-stamp incident (2026-08-06): the no-spine/no-ledger branch
# of `_determine_shipped` performs ZERO evidence lookups. This class pins
# the fix: `shipped` stays True (D7's own posture, unchanged), but
# `evidence_backed` is False so the stamping gate refuses to act on it --
# see `_determine_shipped`'s own docstring/routing comment.
#
# C3 (2026-08-21, "the close ceremony stops paying for the join"): the
# commit-subject/Deliverable-Id join this class originally pinned via a
# `join_provenance` string is deleted along with the rest of that
# machinery -- `_determine_shipped` now reports a plain `evidence_backed`
# bool instead, and `close_out_and_stamp()`'s own return dict no longer
# carries a `join_provenance` key at all.
# ===========================================================================


class TestNoEvidenceSourceProvenance:
    def test_no_spine_no_ledger_reports_not_evidence_backed(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_ZERO_BLOCKS)
        plan_text = plan_file.read_text(encoding="utf-8")

        shipped, missing, evidence_backed, error = coas._determine_shipped(
            plan_text, "plan.md", root
        )

        assert error is None
        assert shipped is True
        assert missing == []
        assert evidence_backed is False

    def test_real_disposition_ref_evidence_still_reports_evidence_backed(
        self, tmp_path, monkeypatch
    ):
        """Sibling pin: a plan with a real `## Tasks` spine whose rows carry
        genuine, verified `disposition_ref` evidence still reports
        `evidence_backed=True` -- this fix narrows ONLY the no-spine/
        no-ledger branch, nothing about the real evidence path."""
        root = tmp_path
        _init_repo(root)
        plan_file = _seed_plan(root, _FIXTURE_VALID_SPINE)
        for chunk_id in ("C1", "C2a", "C2b"):
            _commit_chunk(root, "plan.md", chunk_id, deliverable_id=_DLV_VALID_SPINE)
        plan_text = plan_file.read_text(encoding="utf-8")

        shipped, missing, evidence_backed, error = coas._determine_shipped(
            plan_text, "plan.md", root
        )

        assert error is None
        assert shipped is True
        assert missing == []
        assert evidence_backed is True

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


