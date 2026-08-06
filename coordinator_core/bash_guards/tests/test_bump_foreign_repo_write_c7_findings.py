"""C7 findings-disposition tests for
`coordinator_core.bash_guards.bump_foreign_repo_write` -- kept SEPARATE from
`test_bump_foreign_repo_write.py` (C3's own concurrently-dispatched surface;
this plan's C7 is sequenced after C2 but alongside C3's wave in practice, and
the two chunks were told explicitly not to coordinate on that shared file) so
this dispatch's own additions land with no merge/ownership ambiguity.

Spec backlink: docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md,
chunk C7 -- findings #3 and #4 (carried on example-doctrine-repo's evidence, re-verified here)
and AC14 (the linked-worktree false positive, `_same_repo_root`'s own bug,
fixed alongside #3 since both live in this file).

Covers:
  - Finding #3 -- `record_applicability_event` must log under the SESSION's
    own anchor hub, never under the foreign target's hub (the guard's own
    canonical cross-repo scenario is exactly the shape that broke this).
  - Finding #4 -- the "session anchor owns no git repo, but the write TARGET
    is a registered repo" branch is real and reachable now that C1's
    settings-home anchor write does not require a git repo to resolve from.
  - AC14 -- a linked worktree of the session's own anchor repo does not bump
    (`_same_repo_root` now compares `--git-common-dir`, not the per-worktree
    `--git-dir` `resolve_gitdir` returns).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.session.core import session_dir


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


def _write_registry(reg_dir: Path, **repos: str) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[repos]"]
    for key, val in repos.items():
        escaped = str(val).replace("\\", "\\\\")
        lines.append(f'{key} = "{escaped}"')
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_bump_env(monkeypatch):
    """Same isolation shape as `test_bump_foreign_repo_write.py`'s own
    `_clean_bump_env` (this module's dependencies read `os.environ`
    directly, never an injected mapping) -- kept as a local copy rather than
    an import from C3's concurrently-dispatched file, per this chunk's own
    "coordinate nothing" instruction."""
    for var in (
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "COORDINATOR_SETTINGS_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def repos(tmp_path):
    anchor = _init_repo(tmp_path, "anchor")
    foreign = _init_repo(tmp_path, "foreign")
    home = tmp_path / "home"
    home.mkdir()
    return {"anchor": anchor, "foreign": foreign, "home": home}


def _set_anchor_env(monkeypatch, repos, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repos["anchor"]))
    monkeypatch.setenv("HOME", str(repos["home"]))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


# ---------------------------------------------------------------------------
# Finding #3 -- the observability log must land under the ANCHOR's own hub,
# never under the foreign target's.
# ---------------------------------------------------------------------------


def test_finding3_applicability_log_lands_under_anchor_not_foreign_repo(
    repos, monkeypatch, tmp_path
):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = f"git -C {repos['foreign']} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-f3", str(repos["anchor"]), {}
    )

    assert result is not None  # the bump itself is unaffected by this fix

    anchor_log = Path(session_dir("sess-f3", str(repos["anchor"]))) / (
        "write_bump_applicability_log"
    )
    foreign_log = Path(session_dir("sess-f3", str(repos["foreign"]))) / (
        "write_bump_applicability_log"
    )

    assert anchor_log.is_file(), (
        "the observability log must be written under the SESSION's own "
        "anchor repo hub"
    )
    assert not foreign_log.exists(), (
        "the observability log must NEVER be written into the foreign repo "
        "the guard is bumping the write away from -- finding #3's exact "
        "defect"
    )


def test_finding3_applicability_log_content_names_both_repos(repos, monkeypatch, tmp_path):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = f"git -C {repos['foreign']} commit --allow-empty -m x"

    guard.check_bump_foreign_repo_write(cmd, "sess-f3b", str(repos["anchor"]), {})

    anchor_log = Path(session_dir("sess-f3b", str(repos["anchor"]))) / (
        "write_bump_applicability_log"
    )
    line = anchor_log.read_text(encoding="utf-8")
    assert "sess-f3b" in line
    # `repo=` names the anchor's own repo root, `target=` the foreign one --
    # the log line still records BOTH repos even though it is now written
    # under the anchor's hub, not the target's.
    assert "repo=" in line and "target=" in line
    assert "foreign" in line


# ---------------------------------------------------------------------------
# Finding #4 -- "session anchor owns no repo, registered target still bumps"
# is REAL, not dead, once the settings-home anchor can resolve without a
# git repo of its own.
# ---------------------------------------------------------------------------


def test_finding4_no_repo_anchor_bumps_against_registered_target(tmp_path, monkeypatch):
    """The session's own anchored launch directory is in NO git repo at
    all -- only reachable via C1's settings-home write, which (unlike the
    in-repo `sessions_dir` write) needs no git repo to resolve from. The
    write TARGET is a registered repo in the machine-local registry, so the
    bump must still fire per `target_is_registered_repo`."""
    scratch_anchor = tmp_path / "scratch-anchor"
    scratch_anchor.mkdir()
    target_repo = _init_repo(tmp_path, "registered-target")
    home = tmp_path / "home"
    home.mkdir()

    for var in (
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "COORDINATOR_SETTINGS_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    # C0's SessionStart write -- launch cwd is the no-repo scratch dir. The
    # in-repo leg of this write silently no-ops (no git repo to resolve
    # `sessions_dir` from); the settings-home leg does not need one.
    session_start.write_session_start_record("sess-f4", launch_cwd=str(scratch_anchor))

    reg_dir = tmp_path / "registry"
    _write_registry(reg_dir, target=str(target_repo))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    cmd = f"git -C {target_repo} commit --allow-empty -m x"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-f4", str(scratch_anchor), {}
    )

    assert result is not None, (
        "finding #4: a no-repo anchor must still bump against a REGISTERED "
        "target repo -- this branch is reachable now that the settings-home "
        "anchor write needs no git repo"
    )


def test_finding4_no_repo_anchor_does_not_bump_against_unregistered_target(
    tmp_path, monkeypatch
):
    """Same shape as above, but the target is NOT a registered repo -- per
    `target_is_registered_repo`'s own fail-open contract, this must not
    bump (a fresh, unregistered scaffold tree writes freely)."""
    scratch_anchor = tmp_path / "scratch-anchor"
    scratch_anchor.mkdir()
    unregistered_repo = _init_repo(tmp_path, "unregistered")
    home = tmp_path / "home"
    home.mkdir()

    for var in (
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "COORDINATOR_SETTINGS_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    settings_home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    session_start.write_session_start_record("sess-f4b", launch_cwd=str(scratch_anchor))
    # No registry written at all -- `MACHINE_LOCAL_REGISTRY_DIR` points
    # nowhere, so `target_is_registered_repo` degrades to `[]`/`False`.
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-registry-here"))

    cmd = f"git -C {unregistered_repo} commit --allow-empty -m x"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-f4b", str(scratch_anchor), {}
    )

    assert result is None


# ---------------------------------------------------------------------------
# AC14 -- a linked worktree of the anchor repo does not bump.
# ---------------------------------------------------------------------------


def test_ac14_linked_worktree_of_anchor_repo_does_not_bump(repos, monkeypatch, tmp_path):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    worktree_dir = tmp_path / "anchor-worktree"
    _git(
        str(repos["anchor"]),
        "worktree",
        "add",
        "-b",
        "wt-branch",
        str(worktree_dir),
    )

    cmd = f"git -C {worktree_dir} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ac14", str(repos["anchor"]), {}
    )

    assert result is None, (
        "AC14: a linked worktree of the session's own anchor repo must NOT "
        "bump -- `_same_repo_root` must compare `--git-common-dir`, not "
        "the per-worktree `--git-dir` `resolve_gitdir` returns"
    )


def test_ac14_still_bumps_against_a_genuinely_different_repo(repos, monkeypatch, tmp_path):
    """Negative-of-the-negative: the AC14 fix must not blanket-suppress
    real cross-repo bumps -- a genuinely separate repo (not a worktree of
    the anchor) still bumps exactly as AC1 requires."""
    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = f"git -C {repos['foreign']} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ac14b", str(repos["anchor"]), {}
    )

    assert result is not None
