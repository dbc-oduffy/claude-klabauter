"""
coordinator_core.baton_assemble.tests.test_apply_orphan_on_commit_pipeline_error
-- spec 5 regression coverage (state/handoffs/2026-08-21-scaffold-knows-the-
session.md): a `baton-assemble apply` run whose commit-pipeline step fails
AFTER every directive already landed on disk must not strand the
just-scaffolded successor as an unclaimed, untracked orphan.

`_D1_COMPENSATORS`'s `_compensate_d1_scaffold` only fires from
`apply_base.execute_directives`'s own `except Exception` branch -- i.e. only
when a LATER DIRECTIVE raises (`APPLY_EXIT_PARTIAL_MUTATION`). It never fires
when every directive succeeds and `apply()`'s own commit step
(`_scoped_commit` -> `apply_base.scoped_commit`, which raises `RuntimeError`
on a failed `git add`/`git commit`) is the thing that fails. That gap is
what left an orphan scaffold on disk in the incident this baton describes.

Reuses `coordinator_core.test_baton_assemble._ReplayHarness` -- the one
whole-`apply()`-run-against-a-real-git-repo harness this module's sibling
tests already share, faking only the subprocess-shaped directives (d1/d2/d5)
and letting d6 (`handoff.archive_transition`) run for real.

Spec backlink: state/handoffs/2026-08-21-scaffold-knows-the-session.md § spec 5/AC6
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import baton_assemble as ba
from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.contract import apply_base
from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()

from coordinator_core.test_baton_assemble import (  # noqa: E402
    _FAKE_OPERATOR_CONFIG,
    _PRED_REL,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _ReplayHarness,
)

# Spawns a real external process (git, via `_ReplayHarness`'s real `apply()`
# run); runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module, same values -- see the sibling test file's own
    fixture of this name for why an autouse fixture cannot cross modules."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


class TestOrphanCleanupOnCommitPipelineError:
    def test_successor_not_stranded_when_scoped_commit_raises(self, tmp_path, monkeypatch):
        harness = _ReplayHarness(tmp_path, monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("fake git add/commit failure")

        monkeypatch.setattr(ba_apply, "_scoped_commit", _boom)

        with pytest.raises(RuntimeError, match="fake git add/commit failure"):
            harness.run()

        # d1 scaffolded a successor this run (predecessor.md is the only
        # pre-existing handoff) -- the commit step's failure must not leave
        # it behind, unclaimed and untracked, for a later run to mis-derive
        # provenance from (defect B's incident shape).
        successors = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert successors == [], (
            f"orphan successor(s) survived a commit-pipeline error: {successors}"
        )

    def test_scoped_commit_success_path_is_unaffected(self, tmp_path, monkeypatch):
        """Control: the happy path (no exception) is byte-identical to
        before this fix -- the successor lands and survives."""
        harness = _ReplayHarness(tmp_path, monkeypatch)
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        successors = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert len(successors) == 1, report

    def test_git_add_succeeds_commit_fails_unstages_as_well_as_unlinks(
        self, tmp_path, monkeypatch
    ):
        """Reviewer BLOCKER: `apply_base.scoped_commit` is `git add` then
        `git commit` as two independently-raising steps. Let the real `add`
        land (the scaffold IS staged at the moment `commit` fails) and force
        only `commit` to fail -- the old unlink-only cleanup left a stale
        staged blob for a path no longer on disk. Assert both halves: gone
        from disk AND gone from the index."""
        harness = _ReplayHarness(tmp_path, monkeypatch)
        real_run_git = ba_apply._run_git

        def _fail_commit_only(args, cwd):
            if args and args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "fake commit failure")
            return real_run_git(args, cwd)

        monkeypatch.setattr(ba_apply, "_run_git", _fail_commit_only)

        with pytest.raises(RuntimeError, match="git commit"):
            harness.run()

        successors = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert successors == [], (
            f"orphan successor(s) survived a commit-only failure: {successors}"
        )

        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=harness.repo,
            capture_output=True,
            text=True,
            check=True,
            **_NO_CONSOLE,
        ).stdout.split()
        assert staged == [], (
            f"cleanup unlinked the file but left a stale staged blob: {staged}"
        )

    def test_lineage_fallback_artifact_is_never_deleted(self, tmp_path, monkeypatch):
        """Reviewer WARN: when the decision object carries no lineage,
        `committed_artifact_path` falls back to `effective_artifact_path` --
        per the pre-existing comment above that fallback, the predecessor
        handoff or the plan being handed off, a pre-existing artifact this
        run did NOT author. A commit-pipeline failure on that fallback path
        must leave it alone -- deleting it would be data loss, not orphan
        cleanup."""
        repo_root = tmp_path
        predecessor = repo_root / "state" / "handoffs" / "predecessor.md"
        predecessor.parent.mkdir(parents=True)
        predecessor.write_text("pre-existing content\n", encoding="utf-8")

        # `**_` absorbs every keyword `brief` gains later. Pinned to an exact
        # signature this double went red the moment `brief` gained `session_id`
        # (e78d7e83ee) and stayed red silently; this test asserts orphan-cleanup
        # behaviour and has no stake in the parameter list.
        def _fake_brief(kind, artifact_path, *, decisions=None, repo_root=None, title=None, **_):
            class _FakeBriefResult:
                decision_object = {
                    "directives": [],
                    "judgment_points": [],
                    # No "lineage" key -- forces the fallback branch under
                    # test; committed_artifact_path resolves to this path.
                    "artifact": {"path": artifact_path},
                }

            return _FakeBriefResult()

        def _fake_execute_directives(
            directives, judgment_points, root, *, decisions=None, composition_budget=None
        ):
            return ba_apply.APPLY_EXIT_OK, {"landed": []}

        def _boom(*args, **kwargs):
            raise RuntimeError("fake git add/commit failure")

        monkeypatch.setattr(ba, "brief", _fake_brief)
        monkeypatch.setattr(ba_apply, "_execute_directives", _fake_execute_directives)
        monkeypatch.setattr(ba_apply, "_scoped_commit", _boom)

        with pytest.raises(RuntimeError, match="fake git add/commit failure"):
            ba_apply.apply(
                "handoff",
                "state/handoffs/predecessor.md",
                session_id="test-session",
                repo_root=repo_root,
            )

        assert predecessor.is_file(), (
            "lineage-fallback artifact was deleted by orphan cleanup"
        )
        assert predecessor.read_text(encoding="utf-8") == "pre-existing content\n"
