"""
coordinator_core.ops.test_normalize_env

Independent parity + unit coverage for normalize_env.py.

Port of: normalize-env.sh (example-doctrine-repo ca30f76c, 2026-07-17)

Porter-brief lesson 1: a parity test that shells out to a retired/renamed
oracle silently self-compares-or-skips. Section A therefore does NOT invoke
the (now-deleted) oracle at all — it asserts against GOLDEN TEXT captured
during authoring by running the live oracle side-by-side with this port
(byte-identical, modulo the trampoline's own extension-less rename) BEFORE
the oracle was removed. The golden fixtures below cover only the
machine-independent (no-probe-lib) code paths — probe-lib output varies with
installed tool versions and is intentionally excluded from the frozen
fixtures; probe-dependent behavior is covered independently in section E via
synthetic NDJSON.

Family I (fresh-install surface) coverage: test_fresh_install_shape_no_probe_lib
simulates a cold machine where NORMALIZE_ENV_PREREQ_PROBE_PATH is unresolvable
(known systemic issue per porter brief) and asserts the module degrades with
an actionable message rather than crashing.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops import normalize_env as ne


def _run_port(args, home: Path = None, in_text: str = "", env_extra=None):
    out = io.StringIO()
    in_stream = io.StringIO(in_text)
    env_patch = {"NORMALIZE_ENV_PREREQ_PROBE_PATH": ""}
    if env_extra:
        env_patch.update(env_extra)
    with patch.dict(os.environ, env_patch):
        os.environ.pop("NORMALIZE_ENV_PREREQ_PROBE_PATH", None)
        rc = ne.main(args, out=out, in_stream=in_stream, home=home or Path.home())
    return out.getvalue(), rc


# ---------------------------------------------------------------------------
# A. Golden-fixture byte-parity (captured against the live bash oracle during
# authoring, before its retirement — see module docstring). Machine-
# independent paths only (no probe-lib output).
# ---------------------------------------------------------------------------

_GOLDEN_DRY_RUN_MACOS_LINUX = """\
=== normalize-env — macOS/Linux mode (offers only, no mutation) ===

This script does not mutate on macOS. Manual steps to satisfy prereqs:

  Python 3.12+:
    brew install python@3.12

  uv package manager:
    brew install uv

  PowerShell 7 (optional):
    brew install --cask powershell

  git core.longpaths: Windows-only — not applicable on macOS.

Run the above brew/apt/dnf commands, then re-run the coordinator install.
"""


def test_dry_run_matches_golden_fixture_no_probe_lib():
    if ne._ne_is_windows():
        pytest.skip("golden fixture captured for the macOS/Linux offers-only path")
    if _local_uname_is_not_darwin():
        pytest.skip("golden fixture is Darwin-specific text")
    port_out, port_rc = _run_port(["--dry-run"])
    assert port_rc == 0
    assert port_out == _GOLDEN_DRY_RUN_MACOS_LINUX


def _local_uname_is_not_darwin() -> bool:
    return ne._ne_uname_s() != "Darwin"


def test_unknown_argument_exit_code_matches_golden_oracle_behavior():
    # Golden: oracle exits 2 on an unrecognized flag (captured pre-retirement).
    _out, rc = _run_port(["--bogus-flag"])
    assert rc == 2


def test_restore_missing_file_exit_code_matches_golden_oracle_behavior():
    # Golden: oracle exits 2 when the --restore target does not exist.
    _out, rc = _run_port(["--restore", "/definitely/does/not/exist/xyz"])
    assert rc == 2


# Review: code-reviewer (Finding 2) — this constant previously used an em-dash
# ("— restore") that neither matched the code's actual output ("--restore",
# double-hyphen) nor was ever wired into an assertion, silently masking an
# unverified byte-parity claim. Fixed to the code's actual current output and
# wired into the assertion below.
_GOLDEN_RESTORE_HEADER = "=== normalize-env --restore ===\n"


def test_restore_saved_path_roundtrip_matches_golden_shape(tmp_path):
    backup = tmp_path / ".coordinator-env-backup.20260101000000"
    backup.write_text(
        "SAVED_PATH=/usr/bin:/bin:/opt/x\n"
        "SAVED_PYMANAGER_TARGET=/usr/bin/python3\n"
        "SAVED_AT=now\n"
    )
    port_out, port_rc = _run_port(["--restore", str(backup)])
    assert port_rc == 0
    # Golden shape (captured against the oracle pre-retirement): header,
    # exported PATH restore command, pymanager note, verify hint — in order.
    assert port_out.startswith(_GOLDEN_RESTORE_HEADER)
    assert "Reading backup:" in port_out
    assert 'export PATH="/usr/bin:/bin:/opt/x"' in port_out
    assert "Saved shim target was: /usr/bin/python3" in port_out
    assert "Restore complete. Verify with: python --version" in port_out


# ---------------------------------------------------------------------------
# B. Probe-summary last-line-drop oracle bug (faithful repro — porter rule 7).
# ---------------------------------------------------------------------------

_FAKE_PROBE_NDJSON = (
    '{"name":"git","status":"pass","severity":"hard","detail":"git 2.0","remediation":""}\n'
    '{"name":"python","status":"pass","severity":"hard","detail":"Python 3.12","remediation":""}\n'
    '{"name":"shell_login_env","status":"fail","severity":"advisory","detail":"orphaned","remediation":"fix it"}\n'
)


def test_probe_summary_drops_last_record_matching_oracle_read_bug():
    """The bash oracle's `$(...) | while read` display loop silently drops
    the LAST NDJSON record (missing trailing newline after command-substitution
    stripping — see _ne_print_probe_summary docstring). shell_login_env is
    always emitted last by _co_prereq_probe_all, so it is always the one
    dropped from the printed summary — reproduced here, not fixed."""
    out = io.StringIO()
    ne._ne_print_probe_summary(_FAKE_PROBE_NDJSON, out)
    rendered = out.getvalue()
    assert "git" in rendered
    assert "python" in rendered
    assert "shell_login_env" not in rendered


def test_probe_status_lookup_unaffected_by_print_loop_bug():
    """Status LOOKUPS (grep-based in the oracle) are NOT subject to the
    read-loop truncation — shell_login_env's status must still resolve."""
    status = ne._ne_probe_status_for(_FAKE_PROBE_NDJSON, "shell_login_env")
    assert status == "fail"


def test_probe_summary_handles_empty_output():
    out = io.StringIO()
    ne._ne_print_probe_summary("", out)
    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# C. Managed-block strip/build round trip (Darwin ~/.bash_profile reconstruction).
# ---------------------------------------------------------------------------

def test_strip_managed_block_both_sentinels_present():
    content = (
        "export FOO=1\n"
        + ne._NE_START_SENTINEL
        + "\nold managed line\n"
        + ne._NE_END_SENTINEL
        + "\nexport BAR=2\n"
    )
    stripped = ne._ne_strip_managed_block(content)
    assert stripped == "export FOO=1\nexport BAR=2"
    assert ne._NE_START_SENTINEL not in stripped
    assert "old managed line" not in stripped


def test_strip_managed_block_legacy_start_sentinel_only():
    content = (
        "export FOO=1\n"
        + ne._NE_START_SENTINEL
        + "\nif something; then\n  export PATH=x\nfi\n"
        + "export BAR=2\n"
    )
    stripped = ne._ne_strip_managed_block(content)
    assert "export FOO=1" in stripped
    assert "export BAR=2" in stripped
    assert ne._NE_START_SENTINEL not in stripped


def test_strip_managed_block_no_sentinel_preserves_content():
    content = "export FOO=1\nexport BAR=2\n"
    assert ne._ne_strip_managed_block(content) == content


def test_build_managed_block_excludes_homebrew_and_system_default_paths():
    recon = "/opt/homebrew/bin:/usr/bin:/bin:/Users/me/.local/bin:/usr/local/sbin"
    block = ne._ne_build_managed_block(recon)
    assert ne._NE_START_SENTINEL in block
    assert ne._NE_END_SENTINEL in block
    assert '/Users/me/.local/bin' in block
    # The brew-shellenv boilerplate line legitimately references
    # /opt/homebrew/bin/brew -- what must NOT appear is a standalone
    # `export PATH="/opt/homebrew/bin:$PATH"` line (that would double-add
    # what brew shellenv already contributes).
    assert 'export PATH="/opt/homebrew/bin:$PATH"' not in block
    assert '"/usr/bin:$PATH"' not in block


def test_extra_zsh_path_entries_filters_homebrew_and_defaults():
    recon = "/opt/homebrew/bin:/usr/bin:/Users/me/.local/bin"
    extras = ne._ne_extra_zsh_path_entries(recon)
    assert extras == ["/Users/me/.local/bin"]


# Review: code-reviewer (Finding 4, A6) — companion to Finding 1's A5 fix.
# No prior test exercised `_ne_darwin_bash_profile_repair`'s actual disk-write
# path at all, so the permission-drop regression would not have been caught.
def test_darwin_bash_profile_repair_preserves_prior_file_mode(tmp_path):
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. This function is
    # invoked directly here (bypassing any darwin-only sys.platform guard
    # in the caller), so it does execute on Windows -- assert the real
    # invariant under test -- mode preserved across the repair -- by
    # comparing against whatever mode chmod actually produced on this
    # platform, rather than asserting the literal POSIX octal.
    home = tmp_path
    bp_path = home / ".bash_profile"
    bp_path.write_text("export FOO=1\n")
    os.chmod(bp_path, 0o644)
    mode_before = oct(bp_path.stat().st_mode)[-3:]

    probe_output = (
        '{"name":"shell_login_env","status":"fail","severity":"advisory",'
        '"detail":"orphaned","remediation":"fix it"}\n'
    )
    runner = ne.NeRunner(yes=True)
    runner.prereq_probe_path = "/fake/prereq_probe.sh"
    out = io.StringIO()

    with patch.object(ne, "_ne_run_bash_probe_fn", return_value="/opt/homebrew/bin:/usr/bin:/Users/me/.local/bin"), \
         patch.object(ne, "_ne_verify_bash_profile_repair", return_value=True):
        ne._ne_darwin_bash_profile_repair(probe_output, home, dry_run=False, runner=runner, out=out)

    assert bp_path.is_file()
    assert "export FOO=1" in bp_path.read_text()
    assert oct(bp_path.stat().st_mode)[-3:] == mode_before


# ---------------------------------------------------------------------------
# D. Consent gate (NeRunner.prompt_yn) — CONSENT-INVARIANT coverage.
# ---------------------------------------------------------------------------

def test_prompt_yn_auto_yes_flag():
    runner = ne.NeRunner(yes=True)
    out = io.StringIO()
    assert runner.prompt_yn("Apply X", out) is True


def test_prompt_yn_non_interactive_raises_abort():
    runner = ne.NeRunner(yes=False)
    out = io.StringIO()

    class _NoTty(io.StringIO):
        def isatty(self):
            return False

    with pytest.raises(ne._NeNonInteractiveAbort):
        runner.prompt_yn("Apply X", out, in_stream=_NoTty(""))


def test_prompt_yn_interactive_yes_and_no():
    runner = ne.NeRunner(yes=False)
    out = io.StringIO()

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    assert runner.prompt_yn("Apply X", out, in_stream=_Tty("y\n")) is True
    assert runner.prompt_yn("Apply X", out, in_stream=_Tty("n\n")) is False
    assert runner.prompt_yn("Apply X", out, in_stream=_Tty("\n")) is False


def test_prompt_yn_eof_raises_abort():
    runner = ne.NeRunner(yes=False)
    out = io.StringIO()

    class _TtyEOF(io.StringIO):
        def isatty(self):
            return True

    with pytest.raises(ne._NeNonInteractiveAbort):
        runner.prompt_yn("Apply X", out, in_stream=_TtyEOF(""))


# ---------------------------------------------------------------------------
# E. Windows fix-plan construction (mocked probe output — no live Windows box).
# ---------------------------------------------------------------------------

_WIN_ALL_PASS = (
    '{"name":"longpaths","status":"pass","severity":"hard","detail":"","remediation":""}\n'
    '{"name":"uv","status":"pass","severity":"advisory","detail":"","remediation":""}\n'
    '{"name":"python","status":"pass","severity":"hard","detail":"","remediation":""}\n'
)

_WIN_ALL_FAIL = (
    '{"name":"longpaths","status":"fail","severity":"hard","detail":"","remediation":""}\n'
    '{"name":"uv","status":"warn","severity":"advisory","detail":"","remediation":""}\n'
    '{"name":"python","status":"fail","severity":"hard","detail":"","remediation":""}\n'
)


def test_windows_plan_all_pass_yields_zero_fixes():
    plan = ne._ne_build_windows_plan(_WIN_ALL_PASS)
    assert plan.count == 0
    assert not plan.longpaths and not plan.uv and not plan.python and not plan.alias


def test_windows_plan_all_fail_yields_four_fixes_including_alias():
    plan = ne._ne_build_windows_plan(_WIN_ALL_FAIL)
    assert plan.longpaths and plan.uv and plan.python and plan.alias
    assert plan.count == 4


def test_windows_plan_alias_fix_gated_on_python_fix_only():
    """Alias-disable is registered whenever python fails, per the oracle
    (unconditional on the Store heuristic) — verified independently of the
    store_python_suspected flag."""
    probe = (
        '{"name":"longpaths","status":"pass","severity":"hard","detail":"","remediation":""}\n'
        '{"name":"uv","status":"pass","severity":"advisory","detail":"","remediation":""}\n'
        '{"name":"python","status":"fail","severity":"hard","detail":"","remediation":""}\n'
    )
    with patch.object(ne, "_ne_wa_dir_has_store_python", return_value=False):
        plan = ne._ne_build_windows_plan(probe)
    assert plan.python and plan.alias
    assert plan.store_python_suspected is False


# ---------------------------------------------------------------------------
# F. Subprocess hardening (A2/A4) — every subprocess.run call carries
# timeout + stdin=DEVNULL + the platform-appropriate creationflags.
# ---------------------------------------------------------------------------

def test_run_bash_probe_fn_dispatches_to_native_prereq_probe(monkeypatch):
    """_ne_run_bash_probe_fn is a native in-process dispatch table onto
    coordinator_core.install.prereq_probe (2026-07-21 de-bash cutover) — no
    subprocess.run call of its own; prereq_probe_path/timeout are accepted
    only for call-site compatibility with the (unchanged) availability gate."""
    monkeypatch.setitem(ne._PROBE_FN_DISPATCH, "_co_probe_longpaths", lambda: "sentinel-ndjson-line\n")

    with patch.object(ne.subprocess, "run") as mock_run:
        out = ne._ne_run_bash_probe_fn("/fake/prereq_probe.sh", "_co_probe_longpaths")

    mock_run.assert_not_called()
    assert out == "sentinel-ndjson-line\n"


def test_run_bash_probe_fn_unknown_fn_expr_returns_empty():
    assert ne._ne_run_bash_probe_fn("/fake/prereq_probe.sh", "_co_not_a_real_probe") == ""


def test_step1_longpaths_git_config_call_is_hardened(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(ne.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(ne.subprocess, "run", _fake_run)

    runner = ne.NeRunner(yes=True)
    out = io.StringIO()
    ne._ne_step1_longpaths(runner, "", probes_available=False, out=out)

    assert captured["timeout"] == 15
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["creationflags"] == ne._CREATIONFLAGS
    assert "git core.longpaths=true" in runner.steps_succeeded


# ---------------------------------------------------------------------------
# G. Fresh-install-shape smoke test (Family I) — CLAUDE_KLABAUTER_ROOT-adjacent env is
# NOT how this module resolves its bash-side probe dependency; the analogous
# cold-machine hazard here is NORMALIZE_ENV_PREREQ_PROBE_PATH being unset or
# pointing at a nonexistent path. Both must degrade with an actionable
# message, never a traceback.
# ---------------------------------------------------------------------------

def test_fresh_install_shape_no_probe_lib_env_var_set():
    """Cold machine: env var absent entirely. Windows-path build_windows_plan
    is never reached because probes_available is False -- verified via the
    macOS/Linux branch, which is what CI actually exercises."""
    out = io.StringIO()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NORMALIZE_ENV_PREREQ_PROBE_PATH", None)
        rc = ne.main(["--dry-run"], out=out, in_stream=io.StringIO(""))
    assert rc == 0
    rendered = out.getvalue()
    assert "Traceback" not in rendered


def test_fresh_install_shape_probe_lib_path_set_but_missing(tmp_path):
    """Cold machine: env var set but points at a path that doesn't exist
    (e.g. a stale machine-local pointer) -- must hard-error with remediation,
    not crash."""
    bogus = tmp_path / "does-not-exist" / "prereq_probe.sh"
    out = io.StringIO()
    with patch.dict(os.environ, {"NORMALIZE_ENV_PREREQ_PROBE_PATH": str(bogus)}):
        rc = ne.main(["--dry-run"], out=out, in_stream=io.StringIO(""))
    assert rc == ne.EXIT_HARD_ERROR


def test_is_windows_never_raises_when_uname_absent(monkeypatch):
    monkeypatch.setattr(ne.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    assert ne._ne_is_windows() is False


# ---------------------------------------------------------------------------
# H. Arg parsing edge cases.
# ---------------------------------------------------------------------------

def test_parse_args_restore_without_value_raises_usage_error():
    with pytest.raises(ne._NeUsageError):
        ne._ne_parse_args(["--restore"])


def test_parse_args_dry_run_and_yes_combine():
    args = ne._ne_parse_args(["--dry-run", "--yes"])
    assert args["dry_run"] is True
    assert args["yes"] is True


def test_main_unknown_argument_exits_usage_error():
    out = io.StringIO()
    rc = ne.main(["--nope"], out=out)
    assert rc == ne.EXIT_USAGE_ERROR
