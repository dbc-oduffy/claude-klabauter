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
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home_dir))


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
