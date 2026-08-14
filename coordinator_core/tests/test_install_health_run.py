"""Characterization tests for coordinator_core.ops.install_health_run.

Ported test cases mirror the T1-T5 cases from DoE-claude's former
coordinator/bin/tests/test-install-health-run.sh, plus the trust-gate
contract from DoE-claude's former coordinator/lib/coordinator-
trusted-root-guard.sh's fail-loud mode, which this module reimplements.

Port of: install-health-run.sh (DoE 290997c7, 2026-07-22)
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core import launchable
from coordinator_core.install.shell_rc_guard import write_path_entry_guard_blocks
from coordinator_core.ops.install_health_run import (
    _BIN_DST_KNOWN_FORWARDER,
    _trusted_root,
    check_bareword_path_provisioning,
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
    # it deterministically via the CLAUDE_KLABAUTER_ROOT env var (Rung 1 of the
    # resolver's chain) rather than depend on this machine's real
    # machine-local registry having repos.claude_klabauter set. This is the
    # exact 3/13-on-the-PM's-machine exposure the 2026-07-22 kill-first memo
    # flagged: any test resolving claude-klabauter through the registry ladder is
    # environment-dependent by construction.
    # The residual glob-discovery directory (main()'s "hypothetical FUTURE
    # foreign drop-in" hook) resolves off coordinator_claude_klabauter_root() as
    # <claude_klabauter_root>/coordinator/bin/install-health/ — pin CLAUDE_KLABAUTER_ROOT to the
    # SAME `tmp_path / "root"` that `_mk_root()` below builds under, so a
    # drop-in written to `root / "coordinator" / "bin" / "install-health"`
    # is the directory main() actually globs, not an orphaned sibling tree.
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(tmp_path / "root"))
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
# T2: one always-succeeds script -> exit 0, stdout passthrough
# ---------------------------------------------------------------------------

def test_success_script_passthrough(tmp_path, capfd):
    root = _mk_root(tmp_path)
    script = root / "coordinator" / "bin" / "install-health" / "10-ok.sh"
    script.write_text('#!/usr/bin/env bash\necho "ran ok"\nexit 0\n', encoding="utf-8")
    os.chmod(script, 0o755)
    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capfd.readouterr()
    assert rc == 0
    assert "ran ok" in captured.out


# ---------------------------------------------------------------------------
# T3: fail + success -> exit non-zero, BOTH ran, failure named on stderr
# ---------------------------------------------------------------------------

def test_failure_does_not_abort_loop(tmp_path, capsys):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    fail_script = root / "coordinator" / "bin" / "install-health" / "10-fail.sh"
    ok_script = root / "coordinator" / "bin" / "install-health" / "20-ok.sh"
    fail_script.write_text('#!/usr/bin/env bash\necho "failing"\nexit 1\n', encoding="utf-8")
    ok_script.write_text(f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(fail_script, 0o755)
    os.chmod(ok_script, 0o755)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert marker.read_text(encoding="utf-8").count("ran\n") == 1
    assert "[install-health] FAIL: 10-fail.sh exit=1" in captured.err


# ---------------------------------------------------------------------------
# T4: silent no-op-skip script -> exit 0, no stderr noise
# ---------------------------------------------------------------------------

def test_silent_skip_no_noise(tmp_path, capsys):
    root = _mk_root(tmp_path)
    script = root / "coordinator" / "bin" / "install-health" / "10-skip.sh"
    script.write_text('#!/usr/bin/env bash\nexit 0\n', encoding="utf-8")
    os.chmod(script, 0o755)
    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()
    assert rc == 0
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
# Regression: shebang-driven interpreter resolution (2026-07-21 defect).
#
# A drop-in named `*.sh` may carry non-bash content — the `.sh` suffix
# exists only so the directory glob keeps finding it (see DoE-claude's
# seed-skill-overrides.sh, pure Python under a .sh name). A hardcoded
# `bash <script>` invocation dies on the script's first non-bash line.
# ---------------------------------------------------------------------------


def test_python_content_under_sh_suffix_runs_correctly(tmp_path, capsys):
    """The actual regression: a `.sh`-suffixed drop-in whose content is
    Python must be launched via the Python interpreter, not bash. This test
    MUST fail against a hardcoded `["bash", script]` invocation."""
    root = _mk_root(tmp_path)
    marker = root / "python.marker"
    script = root / "coordinator" / "bin" / "install-health" / "10-python.sh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "import sys\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 0, f"expected rc=0, got {rc}; stderr={captured.err!r}"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "ran"


def test_no_exec_bit_bash_drop_in_still_runs(tmp_path, capsys):
    """Contract guard, not a regression test for this diff: pins the
    exec-bit-independence guarantee (a `#!/usr/bin/env bash` drop-in with
    mode 0o644, no execute bit, must still run successfully — the script is
    passed as an argument to an explicit interpreter, never exec'd bare).
    Both the pre-fix hardcoded `["bash", script]` dispatch and the current
    shebang-resolved dispatch pass this test identically for a
    `#!/usr/bin/env bash` drop-in, so a future reader should not assume this
    test would catch a reintroduction of the shebang-resolution defect that
    `test_python_content_under_sh_suffix_runs_correctly` pins."""
    root = _mk_root(tmp_path)
    marker = root / "noexec.marker"
    script = root / "coordinator" / "bin" / "install-health" / "10-noexec.sh"
    script.write_text(
        f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o644)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 0, f"expected rc=0, got {rc}; stderr={captured.err!r}"
    assert marker.read_text(encoding="utf-8") == "ran\n"


def test_no_shebang_drop_in_still_runs_under_bash_fallback(tmp_path, capsys):
    root = _mk_root(tmp_path)
    marker = root / "noshebang.marker"
    script = root / "coordinator" / "bin" / "install-health" / "10-noshebang.sh"
    script.write_text(f'echo ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(script, 0o755)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 0, f"expected rc=0, got {rc}; stderr={captured.err!r}"
    assert marker.read_text(encoding="utf-8") == "ran\n"


# ---------------------------------------------------------------------------
# Regression: unresolvable interpreter must not abort the loop (Finding 1,
# 2026-07-21 code review of the shebang-resolve slice).
# ---------------------------------------------------------------------------


def test_unresolvable_interpreter_counts_one_failure_and_does_not_abort_loop(tmp_path, capsys):
    """A drop-in whose shebang names a guaranteed-absent interpreter must be
    counted as exactly one failure via subprocess.call's OSError, not raise
    out of the loop and abort the whole install-health run — this is the
    module's own documented 'continue past sub-script failures' contract."""
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    bad_script = root / "coordinator" / "bin" / "install-health" / "10-bad-interpreter.sh"
    ok_script = root / "coordinator" / "bin" / "install-health" / "20-ok.sh"
    bad_script.write_text(
        "#!/usr/bin/env definitely-not-a-real-interpreter-xyz\necho unreachable\n",
        encoding="utf-8",
    )
    ok_script.write_text(f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(bad_script, 0o755)
    os.chmod(ok_script, 0o755)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.err.count("[install-health] FAIL: 10-bad-interpreter.sh") == 1
    assert "1 health script(s) failed" in captured.err
    assert marker.read_text(encoding="utf-8") == "ran\n"


# ---------------------------------------------------------------------------
# Regression: main()'s Windows .cmd-twin branch (Finding 5, 2026-07-21 code
# review of the shebang-resolve slice).
# ---------------------------------------------------------------------------


def test_main_invokes_cmd_twin_alone_not_twin_plus_script(tmp_path, capsys, monkeypatch):
    """On Windows, when a `.cmd` twin exists next to a `.sh` drop-in,
    main()'s loop must invoke the twin alone (resolve_by_shebang's own
    complete-argv convention), not [twin, script]."""
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    script = root / "coordinator" / "bin" / "install-health" / "10-twin.sh"
    twin = root / "coordinator" / "bin" / "install-health" / "10-twin.sh.cmd"
    script.write_text("#!/usr/bin/env bash\necho should-not-run\n", encoding="utf-8")
    twin.write_text(f'echo ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(script, 0o755)

    calls = []
    import coordinator_core.ops.install_health_run as install_health_run_module

    def _fake_call(argv, **_spawn_kwargs):
        # **_spawn_kwargs: the call site passes no-console creationflags plus
        # explicit std-handle fds (see `_leg_spawn_kwargs`); this test pins the
        # argv, not the spawn plumbing.
        calls.append(argv)
        return 0

    monkeypatch.setattr(install_health_run_module.subprocess, "call", _fake_call)

    rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))

    assert rc == 0
    assert len(calls) == 1
    assert calls[0] == [str(twin)]


# ---------------------------------------------------------------------------
# Regression: native in-process repoint (DR-079). seed-skill-overrides.sh has
# a direct claude-klabauter port and must be called IN-PROCESS — no subprocess spawn at
# all — rather than dispatched through resolve_by_shebang + subprocess.call.
# ---------------------------------------------------------------------------


def _touch_native_dropin(root: Path, basename: str) -> Path:
    """Create a placeholder drop-in file for one of the natively-repointed
    basenames. Its CONTENT is irrelevant — the loop must never read/execute
    this file's shebang or body for a basename in `_NATIVE_ENTRYPOINTS`; it
    exists only so glob() finds a matching path to dispatch on."""
    script = root / "coordinator" / "bin" / "install-health" / basename
    script.write_text("#!/usr/bin/env python3\nraise SystemExit(99)\n", encoding="utf-8")
    os.chmod(script, 0o755)
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


def test_native_entrypoint_exception_counts_one_failure_and_does_not_abort_loop(tmp_path, capsys):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    _touch_native_dropin(root, "seed-skill-overrides.sh")
    ok_script = root / "coordinator" / "bin" / "install-health" / "20-ok.sh"
    ok_script.write_text(f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(ok_script, 0o755)

    with patch(
        "coordinator_core.ops.install_health_run.seed_skill_overrides.main",
        side_effect=RuntimeError("boom"),
    ):
        rc = main([], script_path=str(root / "bin" / "install-health-run.sh"))
    captured = capsys.readouterr()

    assert rc == 1
    assert "[install-health] FAIL: seed-skill-overrides raised: boom" in captured.err
    assert marker.read_text(encoding="utf-8") == "ran\n"


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


def test_native_probe_exception_counts_one_failure_and_does_not_abort(tmp_path, capsys):
    root = _mk_root(tmp_path)
    marker = root / "ran.marker"
    ok_script = root / "coordinator" / "bin" / "install-health" / "20-ok.sh"
    ok_script.write_text(f'#!/usr/bin/env bash\necho ran >> "{marker}"\nexit 0\n', encoding="utf-8")
    os.chmod(ok_script, 0o755)

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
    assert marker.read_text(encoding="utf-8") == "ran\n"


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
    so `settings_home()` resolves under it deterministically."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


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
    assert "not yet on PATH" in captured.out
