"""Tests for coordinator_core.bash_guards._write_bump_marker -- the
write-confinement speed bump's session-scoped clear-once marker.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567, chunk C3.
Covers AC6 (marker honoured for the whole session), AC7 (subagent inherits
its EM's marker), AC15 (the advertised clear line works verbatim), plus the
`.git`-as-a-FILE worktree/submodule cases and the unwritable-gitdir
fail-open case named explicitly in the chunk's own test-surface note.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_marker as marker
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_foreign_repo_write as fg_guard



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


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------------------
# resolve_gitdir -- plain repo, worktree, submodule, no-repo, fail-open
# ---------------------------------------------------------------------------


def test_resolve_gitdir_plain_repo(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert gitdir is not None
    assert gitdir.resolve() == (root / ".git").resolve()


def test_resolve_gitdir_no_repo_fails_open(tmp_path):
    scratch = tmp_path / "not-a-repo"
    scratch.mkdir()
    assert marker.resolve_gitdir(str(scratch)) is None


def test_resolve_gitdir_missing_git_binary_fails_open(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)

    def _raise(*_a, **_kw):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert marker.resolve_gitdir(str(root)) is None


def test_resolve_gitdir_worktree_resolves_to_file_backed_dotgit(tmp_path):
    root = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(str(root), "worktree", "add", "-q", str(wt), "-b", "wt-branch")

    # The worktree's own `.git` is a FILE (a `gitdir:` pointer), not a
    # directory -- the exact hazard this module's gitdir resolution exists
    # to avoid composing a literal join against.
    assert (wt / ".git").is_file()

    gitdir = marker.resolve_gitdir(str(wt))
    assert gitdir is not None
    assert gitdir.is_dir()
    # The worktree's private gitdir lives under the main repo's .git/worktrees/.
    assert "worktrees" in gitdir.parts


def test_resolve_gitdir_submodule_resolves_to_file_backed_dotgit(tmp_path):
    parent = _init_repo(tmp_path, "parent")
    child = _init_repo(tmp_path, "child")

    _git(
        str(parent),
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child),
        "sub",
    )
    _git(str(parent), "commit", "-q", "-m", "add submodule")

    sub_checkout = parent / "sub"
    assert (sub_checkout / ".git").is_file()

    gitdir = marker.resolve_gitdir(str(sub_checkout))
    assert gitdir is not None
    assert gitdir.is_dir()
    assert "modules" in gitdir.parts


# ---------------------------------------------------------------------------
# marker_present -- prefix matching, absence re-bumps, unwritable gitdir
# ---------------------------------------------------------------------------


def test_marker_present_false_when_absent(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    assert marker.marker_present(gitdir, "sess-123") is False


def test_marker_present_true_for_exact_basename(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("sess-123")).touch()
    assert marker.marker_present(gitdir, "sess-123") is True


def test_marker_present_true_for_prefix_match_not_just_exact(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    # Deliberately not an exact-name match -- an entry whose basename
    # STARTS WITH the session's marker basename still counts. See module
    # docstring "BASENAME MATCHING ON READ IS BY PREFIX".
    (gitdir / (marker.marker_basename("sess-123") + "-stray-suffix")).touch()
    assert marker.marker_present(gitdir, "sess-123") is True


def test_marker_present_false_for_different_session(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("some-other-session")).touch()
    assert marker.marker_present(gitdir, "sess-123") is False


def test_marker_present_false_when_gitdir_none():
    assert marker.marker_present(None, "sess-123") is False


def test_marker_present_false_when_session_id_empty(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    assert marker.marker_present(gitdir, "") is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_marker_present_allows_rather_than_dead_ends_on_unreadable_gitdir(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("sess-123")).touch()

    original_mode = gitdir.stat().st_mode
    try:
        os.chmod(gitdir, 0o000)
        # Must not raise -- an unlistable gitdir is treated as "no marker
        # found" (re-bumps), never a dead end.
        assert marker.marker_present(gitdir, "sess-123") is False
    finally:
        os.chmod(gitdir, original_mode)


# ---------------------------------------------------------------------------
# marker_path / clear_line -- AC15, the advertised clear line works verbatim
# ---------------------------------------------------------------------------


def test_clear_line_matches_marker_path_and_prefix(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    session_id = "sess-abc"

    line = marker.clear_line(gitdir, session_id)
    assert line == f"touch {(gitdir / marker.marker_basename(session_id)).as_posix()}"

    path_str = line[len("touch "):]
    assert path_str == marker.marker_path(gitdir, session_id).as_posix()


def test_clear_line_uses_posix_separators_so_a_shell_cannot_eat_them(tmp_path):
    """The operator pastes this line into bash. A native `WindowsPath` renders
    backslashes, bash reads each one as an escape, and the touch lands a single
    mangled filename in the CURRENT directory while the gitdir stays empty --
    the guard then denies again with the identical message, which reads as the
    approval not working rather than as a path bug. Observed live 2026-08-07.

    Asserted on the string, not via a shell, so the pin holds on POSIX hosts
    too, where the bug is invisible by construction.
    """
    gitdir = tmp_path / ".git"
    gitdir.mkdir()

    line = marker.clear_line(gitdir, "sess-sep")
    assert "\\" not in line


def test_clear_line_command_works_verbatim_on_a_fresh_machine(tmp_path):
    """AC15: the emitted clear line, run exactly as printed, produces a
    marker that `marker_present()` then reads back as cleared -- with no
    prior provisioning of any kind."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert gitdir is not None

    session_id = "fresh-session-1"
    line = marker.clear_line(gitdir, session_id)
    assert marker.marker_present(gitdir, session_id) is False

    command = line[len("touch "):]
    subprocess.run(["touch", command], check=True)

    assert marker.marker_present(gitdir, session_id) is True


# ---------------------------------------------------------------------------
# resolve_em_session_id / effective_session_id -- AC7, subagent inheritance
# ---------------------------------------------------------------------------


def test_resolve_em_session_id_reads_backpointer(tmp_path):
    root = _init_repo(tmp_path)
    backptr_dir = root / ".git" / "coordinator-sessions" / ".agents" / "agent-abc123def456"
    backptr_dir.mkdir(parents=True)
    (backptr_dir / "em-session-id.txt").write_text("em-session-xyz\n", encoding="utf-8")

    assert marker.resolve_em_session_id(str(root), "agent-abc123def456") == "em-session-xyz"


def test_resolve_em_session_id_absent_file_fails_open(tmp_path):
    root = _init_repo(tmp_path)
    assert marker.resolve_em_session_id(str(root), "no-such-agent") == ""


def test_resolve_em_session_id_malformed_content_fails_open(tmp_path):
    root = _init_repo(tmp_path)
    backptr_dir = root / ".git" / "coordinator-sessions" / ".agents" / "agent-abc"
    backptr_dir.mkdir(parents=True)
    (backptr_dir / "em-session-id.txt").write_text("!! not a session id\n", encoding="utf-8")

    assert marker.resolve_em_session_id(str(root), "agent-abc") == ""


def test_resolve_em_session_id_empty_args_fail_open():
    assert marker.resolve_em_session_id("", "agent-abc") == ""
    assert marker.resolve_em_session_id("/some/root", "") == ""


def test_effective_session_id_prefers_em_session_when_subagent(tmp_path):
    root = _init_repo(tmp_path)
    backptr_dir = root / ".git" / "coordinator-sessions" / ".agents" / "agent-abc123def456"
    backptr_dir.mkdir(parents=True)
    (backptr_dir / "em-session-id.txt").write_text("em-session-xyz\n", encoding="utf-8")

    resolved = marker.effective_session_id(
        "subagent-own-session-id", str(root), "agent-abc123def456"
    )
    assert resolved == "em-session-xyz"


def test_effective_session_id_falls_back_to_payload_session_id(tmp_path):
    root = _init_repo(tmp_path)
    # No back-pointer at all -- EM caller, or unresolved subagent.
    resolved = marker.effective_session_id("own-session-id", str(root), "")
    assert resolved == "own-session-id"

    resolved_unresolved_agent = marker.effective_session_id(
        "own-session-id", str(root), "agent-does-not-exist"
    )
    assert resolved_unresolved_agent == "own-session-id"


def test_subagent_inherits_em_marker_without_a_second_one(tmp_path):
    """AC7: a dispatched subagent's bump_is_cleared() check sees the SAME
    marker its EM created, with no marker of its own."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert gitdir is not None

    em_session_id = "em-session-xyz"
    (gitdir / marker.marker_basename(em_session_id)).touch()

    backptr_dir = root / ".git" / "coordinator-sessions" / ".agents" / "agent-abc123def456"
    backptr_dir.mkdir(parents=True)
    (backptr_dir / "em-session-id.txt").write_text(em_session_id + "\n", encoding="utf-8")

    assert marker.bump_is_cleared(
        str(root),
        "subagent-own-session-id",
        git_root=str(root),
        agent_id="agent-abc123def456",
    ) is True


# ---------------------------------------------------------------------------
# bump_is_cleared -- top-level composition, AC6 (whole-session honouring)
# ---------------------------------------------------------------------------


def test_bump_is_cleared_true_after_marker_created(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert gitdir is not None

    session_id = "sess-em"
    assert marker.bump_is_cleared(str(root), session_id) is False

    (gitdir / marker.marker_basename(session_id)).touch()
    assert marker.bump_is_cleared(str(root), session_id) is True


def test_bump_is_cleared_false_when_no_repo(tmp_path):
    scratch = tmp_path / "not-a-repo"
    scratch.mkdir()
    assert marker.bump_is_cleared(str(scratch), "sess-em") is False


def test_bump_is_cleared_false_when_session_id_empty(tmp_path):
    root = _init_repo(tmp_path)
    assert marker.bump_is_cleared(str(root), "") is False


# ---------------------------------------------------------------------------
# sweep_stale_markers -- AC21, session-end hygiene, never load-bearing
# ---------------------------------------------------------------------------


def test_sweep_stale_markers_removes_only_named_session(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("ended-session")).touch()
    (gitdir / marker.marker_basename("still-live-session")).touch()

    removed = marker.sweep_stale_markers(gitdir, ["ended-session"])

    assert removed == 1
    assert marker.marker_present(gitdir, "ended-session") is False
    # A live session's own marker must never be swept as a side effect of
    # sweeping a DIFFERENT session's marker in the same gitdir.
    assert marker.marker_present(gitdir, "still-live-session") is True


def test_sweep_stale_markers_does_not_remove_prefix_matched_stray_suffix(tmp_path):
    """The DELETE path is exact-match, unlike `marker_present()`'s read-path
    prefix match (see module docstring, "EXACT MATCH, NOT PREFIX"). A stray
    file whose basename merely STARTS WITH an ended session's marker
    basename -- but is not an exact match -- must survive the sweep."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    stray = gitdir / (marker.marker_basename("ended-session") + "-stray-suffix")
    stray.touch()

    removed = marker.sweep_stale_markers(gitdir, ["ended-session"])

    assert removed == 0
    assert stray.is_file()


def test_sweep_stale_markers_does_not_unlink_live_marker_when_ended_id_is_its_prefix(tmp_path):
    """DoE finding #7, AC8. `_SESSION_ID_FORMAT_RE` permits `abc` as a valid
    session id and also permits `abcdef` -- a string-prefix of one another.
    An ended session `abc` must never unlink live session `abcdef`'s marker
    via a prefix match on the delete path."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("abcdef")).touch()

    removed = marker.sweep_stale_markers(gitdir, ["abc"])

    assert removed == 0
    assert marker.marker_present(gitdir, "abcdef") is True


def test_sweep_stale_markers_removes_exact_match_even_when_a_longer_id_would_collide(tmp_path):
    """Mirror of the above: the ended id's OWN exact marker is still removed
    when both `abc` and `abcdef` markers are present in the same gitdir --
    the fix narrows the match, it does not also break the legitimate
    exact-match removal."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    abc_marker = gitdir / marker.marker_basename("abc")
    abc_marker.touch()
    abcdef_marker = gitdir / marker.marker_basename("abcdef")
    abcdef_marker.touch()

    removed = marker.sweep_stale_markers(gitdir, ["abc"])

    assert removed == 1
    # The exact-match `abc` marker file is gone -- swept as the ended
    # session's own record. (`marker_present(gitdir, "abc")` would still
    # read `True` here because its READ path is a deliberate prefix match
    # against the surviving `abcdef` file -- see module docstring "BASENAME
    # MATCHING ON READ IS BY PREFIX" -- so this asserts on the file directly
    # rather than through that read-path helper.)
    assert not abc_marker.exists()
    assert abcdef_marker.exists()
    assert marker.marker_present(gitdir, "abcdef") is True


def test_sweep_stale_markers_does_not_touch_unrelated_files(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    unrelated = gitdir / "HEAD"
    unrelated.write_text("ref: refs/heads/main\n", encoding="utf-8")

    removed = marker.sweep_stale_markers(gitdir, ["ended-session"])

    assert removed == 0
    assert unrelated.is_file()


def test_sweep_stale_markers_never_sweeps_a_live_session_absent_from_the_list(tmp_path):
    """The core AC21 guarantee: a marker read past its owning session's
    liveness must not stand a LIVE session's own bump down -- but the sweep
    side of that guarantee is the mirror case, that a live session's marker
    is never touched unless its own id is explicitly named as ended."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    live_session = "live-session-xyz"
    (gitdir / marker.marker_basename(live_session)).touch()

    removed = marker.sweep_stale_markers(gitdir, [])

    assert removed == 0
    assert marker.marker_present(gitdir, live_session) is True


def test_sweep_stale_markers_no_op_when_gitdir_none():
    assert marker.sweep_stale_markers(None, ["some-session"]) == 0


def test_sweep_stale_markers_no_op_when_gitdir_unlistable(tmp_path, monkeypatch):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("ended-session")).touch()

    def _raise(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", _raise)

    assert marker.sweep_stale_markers(gitdir, ["ended-session"]) == 0


def test_sweep_stale_markers_ignores_non_string_and_empty_ids(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("sess-1")).touch()

    removed = marker.sweep_stale_markers(gitdir, [None, "", 123, "sess-1"])

    assert removed == 1
    assert marker.marker_present(gitdir, "sess-1") is False


# ---------------------------------------------------------------------------
# marker_gitdir_is_writable -- STAFF-ENG F0 / AC5, write-axis fail-open.
# ---------------------------------------------------------------------------


def test_marker_gitdir_is_writable_true_for_ordinary_dir(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    assert marker.marker_gitdir_is_writable(gitdir) is True


def test_marker_gitdir_is_writable_false_when_not_a_directory(tmp_path):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("gitdir: ../elsewhere\n", encoding="utf-8")
    assert marker.marker_gitdir_is_writable(not_a_dir) is False


def test_marker_gitdir_is_writable_false_when_path_does_not_exist(tmp_path):
    assert marker.marker_gitdir_is_writable(tmp_path / "does-not-exist") is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_marker_gitdir_is_writable_false_when_read_only(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    original_mode = gitdir.stat().st_mode
    try:
        os.chmod(gitdir, 0o555)  # read + execute, no write
        assert marker.marker_gitdir_is_writable(gitdir) is False
    finally:
        os.chmod(gitdir, original_mode)


# ---------------------------------------------------------------------------
# AC4/AC5/AC6 end-to-end, through the real guard -- non-vacuous per the
# module's own AC9 requirement: `bump_applies()` is asserted True, and the
# guard is confirmed to have actually FIRED, before any downstream property
# (a clear, a per-target distinction, or an absence property) is asserted
# against the result. See ratified lesson
# state/lessons/2026-07-28-a-test-that-asserts-the-absence-of-a-fai-55498037e129.yaml.
# ---------------------------------------------------------------------------


def _end_to_end_setup(tmp_path, monkeypatch, session_id: str):
    root = _init_repo(tmp_path, "anchor")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(root))
    return root


def test_ac4_clearing_target_a_leaves_target_b_firing(tmp_path, monkeypatch):
    session_id = "sess-ac4-per-target"
    root = _end_to_end_setup(tmp_path, monkeypatch, session_id)
    foreign_a = _init_repo(tmp_path, "foreign-a")
    foreign_b = _init_repo(tmp_path, "foreign-b")

    # AC9 -- non-negotiable precondition before asserting any guard verdict.
    assert applicability.bump_applies(session_id, cwd=str(root)) is True

    a_gitdir = marker.resolve_gitdir(str(foreign_a))
    assert a_gitdir is not None
    (a_gitdir / marker.marker_basename(session_id)).touch()

    cmd_a = f"git -C {_posix(foreign_a)} commit --allow-empty -m x"
    cmd_b = f"git -C {_posix(foreign_b)} commit --allow-empty -m x"

    result_a = fg_guard.check_bump_foreign_repo_write(cmd_a, session_id, str(root), {})
    result_b = fg_guard.check_bump_foreign_repo_write(cmd_b, session_id, str(root), {})

    assert result_a is None, "target A's own marker must clear target A"
    assert result_b is not None, (
        "clearing target A must NOT clear target B -- the marker is scoped "
        "per-(session, target), not per-session (AC4)"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_ac5_unwritable_target_gitdir_allows_matching_unresolvable_precedent(tmp_path, monkeypatch):
    """STAFF-ENG F0 / AC5: an unwritable/unreadable target gitdir takes the
    IDENTICAL disposition as an unresolvable one -- ALLOW (`continue`),
    never an unclearable deny advertising a `touch` the reader cannot
    execute (a read-only mirror synced under another uid)."""
    session_id = "sess-ac5-unwritable"
    root = _end_to_end_setup(tmp_path, monkeypatch, session_id)
    foreign = _init_repo(tmp_path, "foreign")

    assert applicability.bump_applies(session_id, cwd=str(root)) is True

    foreign_gitdir = marker.resolve_gitdir(str(foreign))
    assert foreign_gitdir is not None
    original_mode = foreign_gitdir.stat().st_mode
    try:
        os.chmod(foreign_gitdir, 0o555)  # read + execute, no write
        cmd = f"git -C {_posix(foreign)} commit --allow-empty -m x"
        result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(root), {})
        assert result is None, (
            "an unwritable target gitdir must ALLOW the write, matching "
            "check_bump_foreign_repo_write's existing `marker_gitdir is "
            "None` precedent exactly"
        )
    finally:
        os.chmod(foreign_gitdir, original_mode)


def test_ac5_clear_line_executed_verbatim_clears_the_target(tmp_path, monkeypatch):
    """AC5 -- execute the advertised clear line in-test, verbatim, and
    confirm it actually clears this exact target."""
    session_id = "sess-ac5-clear-verbatim"
    root = _end_to_end_setup(tmp_path, monkeypatch, session_id)
    foreign = _init_repo(tmp_path, "foreign")

    assert applicability.bump_applies(session_id, cwd=str(root)) is True

    cmd = f"git -C {_posix(foreign)} commit --allow-empty -m x"
    result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(root), {})
    assert result is not None, "guard must actually fire for this test to mean anything"

    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    clear_line = next(line.strip() for line in reason.split("\n") if line.strip().startswith("touch "))
    subprocess.run(clear_line.split(" ", 1), check=True)

    result2 = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(root), {})
    assert result2 is None


def test_ac6_marker_carries_no_expiry_identity_gating_or_hard_deny(tmp_path, monkeypatch):
    """AC6 -- `XREPO_MARKER_IS_ORDINARY_FILE` re-asserted post-change: no
    expiry, no identity gating, no `fail_closed=True`, no `CONFINEMENT_DENY`.
    Non-vacuous: `bump_applies()` is True AND the guard actually fired (a
    deny came back) before any absence property is asserted on that fired
    result."""
    session_id = "sess-ac6-non-vacuous"
    root = _end_to_end_setup(tmp_path, monkeypatch, session_id)
    foreign = _init_repo(tmp_path, "foreign")

    assert applicability.bump_applies(session_id, cwd=str(root)) is True

    cmd = f"git -C {_posix(foreign)} commit --allow-empty -m x"
    result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(root), {})
    assert result is not None, "guard must actually fire for the absence assertions below to mean anything"

    hook_output = result["hookSpecificOutput"]
    # This is an advisory bump (`permissionDecision: "deny"`, a passable
    # speed bump), never a hard CONFINEMENT_DENY -- the registration-side
    # `band`/`fail_closed` attributes this asserts are pinned by
    # test_bump_foreign_repo_write.py's own AC19 test; this test asserts the
    # MARKER's own absence properties, on a genuinely fired result.
    assert hook_output["permissionDecision"] == "deny"
    reason = hook_output["permissionDecisionReason"].lower()
    assert "expir" not in reason
    assert "identity" not in reason
    assert "fail_closed" not in reason
    assert "confinement_deny" not in reason

    # The marker itself is a bare, forgeable `touch` of an ordinary file --
    # no unforgeability machinery, no creation guard.
    clear_line = next(
        line.strip()
        for line in result["hookSpecificOutput"]["permissionDecisionReason"].split("\n")
        if line.strip().startswith("touch ")
    )
    marker_path_str = clear_line[len("touch "):]
    assert Path(marker_path_str).name.startswith(marker.MARKER_PREFIX)


def test_ac6_outside_repo_bump_still_clearable_by_single_anchor_gitdir_touch(tmp_path, monkeypatch):
    """Per C3's scope correction: OUTSIDE_ANY_REPO keeps today's
    anchor-gitdir marker, UNCHANGED -- unlike C4's per-target relocation, a
    single `touch` against the session's own anchor gitdir still clears
    every outside-any-repo write for the rest of the session."""
    from coordinator_core.bash_guards import bump_outside_repo_write as outside_guard

    # Repoint the shared temp-root classifier so `tmp_path`'s own real
    # system-temp ancestry (every `tmp_path` lives under the REAL system
    # temp dir) does not get exempted by AC9's temp-scratch carve-out --
    # same isolation `test_bump_outside_repo_write.py`'s own
    # `_clean_bump_env` fixture applies, needed here because this file is
    # not that fixture's scope.
    fake_system_temp = tmp_path / "not-the-real-system-temp"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)

    session_id = "sess-ac6-outside-repo-unchanged"
    root = _end_to_end_setup(tmp_path, monkeypatch, session_id)
    outside_a = root.parent / "outside-scratch-a"
    outside_a.mkdir()
    outside_b = root.parent / "outside-scratch-b"
    outside_b.mkdir()

    assert applicability.bump_applies(session_id, cwd=str(root)) is True

    cmd_a = f"echo hi > {_posix(outside_a / 'a.txt')}"
    cmd_b = f"echo hi > {_posix(outside_b / 'b.txt')}"

    result_a = outside_guard.check_bump_outside_repo_write(cmd_a, session_id, str(root), {})
    assert result_a is not None, "guard must actually fire for this test to mean anything"

    anchor_gitdir = marker.resolve_gitdir(str(root))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker.marker_basename(session_id)).touch()

    result_a_after = outside_guard.check_bump_outside_repo_write(cmd_a, session_id, str(root), {})
    result_b_after = outside_guard.check_bump_outside_repo_write(cmd_b, session_id, str(root), {})

    assert result_a_after is None
    assert result_b_after is None, (
        "one anchor-gitdir touch must clear EVERY outside-any-repo target "
        "for the rest of the session -- unchanged from today, per C3's "
        "scope correction"
    )


def test_sweep_stale_markers_multiple_ended_sessions_in_one_pass(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename("sess-a")).touch()
    (gitdir / marker.marker_basename("sess-b")).touch()
    (gitdir / marker.marker_basename("sess-c")).touch()

    removed = marker.sweep_stale_markers(gitdir, ["sess-a", "sess-b"])

    assert removed == 2
    assert marker.marker_present(gitdir, "sess-a") is False
    assert marker.marker_present(gitdir, "sess-b") is False
    assert marker.marker_present(gitdir, "sess-c") is True
