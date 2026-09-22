"""Characterization tests for coordinator_core.ops.install_health_run.

Ported test cases mirror the T1/T5 cases from DoE-claude's former
coordinator/bin/tests/test-install-health-run.sh, plus the trust-gate
contract from DoE-claude's former coordinator/lib/coordinator-
trusted-root-guard.sh's fail-loud mode, which this module reimplements.

T2/T3/T4 (bash-drop-in-passthrough, continue-past-failure, silent-skip) and
the shebang-resolution/cmd-twin regression tests were retired here: they all
exercised the retired glob-and-shebang dispatch (a real script dropped into
bin/install-health/ that the orchestrator discovered and launched by
sniffing its bytes). That dispatch no longer exists -- a leg is now a
`_NATIVE_LEGS` row (in-process callable, or a `DeclaredLaunch` with a stated
argv), and an on-disk file in the drop-in directory that is not a declared
leg is refused by name, never launched (see `test_undeclared_drop_in_*`
below). The CONTRACT properties those retired tests pinned (continue past a
failing leg, an OSError counts one failure, a nonzero exit counts one
failure) are ported below against monkeypatched `DeclaredLaunch` rows in
`_NATIVE_LEGS` instead of real bash drop-ins.

Port of: install-health-run.sh (DoE 290997c7, 2026-07-22)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import coordinator_core.ops.install_health_run as install_health_run_module
from coordinator_core.install.shell_rc_guard import write_path_entry_guard_blocks
from coordinator_core.install.door_install import DoorInstallError, ImageCurrencyAudit, ProvenanceVerdict
from coordinator_core.install.door_route_signal import DoorRouteResult
from coordinator_core.install.engine_root_for_install import InstallEngineRoot
from coordinator_core.ops.install_health_run import (
    _BIN_DST_KNOWN_FORWARDER,
    _NATIVE_LEGS,
    DeclaredLaunch,
    _trusted_root,
    check_bareword_path_provisioning,
    check_door_provenance,
    check_door_route,
    main,
)


def _mk_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "coordinator" / "bin" / "install-health").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _trust_opt_out(monkeypatch, tmp_path):
    # Every functional test below exercises the ITERATION contract, not the
    # trust gate — opt out of the trust check the same way the bash test
    # harness does (COORDINATOR_PLUGIN_ROOT_TRUSTED=1 env var).
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    # main() unconditionally resolves coordinator_claude_klabauter_root() before
    # running any leg (see install_health_run.py's fail-loud contract) — pin
    # it deterministically via the COORDINATOR_ENGINE_ROOT env var (Rung 1 of
    # the resolver's chain) rather than depend on this machine's real
    # machine-local registry having repos.claude_klabauter set. This is the
    # exact 3/13-on-the-PM's-machine exposure the 2026-07-22 kill-first memo
    # flagged: any test resolving claude-klabauter through the registry ladder is
    # environment-dependent by construction.
    # The retired CLAUDE_KLABAUTER_ROOT is deleted rather than left alone: C14 closed the
    # dual-read window, so a name inherited from an ancestor process no longer
    # answers Rung 1 and instead makes engine_root emit its retired-name
    # advisory to stderr, which the exact-stderr assertions below read as noise.
    # The residual glob-discovery directory (main()'s "hypothetical FUTURE
    # foreign drop-in" hook) resolves off coordinator_claude_klabauter_root() as
    # <claude_klabauter_root>/coordinator/bin/install-health/ — pin
    # COORDINATOR_ENGINE_ROOT to the SAME `tmp_path / "root"` that
    # `_mk_root()` below builds under, so a
    # drop-in written to `root / "coordinator" / "bin" / "install-health"`
    # is the directory main() actually globs, not an orphaned sibling tree.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path / "root"))
    # seed-skill-overrides is a real `_NATIVE_LEGS` entry that runs
    # unconditionally in main() — with no real
    # <claude_klabauter_root>/coordinator/bin/seed-skill-overrides.py helper under the
    # stub root above, its own graceful-degrade path (module docstring:
    # "Degrades gracefully (exit 0, WARNING to stderr) when the helper
    # script [is absent]") would emit stderr noise into every test below
    # that isn't actually exercising seed-skill-overrides, breaking their
    # `captured.err == ""`/exact-message assertions. Default it to a silent
    # no-op here; tests that exercise it directly re-patch it inside their
    # own `with patch(...)` block, which composes fine over this monkeypatch.
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.seed_skill_overrides.main",
        lambda *args, **kwargs: 0,
    )
    # check-bareword-path-provisioning (C3,
    # docs/plans/2026-07-25-posix-bareword-path-provisioning.md) is ALSO a
    # real `_NATIVE_LEGS` entry that runs unconditionally in main() — every
    # test in this file gets a bare, genuinely unprovisioned quarantined HOME
    # (no `.coordinator-claude-settings/bin`), so the leg correctly reports
    # FAIL and returns 1, breaking these tests' unrelated "silent + rc 0"
    # iteration-contract assertions. None of the cases in this file exercise
    # this leg's own behaviour — that coverage lives in the dedicated
    # `test_check_bareword_path_provisioning_*` cases below, which opt back
    # IN by calling the function directly rather than through main(). Default
    # it to a silent no-op here, mirroring the seed-skill-overrides precedent
    # immediately above.
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.check_bareword_path_provisioning",
        lambda *args, **kwargs: 0,
    )
    # check-door-provenance (this dispatch's new leg) is likewise a real
    # `_NATIVE_LEGS` entry that runs unconditionally in main() — every test
    # in this file gets a bare quarantined HOME with no installed door at
    # all, so the leg would correctly print a NOTE ("no door binary") into
    # every unrelated iteration-contract test, breaking their silent/exact-
    # output assertions. Same default-to-no-op precedent as the two legs
    # above; the dedicated `test_check_door_provenance_*` cases below opt
    # back IN by calling the function directly.
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.check_door_provenance",
        lambda *args, **kwargs: 0,
    )
    # check-door-route (this dispatch's new leg, C2) is likewise a real
    # `_NATIVE_LEGS` entry that runs unconditionally in main() — every test
    # in this file gets a bare quarantined HOME with no installed door at
    # all, so the leg would correctly print a NOTE ("no door installed")
    # into every unrelated iteration-contract test, breaking their silent/
    # exact-output assertions. Same default-to-no-op precedent as the legs
    # above; the dedicated `test_check_door_route_*` cases below opt back IN
    # by calling the function directly.
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.check_door_route",
        lambda *args, **kwargs: 0,
    )


# ---------------------------------------------------------------------------
# T1: empty install-health/ dir -> exit 0, no output
# ---------------------------------------------------------------------------

def test_empty_dir_exits_zero_silent(tmp_path, capsys):
    root = _mk_root(tmp_path)
    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# T5: absent install-health/ dir entirely -> exit 0 (valid no-scripts state)
# ---------------------------------------------------------------------------

def test_absent_dir_exits_zero(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    assert rc == 0


# ---------------------------------------------------------------------------
# Trust gate — reimplementation of the trust-core, fail-loud mode.
# ---------------------------------------------------------------------------

def test_trusted_root_under_claude_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    root = str(tmp_path / ".claude" / "plugins" / "coordinator-claude" / "coordinator")
    assert _trusted_root(root) is True


def test_untrusted_root_outside_prefixes(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    assert _trusted_root("/some/random/untrusted/path") is False


def test_traversal_segment_forces_untrusted(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "0")
    claude_dir = tmp_path / ".claude"
    root = str(claude_dir / "plugins" / ".." / ".." / "evil")
    assert _trusted_root(root) is False


def test_opt_out_env_var_trusts_anything(monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    assert _trusted_root("/anything/at/all") is True


def test_untrusted_root_gate_fails_loud(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/untrusted/root")
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "outside trusted prefix" in captured.err


# ---------------------------------------------------------------------------
# DeclaredLaunch — AC3: closed `interpreter` vocabulary, `interpreter_flags`
# validation and argv shape.
# ---------------------------------------------------------------------------


def test_declared_launch_rejects_non_python_interpreter():
    with pytest.raises(ValueError):
        DeclaredLaunch(script="x.py", interpreter="bash")


def test_declared_launch_rejects_interpreter_flag_without_dash_prefix():
    with pytest.raises(ValueError):
        DeclaredLaunch(script="x.py", interpreter_flags=("u",))


def test_declared_launch_argv_includes_interpreter_flags():
    dl = DeclaredLaunch(script="x.py", interpreter_flags=("-u",))
    assert dl.argv("/some/root") == [sys.executable, "-u", os.path.join("/some/root", "x.py")]


# ---------------------------------------------------------------------------
# DeclaredLaunch — AC4/AC5: the launch is stated, never inferred from the
# script's bytes, its filename, or a default.
# ---------------------------------------------------------------------------


def test_declared_launch_sh_named_python_script_runs_under_sys_executable(tmp_path, monkeypatch):
    """AC4 — a `.sh`-named file holding Python content, launched via a real
    subprocess against a tmp engine root: the extension is never sniffed."""
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    script = root / "weird-name.sh"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [("fake-sh-python", DeclaredLaunch(script="weird-name.sh"))],
    )

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "ran"


def test_declared_launch_no_exec_bit_still_launches_via_stated_interpreter(tmp_path, monkeypatch):
    """AC5 — a `DeclaredLaunch` whose script has no execute bit still
    launches: argv[0] is always the interpreter, never the script itself."""
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    script = root / "noexec_leg.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o644)

    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [("fake-noexec", DeclaredLaunch(script="noexec_leg.py"))],
    )

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "ran"


def test_declared_launch_nt_launcher_used_alone_under_windows(tmp_path, monkeypatch):
    """AC5 — the `nt_launcher` tier returns `[<launcher>]` alone under
    `_is_windows() -> True`, never combined with `interpreter`/`script`."""
    root = _mk_root(tmp_path)
    monkeypatch.setattr(install_health_run_module, "_is_windows", lambda: True)

    calls = []

    def _fake_call(argv, **_spawn_kwargs):
        calls.append(argv)
        return 0

    monkeypatch.setattr(install_health_run_module.subprocess, "call", _fake_call)
    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [("fake-nt", DeclaredLaunch(script="leg.py", nt_launcher="leg.exe"))],
    )

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    assert calls == [[os.path.join(str(root), "leg.exe")]]


def test_declared_launch_ignores_undeclared_on_disk_cmd_twin(tmp_path, monkeypatch, capsys):
    """AC5 — an on-disk `<script>.cmd` that is NOT declared is ignored: the
    twin is never probed for, and `.cmd` is not even a scanned drop-in
    extension, so it produces no REFUSED line either."""
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    script = root / "leg.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    stray_cmd = root / "coordinator" / "bin" / "install-health" / "leg.py.cmd"
    stray_cmd.write_text("echo should-not-run\n", encoding="utf-8")

    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [("fake-leg", DeclaredLaunch(script="leg.py"))],
    )

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "ran"
    assert "leg.py.cmd" not in captured.err


# ---------------------------------------------------------------------------
# AC6 — an undeclared drop-in is refused by name, never launched.
# ---------------------------------------------------------------------------


def test_undeclared_drop_in_refused_by_name_not_run_exit_zero(tmp_path, capsys):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    stray = root / "coordinator" / "bin" / "install-health" / "10-foo.sh"
    stray.write_text(f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n', encoding="utf-8")

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 0
    assert not marker.exists()
    assert (
        "[install-health] REFUSED: 10-foo.sh is not a declared leg -- "
        "declare it in install_health_run._NATIVE_LEGS" in captured.err
    )


# ---------------------------------------------------------------------------
# Ported CONTRACT tests: continue-past-failure, nonzero-exit-counts-one-
# failure, OSError-counts-one-failure — against monkeypatched `DeclaredLaunch`
# rows in `_NATIVE_LEGS` rather than real bash drop-ins (see module
# docstring for why the prior glob-driven versions were retired outright).
# ---------------------------------------------------------------------------


def test_declared_launch_nonzero_exit_counts_one_failure_and_loop_continues(tmp_path, capsys, monkeypatch):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    fail_script = root / "fail_leg.py"
    fail_script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    ok_script = root / "ok_leg.py"
    ok_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [
            ("fake-fail", DeclaredLaunch(script="fail_leg.py")),
            ("fake-ok", DeclaredLaunch(script="ok_leg.py")),
        ],
    )

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert marker.read_text(encoding="utf-8") == "ran"
    assert "[install-health] FAIL: fake-fail exit=1" in captured.err
    assert "1 health script(s) failed" in captured.err


def test_declared_launch_oserror_counts_one_failure_and_loop_continues(tmp_path, capsys, monkeypatch):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    ok_script = root / "ok_leg.py"
    ok_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        [
            ("fake-bad", DeclaredLaunch(script="missing_interpreter_leg.py")),
            ("fake-ok", DeclaredLaunch(script="ok_leg.py")),
        ],
    )

    real_call = install_health_run_module.subprocess.call

    def _fake_call(argv, **kwargs):
        if "missing_interpreter_leg.py" in argv[-1]:
            raise OSError("no such interpreter")
        return real_call(argv, **kwargs)

    monkeypatch.setattr(install_health_run_module.subprocess, "call", _fake_call)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: fake-bad" in captured.err
    assert marker.read_text(encoding="utf-8") == "ran"


# ---------------------------------------------------------------------------
# Regression: native in-process repoint (DR-079). seed-skill-overrides.sh has
# a direct claude-klabauter port and must be called IN-PROCESS — no subprocess spawn at
# all — rather than dispatched through resolve_by_shebang + subprocess.call.
# ---------------------------------------------------------------------------


def _touch_native_dropin(root: Path, basename: str) -> Path:
    """Create a placeholder drop-in file for one of the natively-repointed
    basenames. Its CONTENT is irrelevant — the loop must never read/execute
    this file's shebang or body for a basename in `_NATIVE_LEGS`; it exists
    only so the undeclared-drop-in refusal scan sees a matching stem on disk
    and correctly skips it (name/extension match only — the scan never reads
    permission bits, so no exec bit is set here)."""
    script = root / "coordinator" / "bin" / "install-health" / basename
    script.write_text("#!/usr/bin/env python3\nraise SystemExit(99)\n", encoding="utf-8")
    return script


def test_seed_skill_overrides_invoked_in_process_no_subprocess_spawn(tmp_path, capsys):
    root = _mk_root(tmp_path)
    _touch_native_dropin(root, "seed-skill-overrides.sh")

    with patch(
        "coordinator_core.ops.install_health_run.seed_skill_overrides.main",
        return_value=0,
    ) as mock_main, patch("subprocess.call") as mock_call:
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    mock_main.assert_called_once()
    mock_call.assert_not_called()


def test_native_entrypoint_nonzero_return_produces_fail_line_and_nonzero_exit(tmp_path, capsys):
    root = _mk_root(tmp_path)
    _touch_native_dropin(root, "seed-skill-overrides.sh")

    with patch(
        "coordinator_core.ops.install_health_run.seed_skill_overrides.main",
        return_value=1,
    ):
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: seed-skill-overrides exit=1" in captured.err
    assert "1 health script(s) failed" in captured.err


def test_native_entrypoint_exception_counts_one_failure_and_does_not_abort_loop(tmp_path, capsys, monkeypatch):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    _touch_native_dropin(root, "seed-skill-overrides.sh")
    ok_script = root / "ok_leg.py"
    ok_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    # Continuation proof is a declared `DeclaredLaunch` leg appended after the
    # real registry, not an undeclared drop-in -- an undeclared `.sh`/`.py`
    # file is REFUSED, never launched, under the declared-launch model this
    # file now tests (see `test_undeclared_drop_in_refused_by_name_not_run_
    # exit_zero`), so it can no longer stand in for "the loop continues".
    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        _NATIVE_LEGS + [("fake-ok", DeclaredLaunch(script="ok_leg.py"))],
    )

    with patch(
        "coordinator_core.ops.install_health_run.seed_skill_overrides.main",
        side_effect=RuntimeError("boom"),
    ):
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: seed-skill-overrides raised: boom" in captured.err
    assert marker.read_text(encoding="utf-8") == "ran"


# ---------------------------------------------------------------------------
# Regression: `_NATIVE_PROBES` decoupling (2026-07-22 fail-open fix).
#
# ensure-python3-exe-shim and check-windows-ssh-binary must run EVERY time
# main() is invoked, with NO dependency on any file existing under
# bin/install-health/ (glob-discovered or otherwise) — this is the actual
# defect: DoE deleted the two `.sh` siblings these probes were originally
# glob-discovered+basename-intercepted through, and the old code silently
# stopped running both with no error, no warning, no non-zero exit.
#
# This test class MUST fail against the pre-fix `_NATIVE_ENTRYPOINTS`-only
# dispatch (verified manually below — see report): with no drop-in file
# present under bin/install-health/, the old code's glob yielded nothing
# for these two basenames, so the interception clause never matched and
# the mocked native `main()` was never called.
# ---------------------------------------------------------------------------


def test_ensure_python3_exe_shim_runs_even_with_no_sh_sibling_present(tmp_path, capsys):
    """The regression guard: no `ensure-python3-exe-shim.sh` file anywhere —
    exactly DoE's post-deletion state — and the probe must still run."""
    root = _mk_root(tmp_path)  # bin/install-health/ exists but is EMPTY

    with patch(
        "coordinator_core.ops.install_health_run.ensure_python3_exe_shim.main",
        return_value=0,
    ) as mock_main, patch("subprocess.call") as mock_call:
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    mock_main.assert_called_once()
    mock_call.assert_not_called()


def test_check_windows_ssh_binary_runs_even_with_no_sh_sibling_present(tmp_path, capsys):
    """Same regression guard for the other deleted-sibling probe."""
    root = _mk_root(tmp_path)  # bin/install-health/ exists but is EMPTY

    with patch(
        "coordinator_core.ops.install_health_run.check_windows_ssh_binary.main",
        return_value=0,
    ) as mock_main, patch("subprocess.call") as mock_call:
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    mock_main.assert_called_once()
    mock_call.assert_not_called()


def test_native_probes_run_even_when_install_health_dir_is_entirely_absent(tmp_path, capsys):
    """Strongest form of the regression guard: `bin/install-health/` doesn't
    exist AT ALL (not just empty) — the pre-fix code's early
    `if not os.path.isdir(health_dir): return 0` returned before either
    probe could ever run. Both native probes must still fire."""
    root = tmp_path / "root"
    root.mkdir()  # no bin/install-health/ subdirectory at all

    with patch(
        "coordinator_core.ops.install_health_run.ensure_python3_exe_shim.main",
        return_value=0,
    ) as mock_ensure, patch(
        "coordinator_core.ops.install_health_run.check_windows_ssh_binary.main",
        return_value=0,
    ) as mock_check:
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    mock_ensure.assert_called_once()
    mock_check.assert_called_once()


def test_native_probe_nonzero_return_produces_fail_line_and_nonzero_exit(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()

    with patch(
        "coordinator_core.ops.install_health_run.ensure_python3_exe_shim.main",
        return_value=1,
    ), patch(
        "coordinator_core.ops.install_health_run.check_windows_ssh_binary.main",
        return_value=0,
    ):
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: ensure-python3-exe-shim exit=1" in captured.err
    assert "1 health script(s) failed" in captured.err


def test_native_probe_exception_counts_one_failure_and_does_not_abort(tmp_path, capsys, monkeypatch):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    ok_script = root / "ok_leg.py"
    ok_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    # Continuation proof is a declared `DeclaredLaunch` leg (see the sibling
    # `test_native_entrypoint_exception_counts_one_failure_and_does_not_abort_
    # loop`'s comment for why an undeclared drop-in no longer stands in here).
    monkeypatch.setattr(
        install_health_run_module,
        "_NATIVE_LEGS",
        _NATIVE_LEGS + [("fake-ok", DeclaredLaunch(script="ok_leg.py"))],
    )

    with patch(
        "coordinator_core.ops.install_health_run.ensure_python3_exe_shim.main",
        side_effect=RuntimeError("boom"),
    ), patch(
        "coordinator_core.ops.install_health_run.check_windows_ssh_binary.main",
        return_value=0,
    ):
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: ensure-python3-exe-shim raised: boom" in captured.err
    assert marker.read_text(encoding="utf-8") == "ran"


def test_reintroduced_native_probe_sh_sibling_does_not_double_run(tmp_path, capsys):
    """If a `ensure-python3-exe-shim.sh` (or `check-windows-ssh-binary.sh`)
    file were ever reintroduced in a drop-in directory, the glob loop must
    skip it — it already ran unconditionally above — rather than running it
    a second time via subprocess."""
    root = _mk_root(tmp_path)
    _touch_native_dropin(root, "ensure-python3-exe-shim.sh")

    with patch(
        "coordinator_core.ops.install_health_run.ensure_python3_exe_shim.main",
        return_value=0,
    ) as mock_main, patch("subprocess.call") as mock_call:
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    mock_main.assert_called_once()
    mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# check_bareword_path_provisioning (C3) direct coverage.
#
# Every test above runs through main() with this leg no-op'd out by the
# `_trust_opt_out` autouse fixture (it collides with those tests' own
# iteration-contract assertions, none of which are about this leg). These
# cases opt back IN by calling `check_bareword_path_provisioning` directly,
# bypassing the monkeypatch entirely, and assert the leg's own two
# differently-epistemic-status assertions (see the function's docstring):
# (a)/(c) the deterministic, fail-able rc-file + bin_dst + forwarder check,
# and (b) the informational-only, never-a-failure current-shell-PATH check.
# Skipped on native Windows, where the leg is an explicit no-op (POSIX rc
# files have no Windows equivalent) — asserting FAIL/NOTE text against that
# branch would be asserting the wrong contract, not exercising this one.
# ---------------------------------------------------------------------------


def _provision_bin_dst(home: Path) -> Path:
    """Build a fully-provisioned `settings-home/bin` under `home` via the
    REAL writer (`write_path_entry_guard_blocks`) — not hand-rolled sentinel
    text, per this leg's own "no second, independently-buggy sentinel-
    matching implementation" design note."""
    bin_dst = home / ".coordinator-claude-settings" / "bin"
    bin_dst.mkdir(parents=True)
    (bin_dst / "machine-local").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    write_path_entry_guard_blocks(
        path_entry=str(bin_dst),
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
    )
    return bin_dst


def _pin_home(monkeypatch, home: Path) -> None:
    """Pin HOME to an isolated dir and clear the two settings-home overrides
    so `settings_home()` resolves under it deterministically.

    Also opts back IN to real filesystem mutation for this test: the
    suite-wide `COORDINATOR_DISABLE_MACHINE_MUTATION=1` belt-and-braces
    fixture (`conftest.py::_quarantine_real_home`) refuses EVERY write
    through `write_path_entry_guard_blocks` unconditionally — including one
    correctly redirected into this `tmp_path`-rooted `home` — per
    `install/substrate.py::_refuse_machine_mutation`'s trigger 1, which
    applies "regardless of `check_temp_path`". Without this delenv, the
    real-writer helpers below (`_provision_bin_dst`, `_write_rc_block`)
    silently no-op and the C3 cases below see truthfully-reported-missing
    rc blocks that were never written, not a checker defect.
    `install/test_shell_rc_guard.py` (line 85) already establishes this
    delenv as the fix for the identical situation on the writer's own
    direct-coverage tests."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def _write_rc_block(home: Path, bin_dst: Path) -> None:
    """Write ONLY the SETTINGS_HOME_BIN rc guard block via the real writer
    (`write_path_entry_guard_blocks`) — no `bin_dst` filesystem state. Used
    by the narrowed C3 cases below to isolate the rc-block sub-check from
    the bin_dst-existence and known-forwarder sub-checks."""
    write_path_entry_guard_blocks(
        path_entry=str(bin_dst),
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_provisioned_and_on_path_exits_zero_no_note(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)
    bin_dst = _provision_bin_dst(home)
    monkeypatch.setenv("PATH", f"{bin_dst}{os.pathsep}{os.environ.get('PATH', '')}")

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 0
    assert "FAIL" not in captured.err
    assert "NOTE" not in captured.out


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_unprovisioned_home_fails(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 1
    assert "[bareword-path] FAIL" in captured.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_rc_block_present_bin_dst_missing_fails(tmp_path, monkeypatch, capsys):
    """Isolates the bin_dst-existence sub-check: with the rc block genuinely
    written but `bin_dst` never created, only the "does not exist" FAIL line
    may fire — a regression that silently disabled this ONE sub-check while
    the other two stayed live would otherwise be masked by the combined
    "start from nothing" case above."""
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)
    bin_dst = home / ".coordinator-claude-settings" / "bin"
    _write_rc_block(home, bin_dst)

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 1
    assert f"[bareword-path] FAIL: {bin_dst} does not exist" in captured.err
    assert "guard block missing" not in captured.err
    assert "forwarder" not in captured.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_bin_dst_present_missing_forwarder_fails(tmp_path, monkeypatch, capsys):
    """Isolates the known-forwarder sub-check: rc block written and
    `bin_dst` exists, but it is empty of the known `machine-local`
    forwarder — only the "no known forwarder" FAIL line may fire."""
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)
    bin_dst = home / ".coordinator-claude-settings" / "bin"
    bin_dst.mkdir(parents=True)
    _write_rc_block(home, bin_dst)

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 1
    assert (
        f"[bareword-path] FAIL: {bin_dst} exists but has no "
        f"{_BIN_DST_KNOWN_FORWARDER!r} forwarder" in captured.err
    )
    assert "guard block missing" not in captured.err
    assert "does not exist" not in captured.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_rc_block_missing_bin_dst_provisioned_fails(tmp_path, monkeypatch, capsys):
    """Isolates the rc-block-presence sub-check: `bin_dst` is fully
    provisioned (dir + known forwarder) but the rc guard block was never
    written — only the "guard block missing" FAIL line may fire."""
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)
    bin_dst = home / ".coordinator-claude-settings" / "bin"
    bin_dst.mkdir(parents=True)
    (bin_dst / _BIN_DST_KNOWN_FORWARDER).write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 1
    assert "[bareword-path] FAIL: SETTINGS_HOME_BIN guard block missing from:" in captured.err
    assert "does not exist" not in captured.err
    assert "forwarder" not in captured.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only leg; native Windows no-ops rc=0 unconditionally")
def test_check_bareword_path_provisioning_provisioned_but_not_on_path_is_note_not_failure(tmp_path, monkeypatch, capsys):
    """The case that fires on literally every real install: a freshly
    written rc block is by definition not yet active in the already-running
    install shell. Must be rc=0 with an informational NOTE, never a
    failure."""
    home = tmp_path / "home"
    home.mkdir()
    _pin_home(monkeypatch, home)
    _provision_bin_dst(home)
    monkeypatch.setenv("PATH", "/usr/bin")  # deliberately excludes bin_dst

    rc = check_bareword_path_provisioning("", "")
    captured = capsys.readouterr()

    assert rc == 0
    assert "FAIL" not in captured.err
    assert "NOTE" in captured.out


# ---------------------------------------------------------------------------
# check-door-provenance
# ---------------------------------------------------------------------------


def test_check_door_provenance_registered_in_native_legs():
    names = [name for name, _ in _NATIVE_LEGS]
    assert "check-door-provenance" in names


def _patch_verdict(monkeypatch, status, detail="detail"):
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.verify_installed_provenance",
        lambda bin_dst: ProvenanceVerdict(status, detail),
    )


def test_check_door_provenance_ok_exits_zero(monkeypatch, capsys):
    _patch_verdict(monkeypatch, "ok")
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "FAIL" not in captured.err


def test_check_door_provenance_mismatch_exits_one_with_remediation(monkeypatch, capsys):
    _patch_verdict(monkeypatch, "mismatch", "hash mismatch detail")
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 1
    assert "[door-provenance] FAIL" in captured.err
    assert "hash mismatch detail" in captured.err
    assert "python scripts/setup.py" in captured.err


def test_check_door_provenance_unrecorded_exits_zero_with_note(monkeypatch, capsys):
    _patch_verdict(monkeypatch, "unrecorded", "no image_sha256")
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.out


def test_check_door_provenance_absent_exits_one(monkeypatch, capsys):
    _patch_verdict(monkeypatch, "absent", "sidecar missing")
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 1
    assert "[door-provenance] FAIL" in captured.err


def test_check_door_provenance_no_door_exits_zero_with_note(monkeypatch, capsys):
    _patch_verdict(monkeypatch, "no-door", "no door binary")
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.out


def test_prebuilt_behind_its_sources_fails_with_a_rebuild_remediation(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.committed_prebuilt_source_drift",
        lambda: ["door.c"],
    )
    rc = install_health_run_module._report_prebuilt_currency()
    captured = capsys.readouterr()
    assert rc == 1
    assert "door.c" in captured.err
    assert "coordinator_core/warm/door/build.py" in captured.err


def test_current_prebuilt_reports_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.committed_prebuilt_source_drift",
        lambda: [],
    )
    assert install_health_run_module._report_prebuilt_currency() == 0
    assert capsys.readouterr() == ("", "")


# Review: coordinator-code-reviewer -- pin that check_door_provenance's own
# platform gate actually wires _report_prebuilt_currency in on Windows and
# leaves it out elsewhere, through the real entry point rather than by
# calling _report_prebuilt_currency() directly (which the two tests above
# do, bypassing the gate). Monkeypatches the file's own _is_windows()
# predicate, never sys.platform globally.
def test_check_door_provenance_windows_also_runs_prebuilt_currency_check(
    monkeypatch, capsys
):
    _patch_verdict(monkeypatch, "ok")
    monkeypatch.setattr(install_health_run_module, "_is_windows", lambda: True)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.committed_prebuilt_source_drift",
        lambda: ["door.c"],
    )
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 1
    assert "door.c" in captured.err


def test_check_door_provenance_non_windows_skips_prebuilt_currency_check(
    monkeypatch, capsys
):
    _patch_verdict(monkeypatch, "ok")
    monkeypatch.setattr(install_health_run_module, "_is_windows", lambda: False)

    def _boom():
        raise AssertionError("committed_prebuilt_source_drift must not run off-Windows")

    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.committed_prebuilt_source_drift",
        _boom,
    )
    rc = check_door_provenance("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "FAIL" not in captured.err


# ---------------------------------------------------------------------------
# check-door-route (C2)
# ---------------------------------------------------------------------------

_ROOT = Path("/root/klabauter")


def _patch_door_route_base(monkeypatch, *, door_installed=True):
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.is_door_installed",
        lambda bin_dst: door_installed,
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.resolve_engine_root_for_install",
        lambda: InstallEngineRoot(kind="published", root=_ROOT, remediation=None),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.expected_forwarders",
        lambda claude_klabauter_root: {"coordinator-invoke": "coordinator-invoke.py"},
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run._names_the_installer_gives_an_image",
        lambda expected_names, bin_dir: list(expected_names),
    )


def test_check_door_route_registered_in_native_legs():
    names = [name for name, _ in _NATIVE_LEGS]
    assert "check-door-route" in names
    # Deliberately AFTER check-door-provenance, BEFORE check-launch-chain-
    # intact -- the plan-row's own ordering constraint.
    assert names.index("check-door-provenance") < names.index("check-door-route") < names.index(
        "check-launch-chain-intact"
    )


def test_check_door_route_no_door_exits_zero_with_note(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch, door_installed=False)
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.out


def test_check_door_route_no_engine_root_exits_zero_with_discriminator_unavailable(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.resolve_engine_root_for_install",
        lambda: InstallEngineRoot(kind="none", root=None, remediation="no engine root"),
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "DISCRIMINATOR_UNAVAILABLE" in captured.out


def test_check_door_route_unresolved_and_cold_control_unresolved_is_discriminator_unavailable(
    monkeypatch, capsys
):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route",
        lambda door_path, op, *, repo_root, timeout: DoorRouteResult("unresolved", None),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.run_cold_control_invocation",
        lambda op, *, repo_root, params=None: DoorRouteResult("unresolved", None),
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "DISCRIMINATOR_UNAVAILABLE" in captured.out


def test_check_door_route_ac2_pins_same_repo_root_to_both_calls(monkeypatch):
    _patch_door_route_base(monkeypatch)
    seen: dict = {}

    def _read(door_path, op, *, repo_root, timeout):
        seen["read_repo_root"] = repo_root
        return DoorRouteResult("unresolved", None)

    def _cold(op, *, repo_root, params=None):
        seen["cold_repo_root"] = repo_root
        return DoorRouteResult("in_process", {})

    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route", _read
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.run_cold_control_invocation", _cold
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.audit_installed_image_currency",
        lambda bin_dst, names: ImageCurrencyAudit(current=list(names), stale=[]),
    )
    check_door_route("", "")
    assert seen["read_repo_root"] == _ROOT
    assert seen["cold_repo_root"] == _ROOT
    assert seen["read_repo_root"] == seen["cold_repo_root"]


def test_check_door_route_no_hardlink_divergence_exits_zero(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route",
        lambda door_path, op, *, repo_root, timeout: DoorRouteResult("warm_server", {"route": "warm_server"}),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.audit_installed_image_currency",
        lambda bin_dst, names: ImageCurrencyAudit(current=list(names), stale=[]),
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "FAIL" not in captured.err


def test_check_door_route_divergence_and_warmth_expected_exits_one(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route",
        lambda door_path, op, *, repo_root, timeout: DoorRouteResult("warm_server", {"route": "warm_server"}),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.audit_installed_image_currency",
        lambda bin_dst, names: ImageCurrencyAudit(current=[], stale=["age-sweep-lessons"]),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.is_warm_enabled", lambda: True
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.read_discovery_with_cause",
        lambda repo_root: ({"port": 1}, "record_present"),
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 1
    assert "[door-route] FAIL" in captured.err
    assert "age-sweep-lessons" in captured.err
    assert "python scripts/setup.py" in captured.err


def test_check_door_route_divergence_and_warmth_not_expected_exits_zero_with_note(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route",
        lambda door_path, op, *, repo_root, timeout: DoorRouteResult("warm_server", {"route": "warm_server"}),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.audit_installed_image_currency",
        lambda bin_dst, names: ImageCurrencyAudit(current=[], stale=["age-sweep-lessons"]),
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.is_warm_enabled", lambda: False
    )
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.read_discovery_with_cause",
        lambda repo_root: (None, "record_absent"),
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.out
    assert "FAIL" not in captured.err


def test_check_door_route_audit_error_exits_zero_with_discriminator_unavailable(monkeypatch, capsys):
    _patch_door_route_base(monkeypatch)
    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_route_signal.read_door_route",
        lambda door_path, op, *, repo_root, timeout: DoorRouteResult("warm_server", {"route": "warm_server"}),
    )

    def _raise(bin_dst, names):
        raise DoorInstallError("no readable prebuilt for this platform")

    monkeypatch.setattr(
        "coordinator_core.ops.install_health_run.door_install.audit_installed_image_currency", _raise
    )
    rc = check_door_route("", "")
    captured = capsys.readouterr()
    assert rc == 0
    assert "DISCRIMINATOR_UNAVAILABLE" in captured.out
