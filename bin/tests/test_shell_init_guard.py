
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SCRIPT = _REPO_ROOT / "bin" / "shell-init-guard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("shell_init_guard", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DEFAULT_LINE = "ulimit -S -f 8388608"
_FAILGLOB_LINE = _load_module()._FAILGLOB_LINE
_ENV_VAR = "COORDINATOR_OVERRIDE_FSIZE_CAP"


def _run(env_value: str | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_value is None:
        env.pop(_ENV_VAR, None)
    else:
        env[_ENV_VAR] = env_value
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _assert_clean_two_lines(result: subprocess.CompletedProcess, expected_ulimit: str) -> None:
    lines = result.stdout.splitlines()
    assert lines == [expected_ulimit, _FAILGLOB_LINE], (
        f"stdout must be exactly the two guard lines, got: {result.stdout!r}"
    )


@pytest.fixture(scope="module", autouse=True)
def _require_script() -> None:
    if not _SCRIPT.exists():
        pytest.skip("bin/shell-init-guard.py not on disk")


def test_unset_env_default_cap() -> None:
    result = _run(None)
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr == ""


def test_empty_env_default_cap() -> None:
    result = _run("")
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr == ""


@pytest.mark.parametrize("value", ["unlimited", "UNLIMITED", " Unlimited "])
def test_unlimited_case_and_whitespace(value: str) -> None:
    result = _run(value)
    assert result.returncode == 0
    _assert_clean_two_lines(result, "ulimit -S -f unlimited")
    assert result.stderr == ""


def test_16_gib() -> None:
    result = _run("16")
    assert result.returncode == 0
    _assert_clean_two_lines(result, "ulimit -S -f 16777216")
    assert result.stderr == ""


def test_1_gib() -> None:
    result = _run("1")
    assert result.returncode == 0
    _assert_clean_two_lines(result, "ulimit -S -f 1048576")
    assert result.stderr == ""


def test_zero_is_invalid() -> None:
    result = _run("0")
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr.strip() == f"# coordinator: ignored invalid {_ENV_VAR}=0"


@pytest.mark.parametrize("value", ["-5", "abc", "8.5", "1e3"])
def test_invalid_values_fall_back_to_default(value: str) -> None:
    result = _run(value)
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr.strip() == f"# coordinator: ignored invalid {_ENV_VAR}={value}"


def test_stdout_always_has_failglob() -> None:
    for value in [None, "", "unlimited", "16", "0", "abc"]:
        result = _run(value)
        assert _FAILGLOB_LINE in result.stdout.splitlines()


@pytest.mark.parametrize("value", ["+8", "1_000", "٨"])
def test_widened_int_acceptance_forms_rejected(value: str) -> None:
    result = _run(value)
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr.strip() == f"# coordinator: ignored invalid {_ENV_VAR}={value}"


def test_whitespace_padded_invalid_value_echoes_raw_untrimmed() -> None:
    result = _run(" abc ")
    assert result.returncode == 0
    _assert_clean_two_lines(result, _DEFAULT_LINE)
    assert result.stderr.rstrip("\n") == f"# coordinator: ignored invalid {_ENV_VAR}= abc "


def test_huge_n_emits_plain_decimal_no_overflow() -> None:
    result = _run("999999999")
    assert result.returncode == 0
    _assert_clean_two_lines(result, "ulimit -S -f 1048575998951424")
    assert result.stderr == ""


# `_load_module`, defined above alongside `_FAILGLOB_LINE`, since the filename has a hyphen)
@pytest.fixture(scope="module")
def _guard_module():
    return _load_module()


@pytest.mark.parametrize(
    "raw_value,expected_line,expect_comment",
    [
        (None, _DEFAULT_LINE, False),
        ("", _DEFAULT_LINE, False),
        ("unlimited", "ulimit -S -f unlimited", False),
        (" Unlimited ", "ulimit -S -f unlimited", False),
        ("16", "ulimit -S -f 16777216", False),
        ("0", _DEFAULT_LINE, True),
        ("abc", _DEFAULT_LINE, True),
        ("+8", _DEFAULT_LINE, True),
    ],
)
def test_resolve_ulimit_line_unit(
    _guard_module, raw_value: str | None, expected_line: str, expect_comment: bool
) -> None:
    line, comment = _guard_module._resolve_ulimit_line(raw_value)
    assert line == expected_line
    if expect_comment:
        assert comment is not None and comment.endswith(f"={raw_value}")
    else:
        assert comment is None


def test_main_fail_open_on_internal_exception(_guard_module, monkeypatch, capsys) -> None:
    def _boom(_raw_value):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(_guard_module, "_resolve_ulimit_line", _boom)
    rc = _guard_module.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.splitlines() == [_DEFAULT_LINE, _FAILGLOB_LINE]
    assert captured.err == ""
