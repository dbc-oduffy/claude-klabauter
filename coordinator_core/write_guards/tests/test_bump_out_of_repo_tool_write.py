"""Tests for coordinator_core.write_guards.bump_out_of_repo_tool_write -- the
write-confinement speed bump's `Write`/`Edit`/`MultiEdit`/`NotebookEdit` leg.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 [DoE-claude
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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


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


def test_ac13_ac19_registration_attributes_are_explicit():
    assert guard.CLASS == "hard-deny"
    assert guard.CLASS != "advisory"
    assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    assert isinstance(guard.PRIORITY, int)
    assert guard.PRIORITY != 100


def test_ac13_check_is_callable_matching_engine_interface():
    assert callable(guard.check)


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
    ctx = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "hookEventName" not in ctx
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(foreign) in ctx
    assert str(own) in ctx


def test_unwritable_marker_gitdir_fails_open(tmp_path, monkeypatch):
    # guard (STAFF-ENG F0/AC5, "never an unclearable deny") -- a marker
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-unwritable-marker"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    monkeypatch.setattr(guard, "marker_gitdir_is_writable", lambda _gitdir: False)

    target = str(foreign / "notes.txt")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)

    assert result is None


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


def test_marker_present_in_own_gitdir_clears_the_bump(tmp_path):
    """MIGRATION GUARANTEE, not the advertised shape (2026-08-10 per-target
    narrowing). An anchor-sited marker is the PRE-narrowing location; it
    still clears, for every target, so a marker a live session already holds
    does not stop working mid-session. The guard no longer ADVERTISES this
    location -- see the `_marker_locations` tests below for what it prints
    now. Keep this test: it is the only thing pinning the grandfathered
    read, and dropping it would let a "tidy-up" silently deny writes to a
    session that already cleared the bump."""
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


def _assert_denies(payload) -> str:
    result = guard.check(payload)
    assert result is not None, "expected a deny -- a vacuous pass otherwise"
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def test_target_sited_marker_clears_only_the_target_it_was_made_for(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    target_a = _init_repo(tmp_path, "foreign-a")
    target_b = _init_repo(tmp_path, "foreign-b")
    session_id = "sess-per-target"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload_a = _payload("Write", str(target_a / "x.txt"), session_id, str(own))
    payload_b = _payload("Write", str(target_b / "y.txt"), session_id, str(own))
    _assert_denies(payload_a)
    _assert_denies(payload_b)

    gitdir_a = marker.resolve_gitdir(str(target_a))
    assert gitdir_a is not None
    (gitdir_a / marker.marker_basename(session_id)).touch()

    assert guard.check(payload_a) is None
    _assert_denies(payload_b)


def test_advertised_clear_line_names_the_target_gitdir_not_the_anchor(tmp_path):
    """Marker-siting invariant, proved BEHAVIOURALLY (2026-08-13, C4d --
    no renderer in `_write_bump_message` prints a clear line on any
    channel any more, so this can no longer be read off the deny message
    text). The ADVERTISED (narrowed, per-target) location is the target's
    own gitdir -- same idiom as
    `test_target_sited_marker_clears_only_the_target_it_was_made_for`:
    touching it clears this exact write.

    The anchor's own gitdir is a separate, GRANDFATHERED clear location
    (pre-narrowing behaviour, kept for compatibility) -- that fact is
    pinned on its own by `test_marker_locations_split_advertised_from_
    grandfathered` against `guard._marker_locations` directly, so this
    test does not re-assert "the anchor's marker does NOT clear" (it
    does, deliberately, via the legacy leg `check()` also consults)."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-clear-line-target"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload = _payload("Edit", str(foreign / "x.txt"), session_id, str(own))
    _assert_denies(payload)

    target_gitdir = marker.resolve_gitdir(str(foreign))
    own_gitdir = marker.resolve_gitdir(str(own))
    assert target_gitdir is not None and own_gitdir is not None

    (target_gitdir / marker.marker_basename(session_id)).touch()
    assert guard.check(payload) is None


def test_marker_falls_back_to_anchor_when_target_is_in_no_repo(tmp_path, monkeypatch):
    """The one shape with no target gitdir to narrow into -- structurally the
    same no-op as the Bash leg's OUTSIDE_ANY_REPO class.

    Repoints the temp-root classifier off `tmp_path`'s own ancestry (pytest's
    `tmp_path` lives under the REAL system temp root) so this destination is
    not caught by the unrelated `target_is_bare_temp_scratch` AC9 exemption
    instead -- same repoint `test_ac4_non_repo_destination_still_bumps`
    applies, for the identical reason."""
    fake_system_temp = tmp_path / "not-the-real-system-temp-fallback"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)

    own = _init_repo(tmp_path, "own-repo")
    _isolate_home(monkeypatch, tmp_path / "claude-home-anchor-fallback")
    loose = tmp_path / "no-repo-here"
    loose.mkdir()
    session_id = "sess-anchor-fallback"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload = _payload("Write", str(loose / "x.txt"), session_id, str(own))
    _assert_denies(payload)

    own_gitdir = marker.resolve_gitdir(str(own))
    assert own_gitdir is not None

    (own_gitdir / marker.marker_basename(session_id)).touch()
    assert guard.check(payload) is None


def test_marker_locations_split_advertised_from_grandfathered(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    own_gitdir = marker.resolve_gitdir(str(own))
    foreign_gitdir = marker.resolve_gitdir(str(foreign))
    assert own_gitdir is not None and foreign_gitdir is not None

    assert guard._marker_locations(own_gitdir, foreign_gitdir) == (
        foreign_gitdir,
        own_gitdir,
    )
    assert guard._marker_locations(own_gitdir, None) == (own_gitdir, None)
    assert guard._marker_locations(None, foreign_gitdir) == (foreign_gitdir, None)
    assert guard._marker_locations(own_gitdir, own_gitdir) == (own_gitdir, None)


def test_narrowing_adds_no_expiry_identity_gating_or_fail_closed(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ac6-absence"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    assert applicability.bump_applies(session_id, cwd=str(own)) is True

    payload = _payload("Write", str(foreign / "x.txt"), session_id, str(own))
    _assert_denies(payload)

    gitdir = marker.resolve_gitdir(str(foreign))
    assert gitdir is not None
    marker_file = gitdir / marker.marker_basename(session_id)
    # no signature -- still clears, exactly as XREPO_MARKER_IS_ORDINARY_FILE
    marker_file.touch()
    assert marker_file.stat().st_size == 0
    os.utime(marker_file, (0, 0))
    assert guard.check(payload) is None

    # `write_guards/engine.py`, which has no CONFINEMENT_DENY band).
    assert guard.CLASS == "hard-deny"
    assert getattr(guard, "fail_closed", False) is False
    assert getattr(guard, "BAND", None) is None


def _write_publish_registry(reg_dir, mirror_path, owner: str = "claude-central-em") -> None:
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
    ctx = result["hookSpecificOutput"]["permissionDecisionReason"]
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
    ctx = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "is publish mirror" not in ctx


def test_outside_any_repo_anchor_unregistered_target_never_bumps(tmp_path, monkeypatch):
    # CLAUDE_PROJECT_DIR fallback, exactly like _write_bump_applicability's
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


# not exist and this fell straight into the 2026-08-10 "a REGISTERED target
# bumps` above correctly denies for a GENUINELY foreign registered target.


def test_cloud_top_level_session_own_registered_repo_allows(tmp_path, monkeypatch):
    """The exact B2 shape: `HOME`/session-anchor machinery resolves to a
    non-repo workspace parent (simulated here via `CLAUDE_PROJECT_DIR`
    pointing one level above the clone, matching the no-session-start-record
    fallback a launcher-less host hits), while this PreToolUse payload's own
    `cwd` -- and the write target -- sit inside the session's actual,
    registered repo. Must ALLOW."""
    reg_dir = tmp_path / "registry"
    own = _init_repo(tmp_path, "coordinator-claude")
    _write_registry(reg_dir, coordinator_claude=str(own))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    workspace_parent = tmp_path
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(workspace_parent))
    session_id = "sess-cloud-top-level-own-repo"

    payload = _payload("Write", str(own / "f.txt"), session_id, str(own))

    assert guard.check(payload) is None


def test_cloud_repro_does_not_widen_to_a_different_repo_reached_only_via_cwd(
    tmp_path, monkeypatch
):
    """AC12 companion: the fix must not become a general 'trust live cwd'
    bypass. A session whose anchor is genuinely rootless, whose declared
    project dir points at NEITHER the anchor NOR the write target, and whose
    `cwd` sits in a THIRD, unrelated registered repo must still bump when
    the write targets a DIFFERENT registered repo than that `cwd` -- i.e.
    `own_repo_write_gitdir` matching `cwd`'s own repo never excuses a write
    into some OTHER repo (mirrors `test_outside_any_repo_anchor_registered_
    target_still_bumps`'s genuinely-foreign shape, with `cwd` now populated
    by a real repo instead of a bare scaffold directory)."""
    reg_dir = tmp_path / "registry"
    cwd_repo = _init_repo(tmp_path, "operator-cwd-repo")
    target_repo = _init_repo(tmp_path, "registered-target-repo")
    _write_registry(reg_dir, cwd_repo=str(cwd_repo), target_repo=str(target_repo))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "rootless-scaffold"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-cwd-does-not-widen"

    payload = _payload("Write", str(target_repo / "f.txt"), session_id, str(cwd_repo))

    result = guard.check(payload)
    assert result is not None


def test_genuine_subagent_in_cloud_layout_still_bumped_for_a_foreign_target(
    tmp_path, monkeypatch
):
    """coordinator-claude#42 B2, Do step 4: the fix must not loosen
    confinement for a POSITIVELY resolved subagent writing into a repo other
    than the one its own `cwd` sits in -- same layout as the cloud repro
    (rootless anchor, `CLAUDE_PROJECT_DIR` set), but `agent_id` is present
    and the write target is a genuinely different, registered repo. Must
    still bump, AND must still render the subagent-sandbox message (never
    the EM/unknown one)."""
    reg_dir = tmp_path / "registry"
    subagent_cwd = _init_repo(tmp_path, "subagent-own-repo")
    foreign_target = _init_repo(tmp_path, "registered-foreign-for-subagent")
    _write_registry(reg_dir, foreign_target=str(foreign_target))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    workspace_parent = tmp_path
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(workspace_parent))
    session_id = "sess-subagent-cloud-layout"

    payload = _payload(
        "Write",
        str(foreign_target / "f.txt"),
        session_id,
        str(subagent_cwd),
        # actually resolves SUBAGENT rather than degrading to UNKNOWN on an
        agent_id="abcdef123456789012",
    )

    result = guard.check(payload)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sandbox" in reason


def test_unknown_agent_identity_never_gets_sandbox_routing_or_subagent_copy(
    tmp_path, monkeypatch
):
    """coordinator-claude#42 B2, Do step 3: when `resolve_agent_class`
    degrades to `AGENT_CLASS_UNKNOWN` (a resolution failure, never a
    positive claim either way -- see `_write_bump_message.resolve_agent_
    class`'s own docstring), this guard must render the non-subagent path's
    copy and must not compute a sandbox path for a class that was never
    positively identified as confined to one."""
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-unknown-identity"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    try:
        payload = _payload("Write", str(foreign / "f.txt"), session_id, str(own))

        monkeypatch.setattr(guard, "resolve_agent_class", lambda *_a, **_k: "unknown")

        result = guard.check(payload)

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "sandbox" not in reason
        assert "report to the EM that dispatched you" not in reason
    finally:
        import shutil

        for anchor_dir in (
            session_start._settings_home_anchor_dir(),
            session_start.sessions_dir(str(own)),
        ):
            if not anchor_dir:
                continue
            record_path = Path(anchor_dir) / session_id
            if record_path.exists():
                if record_path.is_dir():
                    shutil.rmtree(record_path, ignore_errors=True)
                else:
                    record_path.unlink(missing_ok=True)


# ANCHOR-RESOLUTION MISFIRE REGRESSION (bug reproduced live in-session,


def _patch_resolve_gitdir_to_fail_for(monkeypatch, failing_cwd: str) -> None:
    real_resolve_gitdir = guard.resolve_gitdir
    failing_abs = os.path.abspath(str(failing_cwd))

    def _flaky_resolve_gitdir(cwd=None):
        if cwd is not None and os.path.abspath(str(cwd)) == failing_abs:
            return None
        return real_resolve_gitdir(cwd)

    monkeypatch.setattr(guard, "resolve_gitdir", _flaky_resolve_gitdir)


def test_transient_anchor_gitdir_spawn_failure_does_not_bump_a_same_repo_write(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-transient-anchor-spawn-failure"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    _patch_resolve_gitdir_to_fail_for(monkeypatch, str(own))

    (own / "nested").mkdir()
    payload = _payload("Write", str(own / "nested" / "file.txt"), session_id, str(own))

    assert guard.check(payload) is None


def test_transient_anchor_gitdir_spawn_failure_still_allows_a_registered_foreign_target(tmp_path, monkeypatch):
    """Same simulated spawn failure, but the target is a DIFFERENT,
    registered repo. Before the fix, `own_gitdir is None` fell straight into
    `_verdict_bumps`'s repo-less-anchor branch, where a registered target
    bumps unconditionally -- exactly the misfire reproduced live (the EM's
    repro target and the session's own anchor were the SAME repo). After
    the fix, `path_has_git_ancestor(anchor)` finds the anchor's real `.git`
    and this guard treats resolution as UNRESOLVED, allowing here too,
    never reaching that branch at all."""
    own = _init_repo(tmp_path, "own-repo")
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-foreign-repo")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    session_id = "sess-transient-anchor-spawn-failure-registered-target"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    _patch_resolve_gitdir_to_fail_for(monkeypatch, str(own))

    payload = _payload("Write", str(registered / "f.txt"), session_id, str(own))

    assert guard.check(payload) is None


def test_genuinely_repo_less_anchor_still_bumps_registered_target_after_the_fix(tmp_path, monkeypatch):
    """Companion pin for the 2026-08-10 PM ruling `path_has_git_ancestor`
    must NOT touch: when the anchor truly has no `.git` ancestor anywhere
    (not merely a failed spawn), a REGISTERED target still bumps
    unconditionally -- same shape as `test_outside_any_repo_anchor_
    registered_target_still_bumps` above, pinned again here, side by side
    with the two UNRESOLVED-allows tests immediately above, so a future edit
    cannot collapse the two behaviours back together without a visible test
    failure in this same section."""
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo-2")
    _write_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "another-new-project"
    scaffold.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-scaffold-registered-target-2"

    assert marker.path_has_git_ancestor(str(scaffold)) is False

    payload = _payload("Write", str(registered / "f.txt"), session_id, str(scaffold))

    result = guard.check(payload)
    assert result is not None


# PINS THE EXACT PAYLOAD SHAPE FROM THE EM's VERIFICATION TRANSCRIPT
# `CLAUDE_PROJECT_DIR` -- `resolve_launch_anchor` returns `None`,


def test_em_repro_payload_unanchored_session_fails_open_and_matches_bash_parity(tmp_path):
    """The EM's literal transcript payload, byte-for-byte: no session-start
    record was ever written for `session_id`, and no `CLAUDE_PROJECT_DIR` is
    set -- so `check()` returns `None` (ALLOW), on BOTH surfaces. Proves the
    allow is the shared applicability contract's fail-open behaviour, not an
    asymmetry between the Bash and tool-write legs."""
    session_id = "verify-boundary-probe"
    if os.name == "nt":
        cwd = r"X:\claude-klabauter"
        foreign_file = r"X:\experiments\coordinator.local.md"
        foreign_dir_for_bash = "X:/experiments"
    else:
        cwd = "/opt/claude-klabauter"
        foreign_file = "/opt/experiments/coordinator.local.md"
        foreign_dir_for_bash = "/opt/experiments"
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": foreign_file,
            "old_string": "a",
            "new_string": "b",
        },
        "cwd": cwd,
        "session_id": session_id,
    }

    assert applicability.resolve_launch_anchor(session_id, cwd=cwd) is None
    assert applicability.bump_applies(session_id, cwd=cwd) is False

    assert guard.check(payload) is None
    payload_with_agent = dict(payload, agent_id="probe-agent-123")
    assert guard.check(payload_with_agent) is None

    from coordinator_core.bash_guards.bump_foreign_repo_write import (
        check_bump_foreign_repo_write,
    )

    bash_payload = {"session_id": session_id, "cwd": cwd, "agent_id": ""}
    bash_result = check_bump_foreign_repo_write(
        f"git -C {foreign_dir_for_bash} checkout -- coordinator.local.md",
        session_id,
        cwd,
        bash_payload,
    )
    assert bash_result is None


def test_em_repro_payload_denies_once_session_has_its_real_anchor_record(tmp_path):
    """The IDENTICAL payload from the EM's transcript, with the ONE thing a
    hand-constructed probe skips restored: the session-start anchor record
    every live session gets written automatically at SessionStart. This is
    the pinned regression for the bug the dispatch brief actually names --
    without this fix, this test's `check()` call would return `None`
    (`additionalContext`, non-blocking) instead of a real `permissionDecision:
    "deny"`."""
    session_id = "verify-boundary-probe-anchored"
    if os.name == "nt":
        cwd = r"X:\claude-klabauter"
        foreign_file = r"X:\experiments\coordinator.local.md"
    else:
        own_repo = _init_repo(tmp_path, "verify-boundary-own-repo")
        foreign_repo = _init_repo(tmp_path, "verify-boundary-experiments")
        cwd = str(own_repo)
        foreign_file = str(foreign_repo / "coordinator.local.md")
    session_start.write_session_start_record(session_id, launch_cwd=cwd)
    try:
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": foreign_file,
                "old_string": "a",
                "new_string": "b",
            },
            "cwd": cwd,
            "session_id": session_id,
        }

        result = guard.check(payload)

        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "experiments" in out["permissionDecisionReason"]
    finally:
        import shutil

        for anchor_dir in (
            session_start._settings_home_anchor_dir(),
            session_start.sessions_dir(cwd),
        ):
            if not anchor_dir:
                continue
            record_path = Path(anchor_dir) / session_id
            if record_path.exists():
                if record_path.is_dir():
                    shutil.rmtree(record_path, ignore_errors=True)
                else:
                    record_path.unlink(missing_ok=True)


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


def test_parity_verdict_matches_manual_composition_of_shared_primitives(tmp_path):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-parity"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(foreign / "notes.txt")
    payload = _payload("Write", target, session_id, str(own))

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


# "SYSTEM-TEMP SCRATCH IS NOT A FOREIGN REPO".


def _pin_temp_root(monkeypatch, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(root))
    # real `TMPDIR` (which, on this host, is an ANCESTOR of pytest's own
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
    assert str(foreign) in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_foreign_repo_outside_temp_root_still_bumps(tmp_path, monkeypatch):
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
    """DoE finding #1. `_resolve_target_gitdir` must walk UP to the nearest
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
    """DoE finding #3. A bare relative `file_path` (no dirname) must not have
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

    resolved = guard._resolve_target_gitdir("decoy", str(own))
    own_gitdir = marker.resolve_gitdir(str(own))
    decoy_gitdir = marker.resolve_gitdir(str(decoy_repo))
    assert resolved == own_gitdir
    assert resolved != decoy_gitdir

    assert guard._resolve_target_gitdir("decoy", None) is None


def test_non_temp_path_outside_any_repo_is_not_exempted(tmp_path, monkeypatch):
    _pin_temp_root(monkeypatch, tmp_path / "tmproot")
    elsewhere = tmp_path / "Documents" / "loose"
    elsewhere.mkdir(parents=True)

    assert guard._target_is_bare_temp_scratch(str(elsewhere / "f.txt"), None) is False


def test_symlinked_temp_root_resolves_to_the_same_verdict(tmp_path, monkeypatch):
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
    if not os.path.islink("/tmp"):
        pytest.skip("/tmp is not a symlink on this platform")

    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: os.path.realpath("/tmp"))

    assert guard._target_is_bare_temp_scratch("/tmp/claude-501/x/scratchpad/f.md", None) is True


def test_temp_exemption_never_raises_on_unresolvable_temp_root(tmp_path, monkeypatch):

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
    root = tempfile.gettempdir()
    assert root
    assert os.path.isdir(root)
    assert os.path.realpath(root).rstrip("/") != ""


# REGRESSION -- the confirmed live false positive: the harness-designated


def test_regression_real_harness_scratchpad_shape_never_bumps(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-real-scratchpad-c7"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

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
    assert str(foreign) in result["hookSpecificOutput"]["permissionDecisionReason"]


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
    assert str(foreign) in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_settings_home_exemption_parity_with_bash_surface(tmp_path, monkeypatch):
    """AC7 -- the settings-home exemption behaves IDENTICALLY on the Bash
    surface (`bump_outside_repo_write.py`) and this tool-write surface for
    the SAME destination. Pins the parity DoE finding #2 names: before this
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


# LESSONS-OUTBOX IS NOT A MISWRITE -- `coordinator-lesson-promote` writes a
# "LESSONS-OUTBOX IS NOT A MISWRITE, EVEN THOUGH IT IS A FOREIGN REPO", and
# the dispatch brief's CRITICAL CONSTRAINT -- this must NOT widen to


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
    assert str(foreign) in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_ordinary_foreign_repo_write_still_bumps_alongside_lessons_outbox_exemption(
    tmp_path,
):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-ordinary-foreign"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    (foreign / "state" / "lessons-outbox").mkdir(parents=True)
    payload = _payload("Write", str(foreign / "README.md"), session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert str(foreign) in result["hookSpecificOutput"]["permissionDecisionReason"]


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


def test_settings_json_under_claude_home_now_allowed(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-settings")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-settings-json"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(claude_home / ".claude" / "settings.json")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_project_dir_write_not_under_memory_now_allowed(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-project-dir")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-mem-project-dir"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    project_dir = claude_home / ".claude" / "projects" / "-Users-example-operator-X-some-project"
    project_dir.mkdir(parents=True)
    target = str(project_dir / "not-memory.md")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_agent_memory_store_case_insensitive_directory_still_exempted(tmp_path, monkeypatch):
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


# -- `~/.claude` is exempt from the write boundary WHOLESALE, from any


def test_ac1_settings_json_write_allowed_from_any_repo_anchor(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    claude_home = _init_repo(tmp_path, "claude-home-ac1")
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-ac1-settings"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(claude_home / ".claude" / "settings.json")
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_ac3_claude_home_carve_out_resolves_from_env_not_a_hardcoded_path(
    tmp_path, monkeypatch
):
    """AC3: the carve-out is keyed on the resolved `CLAUDE_HOME`/`HOME`/
    `USERPROFILE` env mapping, not a hardcoded path -- proven by pointing
    home at TWO different sandboxed locations across two checks and getting
    the carve-out in both, plus a target under the FIRST home no longer
    resolving as exempt once home has moved to the second (i.e. this is a
    live env resolution, not a cached/hardcoded string)."""
    own = _init_repo(tmp_path, "own-repo")
    home_a = _init_repo(tmp_path, "claude-home-a")
    home_b = _init_repo(tmp_path, "claude-home-b")

    _isolate_home(monkeypatch, home_a)
    session_id = "sess-ac3-a"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    target_a = str(home_a / ".claude" / "settings.json")
    assert guard.check(_payload("Write", target_a, session_id, str(own))) is None

    _isolate_home(monkeypatch, home_b)
    session_id_b = "sess-ac3-b"
    session_start.write_session_start_record(session_id_b, launch_cwd=str(own))
    target_b = str(home_b / ".claude" / "settings.json")
    assert guard.check(_payload("Write", target_b, session_id_b, str(own))) is None
    assert not applicability.target_is_under_claude_home(target_a)


def test_ac4_non_repo_destination_still_bumps(tmp_path, monkeypatch):
    fake_system_temp = tmp_path / "not-the-real-system-temp-ac4"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)

    own = _init_repo(tmp_path, "own-repo")
    claude_home = tmp_path / "claude-home-ac4-nonrepo"
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-ac4-nonrepo"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    outside = tmp_path / "outside-scratch-ac4"
    outside.mkdir()
    target = str(outside / "note.txt")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac4_unregistered_repo_destination_still_bumps(tmp_path, monkeypatch):
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-unregistered-ac4")
    claude_home = tmp_path / "claude-home-ac4-unreg"
    _isolate_home(monkeypatch, claude_home)
    session_id = "sess-ac4-unreg"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(foreign / "README.md")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac4_registered_repo_destination_still_bumps(tmp_path, monkeypatch):
    """AC4 regression: a REGISTERED foreign repo outside `~/.claude` keeps
    bumping too -- same control matrix cell as the spike verdict record's
    "(c) REGISTERED repo" row."""
    own = _init_repo(tmp_path, "own-repo")
    registered = _init_repo(tmp_path, "foreign-registered-ac4")
    claude_home = tmp_path / "claude-home-ac4-reg"
    _isolate_home(monkeypatch, claude_home)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry-ac4"))
    _write_registry(tmp_path / "registry-ac4", claude_klabauter=str(registered))
    session_id = "sess-ac4-reg"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = str(registered / "README.md")
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None
    assert "hookSpecificOutput" in result


def test_target_is_lessons_outbox_write_helper_path_shape(tmp_path):
    doe_root = str(tmp_path / "DoE-claude")
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


def _msys_form(p: Path) -> str:
    drive = p.drive
    rest = str(p)[len(drive):].replace("\\", "/")
    return f"/{drive[0].lower()}{rest}"


def test_ac1_msys_absolute_target_resolves_inside_own_repo_no_bump(tmp_path):
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")
    session_id = "sess-msys-ac1"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(own) + "/scratch/t.txt"
    payload = _payload("Write", target, session_id, str(own))

    assert guard.check(payload) is None


def test_ac2_msys_path_translates_to_its_drive_form(tmp_path):
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")

    msys_target = _msys_form(own) + "/f.txt"
    resolved = guard._resolve_target_gitdir(msys_target, None)
    expected = marker.resolve_gitdir(str(own))

    assert resolved == expected


def test_ac3_msys_foreign_target_still_bumps(tmp_path):
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    own = _init_repo(tmp_path, "own-repo")
    foreign = _init_repo(tmp_path, "foreign-repo")
    session_id = "sess-msys-ac3"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))

    target = _msys_form(foreign) + "/notes.txt"
    payload = _payload("Write", target, session_id, str(own))

    result = guard.check(payload)
    assert result is not None


def test_ac4_simulated_posix_host_matches_pre_fix_join_semantics(monkeypatch):
    import posixpath

    from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes

    monkeypatch.setattr(os, "path", posixpath)
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)
    monkeypatch.setattr(guard, "nearest_existing_ancestor", lambda p: p)
    monkeypatch.setattr(guard, "resolve_gitdir", lambda p: p)

    assert guard._resolve_target_gitdir("/repo/sub/file.txt", None) == "/repo/sub"

    assert guard._resolve_target_gitdir("relative/file.txt", "/base/cwd") == posixpath.join(
        "/base/cwd", "relative"
    )

    assert guard._resolve_target_gitdir("relative/file.txt", None) is None


def test_ac6_untranslatable_target_never_reaches_ancestor_walk(monkeypatch):
    if os.name != "nt":
        pytest.skip("MSYS drive-mount re-rooting defect is Windows-specific")
    called = {"hit": False}

    def _fake_ancestor(path):
        called["hit"] = True
        return path

    monkeypatch.setattr(guard, "nearest_existing_ancestor", _fake_ancestor)

    result = guard._resolve_target_gitdir(
        "/usr/local/bin/file", "C:\\Users\\me"
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

    assert result is not None


def test_ac7_msys_own_repo_target_never_bumps_through_dispatcher(tmp_path):
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
    ctx = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert str(foreign) in ctx
    assert os.path.isdir(str(foreign))


def test_c4b_simulated_posix_host_byte_identical_for_verdict_and_target_repo(monkeypatch):
    import posixpath

    from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes

    monkeypatch.setattr(os, "path", posixpath)
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)

    resolved = guard._resolve_target_dir("/repo/sub/file.txt", None)
    assert resolved == "/repo/sub"


def test_c4b_ac7_msys_registered_target_bumps_through_dispatcher(tmp_path, monkeypatch):
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


# resolution -- see review finding [P3] on commit fc1419657. THE HEADLINE
# REGRESSION: an MSYS-spelled write to the harness scratchpad on Windows


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
    assert guard._target_is_bare_temp_scratch("", None) is True
    assert guard._target_is_under_settings_home("", None) is False
    assert guard._target_is_lessons_outbox_write("") is False


def test_c4c_msys_foreign_target_still_bumps_not_converted_to_permit(tmp_path):
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
