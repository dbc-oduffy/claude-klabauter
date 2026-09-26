
from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sys

import coordinator_core.resolve_validation_cmd as core_rvc

_TARGET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "coordinator-resolve-validation-cmd.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("coordinator_resolve_validation_cmd", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rvc = _load_module()

_EXP_INTERP = shlex.quote(rvc._resolve_python_interp(None))


def _write_local_md_with_cmd(dir_: str, cmd_val: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write(f'---\nproject_type: test\nfast_test_cmd: "{cmd_val}"\n---\n')


def _write_local_md_without_cmd(dir_: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nproject_type: test\n---\n")


def _write_local_md_with_both_cmds(dir_: str, fast_val: str, full_val: str) -> None:
    with open(os.path.join(dir_, "coordinator.local.md"), "w", encoding="utf-8") as fh:
        fh.write(f'---\nproject_type: test\nfast_test_cmd: "{fast_val}"\nfull_test_cmd: "{full_val}"\n---\n')


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


def test_local_md_wins_when_no_env(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "python foo.py")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} foo.py"
    assert "step=local-md" in stderr


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


def test_bare_python_prefers_repo_venv(tmp_path, monkeypatch, capsys):
    # (no PATHEXT-recognized extension, no PATHEXT sibling) — the real
    if os.name == "nt":
        venv_bin = tmp_path / ".venv" / "Scripts"
        venv_bin.mkdir(parents=True)
        interp = venv_bin / "python.exe"
        interp.write_text("")
    else:
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
    assert result.stdout.strip() == f"{shlex.quote(str(interp))} -m pytest"
    assert "step=interp" in stderr


def test_bare_python_falls_back_to_ambient_without_venv(tmp_path, monkeypatch):
    _write_local_md_with_cmd(str(tmp_path), "python -m pytest")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f"{_EXP_INTERP} -m pytest"


def test_explicit_python3_is_rewritten_by_venv(tmp_path, monkeypatch):
    # on Windows (no PATHEXT-recognized extension), so this test would pass
    if os.name == "nt":
        venv_bin = tmp_path / ".venv" / "Scripts"
        venv_bin.mkdir(parents=True)
        interp = venv_bin / "python.exe"
        interp.write_text("")
    else:
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        interp = venv_bin / "python"
        interp.write_text("#!/bin/sh\n")
        interp.chmod(0o755)
    _write_local_md_with_cmd(str(tmp_path), "python3 -m pytest")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))

    assert result.returncode == 0
    assert result.stdout.strip() == f"{shlex.quote(str(interp))} -m pytest"


def test_skip_with_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 2
    assert result.stdout == ""
    assert "COORDINATOR_FAST_TEST_CMD" in stderr
    assert "coordinator.local.md" in stderr


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


def test_configured_cmd_exit_code_passthrough(tmp_path, monkeypatch, capsys):
    _write_local_md_with_cmd(str(tmp_path), "exit 42")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_fast_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert result.stdout.strip() == "exit 42"
    assert "step=local-md" in stderr


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
    assert "cs_resolve_fast_test_cmd" not in stderr.replace("cs_resolve_full_test_cmd", "")


def test_full_unconfigured_both_tiers(tmp_path, monkeypatch):
    _write_local_md_without_cmd(str(tmp_path))
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.returncode == 2
    assert result.stdout == ""


def test_full_adds_bounded_worker_count_when_xdist_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "python3 -m pytest --timeout=300")
    monkeypatch.setattr(core_rvc, "_pytest_plugin_available", lambda name: True)
    monkeypatch.setattr(
        "coordinator_core.install.derive_worker_cap.derive_cap", lambda: 4
    )

    result = rvc.resolve_full_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert "-n 4" in result.stdout
    assert "-rfE" in result.stdout
    assert "step=worker-bound" in stderr


def test_full_leaves_command_serial_when_xdist_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "python3 -m pytest --timeout=300")
    monkeypatch.setattr(core_rvc, "_pytest_plugin_available", lambda name: name != "xdist")

    result = rvc.resolve_full_test_cmd(str(tmp_path))
    stderr = capsys.readouterr().err

    assert result.returncode == 0
    assert "-n " not in result.stdout
    assert "--timeout=300" in result.stdout
    assert "pytest-xdist not importable" in stderr


def test_full_never_bounds_an_explicit_worker_count(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "python3 -m pytest -n 2 --timeout=300")
    monkeypatch.setattr(core_rvc, "_pytest_plugin_available", lambda name: True)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.stdout.count("-n ") == 1
    assert "-n 2" in result.stdout


def test_full_never_drops_a_configured_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "python3 -m pytest --timeout=300")
    monkeypatch.setattr(core_rvc, "_pytest_plugin_available", lambda name: name != "pytest_timeout")

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert "--timeout=300" in result.stdout


def test_full_leaves_existing_result_flag_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "python3 -m pytest -rfE --timeout=300")
    monkeypatch.setattr(core_rvc, "_pytest_plugin_available", lambda name: True)

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.stdout.count("-rfE") == 1


def test_full_non_pytest_command_untouched_by_augmentation(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", "pnpm run tier:full")

    result = rvc.resolve_full_test_cmd(str(tmp_path))

    assert result.stdout.strip() == "pnpm run tier:full"


def test_normalize_bare_python():
    out = rvc._normalize_python_token("python .github/scripts/run-all-checks.py")
    assert out == f"{_EXP_INTERP} .github/scripts/run-all-checks.py"


def test_bare_python3_also_normalized():
    # coincidence rather than by actually exercising this behaviour. `_EXP_INTERP`
    out = rvc._normalize_python_token("python3 -m pytest x")
    assert out == f"{_EXP_INTERP} -m pytest x"


def test_non_python_untouched():
    out = rvc._normalize_python_token("pnpm test")
    assert out == "pnpm test"


def test_missing_interpreter_fails_loud(monkeypatch):
    monkeypatch.setattr(rvc.shutil, "which", lambda _name: None)
    monkeypatch.setattr(rvc.sys, "executable", "")
    monkeypatch.setattr(core_rvc, "_shared_console_python", lambda: None)
    try:
        rvc._normalize_python_token("python x.py")
        assert False, "expected InterpreterMissing"
    except rvc.InterpreterMissing:
        pass


# python, else None). _EXP_INTERP above is a tautology w.r.t. this function

def test_resolve_python_interp_windows_prefers_shared_ladder(monkeypatch):
    monkeypatch.setattr(rvc.os, "name", "nt")
    monkeypatch.setattr(rvc.sys, "executable", "C:\\Forwarder\\coordinator.exe")
    monkeypatch.setattr(
        core_rvc, "_shared_console_python", lambda: "C:\\Console\\python.exe"
    )
    monkeypatch.setattr(rvc.shutil, "which", lambda name: f"C:\\fake\\{name}.exe")

    result = rvc._resolve_python_interp(None)

    assert result == "C:\\Console\\python.exe"
    assert result != "C:\\Forwarder\\coordinator.exe"


def test_resolve_python_interp_windows_falls_back_when_ladder_returns_none(monkeypatch):
    monkeypatch.setattr(rvc.os, "name", "nt")
    monkeypatch.setattr(rvc.sys, "executable", "C:\\Forwarder\\coordinator.exe")
    monkeypatch.setattr(core_rvc, "_shared_console_python", lambda: None)
    monkeypatch.setattr(
        rvc.shutil, "which", lambda name: "/fake/python3" if name == "python3" else None
    )

    result = rvc._resolve_python_interp(None)

    assert result == "python3"
    assert result != "C:\\Forwarder\\coordinator.exe"


def test_resolve_python_interp_posix_prefers_python3_on_path(monkeypatch):
    monkeypatch.setattr(rvc.os, "name", "posix")
    monkeypatch.setattr(rvc.sys, "executable", "/usr/bin/python3.11")
    monkeypatch.setattr(
        rvc.shutil,
        "which",
        lambda name: "/usr/bin/python3" if name == "python3" else "/usr/bin/python",
    )

    assert rvc._resolve_python_interp(None) == "python3"


def test_resolve_python_interp_posix_falls_back_to_python(monkeypatch):
    monkeypatch.setattr(rvc.os, "name", "posix")
    monkeypatch.setattr(rvc.sys, "executable", "/usr/bin/python3.11")
    monkeypatch.setattr(
        rvc.shutil, "which", lambda name: "/usr/bin/python" if name == "python" else None
    )

    assert rvc._resolve_python_interp(None) == "python"


def test_resolve_python_interp_returns_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(rvc.os, "name", "posix")
    monkeypatch.setattr(rvc.sys, "executable", "")
    monkeypatch.setattr(rvc.shutil, "which", lambda name: None)

    assert rvc._resolve_python_interp(None) is None


def test_ac11_bin_path_is_byte_identical_re_export(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    (tmp_path / "coordinator.local.md").write_text(
        '---\nfast_test_cmd: "pytest -q"\n---\n', encoding="utf-8"
    )

    bin_fast = rvc.resolve_fast_test_cmd(str(tmp_path))
    core_fast = core_rvc.resolve_fast_test_cmd(str(tmp_path))
    assert (bin_fast.stdout, bin_fast.returncode, bin_fast.stderr) == (
        core_fast.stdout,
        core_fast.returncode,
        core_fast.stderr,
    )

    bin_full = rvc.resolve_full_test_cmd(str(tmp_path))
    core_full = core_rvc.resolve_full_test_cmd(str(tmp_path))
    assert (bin_full.stdout, bin_full.returncode, bin_full.stderr) == (
        core_full.stdout,
        core_full.returncode,
        core_full.stderr,
    )

    assert rvc.resolve_fast_test_cmd is core_rvc.resolve_fast_test_cmd
    assert rvc.resolve_full_test_cmd is core_rvc.resolve_full_test_cmd
    assert rvc.main is not core_rvc.main
    assert rvc.main(["--fast", str(tmp_path)]) == core_rvc.main(
        ["--fast", str(tmp_path)]
    )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
