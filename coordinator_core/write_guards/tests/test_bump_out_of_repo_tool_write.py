"""Tests for coordinator_core.write_guards.bump_out_of_repo_tool_write -- the
write-confinement speed bump's `Write`/`Edit`/`MultiEdit`/`NotebookEdit` leg.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C7. Covers AC2 (a Write/Edit/MultiEdit with a path outside the
session's repo bumps), AC13 (registered as a real `write_guards/engine.py`
entry via CLASS/MATCHERS/PRIORITY, not a call-site patch), AC19 (those
attributes are pinned rather than left to a default), plus parity tests
asserting this surface's verdict is driven ENTIRELY by the same C2/C3 shared
primitives the Bash-surface guards also consume.

PARITY GAP, NAMED PER THE DISPATCH BRIEF: `bump_foreign_repo_write.py` [C4]
was not yet present in the tree when this file was written (concurrent
chunk). The parity tests below therefore assert this module's verdict
against the shared `_write_bump_applicability`/`_write_bump_marker`
primitives directly, rather than against C4's own `check()` -- once C4
lands, a follow-up test asserting the two guards' `check()` outputs agree on
an identical payload shape (same target path, same session) would close the
remaining gap; that comparison is out of this chunk's reach today.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_marker as marker
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_outside_repo_write as bash_guard
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard


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


def _payload(tool_name, file_path, session_id, cwd, agent_id="", notebook=False):
    tool_input = {"notebook_path": file_path} if notebook else {"file_path": file_path}
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "cwd": cwd,
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# AC13/AC19 -- registration attributes are explicit, not left to default
# ---------------------------------------------------------------------------


def test_ac13_ac19_registration_attributes_are_explicit():
    assert guard.CLASS == "advisory"
    assert guard.CLASS != "hard-deny"
    assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    assert isinstance(guard.PRIORITY, int)
    assert guard.PRIORITY != 100  # not the engine's silent default


def test_ac13_check_is_callable_matching_engine_interface():
    assert callable(guard.check)


# ---------------------------------------------------------------------------
# AC2 -- a Write/Edit/MultiEdit/NotebookEdit outside the session's own repo
# bumps; the same tool inside its own repo never does.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit"])
def test_ac2_cross_repo_write_bumps(tmp_path, tool_name):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ac2-cross"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(foreign / "notes.txt")
    payload = _payload(tool_name, target, session_id, str(own))

    result = guard.check(payload)

    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "hookEventName" not in ctx  # sanity: ctx is the message string, not the envelope
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in result["hookSpecificOutput"]
    assert str(foreign) in ctx
    assert str(own) in ctx


def test_ac2_notebook_edit_uses_notebook_path(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ac2-notebook"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(foreign / "analysis.ipynb")
    payload = _payload("NotebookEdit", target, session_id, str(own), notebook=True)

    result = guard.check(payload)

    assert result is not None


def test_ac2_same_repo_write_never_bumps(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-ac2-same"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(own / "nested" / "file.txt")
    (own / "nested").mkdir()
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_ac2_non_matcher_tool_never_bumps(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ac2-nonmatcher"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    payload = _payload("Bash", str(foreign / "x.txt"), session_id, str(own))
    payload["tool_name"] = "Bash"

    assert guard.check(payload) is None


# ---------------------------------------------------------------------------
# Marker clears the bump -- one clear stands down every subsequent target.
# ---------------------------------------------------------------------------


def test_marker_present_in_own_gitdir_clears_the_bump(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-marker-clear"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    own_gitdir = marker.resolve_gitdir(str(own))
    assert own_gitdir is not None
    (own_gitdir / marker.marker_basename(session_id)).touch()

    payload = _payload("Write", str(foreign / "x.txt"), session_id, str(own))
    assert guard.check(payload) is None

    other_foreign = _init_repo(tmp_path, "another-foreign-repo")
    payload2 = _payload("Edit", str(other_foreign / "y.txt"), session_id, str(own))
    assert guard.check(payload2) is None


# ---------------------------------------------------------------------------
# C5 -- destination-class axis wired through, consuming C1's classifier
# (never re-derived locally). AC9 asserted first in every case.
# ---------------------------------------------------------------------------


def _write_publish_registry(reg_dir, mirror_path, owner: str = "claude-central-em") -> None:
    """A real `[publish.mirrors.<key>]` nested table -- the shape
    `target_is_publish_destination` (C1) parses, not the flat-string shape
    `_write_registry`'s own `[repos]` table uses."""
    reg_dir.mkdir(parents=True, exist_ok=True)
    escaped = str(mirror_path).replace("\\", "\\\\")
    lines = ["[publish.mirrors.testmirror]", f'path = "{escaped}"', f'owner = "{owner}"']
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ac3_publish_destination_write_renders_publish_class_copy_naming_owner(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    mirror = _init_repo(tmp_path, "mirror-target")
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(mirror), owner="claude-central-em")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    session_id = "sess-tool-publish"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload = _payload("Write", str(mirror / "published.txt"), session_id, str(own))
    result = guard.check(payload)

    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "claude-central-em" in ctx
    assert "is publish mirror" in ctx
    assert "repos you don't own" not in ctx


def test_ac1_ordinary_foreign_repo_write_keeps_foreign_class_copy(tmp_path, monkeypatch):
    """No publish-mirror registry entry for this target -- destination_class
    stays DESTINATION_FOREIGN, matching the Bash surface's own copy."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir"))

    session_id = "sess-tool-foreign"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload = _payload("Write", str(foreign / "sibling.txt"), session_id, str(own))
    result = guard.check(payload)

    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "is publish mirror" not in ctx


# ---------------------------------------------------------------------------
# § Where the bump does not fire -- session anchor in no git repo.
# ---------------------------------------------------------------------------


def test_outside_any_repo_anchor_unregistered_target_never_bumps(tmp_path, monkeypatch):
    # No git repo to write C0's session-start record against (sessions_dir()
    # itself requires a git root) -- the anchor here can only resolve via the
    # CLAUDE_PROJECT_DIR fallback, exactly like _write_bump_applicability's
    # own AC11 tests for this same anchor-outside-any-repo shape.
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir"))
    scaffold = tmp_path / "Documents" / "new-project"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-scaffold"

    other_scratch = tmp_path / "elsewhere"
    other_scratch.mkdir()
    payload = _payload("Write", str(other_scratch / "f.txt"), session_id, str(scaffold))

    assert guard.check(payload) is None


def test_outside_any_repo_anchor_registered_target_still_bumps(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "new-project"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-scaffold-registered-target"

    payload = _payload("Write", str(registered / "f.txt"), session_id, str(scaffold))

    result = guard.check(payload)
    assert result is not None


# ---------------------------------------------------------------------------
# Fail-open surfaces.
# ---------------------------------------------------------------------------


def test_no_session_id_no_anchor_fails_open(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    payload = _payload("Write", str(own / "x.txt"), "", str(own))
    assert guard.check(payload) is None


def test_missing_file_path_fails_open(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-missing-path"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    payload = {"tool_name": "Write", "tool_input": {}, "session_id": session_id, "cwd": str(own)}
    assert guard.check(payload) is None


def test_never_raises_on_malformed_payload():
    assert guard.check({}) is None
    assert guard.check({"tool_name": "Write"}) is None
    assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None


# ---------------------------------------------------------------------------
# Parity -- this surface's verdict is driven entirely by the shared C2/C3
# primitives, not by any independent logic of its own (see module docstring
# "PARITY GAP" for why this cannot yet compare directly against C4).
# ---------------------------------------------------------------------------


def test_parity_verdict_matches_manual_composition_of_shared_primitives(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-parity"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(foreign / "notes.txt")
    payload = _payload("Write", target, session_id, str(own))

    # Manual composition using ONLY the shared applicability/marker modules
    # (the same primitives the Bash-surface guards consume), independent of
    # this module's own private helpers.
    applies = applicability.bump_applies(session_id, cwd=str(own))
    anchor = applicability.resolve_launch_anchor(session_id, cwd=str(own))
    own_gitdir = marker.resolve_gitdir(anchor)
    target_gitdir = marker.resolve_gitdir(str(foreign))
    same_repo = (
        own_gitdir is not None
        and target_gitdir is not None
        and str(own_gitdir).rstrip("/") == str(target_gitdir).rstrip("/")
    )
    expected_bump = applies and not same_repo

    actual = guard.check(payload) is not None
    assert actual == expected_bump


def test_parity_same_repo_no_bump_matches_manual_composition(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-parity-same"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(own / "f.txt")
    payload = _payload("Write", target, session_id, str(own))

    applies = applicability.bump_applies(session_id, cwd=str(own))
    anchor = applicability.resolve_launch_anchor(session_id, cwd=str(own))
    own_gitdir = marker.resolve_gitdir(anchor)
    target_gitdir = marker.resolve_gitdir(str(own))
    same_repo = (
        own_gitdir is not None
        and target_gitdir is not None
        and str(own_gitdir).rstrip("/") == str(target_gitdir).rstrip("/")
    )
    expected_bump = applies and not same_repo

    actual = guard.check(payload) is not None
    assert actual == expected_bump
    assert expected_bump is False


# ---------------------------------------------------------------------------
# System-temp scratch is not a foreign repo -- the harness designates a
# per-session scratchpad under the system temp root for ALL temporary files,
# and a bare temp path is in NO repo, so the cross-repo advice this guard
# renders is a category error there. See the guard's own docstring,
# "SYSTEM-TEMP SCRATCH IS NOT A FOREIGN REPO".
#
# The temp root is PINNED in these tests rather than read live: pytest's own
# `tmp_path` already sits under the real `tempfile.gettempdir()` on macOS and
# Linux, so "outside the temp root" is not expressible against the live value.
# Pinning makes both sides of the conjunctive condition reachable on every
# platform, Windows included.
# ---------------------------------------------------------------------------


def _pin_temp_root(monkeypatch, root: Path) -> None:
    """Repoints BOTH recognized-temp-root primitives this module's
    `_target_is_bare_temp_scratch` now delegates to (the SHARED
    `_write_bump_applicability.target_is_bare_temp_scratch` -- this module
    no longer owns a `tempfile` reference of its own): `gettempdir()` AND
    the `_posix_tmp_literal()` seam. Both must move together for isolation
    to hold on every platform -- see that seam's own docstring: on a
    platform where pytest's `tmp_path` defaults to living directly under
    the real `/tmp` (Linux, no `TMPDIR` set), the unconditional `/tmp`
    candidate would otherwise still catch every fixture path regardless of
    a `gettempdir()`-only patch."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(root))
    # The shared classifier ALSO consults `TMPDIR`/`TEMP`/`TMP` directly
    # (not merely via `gettempdir()`) -- clear them so this process's own
    # real `TMPDIR` (which, on this host, is an ANCESTOR of pytest's own
    # `tmp_path`) cannot leak a second, unpinned recognized-temp-root
    # candidate into these isolation-sensitive tests.
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)


def test_temp_scratchpad_outside_any_repo_never_bumps(tmp_path, monkeypatch):
    """REGRESSION GUARD. The live defect: writing to the harness's own
    per-session scratchpad produced the cross-repo memo advice. Must FAIL
    against the pre-fix module."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-temp-scratchpad"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot"
    _pin_temp_root(monkeypatch, temp_root)
    scratchpad = temp_root / "claude-501" / "-project" / session_id / "scratchpad"
    scratchpad.mkdir(parents=True)

    payload = _payload("Write", str(scratchpad / "draft-memo.md"), session_id, str(own))

    assert guard.check(payload) is None


def test_temp_scratchpad_exemption_covers_every_matcher(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-temp-matchers"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot"
    _pin_temp_root(monkeypatch, temp_root)
    scratchpad = temp_root / "scratchpad"
    scratchpad.mkdir(parents=True)

    for tool_name in ("Write", "Edit", "MultiEdit"):
        payload = _payload(tool_name, str(scratchpad / "f.txt"), session_id, str(own))
        assert guard.check(payload) is None, tool_name

    nb = _payload(
        "NotebookEdit", str(scratchpad / "f.ipynb"), session_id, str(own), notebook=True
    )
    assert guard.check(nb) is None


def test_real_git_repo_under_temp_root_still_bumps(tmp_path, monkeypatch):
    """The exemption is CONJUNCTIVE -- under temp AND in no repo. A genuine
    checkout that happens to live under the temp root is a foreign repo and
    must still bump; a blanket temp exemption would open a hole the size of
    `git clone $TMPDIR/...`."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-temp-real-repo"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot"
    temp_root.mkdir()
    foreign = _init_repo(temp_root, "checkout-under-temp")
    _pin_temp_root(monkeypatch, temp_root)

    payload = _payload("Write", str(foreign / "notes.txt"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["additionalContext"]


def test_foreign_repo_outside_temp_root_still_bumps(tmp_path, monkeypatch):
    """Pre-existing behaviour, asserted against an explicitly pinned temp
    root so the exemption cannot be what is carrying this test."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-outside-temp-foreign"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    _pin_temp_root(monkeypatch, tmp_path / "tmproot")

    payload = _payload("Write", str(foreign / "notes.txt"), session_id, str(own))

    assert guard.check(payload) is not None


def test_own_repo_write_unaffected_by_temp_exemption(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-own-repo-temp-pinned"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    _pin_temp_root(monkeypatch, tmp_path / "tmproot")

    (own / "nested").mkdir()
    payload = _payload("Write", str(own / "nested" / "file.txt"), session_id, str(own))

    assert guard.check(payload) is None


def test_own_repo_write_into_not_yet_created_directory_does_not_bump(tmp_path, monkeypatch):
    """example-doctrine-repo finding #1. `_resolve_target_gitdir` must walk UP to the nearest
    EXISTING ancestor before resolving the target's git-dir -- a `Write` to
    `<own-repo>/newdir/file.txt` where `newdir/` does not exist yet must
    still resolve to the session's OWN repo and must NOT bump. Prior to the
    C5b fix, probing the not-yet-created `newdir/` directly always failed to
    resolve a git-dir, which was misread as "no repo" and bumped even though
    the write lands squarely inside the session's own repo. The sibling test
    above pre-creates its target directory and therefore cannot catch this --
    this test deliberately leaves `newdir/` uncreated."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-own-repo-uncreated-dir"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    _pin_temp_root(monkeypatch, tmp_path / "tmproot")

    assert not (own / "newdir").exists()
    payload = _payload("Write", str(own / "newdir" / "file.txt"), session_id, str(own))

    assert guard.check(payload) is None


def test_bare_relative_file_path_does_not_resolve_against_engine_process_cwd(
    tmp_path, monkeypatch
):
    """example-doctrine-repo finding #3. A bare relative `file_path` (no dirname) must not have
    its ancestor walk resolved against the coordinator ENGINE PROCESS's own
    cwd -- `_resolve_target_gitdir` must anchor a non-absolute path against
    the PAYLOAD's own `cwd` instead. Constructs the exact case the finding
    names: a DIFFERENT git repo ("decoy-repo") exists at the engine process's
    own cwd, distinct from the payload's own `cwd` (`own`, which has no
    "decoy" subdirectory of its own). If the ancestor walk fell through to
    `os.path.isdir()` against the ambient process cwd rather than the
    payload cwd, it would silently resolve to `decoy-repo`'s git-dir instead
    of walking up `own`'s own ancestry to `own`'s git-dir."""
    own = _init_repo(tmp_path, "own-repo")
    decoy_repo = _init_repo(tmp_path, "decoy-repo")
    (decoy_repo / "decoy").mkdir()
    session_id = "sess-bare-relative-cwd-independent"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    _pin_temp_root(monkeypatch, tmp_path / "tmproot")

    monkeypatch.chdir(str(decoy_repo))

    # Anchored against the payload's own cwd (`own`), "decoy" (no dirname)
    # walks up `own`'s own ancestry (since `own` has no "decoy"
    # subdirectory) and resolves to `own`'s git-dir -- NEVER to
    # `decoy_repo`'s git-dir, which is what a process-cwd-relative
    # resolution would wrongly produce.
    resolved = guard._resolve_target_gitdir("decoy", str(own))
    own_gitdir = marker.resolve_gitdir(str(own))
    decoy_gitdir = marker.resolve_gitdir(str(decoy_repo))
    assert resolved == own_gitdir
    assert resolved != decoy_gitdir

    # No payload cwd at all -- fail closed to None rather than falling back
    # to the ambient process cwd.
    assert guard._resolve_target_gitdir("decoy", None) is None


def test_non_temp_path_outside_any_repo_is_not_exempted(tmp_path, monkeypatch):
    """The exemption keys on the temp root, not on "no repo" alone -- the
    outside-any-repo branch keeps its own registry-gated verdict."""
    _pin_temp_root(monkeypatch, tmp_path / "tmproot")
    elsewhere = tmp_path / "Documents" / "loose"
    elsewhere.mkdir(parents=True)

    assert guard._target_is_bare_temp_scratch(str(elsewhere / "f.txt"), None) is False


def test_symlinked_temp_root_resolves_to_the_same_verdict(tmp_path, monkeypatch):
    """`/tmp` -> `/private/tmp` shape, expressed portably: a target reached
    through a symlink to the temp root must resolve to the same verdict as
    one spelled with the real path. Skips where symlinks are unavailable
    (unprivileged Windows) rather than hard-failing."""
    real_temp = tmp_path / "private-tmproot"
    real_temp.mkdir()
    link_temp = tmp_path / "tmplink"
    try:
        os.symlink(str(real_temp), str(link_temp), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")

    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-temp-symlink"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    _pin_temp_root(monkeypatch, real_temp)

    (real_temp / "scratchpad").mkdir()

    via_real = _payload(
        "Write", str(real_temp / "scratchpad" / "f.txt"), session_id, str(own)
    )
    via_link = _payload(
        "Write", str(link_temp / "scratchpad" / "f.txt"), session_id, str(own)
    )

    assert guard.check(via_real) is None
    assert guard.check(via_link) is None


def test_macos_private_tmp_symlink_shape(monkeypatch):
    """The literal shape from the live incident, on a box that actually has
    the macOS `/tmp` -> `/private/tmp` symlink. Pure helper call -- no
    filesystem writes outside pytest's own tmp area."""
    if not os.path.islink("/tmp"):
        pytest.skip("/tmp is not a symlink on this platform")

    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: os.path.realpath("/tmp"))

    assert guard._target_is_bare_temp_scratch("/tmp/claude-501/x/scratchpad/f.md", None) is True


def test_temp_exemption_never_raises_on_unresolvable_temp_root(tmp_path, monkeypatch):
    """Fail open, unconditionally: a `gettempdir()` that blows up must yield
    no bump, never a raise -- matching this module's outer contract."""

    own = _init_repo(tmp_path, "own-repo")
    foreign_dir = tmp_path / "loose"
    foreign_dir.mkdir()
    session_id = "sess-temp-raises"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    def _boom():
        raise RuntimeError("no temp root")

    monkeypatch.setattr(applicability.tempfile, "gettempdir", _boom)

    payload = _payload("Write", str(foreign_dir / "f.txt"), session_id, str(own))

    assert guard.check(payload) is None
    assert guard._target_is_bare_temp_scratch(str(foreign_dir / "f.txt"), None) is True


def test_live_temp_root_is_resolvable_and_non_trivial():
    """Sanity on the primitive the exemption keys off: `gettempdir()` must
    yield a real, non-root directory. A `/` here would exempt everything."""
    root = tempfile.gettempdir()
    assert root
    assert os.path.isdir(root)
    assert os.path.realpath(root).rstrip("/") != ""


# ---------------------------------------------------------------------------
# REGRESSION -- the confirmed live false positive: the harness-designated
# per-session scratchpad is NOT covered by `tempfile.gettempdir()` alone on
# macOS (`TMPDIR` resolves under `/var/folders/...`; the scratchpad lives
# under `/private/tmp`). Uses a REAL session-start record so applicability is
# genuinely True -- a test that passes only because applicability failed open
# proves nothing. MUST fail against the pre-fix module (the old
# `_target_is_bare_temp_scratch`, keyed on `tempfile.gettempdir()` alone, no
# `/tmp` realpath candidate).
# ---------------------------------------------------------------------------


def test_regression_real_harness_scratchpad_shape_never_bumps(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-real-scratchpad-c7"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    # Simulate the live macOS divergence: `gettempdir()` (TMPDIR) points
    # somewhere OTHER than the scratchpad's real root -- only the `/tmp`
    # realpath candidate (via the testability seam) catches it.
    fake_gettempdir_root = tmp_path / "var-folders-stand-in"
    fake_gettempdir_root.mkdir()
    real_tmp_stand_in = tmp_path / "private-tmp-stand-in"
    real_tmp_stand_in.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_gettempdir_root))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(real_tmp_stand_in))

    scratchpad = (
        real_tmp_stand_in
        / "claude-501"
        / "-Users-example-operator-X-claude-klabauter"
        / session_id
        / "scratchpad"
    )
    scratchpad.mkdir(parents=True)
    dest = str(scratchpad / "draft-memo.md")

    payload = _payload("Write", dest, session_id, str(own))

    assert guard.check(payload) is None


def test_regression_git_repo_under_temp_root_still_bumps_on_tool_surface(tmp_path, monkeypatch):
    """THE CONJUNCTION -- a real checkout under the recognized temp root is
    a foreign repo and must still bump on the tool surface too."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-repo-under-temp-c7"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot-c7"
    temp_root.mkdir()
    foreign = _init_repo(temp_root, "checkout-under-temp")
    _pin_temp_root(monkeypatch, temp_root)

    payload = _payload("Write", str(foreign / "notes.txt"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# example-doctrine-repo finding #2 (parity) -- the settings-home exemption. Before this fix,
# this module contained neither `_settings_home_dir_from_env` nor a
# `_settings_home` concept at all, so a write into the SAME destination
# bumped here while `bump_outside_repo_write.py` (Bash surface) already
# exempted it via its own AC9 always-allowed-roots list. Binds AC7: the two
# surfaces must agree on this destination.
# ---------------------------------------------------------------------------


def test_settings_home_write_never_bumps_on_tool_surface(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-settings-home"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    target = str(settings_home / "claude-klabauter" / "anchor.json")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_settings_home_exemption_covers_every_matcher(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-settings-home-matchers"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    for tool_name in ("Write", "Edit", "MultiEdit"):
        payload = _payload(tool_name, str(settings_home / "f.txt"), session_id, str(own))
        assert guard.check(payload) is None, tool_name

    nb = _payload(
        "NotebookEdit", str(settings_home / "f.ipynb"), session_id, str(own), notebook=True
    )
    assert guard.check(nb) is None


def test_real_git_repo_under_settings_home_still_bumps(tmp_path, monkeypatch):
    """The exemption is CONJUNCTIVE, same shape as the temp-scratch one --
    under settings home AND in no repo. A genuine checkout that happens to
    live under settings home is still a foreign repo and must still bump."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-settings-home-real-repo"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    foreign = _init_repo(settings_home, "checkout-under-settings-home")

    payload = _payload("Write", str(foreign / "notes.txt"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["additionalContext"]


def test_settings_home_exemption_parity_with_bash_surface(tmp_path, monkeypatch):
    """AC7 -- the settings-home exemption behaves IDENTICALLY on the Bash
    surface (`bump_outside_repo_write.py`) and this tool-write surface for
    the SAME destination. Pins the parity example-doctrine-repo finding #2 names: before this
    chunk, the Bash surface allowed and this surface bumped for an
    identical write target."""
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-settings-home-parity"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    target = settings_home / "claude-klabauter" / "anchor.json"

    tool_payload = _payload("Write", str(target), session_id, str(own))
    tool_verdict = guard.check(tool_payload) is not None

    cmd = f'echo hi > "{target}"'
    bash_payload = {"session_id": session_id, "agent_id": ""}
    bash_verdict = (
        bash_guard.check_bump_outside_repo_write(cmd, session_id, str(own), bash_payload)
        is not None
    )

    assert tool_verdict == bash_verdict
    assert tool_verdict is False


# ---------------------------------------------------------------------------
# LESSONS-OUTBOX IS NOT A MISWRITE -- `coordinator-lesson-promote` writes a
# universal lesson's durable home to `<doe_root>/state/lessons-outbox/*.yaml`
# BY DESIGN; a foreign-repo write there is not the "used the wrong repo"
# mistake this guard exists to flag. See the guard's own module docstring,
# "LESSONS-OUTBOX IS NOT A MISWRITE, EVEN THOUGH IT IS A FOREIGN REPO", and
# the dispatch brief's CRITICAL CONSTRAINT -- this must NOT widen to
# `cross-repo/inbox/`/`cross-repo/outbox/`, which stay forbidden and must
# keep bumping.
# ---------------------------------------------------------------------------


def test_foreign_repo_lessons_outbox_write_never_bumps(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-lessons-outbox"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    outbox = foreign / "state" / "lessons-outbox"
    outbox.mkdir(parents=True)
    target = str(outbox / "some-lesson.yaml")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_foreign_repo_lessons_outbox_exemption_covers_every_matcher(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-lessons-outbox-matchers"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    outbox = foreign / "state" / "lessons-outbox"
    outbox.mkdir(parents=True)

    for tool_name in ("Write", "Edit", "MultiEdit"):
        payload = _payload(tool_name, str(outbox / "f.yaml"), session_id, str(own))
        assert guard.check(payload) is None, tool_name

    nb = _payload(
        "NotebookEdit", str(outbox / "f.ipynb"), session_id, str(own), notebook=True
    )
    assert guard.check(nb) is None


def test_foreign_repo_lessons_outbox_subdirectory_also_exempted(tmp_path):
    """`priority_drain.py` adopts a `drained/` subdirectory under
    `state/lessons-outbox/` -- the exemption must cover it too, not merely
    the direct-child file shape."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-lessons-outbox-drained"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    drained = foreign / "state" / "lessons-outbox" / "drained"
    drained.mkdir(parents=True)
    payload = _payload("Write", str(drained / "old-lesson.yaml"), session_id, str(own))

    assert guard.check(payload) is None


def test_foreign_repo_cross_repo_inbox_write_still_bumps(tmp_path):
    """CRITICAL CONSTRAINT -- the lessons-outbox exemption must NOT widen to
    `cross-repo/inbox/`. Hand-writing a memo into a sibling's tree is
    forbidden by this repo's own CLAUDE.md, and this guard's
    `cross-repo-memo` message is correct for that path shape."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-cross-repo-inbox"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    inbox = foreign / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    payload = _payload("Write", str(inbox / "memo.md"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["additionalContext"]


def test_ordinary_foreign_repo_write_still_bumps_alongside_lessons_outbox_exemption(
    tmp_path,
):
    """Sanity: the new exemption is narrowly keyed on the literal
    `state/lessons-outbox` segment -- an ordinary foreign-repo write
    elsewhere in the same tree keeps bumping."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ordinary-foreign"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    (foreign / "state" / "lessons-outbox").mkdir(parents=True)
    payload = _payload("Write", str(foreign / "README.md"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# AGENT MEMORY STORE IS NOT A FOREIGN REPO -- Claude Code's own persistent
# per-project memory (`<home>/.claude/projects/<slug>/memory/**`) is not a
# sibling repo, not a doctrine surface, and not a cross-repo delivery, even
# though `~/.claude` is itself a real git checkout on this fleet -- see the
# shared `is_agent_memory_store_path` classifier's own docstring for the
# false positive this closes. Other `~/.claude` paths (`settings.json`,
# `CLAUDE.md`, `skills/`, etc.) are discovery/doctrine surfaces and MUST
# still bump -- asserted below alongside the exemption itself.
# ---------------------------------------------------------------------------


def _isolate_home(monkeypatch, home_dir: Path) -> None:
    """Cross-platform home isolation -- see `coordinator_core.conftest`'s own
    `_quarantine_real_home` docstring, which points at
    `coordinator_core.testing.home_sandbox.sandbox_home` for exactly this
    case: a bare `HOME`-only `delenv`/`setenv` leaves `USERPROFILE` (and, on
    the autouse fixture's own quarantine dir, `HOMEDRIVE`/`HOMEPATH`) either
    absent or pointed elsewhere on Windows, where `Path.home()` prefers
    `USERPROFILE` first and raises `RuntimeError` once every Windows
    home-resolution variable is gone -- observed swallowing into a silent
    allow inside `target_is_publish_destination`'s `Path.home()` call via
    the guard's own blanket `except Exception: return None`. `sandbox_home`
    sets `HOME` AND `USERPROFILE` together and clears `HOMEDRIVE`/`HOMEPATH`,
    so `Path.home()` resolves to `home_dir` instead of raising.
    `CLAUDE_HOME` stays cleared here (not part of `sandbox_home`'s contract)
    because these tests are specifically about the `CLAUDE_HOME`-absent,
    `HOME`-only resolution path this module falls back to."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    sandbox_home(monkeypatch, home_dir)


def test_agent_memory_store_write_never_bumps_on_tool_surface(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-that-is-a-repo")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-tool-1"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    memory_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    target = str(memory_dir / "note.md")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_agent_memory_store_index_write_never_bumps_on_tool_surface(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-that-is-a-repo-2")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-tool-2"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    memory_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    target = str(memory_dir / "MEMORY.md")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_agent_memory_store_exemption_covers_every_matcher(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-that-is-a-repo-3")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-tool-matchers"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    memory_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)

    for tool_name in ("Write", "Edit", "MultiEdit"):
        payload = _payload(tool_name, str(memory_dir / "f.txt"), session_id, str(own))
        assert guard.check(payload) is None, tool_name

    nb = _payload(
        "NotebookEdit", str(memory_dir / "f.ipynb"), session_id, str(own), notebook=True
    )
    assert guard.check(nb) is None


def test_settings_json_under_claude_home_still_bumps(tmp_path, monkeypatch):
    """The exemption is scoped to `memory/`, not `~/.claude` wholesale --
    `settings.json` is a discovery/doctrine surface and must keep bumping."""
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-settings")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-settings-json"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(claude_home / ".claude" / "settings.json")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert "hookSpecificOutput" in result


def test_project_dir_write_not_under_memory_still_bumps_on_tool_surface(tmp_path, monkeypatch):
    """Proves the exemption is scoped to `memory/`, not the whole
    per-project `<slug>/` directory."""
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-project-dir")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-project-dir"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    project_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project"
    project_dir.mkdir(parents=True)
    target = str(project_dir / "not-memory.md")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert "hookSpecificOutput" in result


def test_agent_memory_store_case_insensitive_directory_still_exempted(tmp_path, monkeypatch):
    """Case-insensitive-filesystem behaviour -- mirrors this family's own
    `_case_fold_path` treatment (`guard_memory_store_cap.py`'s "Memory" vs
    "memory" case-bypass finding). A case-varied `Memory/` directory name
    resolves under the same real guarded path on a case-insensitive-but-
    case-preserving filesystem (macOS APFS) and must still be exempted."""
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-case")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-case"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    memory_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "Memory"
    memory_dir.mkdir(parents=True)
    target = str(memory_dir / "Note.MD")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_target_is_lessons_outbox_write_helper_path_shape(tmp_path):
    doe_root = str(tmp_path / "example-doctrine-repo")
    assert guard._target_is_lessons_outbox_write(
        doe_root + "/state/lessons-outbox/some-lesson.yaml"
    )
    assert guard._target_is_lessons_outbox_write(
        doe_root + "/state/lessons-outbox/drained/old.yaml"
    )
    assert not guard._target_is_lessons_outbox_write(doe_root + "/cross-repo/inbox/memo.md")
    assert not guard._target_is_lessons_outbox_write(
        doe_root + "/state/lessons-outbox-unrelated/f.txt"
    )
    assert not guard._target_is_lessons_outbox_write("")


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-07-guard-posix-path-rerooting.md) -- the tool-write
# surface's `_resolve_target_gitdir` gets the same MSYS drive-mount
# translation fix C1/C2 give the two Bash-surface bump guards. AC1/AC2/AC3
# run for real against this actual Windows host (no simulation needed);
# AC4 simulates a POSIX host via the same `os.path` swap pattern
# `test_windows_platform_simulation.py` uses. AC6 pins the fail-open branch
# never reaching the ancestor walk. AC7 proves reachability through the
# write-guard dispatcher, not merely a direct `check()` call (DR-280).
# ---------------------------------------------------------------------------


def _msys_form(p: Path) -> str:
    """`X:\\Users\\...` -> `/x/Users/...` -- the MSYS/MinGW drive-mount
    spelling Git-for-Windows' bash hands tools as `$PWD`/argument expansion,
    constructed from a REAL path so the resulting candidate resolves to a
    real, existing (or creatable) location on this host."""
    drive = p.drive  # e.g. "X:"
    rest = str(p)[len(drive):].replace("\\", "/")
    return f"/{drive[0].lower()}{rest}"


def test_ac1_msys_absolute_target_resolves_inside_own_repo_no_bump(tmp_path):
    """AC1 regression, on THIS actual Windows host: a tool-surface write to
    a `/x/claude-klabauter/<path>`-shaped MSYS path resolves inside the
    session's own repo and must NOT bump. Confirmed red before the fix by
    temporarily reverting `_resolve_target_gitdir` to the pre-C4 join shape
    and re-running this exact test: pre-fix, `own_gitdir` resolved correctly
    but `target_gitdir` re-rooted onto the process's own drive and resolved
    to `None`, producing `result is not None` (a wrongful bump)."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-msys-ac1"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(own) + "/scratch/t.txt"
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_ac2_msys_path_translates_to_its_drive_form(tmp_path):
    """AC2: a `/c/Users/...`-shaped path translates to its drive form --
    asserted via `_resolve_target_gitdir` resolving to the SAME git-dir as
    the native-spelled equivalent."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")

    msys_target = _msys_form(own) + "/f.txt"
    resolved = guard._resolve_target_gitdir(msys_target, None)
    expected = marker.resolve_gitdir(str(own))

    assert resolved == expected


def test_ac3_msys_foreign_target_still_bumps(tmp_path):
    """AC3: a genuinely foreign target, spelled in MSYS form, still bumps --
    the fix must not turn a real bump into a permit."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-msys-ac3"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(foreign) + "/notes.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    # Verdict only -- see AC7 dispatcher test's own comment for why the
    # rendered message's `target_repo` display string (a separate,
    # untranslated call site, out of this chunk's scope) is not asserted on.
    assert result is not None


def test_ac4_simulated_posix_host_matches_pre_fix_join_semantics(monkeypatch):
    """AC4: on a simulated POSIX host, behaviour is byte-identical to today.
    Swaps `os.path` to `posixpath` (native semantics, per the pattern in
    `test_windows_platform_simulation.py`) AND the shared `_host_is_windows`
    seam to `False` -- per the C1 executor's own warning, forgetting the
    `os.path` swap on this actual Windows box would silently run under
    native backslash semantics and prove nothing. `nearest_existing_ancestor`
    and `resolve_gitdir` are stubbed to identity so this test isolates the
    translation-and-join step this chunk changes, independent of filesystem
    state."""
    import posixpath

    from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes

    monkeypatch.setattr(os, "path", posixpath)
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)
    monkeypatch.setattr(guard, "nearest_existing_ancestor", lambda p: p)
    monkeypatch.setattr(guard, "resolve_gitdir", lambda p: p)

    # Absolute target -- no payload cwd needed, unchanged from before the fix.
    assert guard._resolve_target_gitdir("/repo/sub/file.txt", None) == "/repo/sub"

    # Relative target -- joined against payload cwd, identical to the old
    # `os.path.join(payload_cwd, target_dir)` shape.
    assert guard._resolve_target_gitdir("relative/file.txt", "/base/cwd") == posixpath.join(
        "/base/cwd", "relative"
    )

    # Relative target, no payload cwd -- fail open to None, unchanged.
    assert guard._resolve_target_gitdir("relative/file.txt", None) is None


def test_ac6_untranslatable_target_never_reaches_ancestor_walk(monkeypatch):
    """AC6: an untranslatable candidate (an MSYS-shaped leading-slash form
    `translate_msys_path` deliberately does not decode, e.g. `/usr/...`)
    takes the SAME fail-open `None` branch as any other unresolvable anchor
    -- never treated as a bump, and never reaching
    `nearest_existing_ancestor`/`resolve_gitdir`."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    called = {"hit": False}

    def _fake_ancestor(path):
        called["hit"] = True
        return path

    monkeypatch.setattr(guard, "nearest_existing_ancestor", _fake_ancestor)

    result = guard._resolve_target_gitdir(
        "/usr/local/bin/file", "C:\\Users\\me"  # abs-path-ok: untranslatable-shape test fixture, not a machine-specific citation
    )

    assert result is None
    assert called["hit"] is False


def test_ac7_msys_foreign_target_still_bumps_through_dispatcher(tmp_path):
    """AC7 (DR-280) -- reachability through the write-guard DISPATCHER
    (`write_guards.engine.evaluate`), not only a direct `check()` call. A
    probe that calls `check()` directly is a capability test, not a
    production repro."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    from coordinator_core.write_guards import engine

    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-msys-ac7-dispatcher"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(foreign) + "/notes.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = engine.evaluate(payload)

    # Verdict only -- the rendered message's `target_repo` display string is
    # composed from the RAW (untranslated) `file_path` at a separate call
    # site in `check()` and is out of this chunk's scope (only
    # `_resolve_target_gitdir`, which drives the bump/no-bump verdict, is
    # touched here); asserting on message content would couple this test to
    # that unrelated display-string behaviour.
    assert result is not None


def test_ac7_msys_own_repo_target_never_bumps_through_dispatcher(tmp_path):
    """AC7 companion -- the AC1 own-repo no-bump shape, also proven through
    the dispatcher rather than only a direct `check()` call."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    from coordinator_core.write_guards import engine

    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-msys-ac7-own"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(own) + "/scratch/t.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = engine.evaluate(payload)

    assert result is None


# ---------------------------------------------------------------------------
# C4b (docs/plans/2026-08-07-guard-posix-path-rerooting.md) -- the SAME
# module resolves the target twice more, and one of them is a verdict.
# `_verdict_bumps`' `own_gitdir is None` branch and `check()`'s own
# `target_repo`/`destination_class` resolution both recomputed
# `os.path.dirname(file_path) or file_path` RAW before this chunk -- these
# tests pin both fixes.
# ---------------------------------------------------------------------------


def test_c4b_verdict_bumps_untranslated_msys_path_was_red_before_fix(tmp_path):
    """RED-before-fix regression: `_verdict_bumps` on the `own_gitdir is
    None` branch, given the RAW (untranslated) MSYS-form `file_path`, would
    never match `target_is_registered_repo` since no registered repo path
    is ever spelled in MSYS form -- so this returned `False` (no bump)
    pre-fix even for a target that IS registered. Post-fix, this call goes
    through the ALREADY-TRANSLATED `target_dir` `check()` resolves once, so
    a registered target bumps regardless of the incoming path spelling."""
    registered = _init_repo(tmp_path, "registered-repo")

    raw_msys_target_dir = _msys_form(registered) if os.name == "nt" else str(registered)

    # Manual reproduction of the pre-fix call shape: pass the RAW dirname
    # (as `_verdict_bumps` used to compute it internally) as `target_dir`.
    # On Windows this MSYS spelling never matches the registry (raw path
    # comparison), reproducing the pre-fix false-negative directly.
    if os.name == "nt":
        assert guard.target_is_registered_repo(raw_msys_target_dir) is False


def test_c4b_verdict_bumps_uses_translated_target_dir_for_registered_target(tmp_path, monkeypatch):
    """Post-fix: `_verdict_bumps` bumps for a REGISTERED target on the
    `own_gitdir is None` branch when given the properly TRANSLATED
    `target_dir` -- the shape `check()` now threads in."""
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo-c4b")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    result = guard._verdict_bumps(
        "sess", None, "anchor", None, Path("some-gitdir"), str(registered)
    )
    assert result is True


def test_c4b_verdict_bumps_untranslatable_target_dir_never_bumps():
    """`target_dir is None` (untranslatable) takes the same fail-open
    `return False` branch as `target_gitdir is None` -- never a bump on a
    path this guard could not resolve."""
    assert (
        guard._verdict_bumps("sess", None, "anchor", None, Path("some-gitdir"), None)
        is False
    )


def test_c4b_msys_registered_target_bumps_through_check_end_to_end(tmp_path, monkeypatch):
    """End-to-end regression for the `_verdict_bumps` fix, through
    `check()`: a session anchor with NO git repo, writing an MSYS-form path
    into a REGISTERED repo, must bump. Confirmed RED before the C4b fix by
    temporarily reverting `_verdict_bumps` to recompute
    `os.path.dirname(file_path) or file_path` raw."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo-c4b-e2e")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "new-project-c4b"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-c4b-msys-registered"

    target = _msys_form(registered) + "/f.txt"
    payload = _payload("Write", target, session_id, str(scaffold))

    result = guard.check(payload)
    assert result is not None


def test_c4b_check_target_repo_resolved_from_translated_msys_path(tmp_path):
    """`check()`'s own `target_repo`/`destination_class` resolution must use
    the TRANSLATED `target_dir`, not the raw `os.path.dirname(file_path)` --
    this drives `_resolve_git_root`/`target_is_publish_destination`
    (behavioural, not display-only). Asserts the rendered advisory names a
    path that actually exists on disk (the translated, native form)."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo-c4b-target-repo")
    foreign = _init_repo(tmp_path, "foreign-repo-c4b-target-repo")
    session_id = "sess-c4b-target-repo"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(foreign) + "/notes.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # The message names the foreign repo's NATIVE path (the translated
    # form), which exists on disk -- never the raw MSYS spelling.
    assert str(foreign) in ctx
    assert os.path.isdir(str(foreign))


def test_c4b_simulated_posix_host_byte_identical_for_verdict_and_target_repo(monkeypatch):
    """AC4-style regression for C4b's two sites: on a simulated POSIX host,
    with a native drive-absolute-equivalent (already-native) input,
    `_verdict_bumps` and `check()`'s `target_repo` resolution behave
    byte-identically to before this chunk -- `translate_msys_path` is
    identity on POSIX."""
    import posixpath

    from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes

    monkeypatch.setattr(os, "path", posixpath)
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)

    resolved = guard._resolve_target_dir("/repo/sub/file.txt", None)
    assert resolved == "/repo/sub"


def test_c4b_ac7_msys_registered_target_bumps_through_dispatcher(tmp_path, monkeypatch):
    """AC7 companion for the `_verdict_bumps` fix -- proven through
    `write_guards.engine.evaluate`, not only a direct `check()` call
    (DR-280)."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    from coordinator_core.write_guards import engine

    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo-c4b-dispatcher")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "new-project-c4b-dispatcher"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-c4b-msys-registered-dispatcher"

    target = _msys_form(registered) + "/f.txt"
    payload = _payload("Write", target, session_id, str(scaffold))

    result = engine.evaluate(payload)
    assert result is not None


def test_extended_length_prefix_does_not_desync_same_gitdir(monkeypatch, tmp_path):
    """`state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`
    -- tool-write surface (C7). Same injected-asymmetry shape as the
    bash-surface test in `test_bump_foreign_repo_write.py`: `os.path.
    realpath` returns the Windows extended-length form for one gitdir and
    the bare form for the other. Before this fix, `_normalize_for_compare`'s
    `casefold_path` call preserved the prefix and `_same_gitdir` wrongly
    reported "different gitdir" for the identical directory."""
    real_dir = tmp_path / "gitdir"
    real_dir.mkdir()
    bare_form = str(real_dir)
    prefixed_form = "\\\\?\\" + bare_form

    real_realpath = guard.os.path.realpath

    def fake_realpath(path, *a, **kw):
        if path == "gitdir-a":
            return prefixed_form
        if path == "gitdir-b":
            return bare_form
        return real_realpath(path, *a, **kw)

    monkeypatch.setattr(guard.os.path, "realpath", fake_realpath)

    assert guard._same_gitdir(Path("gitdir-a"), Path("gitdir-b")) is True


# ---------------------------------------------------------------------------
# C4c (docs/plans/2026-08-07-guard-posix-path-rerooting.md) -- the exemption
# predicates (`_target_is_bare_temp_scratch`, `_target_is_under_settings_
# home`, `_target_is_lessons_outbox_write`, `is_agent_memory_store_path`)
# never got the translated `file_path` C4/C4b already thread to the VERDICT
# resolution -- see review finding [P3] on commit fc1419657. THE HEADLINE
# REGRESSION: an MSYS-spelled write to the harness scratchpad on Windows
# matched no recognized native temp root (raw POSIX-spelled string, compared
# against native temp roots), so the temp-scratch exemption never fired and
# the write fell through to `_verdict_bumps`, which bumped it -- fails
# CLOSED, the one branch in this module the docstring says must never do
# that.
# ---------------------------------------------------------------------------


def test_c4c_headline_regression_msys_scratchpad_write_never_bumps(tmp_path, monkeypatch):
    """THE ORIGINATING INCIDENT. Confirmed RED against the pre-fix module
    (predicates fed the raw, untranslated `file_path`): the MSYS-spelled
    scratchpad candidate matched no recognized native temp root, the
    temp-scratch exemption did not fire, and the write fell through to
    `_verdict_bumps` -- `target_gitdir` (correctly translated) was `None`
    while `own_gitdir` was not, so `not _same_gitdir(...)` was `True` and the
    write bumped. Post-fix, the SAME translated path now feeds the
    exemption predicate too, so it fires before `_verdict_bumps` is ever
    reached."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo-c4c-headline")
    session_id = "sess-c4c-headline-scratchpad"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot-c4c"
    _pin_temp_root(monkeypatch, temp_root)
    scratchpad = temp_root / "claude-501" / "-project" / session_id / "scratchpad"
    scratchpad.mkdir(parents=True)

    msys_target = _msys_form(scratchpad) + "/draft-memo.md"
    payload = _payload("Write", msys_target, session_id, str(own))

    assert guard.check(payload) is None


def test_c4c_msys_settings_home_write_never_bumps(tmp_path, monkeypatch):
    """Same defect, the settings-home exemption -- an MSYS-spelled path
    under settings home must still be exempt, not just the native form."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo-c4c-settings")
    session_id = "sess-c4c-msys-settings-home"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    settings_home = tmp_path / "settings-home-c4c"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    target_dir = settings_home / "claude-klabauter"
    target_dir.mkdir()
    msys_target = _msys_form(target_dir) + "/anchor.json"
    payload = _payload("Write", msys_target, session_id, str(own))

    assert guard.check(payload) is None


def test_c4c_untranslatable_file_path_no_bump_no_crash_outside_any_repo_anchor(
    tmp_path, monkeypatch
):
    """Untranslatable candidates (`/tmp/...`, `//server/share/...` -- shapes
    `translate_msys_path` deliberately does not decode) must never bump and
    must never raise. Exercised on the anchor-outside-any-repo branch, whose
    own `target_dir is None` fail-open route is already established/in-scope
    for this guard (C4b) -- this test proves the exemption predicates
    upstream of it degrade the same way (fall through to "not exempt")
    rather than raising on the untranslatable input."""
    if os.name != "nt":
        pytest.skip("untranslatable-shape probe is Windows-specific (identity on POSIX)")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir"))
    scaffold = tmp_path / "Documents" / "new-project-c4c"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-c4c-untranslatable"

    for untranslatable in ("/tmp/x/foo.txt", "//server/share/foo.txt"):
        payload = _payload("Write", untranslatable, session_id, str(scaffold))
        assert guard.check(payload) is None, untranslatable


def test_c4c_untranslatable_translated_path_predicates_fall_through_safely():
    """Direct predicate-level probe: an empty/untranslatable translated
    `file_path` (what `_resolve_translated_file_path` yields as `None`,
    coerced to `""` at each call site) never crashes.
    `_target_is_bare_temp_scratch` delegates to the SHARED classifier
    (`_write_bump_applicability.target_is_bare_temp_scratch`), whose own
    documented contract fails OPEN (`True`, i.e. "treat as scratch, do not
    bump") on an unresolvable candidate -- so `""` in gives `True`, not a
    widening of THIS chunk's own logic, and consistent with `check()`'s
    overall "never bump on a path this guard could not resolve" contract.
    The other two predicates gate on `not file_path` explicitly and return
    `False` ("not exempt") -- also safe, since `check()`'s `_verdict_bumps`
    independently no-bumps whenever the translated `target_dir` is `None`
    on the `own_gitdir is None` branch (out of this chunk's scope; C4b)."""
    assert guard._target_is_bare_temp_scratch("", None) is True
    assert guard._target_is_under_settings_home("", None) is False
    assert guard._target_is_lessons_outbox_write("") is False


def test_c4c_msys_foreign_target_still_bumps_not_converted_to_permit(tmp_path):
    """A genuinely foreign target, MSYS-spelled, must still bump -- proves
    this chunk's predicate-threading fix did not accidentally widen any
    exemption into covering an ordinary foreign-repo write."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo-c4c-foreign")
    foreign = _init_repo(tmp_path, "foreign-repo-c4c-foreign")
    session_id = "sess-c4c-msys-foreign"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(foreign) + "/notes.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None


def test_c4c_posix_host_and_native_drive_absolute_byte_identical(monkeypatch):
    """POSIX-host regression -- `translate_msys_path` is identity on POSIX,
    so `_resolve_translated_file_path` must resolve a POSIX-absolute
    `file_path` unchanged. Simulates POSIX via BOTH the `_host_is_windows`
    seam AND `os.path` -> `posixpath` (per this chunk's own instruction --
    the second swap is required or the assertions silently run under native
    backslash semantics and prove nothing)."""
    import posixpath

    from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes

    monkeypatch.setattr(os, "path", posixpath)
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)

    assert guard._resolve_translated_file_path("/repo/sub/file.txt", None) == (
        "/repo/sub/file.txt"
    )
    assert guard._resolve_translated_file_path("relative/file.txt", "/base/cwd") == (
        posixpath.join("/base/cwd", "relative/file.txt")
    )
    assert guard._resolve_translated_file_path("relative/file.txt", None) is None


def test_c4c_native_drive_absolute_byte_identical(tmp_path):
    """Native-drive-absolute input regression -- `translate_msys_path` is a
    no-op for an already-native path on every host; the translated file path
    must equal the input unchanged."""
    target = str(tmp_path / "already-native" / "f.txt")
    assert guard._resolve_translated_file_path(target, None) == target


def test_c4c_msys_scratchpad_write_never_bumps_through_dispatcher(tmp_path, monkeypatch):
    """DR-280 -- reachability through the write-guard DISPATCHER
    (`write_guards.engine.evaluate`), not only a direct `check()` call."""
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    from coordinator_core.write_guards import engine

    own = _init_repo(tmp_path, "own-repo-c4c-dispatcher")
    session_id = "sess-c4c-dispatcher-scratchpad"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    temp_root = tmp_path / "tmproot-c4c-dispatcher"
    _pin_temp_root(monkeypatch, temp_root)
    scratchpad = temp_root / "scratchpad"
    scratchpad.mkdir(parents=True)

    msys_target = _msys_form(scratchpad) + "/draft-memo.md"
    payload = _payload("Write", msys_target, session_id, str(own))

    result = engine.evaluate(payload)
    assert result is None
