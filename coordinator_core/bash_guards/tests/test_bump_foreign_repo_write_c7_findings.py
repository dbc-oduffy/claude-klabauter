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

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.session.core import session_dir



def _msys_form(p) -> str:
    """MSYS/MinGW drive-mount spelling (`/c/Users/...`) of an absolute
    native path -- the shape Git-for-Windows' bash hands to tools as
    argument expansion, and the exact shape `_write_bump_sink_shapes.
    translate_msys_path`/`resolve_relative` exist to translate correctly."""
    s = str(p)
    drive, rest = os.path.splitdrive(s)
    letter = drive.rstrip(":").lower()
    rest_posix = rest.replace("\\", "/")
    return f"/{letter}{rest_posix}"


def _posix(p) -> str:
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized. Accepts a ``Path`` or a
    plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


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
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

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
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

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

    cmd = f"git -C {_posix(target_repo)} commit --allow-empty -m x"
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

    cmd = f"git -C {_posix(unregistered_repo)} commit --allow-empty -m x"
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

    cmd = f"git -C {_posix(worktree_dir)} commit --allow-empty -m x"

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
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ac14b", str(repos["anchor"]), {}
    )

    assert result is not None


# ---------------------------------------------------------------------------
# C2 -- AC1: the guard-posix-path-rerooting defect, regression. On Windows,
# an MSYS-spelled `-C <dir>` target naming the session's OWN anchor repo
# must not be re-rooted onto the process's current drive and wrongly
# classified as foreign.
# ---------------------------------------------------------------------------


def test_ac1_msys_absolute_dash_c_target_same_repo_not_denied(repos, monkeypatch, tmp_path):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    msys_anchor = _msys_form(repos["anchor"])
    cmd = f"git -C {msys_anchor} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ac1-foreign", str(repos["anchor"]), {}
    )

    assert result is None, (
        "MSYS-spelled -C target naming the session's own anchor repo was "
        "wrongly denied -- the guard-posix-path-rerooting defect"
    )


# ---------------------------------------------------------------------------
# C2 -- AC3: the fix must not turn a real bump into a permit. A genuinely
# foreign repo, spelled in the same MSYS form, still bumps.
# ---------------------------------------------------------------------------


def test_ac3_msys_absolute_dash_c_target_genuinely_foreign_still_denies(
    repos, monkeypatch, tmp_path
):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    msys_foreign = _msys_form(repos["foreign"])
    cmd = f"git -C {msys_foreign} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ac3-foreign", str(repos["anchor"]), {}
    )

    assert result is not None


# ---------------------------------------------------------------------------
# PowerShell `Set-Location` cwd-tracking parity fix (2026-08-08, backlog row
# `2026-08-07-bump-foreign-repo-write-s-powershell-leg-3254b856d676`). These
# assert through `check_bump_foreign_repo_write`'s own real return value
# (a deny envelope, or `None`) -- never by re-reading a constant the test
# itself set -- so a revert of the `Set-Location` tracking fix flips these
# from PASS to FAIL.
# ---------------------------------------------------------------------------


def _ps_payload() -> dict:
    return {"tool_name": "PowerShell"}


def test_set_location_into_foreign_repo_then_write_is_detected(repos, monkeypatch, tmp_path):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = f'Set-Location {_posix(repos["foreign"])}; New-Item -Path file.txt -ItemType File'

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-setloc-foreign", str(repos["anchor"]), _ps_payload()
    )

    assert result is not None, (
        "a New-Item write extracted AFTER a Set-Location into a foreign "
        "git root must resolve against the CHANGED base and bump -- the "
        "exact parity gap this fix closes"
    )


def test_set_location_into_own_anchor_repo_then_write_does_not_bump(
    repos, monkeypatch, tmp_path
):
    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = f'Set-Location {_posix(repos["anchor"])}; New-Item -Path file.txt -ItemType File'

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-setloc-anchor", str(repos["anchor"]), _ps_payload()
    )

    assert result is None, (
        "a Set-Location into the session's OWN anchor repo followed by a "
        "write must not bump -- the tracking fix must not turn an in-repo "
        "write into a false bump"
    )


def test_set_location_unresolvable_target_yields_silent_not_deny(
    repos, monkeypatch, tmp_path
):
    """A `Set-Location` to a variable-valued (unresolvable) target must
    never guess a base for the write that follows -- the leg goes SILENT
    (via `_verdict.record_silent`) for the rest of the command, and
    `check_bump_foreign_repo_write` itself still returns `None` (never a
    manufactured deny off an untrusted cwd)."""
    from coordinator_core.bash_guards import _verdict

    _set_anchor_env(monkeypatch, repos, tmp_path)
    cmd = 'Set-Location $SomeUnresolvedVar; New-Item -Path file.txt -ItemType File'

    with _verdict.collecting() as silences:
        result = guard.check_bump_foreign_repo_write(
            cmd, "sess-ps-setloc-unresolvable", str(repos["anchor"]), _ps_payload()
        )

    assert result is None, (
        "an unresolvable Set-Location target must never produce a "
        "verdict off a guessed base"
    )
    assert any(
        s.guard_name == "bump-foreign-repo-write" and "Set-Location" in s.reason
        for s in silences
    ), "the unresolvable Set-Location must be recorded SILENT, not merely swallowed"
