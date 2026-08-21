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

import pytest

from coordinator_core import baton_assemble as ba
from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.contract import apply_base

from coordinator_core.test_baton_assemble import (  # noqa: E402
    _FAKE_OPERATOR_CONFIG,
    _PRED_REL,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _ReplayHarness,
)


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
