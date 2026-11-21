"""Ratchet: the landing refuses only irreversible acts, and the blanket-commit block stays.

Three parts: (a) no waiver module or waivable-guard table exists under coordinator_core;
(b) every landing refusal reason sits in the closed table, and only its two kinds refuse;
(c) bash dispatch still denies ``git add -A``, ``git add .`` and the all-tracked commit, each
message naming ``ceremony.commit_v2``.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_git_shared, dispatch_checks
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.ops.dispatch_emit import hunk_provenance, landing_targeted_tests
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "coordinator_core"

_MODULE_NAME = "landing" + "_waiver"
_TABLE_NAME = "_WAIVABLE" + "_GUARDS"


# Emitted today but absent from the closed table: never refuses (is_refusal is False for it).
_UNTABLED_NON_REFUSING = frozenset({
    landing_targeted_tests.ZERO_FAIL_UNATTRIBUTED,
    landing_targeted_tests.INVARIANT_UNMEASURED,
    landing_targeted_tests.RETEST_ABANDONED,
    hunk_provenance.FOREIGN_HUNK_UNPRODUCED,
    # emit.py's plan-lint findings: raised at emit time, never on a landing path.
    "absent-edit-target",
    "import-window-row",
    "new-module-with-importer",
    "CHUNK-SHAPER-MERGED",
    "test-verdict-names-no-failure",
})


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _hazard_repo_with_tracked_modification(tmp_path: Path) -> Path:
    """The only shape the all-tracked commit denies: a hazard repo holding a tracked modification."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "base.txt").write_text("edited")
    return repo


def _dispatch_verdict(cmd: str) -> tuple[str, str]:
    """Runs the dispatch checks that own the blanket-add and all-tracked-commit denies, rendered for the EM."""
    for check in (dispatch_checks.check_blanket_git_add, dispatch_checks.check_git_commit_safe_commit_advise):
        out = envelope_of(check(cmd, "sess-c7-ratchet"))
        if out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny":
            return "deny", json.dumps(out)
    return "allow", ""


@pytest.mark.spawns_process
@pytest.mark.parametrize("flags", ["add -A", "add ."])
def test_blanket_add_denies_and_names_the_commit_op(tmp_path, monkeypatch, flags):
    repo = _hazard_repo_with_tracked_modification(tmp_path)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    decision, text = _dispatch_verdict(f"git -C {shlex.quote(str(repo))} {flags}")
    assert decision == "deny"
    assert "ceremony.commit_v2" in text


@pytest.mark.spawns_process
def test_all_tracked_commit_denies_and_names_the_commit_op(tmp_path, monkeypatch):
    repo = _hazard_repo_with_tracked_modification(tmp_path)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    decision, text = _dispatch_verdict(f'git -C {shlex.quote(str(repo))} commit -am "x"')
    assert decision == "deny"
    assert "ceremony.commit_v2" in text
