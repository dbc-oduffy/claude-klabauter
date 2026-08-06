"""Tests for coordinator_core.bash_guards.bump_outside_repo_write -- the
Bash-surface OUTSIDE-REPO write-confinement speed bump (C5).

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C5 "Outside-repo detection, inline-interpreter classification,
sandbox reroute".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY -- see the plan's "Design
posture -- passable by construction". These tests verify the bump FIRES for
a plain-bash write-sink target resolving under no git root at all (AC3), for
an inline `python3 -c` payload carrying the same shape (AC4) while a
file-invoked script stays exempt (AC4's negative case, and the install-path
carve-out this module's own docstring names), that the always-allowed
destinations never bump (AC9), that this guard is registered as a
`GuardEntry` with its attributes pinned (AC13/AC19), and that the enumerated
write-sink set is pinned by a fixture (AC20).

No AC10/AC10b-analog (real-chain `offer-git-c` reachability test, see
`test_bump_foreign_repo_write.py`) is needed here: this guard's own
"WRITE-SINK CLASSIFICATION" docstring section deliberately excludes git
subcommands from its candidate set, so a `cd <dir> && git <sub>` shape was
never in scope for it at any chain position -- there is no reachability gap
to test.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards._write_bump_marker import (
    marker_basename,
    resolve_gitdir,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import WRITE_SINK_BINARIES


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


@pytest.fixture()
def env(tmp_path):
    """Anchor repo (the session's own, WITH a git root), a scratch
    non-repo directory to write outside into, and an unregistered fake HOME
    (so `~/.claude` never accidentally matches)."""
    anchor = _init_repo(tmp_path, "anchor")
    outside = tmp_path / "outside-scratch"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {"anchor": anchor, "outside": outside, "home": home}


@pytest.fixture(autouse=True)
def _clean_bump_env(monkeypatch, tmp_path):
    """Same isolation `test_bump_foreign_repo_write.py` uses -- this module
    also reads `os.environ` directly, never an injected `env` dict.

    `COORDINATOR_SETTINGS_HOME` is `setenv`'d to an isolated `tmp_path`
    subdirectory rather than `delenv`'d (binds AC13, example-doctrine-repo finding-#6-adjacent
    test-isolation gap): with it and `CLAUDE_HOME`/`HOME`/`USERPROFILE` all
    absent, `_settings_home_dir_from_env` returns `""`, which -- now that
    C1/C2 have landed the settings-home anchor -- would make that anchor
    silently no-op in exactly this suite (the one meant to exercise the
    write-confinement guards it now primarily resolves through), and any
    settings-home write this suite's own fixtures trigger would otherwise
    land in the developer's real `~/.coordinator-claude-settings/
    claude-klabauter/`. Isolated per-test via `tmp_path`, matching every
    other isolation root this fixture builds.

    ALSO repoints the SHARED classifier's recognized-temp-root primitives
    (`_write_bump_applicability.tempfile.gettempdir()` AND its
    `_posix_tmp_literal()` seam -- this guard no longer owns a `tempfile`
    reference of its own; it delegates entirely to that shared module's
    `target_is_bare_temp_scratch`) to sentinel directories under `tmp_path`,
    distinct from the fixture repos built elsewhere under the SAME
    `tmp_path`: pytest's own `tmp_path` lives under the REAL system temp dir
    on every platform this suite runs on, so without this repoint every
    "outside repo" candidate this file's other fixtures build would ALSO
    resolve under the real system temp and get silently exempted by this
    guard's own AC9 carve-out -- not a guard bug, a test-isolation
    necessity. `_posix_tmp_literal` specifically must ALSO be repointed
    (not just `gettempdir()`): on a platform where `tmp_path` defaults to
    living directly under `/tmp` (Linux with no `TMPDIR` set), the shared
    classifier's unconditional `os.path.realpath("/tmp")` candidate would
    otherwise still catch every fixture path regardless of the
    `gettempdir()` patch. Example-doctrine-repo finding #6 describes this exact hazard as
    reproducing in this fixture ("patches ONE of two candidates"); it does
    NOT reproduce here -- both `gettempdir()` and `_posix_tmp_literal()`
    were already repointed together before this dispatch (see the two
    `monkeypatch.setattr` calls below), so `gettempdir()` and the `/tmp`
    literal never collapse to one unpatched root on Linux CI. The one test
    that exercises the REAL AC9 system-temp carve-out
    (`test_ac9_system_temp_write_never_bumps`) explicitly re-points both
    back."""
    for var in (
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "TMPDIR",
        "TEMP",
        "TMP",
    ):
        monkeypatch.delenv(var, raising=False)
    isolated_settings_home = tmp_path / "isolated-settings-home"
    isolated_settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(isolated_settings_home))
    fake_system_temp = tmp_path / "not-the-real-system-temp"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))


def _set_anchor(monkeypatch, env, session_id: str, extra: dict | None = None) -> None:
    """Establishes applicability the same way a real session does -- a
    settings-home `write_session_start_record`, not `CLAUDE_PROJECT_DIR`
    (example-doctrine-repo finding #5 / AC5). Mirrors `test_bump_foreign_repo_write._set_anchor`
    exactly -- see that helper's own docstring for the full rationale.
    `CLAUDE_PROJECT_DIR` is left unset throughout; `HOME` is still set (to an
    unrelated scratch dir) so `_anchor_is_under_claude_home` resolves to "not
    under" rather than fail-opening on an unresolvable home."""
    monkeypatch.setenv("HOME", str(env["home"]))
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))


# ---------------------------------------------------------------------------
# AC3 -- a plain-bash write resolving to no git repo bumps.
# ---------------------------------------------------------------------------


def test_ac3_cp_to_outside_repo_target_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-1")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "newfile.txt"
    cmd = f"cp {src} {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-1", str(env["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac3_output_redirection_to_outside_repo_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-2")
    dest = env["outside"] / "redir.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-2", str(env["anchor"]), {})

    assert result is not None


def test_ac3_mkdir_to_not_yet_existing_outside_repo_dir_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-3")
    new_dir = env["outside"] / "brand-new-subdir"
    assert not new_dir.exists()
    cmd = f"mkdir -p {new_dir}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-3", str(env["anchor"]), {})

    assert result is not None


def test_same_repo_write_does_not_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-4")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["anchor"] / "dest.txt"
    cmd = f"cp {src} {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-4", str(env["anchor"]), {})

    assert result is None


def test_write_into_a_different_git_repo_does_not_bump_here(env, tmp_path, monkeypatch):
    """A target that resolves to SOME git root (even a foreign one) is C4's
    concern, never this guard's -- see module docstring, "PREDICATE"."""
    _set_anchor(monkeypatch, env, "sess-5")
    foreign = _init_repo(tmp_path, "foreign")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = foreign / "dest.txt"
    cmd = f"cp {src} {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-5", str(env["anchor"]), {})

    assert result is None


def test_subagent_class_message_names_the_sandbox_route(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-6")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "newfile.txt"
    cmd = f"cp {src} {dest}"
    # `_canonical_agent_id` (subagent_sandbox.engine) only recognizes a
    # bare-hex unnamed-agent id (`^[a-f0-9]{12,}$`) or a named teammate id
    # -- an arbitrary string does not canonicalize and falls back to
    # EM-class, so this must be a real bare-hex shape.
    payload = {"agent_id": "af307f34d8afa24eb"}

    result = guard.check_bump_outside_repo_write(cmd, "sess-6", str(env["anchor"]), payload)

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sandbox" in reason.lower() or "state" in reason.lower()


# ---------------------------------------------------------------------------
# § Where the bump does not fire -- session anchor itself has no git repo.
# ---------------------------------------------------------------------------


def test_session_anchor_with_no_git_repo_never_bumps_outside_repo_writes(tmp_path, monkeypatch):
    """AC11's C5 half: a session launched to scaffold a fresh, un-repo'd
    tree writes freely -- outside-repo writes never bump when the SESSION's
    own anchor has no git repo of its own, regardless of target."""
    no_repo_anchor = tmp_path / "fresh-scaffold"
    no_repo_anchor.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record("sess-7", launch_cwd=str(no_repo_anchor))
    dest = tmp_path / "elsewhere" / "file.txt"
    dest.parent.mkdir()
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-7", str(no_repo_anchor), {})

    assert result is None


# ---------------------------------------------------------------------------
# AC4 -- inline `python3 -c` payload write sinks bump; file-invoked scripts
# stay exempt.
# ---------------------------------------------------------------------------


def test_ac4_inline_python3_dash_c_write_sink_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-8")
    dest = env["outside"] / "inline.txt"
    cmd = f'python3 -c "echo hi > {dest}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-8", str(env["anchor"]), {})

    assert result is not None


def test_ac4_inline_python_bundled_short_flag_write_sink_bumps(env, monkeypatch):
    """`-ic` (bundled) is the SAME `-c` shape `_BUNDLED_C_FLAG_RE` matches,
    not only the standalone `-c` spelling."""
    _set_anchor(monkeypatch, env, "sess-9")
    dest = env["outside"] / "bundled.txt"
    cmd = f'python3 -ic "echo hi > {dest}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-9", str(env["anchor"]), {})

    assert result is not None


def test_ac4_inline_bash_dash_c_write_sink_bumps_via_shared_tokenizer_recursion(env, monkeypatch):
    """`bash -c` is not this module's own manual unwrap -- it is already
    auto-recursed by `resolve_command_positions` itself (module docstring,
    "INLINE PYTHON `-c` PAYLOADS")."""
    _set_anchor(monkeypatch, env, "sess-10")
    dest = env["outside"] / "bash-inline.txt"
    cmd = f'bash -c "echo hi > {dest}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-10", str(env["anchor"]), {})

    assert result is not None


def test_ac4_file_invoked_python_script_does_not_bump(env, monkeypatch):
    """`python3 script.py` -- no `-c` at all -- is NOT classified (deferred
    to plan D1). Even though the script's own (unexamined) content might
    write outside the repo, this module never reads file contents."""
    _set_anchor(monkeypatch, env, "sess-11")
    script = env["anchor"] / "script.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    cmd = f"python3 {script}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-11", str(env["anchor"]), {})

    assert result is None


def test_ac4_file_invoked_install_script_writing_settings_home_is_allowed(env, monkeypatch):
    """The explicit negative case this chunk's own test surface names: a
    file-invoked `bash install.sh` is never classified at all (this module
    never opens the script), so it is allowed regardless of what it writes
    -- distinct from (and a superset of) the AC9 settings-home carve-out."""
    settings_home = env["home"] / ".coordinator-claude-settings"
    settings_home.mkdir()
    _set_anchor(monkeypatch, env, "sess-12")
    install_sh = env["anchor"] / "install.sh"
    install_sh.write_text("#!/bin/sh\necho installing\n", encoding="utf-8")
    cmd = f"bash {install_sh}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-12", str(env["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# AC9 -- always-allowed destinations never bump.
# ---------------------------------------------------------------------------


def test_ac9_settings_home_write_never_bumps(env, monkeypatch):
    settings_home = env["home"] / ".coordinator-claude-settings"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    _set_anchor(monkeypatch, env, "sess-13", {"COORDINATOR_SETTINGS_HOME": str(settings_home)})
    dest = settings_home / "bin" / "note.txt"
    dest.parent.mkdir(parents=True)
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-13", str(env["anchor"]), {})

    assert result is None


def test_ac9_system_temp_write_never_bumps(env, monkeypatch):
    # Undo `_clean_bump_env`'s fake-tempdir repoint -- this is the one test
    # that exercises the REAL system-temp carve-out.
    monkeypatch.setattr(applicability.tempfile, "gettempdir", tempfile.gettempdir)
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: "/tmp")
    _set_anchor(monkeypatch, env, "sess-14")
    real_tmp = os.path.realpath(tempfile.gettempdir())
    dest_dir = Path(real_tmp) / "write-confinement-ac9-probe"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "note.txt"
    cmd = f"echo hi > {dest}"

    try:
        result = guard.check_bump_outside_repo_write(cmd, "sess-14", str(env["anchor"]), {})
        assert result is None
    finally:
        if dest.exists():
            dest.unlink()
        dest_dir.rmdir()


def test_ac9_subagent_sandbox_root_write_never_bumps(env, monkeypatch):
    """`state/subagent-share/<effective-session-id>/` under the anchor's
    OWN git root, per `_sandbox_root` -- ordinarily inside a repo already
    (so never reaches this guard's "no git root" predicate in the first
    place), but the carve-out is asserted directly here as a property of
    this module's own allow-list logic (module docstring, "ALWAYS-ALLOWED
    DESTINATIONS")."""
    session_id = "sess-15"
    _set_anchor(monkeypatch, env, session_id)
    sandbox = env["anchor"] / "state" / "subagent-share" / session_id
    sandbox.mkdir(parents=True)
    dest = sandbox / "note.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_agent_memory_store_write_never_bumps(env, monkeypatch):
    """A write into `<home>/.claude/projects/<slug>/memory/<file>.md` never
    bumps -- the memory store is Claude Code's own per-project persistent
    scratch, not a sibling repo, even when (as here, under the fake `HOME`
    this suite builds) `~/.claude` resolves to no git repo at all -- this
    guard's own predicate."""
    session_id = "sess-mem-1"
    _set_anchor(monkeypatch, env, session_id)
    memory_dir = env["home"] / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "note.md"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_agent_memory_store_index_write_never_bumps(env, monkeypatch):
    """Same as above for the `MEMORY.md` index file specifically."""
    session_id = "sess-mem-2"
    _set_anchor(monkeypatch, env, session_id)
    memory_dir = env["home"] / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "MEMORY.md"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# The marker clears the bump.
# ---------------------------------------------------------------------------


def test_marker_present_for_session_clears_the_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-16")
    anchor_gitdir = resolve_gitdir(str(env["anchor"]))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker_basename("sess-16")).touch()
    dest = env["outside"] / "cleared.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-16", str(env["anchor"]), {})

    assert result is None


def test_marker_for_a_different_session_does_not_clear_this_ones_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-17")
    anchor_gitdir = resolve_gitdir(str(env["anchor"]))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker_basename("some-other-session")).touch()
    dest = env["outside"] / "not-cleared.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-17", str(env["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# C5 -- destination-class axis wired through (always DESTINATION_FOREIGN in
# practice: this guard's own predicate guarantees the target resolves to no
# git repo, and a `publish.mirrors.*.path` entry is itself always a real
# repo, so the two can never coincide). Classified explicitly via C1 rather
# than hardcoded -- AC9 asserted first.
# ---------------------------------------------------------------------------


def test_destination_class_kwarg_is_passed_explicitly_as_foreign(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_applicability as applicability
    from coordinator_core.bash_guards._write_bump_message import DESTINATION_FOREIGN

    session_id = "sess-destination-class"
    _set_anchor(monkeypatch, env, session_id)

    assert applicability.bump_applies(session_id, cwd=str(env["anchor"])) is True

    captured = []

    def _spy(**kwargs):
        captured.append(kwargs)
        return "stub message -- test double, never asserted on text"

    monkeypatch.setattr(guard, "render_bump_message", _spy)

    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {dest}"
    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is not None
    assert len(captured) == 1
    assert captured[0].get("destination_class") == DESTINATION_FOREIGN


# ---------------------------------------------------------------------------
# Fail-open cases -- unresolvable inputs never bump.
# ---------------------------------------------------------------------------


def test_fail_open_when_session_id_empty(env, monkeypatch):
    # Guard short-circuits on an empty `session_id` before ever resolving an
    # anchor -- no applicability setup needed either way (mirrors C3's own
    # `test_fail_open_when_session_id_empty`).
    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {dest}"

    assert guard.check_bump_outside_repo_write(cmd, "", str(env["anchor"]), {}) is None


def test_fail_open_when_cmd_empty(env, monkeypatch):
    # Guard short-circuits on an empty `cmd` before ever resolving an anchor
    # -- no applicability setup needed either way (mirrors C3's own
    # `test_fail_open_when_cmd_empty`).
    assert guard.check_bump_outside_repo_write("", "sess-18", str(env["anchor"]), {}) is None


def test_fail_open_when_anchor_unresolvable(env, monkeypatch):
    # No CLAUDE_PROJECT_DIR / session-start record at all.
    monkeypatch.setenv("HOME", str(env["home"]))
    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-19", str(env["anchor"]), {})

    assert result is None


def test_fail_open_when_write_sink_target_has_no_existing_ancestor_at_all():
    """`_nearest_existing_ancestor` (shared with C4 via
    `_write_bump_sink_shapes.nearest_existing_ancestor`) -- a bogus path
    with no real filesystem root resolves to `None` (never raises)."""
    assert guard._nearest_existing_ancestor("") is None


# ---------------------------------------------------------------------------
# AC13 / AC19 -- registered as a GuardEntry, not a call-site patch, with
# every registration attribute pinned explicitly.
# ---------------------------------------------------------------------------


def test_ac13_registered_as_a_guard_entry_in_dispatch_build_guard_chain():
    from coordinator_core.bash_guards import dispatch

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entries = [e for e in chain if e.name == "bump-outside-repo-write"]

    assert len(entries) == 1


def test_ac19_registration_attributes_pinned_not_left_to_default():
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entry = next(e for e in chain if e.name == "bump-outside-repo-write")

    # `fail_closed=False` -- the OPPOSITE of every neighbouring
    # CONFINEMENT_DENY entry: a crash in this guard must swallow to
    # "allow", never route through the hard-deny crash path.
    assert entry.fail_closed is False
    # `band=ADVISORY_REWRITE`, NOT `CONFINEMENT_DENY` -- the blanket-disarm
    # marker can suppress every band except CONFINEMENT_DENY; registering a
    # deliberately passable bump there would make it the LEAST passable
    # guard in the suite.
    assert entry.band is dispatch.GuardBand.ADVISORY_REWRITE
    # Explicit, never the UNCLASSIFIED default.
    assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED
    assert entry.advisory_value is AdvisoryValue.NOT_COST_ARGUED


def test_ac19_guard_band_membership_and_advisory_registry_tests_also_pass():
    """This guard's registration is additionally pinned by the package's
    OWN pre-existing structural tests -- see
    `test_guard_band_membership.py::ADVISORY_REWRITE_NAMES` and
    `test_override_route_inventory.py::_NO_OVERRIDE_NOTE_ALLOWLIST`, both
    updated alongside this guard's own registration."""
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    for entry in chain:
        assert entry.advisory_value is not None
        if entry.band is dispatch.GuardBand.CONFINEMENT_DENY:
            assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED


# ---------------------------------------------------------------------------
# AC20 -- the enumerated write-sink set is pinned by a fixture, so a later
# omission reads as a diff against a named list rather than a silent gap.
# Incident-derived where practical -- the two cited incidents
# (`nested-layout-bisect-tmp`, `tasks`, see the plan's own § Problem) were
# both plain directory/file creation, matching `mkdir`/output-redirection
# below directly; the remaining entries are this module's own enumerated
# superset, pinned for drift-detection rather than incident-recreated one
# by one.
# ---------------------------------------------------------------------------


def test_ac20_write_sink_binary_set_is_pinned():
    assert WRITE_SINK_BINARIES == frozenset(
        {"tee", "cp", "mv", "mkdir", "install", "sed", "rsync", "tar"}
    )


# ---------------------------------------------------------------------------
# REGRESSION -- the confirmed live false positive: the harness-designated
# per-session scratchpad is NOT covered by `tempfile.gettempdir()` alone on
# macOS (`TMPDIR` resolves under `/var/folders/...`; the scratchpad lives
# under `/private/tmp`). Uses a REAL session-start record so applicability is
# genuinely True -- a test that passes only because applicability failed open
# proves nothing (see this fix's own dispatch brief). MUST fail against the
# pre-fix module (`_always_allowed_roots`'s single `tempfile.gettempdir()`
# entry, no `/tmp` realpath candidate).
# ---------------------------------------------------------------------------


def test_regression_real_harness_scratchpad_shape_never_bumps(env, monkeypatch):
    # Point the REAL `/tmp` candidate (via the testability seam) at a
    # sentinel standing in for `/private/tmp` -- `gettempdir()` stays
    # repointed to the fixture's OWN fake system temp (simulating the
    # macOS `TMPDIR` divergence: gettempdir() and the scratchpad's real
    # root are two DIFFERENT directories, exactly like the live incident).
    real_tmp_stand_in = env["anchor"].parent / "private-tmp-stand-in"
    real_tmp_stand_in.mkdir()
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(real_tmp_stand_in))

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(env["anchor"]))
    monkeypatch.setenv("HOME", str(env["home"]))
    session_id = "sess-real-scratchpad"
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))

    scratchpad = (
        real_tmp_stand_in
        / "claude-501"
        / "-Users-example-operator-X-claude-klabauter"
        / session_id
        / "scratchpad"
    )
    scratchpad.mkdir(parents=True)
    dest = scratchpad / "draft-memo.md"
    cmd = f"cat > {dest} <<'E'\nx\nE"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_regression_shared_classifier_conjunction_git_repo_under_temp_not_exempt(
    env, tmp_path, monkeypatch
):
    """THE CONJUNCTION, unit-tested directly against the shared classifier
    this guard now delegates to: a real checkout under the recognized temp
    root IS a foreign repo and must NOT classify as bare scratch -- an
    unconditional temp exemption would open a hole the size of
    `git clone $TMPDIR/...`. (This guard's OWN candidate loop never even
    reaches the temp-exemption check for a git-resolved target -- it is
    filtered out earlier as "not this guard's concern", C4's territory --
    so the conjunction is exercised here at the classifier level, which is
    where both C5 and C7 actually consume it.)"""
    real_tmp_stand_in = tmp_path / "private-tmp-stand-in-2"
    real_tmp_stand_in.mkdir()
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(real_tmp_stand_in))
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(real_tmp_stand_in))

    foreign = _init_repo(real_tmp_stand_in, "checkout-under-temp")

    assert applicability.target_is_bare_temp_scratch(str(foreign / "dest.txt")) is False


def test_regression_symlinked_tmp_root_resolves_to_same_verdict_as_real(env, tmp_path, monkeypatch):
    """`/tmp` -> `/private/tmp` shape, expressed portably via a real
    symlink: a target reached through the symlink must classify identically
    to one spelled with the resolved real path."""
    real_root = tmp_path / "private-tmp-real"
    real_root.mkdir()
    link_root = tmp_path / "tmp-link"
    try:
        os.symlink(str(real_root), str(link_root), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")

    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(link_root))
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(real_root))

    monkeypatch.setenv("HOME", str(env["home"]))
    session_start.write_session_start_record("sess-symlink-real", launch_cwd=str(env["anchor"]))
    session_start.write_session_start_record("sess-symlink-link", launch_cwd=str(env["anchor"]))
    scratchpad = real_root / "scratchpad"
    scratchpad.mkdir()

    dest_via_real = real_root / "scratchpad" / "via-real.txt"
    dest_via_link = link_root / "scratchpad" / "via-link.txt"

    result_real = guard.check_bump_outside_repo_write(
        f"echo hi > {dest_via_real}", "sess-symlink-real", str(env["anchor"]), {}
    )
    result_link = guard.check_bump_outside_repo_write(
        f"echo hi > {dest_via_link}", "sess-symlink-link", str(env["anchor"]), {}
    )

    assert result_real is None
    assert result_link is None


# ---------------------------------------------------------------------------
# AC6 -- cross-repo `cwd` drift, applied to the OUTSIDE-repo surface (the
# same scenario `test_bump_foreign_repo_write.py` [C3] pins for the
# cross-repo surface, so the anchor fix is pinned on more than the one
# surface the memo reproduced against). Drifts the live payload `cwd` ACROSS
# a repo boundary -- into an unrelated FOREIGN repo, never merely a
# subdirectory of the anchor repo (the `[subdir]` row in the plan's own
# repro table already passed before this fix; the `[FOREIGN]` row is the
# one this guard's own anchor resolution must now get right too). Uses a
# REAL `write_session_start_record` so applicability is genuinely True, per
# this plan's own repro methodology -- a test that passes only because
# applicability failed open proves nothing.
# ---------------------------------------------------------------------------


def test_ac6_cwd_drifted_to_a_foreign_repo_still_bumps_an_outside_repo_write(
    env, tmp_path, monkeypatch
):
    """Session launches in `env["anchor"]`; the live payload `cwd` later
    drifts to an unrelated FOREIGN repo (never `CLAUDE_PROJECT_DIR`, unset
    throughout -- the production condition the plan's repro table names).
    Pre-C1/C2, `resolve_launch_anchor`'s only anchor source was the in-repo
    record resolved from the LIVE `cwd` (`sessions_dir(cwd)`) -- which,
    once `cwd` has crossed into the foreign repo, addresses a hub where
    this session's record was never written, so the anchor resolves to
    `None`, `bump_applies` fails open, and this guard never even reaches
    its own outside-repo predicate for a target that plainly resolves
    under no git root at all. Post-C1/C2, the settings-home hub is
    cwd-independent and resolves the real anchor regardless of where the
    live `cwd` has drifted to, so the bump fires correctly."""
    monkeypatch.setenv("HOME", str(env["home"]))
    session_id = "sess-ac6-drift"
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))

    foreign = _init_repo(tmp_path, "foreign-drift-target")
    dest = env["outside"] / "drifted-write.txt"
    cmd = f"echo hi > {dest}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(foreign), {})

    assert result is not None


def test_extended_length_prefix_does_not_desync_is_under(monkeypatch, tmp_path):
    """`state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`
    -- outside-repo surface (C5). Same injected-asymmetry shape as the
    other two surfaces: `os.path.realpath` returns the Windows
    extended-length form for the candidate and the bare form for an
    always-allowed root. Before this fix, `_resolve_and_casefold`'s
    `casefold_path` call preserved the prefix and `_is_under` wrongly
    reported "not under" for the identical directory -- which, on the
    AC9 always-allowed-roots check this helper backs, would wrongly let a
    bump fire against a destination that should have been exempted."""
    real_dir = tmp_path / "always-allowed-root"
    real_dir.mkdir()
    bare_form = str(real_dir)
    prefixed_form = "\\\\?\\" + bare_form

    real_realpath = guard.os.path.realpath

    def fake_realpath(path, *a, **kw):
        if path == "candidate-input":
            return prefixed_form
        if path == "root-input":
            return bare_form
        return real_realpath(path, *a, **kw)

    monkeypatch.setattr(guard.os.path, "realpath", fake_realpath)

    candidate_cf = guard._resolve_and_casefold("candidate-input")
    root_cf = guard._resolve_and_casefold("root-input")
    assert candidate_cf is not None and root_cf is not None
    assert guard._is_under(candidate_cf, root_cf) is True
