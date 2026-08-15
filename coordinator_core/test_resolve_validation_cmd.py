"""
test_resolve_validation_cmd.py — pytest coverage for
coordinator_core.resolve_validation_cmd.

Port of: coordinator-resolve-validation-cmd.sh (DoE c187f5b9, 2026-07-21)
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

import pytest

from coordinator_core.resolve_validation_cmd import (
    FAST_TEST_CMD_ENV,
    FULL_TEST_CMD_ENV,
    SUPPRESS_METACHAR_WARN_ENV,
    NoPythonInterpreterError,
    ResolvedCommand,
    cs_read_local_md_key,
    cs_resolve_fast_test_cmd,
    cs_resolve_full_test_cmd,
    metachar_warn,
    normalize_python_token,
    redact_for_diag,
    resolve_python_interp,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (FAST_TEST_CMD_ENV, FULL_TEST_CMD_ENV, SUPPRESS_METACHAR_WARN_ENV):
        monkeypatch.delenv(var, raising=False)


# --- Unit 1: helpers ----------------------------------------------------------


def test_redact_for_diag_short_passthrough():
    assert redact_for_diag("pytest") == "pytest"


def test_redact_for_diag_truncates_past_60_chars():
    s = "x" * 80
    out = redact_for_diag(s)
    assert out.startswith("x" * 60)
    assert "…(truncated; 80 chars total)" in out


def test_redact_for_diag_boundary_exactly_60_not_truncated():
    s = "x" * 60
    assert redact_for_diag(s) == s


def test_metachar_warn_fires_on_dollar_paren(capsys):
    metachar_warn("echo $(whoami)", "env-var")
    err = capsys.readouterr().err
    assert "WARN (step=env-var)" in err
    assert "cs_resolve_fast_test_cmd" in err


def test_metachar_warn_fires_on_backtick(capsys):
    metachar_warn("echo `whoami`", "local-md")
    assert "WARN" in capsys.readouterr().err


def test_metachar_warn_fires_on_semicolon_space(capsys):
    metachar_warn("pytest; rm -rf /", "env-var")
    assert "WARN" in capsys.readouterr().err


def test_metachar_warn_silent_on_clean_command(capsys):
    metachar_warn("pnpm test", "env-var")
    assert capsys.readouterr().err == ""


def test_metachar_warn_suppressed_via_env(monkeypatch, capsys):
    monkeypatch.setenv(SUPPRESS_METACHAR_WARN_ENV, "1")
    metachar_warn("echo $(whoami)", "env-var")
    assert capsys.readouterr().err == ""


def test_metachar_warn_custom_caller_label(capsys):
    metachar_warn("echo $(x)", "env-var", "cs_resolve_full_test_cmd")
    assert "[cs_resolve_full_test_cmd]" in capsys.readouterr().err


def test_resolve_python_interp_prefers_python3(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "python3" else None,
    )
    assert resolve_python_interp() == "python3"


def test_resolve_python_interp_falls_back_to_python(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.shutil.which",
        lambda name: "/usr/bin/python" if name == "python" else None,
    )
    assert resolve_python_interp() == "python"


def test_resolve_python_interp_none_when_neither_present(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.shutil.which", lambda name: None
    )
    assert resolve_python_interp() is None


def test_normalize_python_token_bare_python_no_args(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp",
        lambda: "python3",
    )
    assert normalize_python_token("python") == "python3"


def test_normalize_python_token_bare_python_with_args(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp",
        lambda: "python3",
    )
    assert (
        normalize_python_token("python .github/scripts/run-all-checks.py")
        == "python3 .github/scripts/run-all-checks.py"
    )


def test_normalize_python_token_does_not_match_python3():
    assert normalize_python_token("python3 -m pytest") == "python3 -m pytest"


def test_normalize_python_token_does_not_match_python2():
    assert normalize_python_token("python2 foo.py") == "python2 foo.py"


def test_normalize_python_token_does_not_match_explicit_path():
    assert normalize_python_token("/usr/bin/python foo.py") == "/usr/bin/python foo.py"


def test_normalize_python_token_does_not_match_env_prefixed():
    assert normalize_python_token("FOO=1 python foo.py") == "FOO=1 python foo.py"


def test_normalize_python_token_passthrough_non_python():
    assert normalize_python_token("pnpm test") == "pnpm test"


def test_normalize_python_token_raises_when_no_interpreter(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp", lambda: None
    )
    with pytest.raises(NoPythonInterpreterError):
        normalize_python_token("python foo.py")
    assert "no python3/python on PATH" in capsys.readouterr().err


# --- Unit 2: cs_resolve_fast_test_cmd ------------------------------------------


def test_fast_env_var_wins_over_local_md(monkeypatch, tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest local\n---\n"
    )
    monkeypatch.setenv(FAST_TEST_CMD_ENV, "pytest env")
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest env", 0)


def test_fast_local_md_resolution(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest -x tests/\n---\n"
    )
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest -x tests/", 0)


def test_fast_local_md_preserves_internal_whitespace_strips_quotes(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        '---\nfast_test_cmd: "pytest  -x  tests/"\n---\n'
    )
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    # _strip_wrapping_quotes removes only the ONE outer wrapping quote pair;
    # internal double-spacing survives untouched (no interior quotes here, so
    # this case is unaffected by the wrapping-pair-only fix).
    assert result.cmd == "pytest  -x  tests/"
    assert result.exit_code == 0


def test_fast_local_md_preserves_interior_single_quoted_marker_expression(tmp_path):
    """Regression: a quoted multi-word marker-selector argument (the
    coordinator.local.md shape from the BREAK-CLASS bug this pins) must
    survive resolution intact, as ONE shell-executable argument — not word-
    split by a naive "strip every quote character" pass."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: \"python3 -m pytest -m 'not cadence and not "
        "pending_fix and not designed_red'\"\n---\n"
    )
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result.exit_code == 0
    assert (
        result.cmd
        == "python3 -m pytest -m 'not cadence and not pending_fix and not designed_red'"
    )
    # The resolved string must remain shell-executable through `sh -c`: the
    # marker expression is one argv token, not four.
    argv = shlex.split(result.cmd)
    assert argv[:5] == ["python3", "-m", "pytest", "-m", "not cadence and not pending_fix and not designed_red"]


def test_full_local_md_preserves_interior_single_quoted_marker_expression(tmp_path):
    """Same regression for the FULL tier / cs_read_local_md_key path — a
    distinct implementation from the fast tier's inline parser, and the one
    that previously corrupted this shape differently (an unbalanced trailing
    quote from independent, un-paired leading/trailing strips)."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfull_test_cmd: \"python3 -m pytest -m 'not pending_fix and "
        "not designed_red'\"\n---\n"
    )
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result.exit_code == 0
    assert (
        result.cmd
        == "python3 -m pytest -m 'not pending_fix and not designed_red'"
    )
    argv = shlex.split(result.cmd)
    assert argv == ["python3", "-m", "pytest", "-m", "not pending_fix and not designed_red"]


def test_fast_local_md_marker_expression_survives_a_real_shell(tmp_path):
    """End-to-end: resolve, then hand the resolved string to a real POSIX
    shell (as validate-fast-and-packageability.py's `bash -c` caller does)
    via `sh -c '<resolved> --collect-only'` against a script that just
    echoes argv, confirming the marker expression arrives as ONE argument
    rather than being word-split into stray positional path args (the
    corrupted-shape symptom: pytest exits 4, "file or directory not found:
    cadence")."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: \"python3 -m pytest -m 'not cadence and not "
        "pending_fix and not designed_red'\"\n---\n"
    )
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result.exit_code == 0
    echo_argv = tmp_path / "echo_argv.py"
    echo_argv.write_text("import sys, json; print(json.dumps(sys.argv[1:]))\n")
    # Windows: tmp_path renders with backslashes, and interpolating that raw
    # into a string handed to `sh -c` is fatal -- outside quotes, POSIX sh
    # treats `\U`, `\A`, etc. as escape sequences and strips the backslash,
    # mangling the path (and, since this cmd's cwd differs, resolving it
    # against the WRONG directory entirely). Forward slashes are accepted by
    # both Windows Python and POSIX sh, and shlex.quote guards embedded
    # spaces/metacharacters -- same fix shape on POSIX, where it's a no-op.
    echo_argv_posix = str(echo_argv).replace("\\", "/")
    shell_cmd = result.cmd.replace(
        "python3 -m pytest", f"python3 {shlex.quote(echo_argv_posix)}", 1
    )
    proc = subprocess.run(["sh", "-c", shell_cmd], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    argv = json.loads(proc.stdout)
    marker_idx = argv.index("-m")
    assert argv[marker_idx + 1] == (
        "not cadence and not pending_fix and not designed_red"
    )


def test_check_escape_residue_raises_on_unresolvable_escaped_quote(monkeypatch):
    monkeypatch.setenv(FAST_TEST_CMD_ENV, 'pytest -m \\"not slow\\"')
    result = cs_resolve_fast_test_cmd("/nonexistent")
    assert result == ResolvedCommand(None, 126)


def test_full_check_escape_residue_raises_on_unresolvable_escaped_quote(monkeypatch):
    monkeypatch.setenv(FULL_TEST_CMD_ENV, 'pytest -m \\"not slow\\"')
    result = cs_resolve_full_test_cmd("/nonexistent")
    assert result == ResolvedCommand(None, 126)


def test_fast_skip_when_unconfigured(tmp_path, capsys):
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result == ResolvedCommand(None, 2)
    err = capsys.readouterr().err
    assert "step=skipped" in err
    assert "COORDINATOR_FAST_TEST_CMD" in err
    assert "fast_test_cmd" in err


def test_fast_normalizes_bare_python_from_env(monkeypatch):
    monkeypatch.setenv(FAST_TEST_CMD_ENV, "python run.py")
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp",
        lambda: "python3",
    )
    result = cs_resolve_fast_test_cmd("/nonexistent")
    assert result == ResolvedCommand("python3 run.py", 0)


def test_fast_exit_127_when_bare_python_and_no_interpreter(monkeypatch):
    monkeypatch.setenv(FAST_TEST_CMD_ENV, "python")
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp", lambda: None
    )
    result = cs_resolve_fast_test_cmd("/nonexistent")
    assert result == ResolvedCommand(None, 127)


def test_fast_ignores_local_md_missing_fast_test_cmd_key(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nfull_test_cmd: pytest\n---\n")
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result == ResolvedCommand(None, 2)


def test_fast_only_reads_between_first_and_second_marker(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "preamble\nfast_test_cmd: should-not-count\n"
        "---\nfast_test_cmd: pytest tests/\n---\nfast_test_cmd: should-not-count-either\n"
    )
    result = cs_resolve_fast_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest tests/", 0)


# --- Unit 2: cs_read_local_md_key ----------------------------------------------


def test_read_local_md_key_basic(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nfull_test_cmd: pytest -q\n---\n")
    assert cs_read_local_md_key(str(tmp_path), "full_test_cmd") == "pytest -q"


def test_read_local_md_key_missing_file(tmp_path):
    assert cs_read_local_md_key(str(tmp_path), "full_test_cmd") == ""


def test_read_local_md_key_missing_key(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nfast_test_cmd: pytest\n---\n")
    assert cs_read_local_md_key(str(tmp_path), "full_test_cmd") == ""


def test_read_local_md_key_strips_quotes(tmp_path):
    (tmp_path / "coordinator.local.md").write_text('---\nfull_test_cmd: "pytest -q"\n---\n')
    assert cs_read_local_md_key(str(tmp_path), "full_test_cmd") == "pytest -q"


def test_read_local_md_key_faithful_unanchored_match_no_prefix_strip(tmp_path):
    """Negative-spec: bash oracle's grep -F is unanchored (matches key: anywhere
    in the line) but the prefix-strip only fires when key: is at column 0. A
    line where full_test_cmd: appears mid-line (not a true prefix) is a
    faithfully-reproduced oracle quirk, NOT a bug fix — the whole line is
    returned untouched by the (no-op) prefix strip."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsome_prefix full_test_cmd: pytest -q\n---\n"
    )
    result = cs_read_local_md_key(str(tmp_path), "full_test_cmd")
    assert result == "some_prefix full_test_cmd: pytest -q"


# --- Unit 2: cs_resolve_full_test_cmd ------------------------------------------


def test_full_env_var_resolution(monkeypatch):
    monkeypatch.setenv(FULL_TEST_CMD_ENV, "pytest --full")
    result = cs_resolve_full_test_cmd("/nonexistent")
    assert result == ResolvedCommand("pytest --full", 0)


def test_full_local_md_resolution(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfull_test_cmd: pytest --full-suite\n---\n"
    )
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest --full-suite", 0)


def test_full_falls_back_to_fast_with_exit_3(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest -x tests/\n---\n"
    )
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest -x tests/", 3)


def test_full_falls_back_to_fast_suppresses_fast_tier_diags(tmp_path, capsys):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest -x tests/\n---\n"
    )
    cs_resolve_full_test_cmd(str(tmp_path))
    err = capsys.readouterr().err
    assert "cs_resolve_fast_test_cmd" not in err
    assert "step=fast-fallback" in err


def test_full_skip_when_neither_tier_configured(tmp_path, capsys):
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand(None, 2)
    assert "step=skipped" in capsys.readouterr().err


def test_full_propagates_hard_failure_not_a_skip(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(FAST_TEST_CMD_ENV, "python")
    monkeypatch.setattr(
        "coordinator_core.resolve_validation_cmd.resolve_python_interp", lambda: None
    )
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand(None, 127)
    assert "propagating (NOT a skip)" in capsys.readouterr().err


def test_full_env_var_wins_over_local_md_key(monkeypatch, tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfull_test_cmd: pytest local\n---\n"
    )
    monkeypatch.setenv(FULL_TEST_CMD_ENV, "pytest env")
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest env", 0)


def test_full_local_md_key_wins_over_fast_fallback(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        "---\nfull_test_cmd: pytest full\nfast_test_cmd: pytest fast\n---\n"
    )
    result = cs_resolve_full_test_cmd(str(tmp_path))
    assert result == ResolvedCommand("pytest full", 0)
