"""bin/tests/test_coordinator_resolve_validation_cmd.py — pytest suite for
resolve_fast_test_cmd and resolve_full_test_cmd.

Purpose: verifies the five AC cases for resolve_fast_test_cmd — env-var precedence,
local-md fallback, skip-with-notice, no-conventional-fallback (the Staff Engineer F1), and
exit-code passthrough semantics (the Staff Engineer F5). Also verifies the four
resolve_full_test_cmd cases: env-var, full_test_cmd: key, fast-tier fallback
(exit 3), and both-tiers-unconfigured (exit 2), plus the interpreter-normalization
helper (Tests 10-13).

Spec backlink: archive/specs/2026-05-28-workday-complete-fast-test-resolution.md § 5
Port backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (E3-e)
Bin-residency port backlink: coordinator/bin/coordinator-resolve-validation-cmd.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys

_TARGET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "coordinator-resolve-validation-cmd.py"
)


def _load_module():
    # The target's on-disk filename is hyphenated (a bin/-resident CLI, not
    # an importable package member) — a hyphen is not a valid Python
    # identifier character, so a bareword `import` can never resolve it
    # regardless of sys.path. Load by explicit file path instead, matching
    # coordinator/bin/tests/test_workday_start_day_branch_resolve.py's
    # convention for a bin/-resident target.
    spec = importlib.util.spec_from_file_location("coordinator_resolve_validation_cmd", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec_module: the target uses @dataclass at module
    # scope, which resolves annotation types via sys.modules.get(cls.__module__)
    # during exec — on Python versions where that lookup fires mid-exec (seen
    # on 3.14), an unregistered module raises here instead of importing clean.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rvc = _load_module()

_EXP_INTERP = "python3" if shutil.which("python3") else "python"


def _write_local_md_with_cmd(dir_: str, cmd_val: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write(f'---\nproject_type: test\nfast_test_cmd: "{cmd_val}"\n---\n')


def _write_local_md_without_cmd(dir_: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nproject_type: test\n---\n")


def _write_local_md_with_both_cmds(dir_: str, fast_val: str, full_val: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write(f'---\nproject_type: test\nfast_test_cmd: "{fast_val}"\nfull_test_cmd: "{full_val}"\n---\n')


# ---------------------------------------------------------------------------
# Test 1 / 1b: env var wins over local.md (fast tier)
# ---------------------------------------------------------------------------

def test_env_var_wins(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "python other.py")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "echo HELLO")

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == "echo HELLO"
    assert "step=env-var" in stderr
    assert "python other.py" not in result.stdout


def test_env_var_bare_python_normalized(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "python foo.py")

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} foo.py"


# ---------------------------------------------------------------------------
# Test 2: local.md wins when env unset
# ---------------------------------------------------------------------------

def test_local_md_wins_when_no_env(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "python foo.py")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} foo.py"
    assert "step=local-md" in stderr


# ---------------------------------------------------------------------------
# Test 2b: interior quotes survive resolution (regression — the retired bash
# oracle's `tr -d` pipeline stripped EVERY quote, disintegrating a quoted
# marker expression into separate argv tokens at shell-split time).
# ---------------------------------------------------------------------------

def test_local_md_preserves_interior_quotes(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "python -m pytest -m 'not slow and not integration'")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} -m pytest -m 'not slow and not integration'"


def test_read_local_md_key_preserves_interior_quotes(tmp_path):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nfull_test_cmd: "python -m pytest -m \'not slow\'"\n---\n')

    assert rvc.read_local_md_key(str(tmp_path), "full_test_cmd") == "python -m pytest -m 'not slow'"


def test_unquoted_and_unbalanced_values_are_left_intact(tmp_path):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nbare_key: make test\nragged_key: "unbalanced\n---\n')

    assert rvc.read_local_md_key(str(tmp_path), "bare_key") == "make test"
    assert rvc.read_local_md_key(str(tmp_path), "ragged_key") == '"unbalanced'


def test_key_matched_at_line_start_only(tmp_path):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nnote: "see full_test_cmd: below"\nfull_test_cmd: "make check"\n---\n')

    assert rvc.read_local_md_key(str(tmp_path), "full_test_cmd") == "make check"


# ---------------------------------------------------------------------------
# Test 2c: YAML double-quoted scalars resolve their escaped interior quotes.
# Removing only the wrapping pair left `\"` backslashes in the value, which
# `bash -c` then re-split exactly as the retired `tr -d` transform did — the
# example-retrieval-repo shape from the 2026-07-22 memo.
# ---------------------------------------------------------------------------

def test_double_quoted_scalar_escapes_are_resolved(tmp_path, monkeypatch):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nfast_test_cmd: "python -m pytest -m \\"a and not b\\" --timeout=60"\n---\n')
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f'{_EXP_INTERP} -m pytest -m "a and not b" --timeout=60'


def test_single_quoted_scalar_doubled_quote_is_resolved(tmp_path):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write("---\nfull_test_cmd: 'pytest --desc ''all tests'''\n---\n")

    assert rvc.read_local_md_key(str(tmp_path), "full_test_cmd") == "pytest --desc 'all tests'"


def test_windows_path_backslashes_survive(tmp_path):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nfull_test_cmd: "C:\\tools\\py -m pytest"\n---\n')

    assert rvc.read_local_md_key(str(tmp_path), "full_test_cmd") == "C:\\tools\\py -m pytest"


# ---------------------------------------------------------------------------
# Test 2d: an escaped quote this resolver cannot interpret fails loud (126)
# rather than reaching `bash -c` corrupted. The gate reporting "build failure"
# for what is really a config defect is the day-of-unnoticed-red failure mode.
# ---------------------------------------------------------------------------

def test_uninterpretable_escaped_quote_fails_loud(tmp_path, monkeypatch, capsys):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nfast_test_cmd: python -m pytest -m \\"a and b\\"\n---\n')
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 126
    assert result.stdout == ""
    assert "cannot interpret" in stderr


def test_uninterpretable_escaped_quote_fails_loud_on_env_var(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", 'pytest -m \\"a and b\\"')

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 126
    assert result.stdout == ""


def test_full_tier_propagates_malformed_fast_value(tmp_path, monkeypatch):
    with open(tmp_path / "coordinator.local.md", "w", encoding="utf-8") as fh:
        fh.write('---\nfast_test_cmd: pytest -m \\"a and b\\"\n---\n')
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.returncode == 126


# ---------------------------------------------------------------------------
# Test 2e: a bare `python` token resolves to the repo-local .venv when one
# exists — the ambient system interpreter has none of a venv-primary repo's
# runtime deps, so its collection errors read as test failures.
# ---------------------------------------------------------------------------

def test_bare_python_prefers_repo_venv(tmp_path, monkeypatch, capsys):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    interp = venv_bin / "python"
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)
    _write_local_md_with_cmd(str(tmp_path), "python -m pytest")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == f"{interp} -m pytest"
    assert "step=interp" in stderr


def test_bare_python_falls_back_to_ambient_without_venv(tmp_path, monkeypatch):
    _write_local_md_with_cmd(str(tmp_path), "python -m pytest")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} -m pytest"


def test_explicit_interpreter_is_not_rewritten_by_venv(tmp_path, monkeypatch):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")
    (venv_bin / "python").chmod(0o755)
    _write_local_md_with_cmd(str(tmp_path), "python3 -m pytest")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == "python3 -m pytest"


# ---------------------------------------------------------------------------
# Test 3: skip-with-notice
# ---------------------------------------------------------------------------

def test_skip_with_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 2
    assert result.stdout == ""
    assert "COORDINATOR_FAST_TEST_CMD" in stderr
    assert "coordinator.local.md" in stderr


# ---------------------------------------------------------------------------
# Test 4: no conventional fallback (the Staff Engineer F1)
# ---------------------------------------------------------------------------

def test_no_conventional_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    scripts_dir = tmp_path / ".github" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run-all-checks.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    out = capsys.readouterr()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "run-all-checks" not in (result.stdout + out.err)


# ---------------------------------------------------------------------------
# Test 5: configured cmd exit-code passthrough (the Staff Engineer F5)
# ---------------------------------------------------------------------------

def test_configured_cmd_exit_code_passthrough(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "exit 42")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == "exit 42"
    assert "step=local-md" in stderr


# ---------------------------------------------------------------------------
# Tests 6-9: resolve_full_test_cmd
# ---------------------------------------------------------------------------

def test_full_env_var_wins(tmp_path, monkeypatch, capsys):
    _write_local_md_with_both_cmds(str(tmp_path), "fast.py", "full.py")
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "echo FULL")

    result = rvc.resolve_full_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == "echo FULL"
    assert "[cs_resolve_full_test_cmd] step=env-var" in stderr


def test_full_local_md_key_wins(tmp_path, monkeypatch):
    _write_local_md_with_both_cmds(str(tmp_path), "fast.py", "full.py")
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == "full.py"
    assert "fast.py" not in result.stdout


def test_full_falls_back_to_fast(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "fast-only.py")
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_full_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 3
    assert result.stdout.strip() == "fast-only.py"
    assert "fast-fallback" in stderr
    # Inner fast-resolver diagnostic must be suppressed (matches bash oracle's
    # `2>/dev/null` on the fallback call) — only the fast-fallback caveat surfaces.
    assert "cs_resolve_fast_test_cmd" not in stderr.replace("cs_resolve_full_test_cmd", "")


def test_full_unconfigured_both_tiers(tmp_path, monkeypatch):
    _write_local_md_without_cmd(str(tmp_path))
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.returncode == 2
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# Tests 10-13: _normalize_python_token
# ---------------------------------------------------------------------------

def test_normalize_bare_python():
    out = rvc._normalize_python_token("python .github/scripts/run-all-checks.py")
    assert out == f"{_EXP_INTERP} .github/scripts/run-all-checks.py"


def test_python3_untouched():
    out = rvc._normalize_python_token("python3 -m pytest x")
    assert out == "python3 -m pytest x"


def test_non_python_untouched():
    out = rvc._normalize_python_token("pnpm test")
    assert out == "pnpm test"


def test_missing_interpreter_fails_loud(monkeypatch):
    monkeypatch.setattr(rvc.shutil, "which", lambda _name: None)
    try:
        rvc._normalize_python_token("python x.py")
        assert False, "expected InterpreterMissing"
    except rvc.InterpreterMissing:
        pass


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
