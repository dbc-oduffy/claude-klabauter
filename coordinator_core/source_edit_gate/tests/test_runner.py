"""Tests for `coordinator_core.source_edit_gate.runner.run_selected` temp-dir
hygiene -- each pytest/vitest spawn must use a caller-owned temp dir (pytest's
own `--basetemp`, plus the junit/json report scratch dir) that is removed in a
`finally` after the run, on both success and failure, so gate runs never
accumulate temp data across invocations.

In-process only: `subprocess.run` is monkeypatched, so nothing here spawns a
real pytest/vitest process (no `spawns_process` mark needed).
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

from coordinator_core.source_edit_gate import runner as runner_module


def _tmp_dirs_matching(prefix: str) -> list:
    return glob.glob(os.path.join(__import__("tempfile").gettempdir(), f"{prefix}*"))


def test_run_selected_pytest_removes_temp_dir_on_success(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, cwd, capture_output, **kwargs):
        # Record the basetemp/junit dir so we can assert it existed during
        # the run and is gone afterward.
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--basetemp="):
                captured["basetemp"] = Path(arg.split("=", 1)[1])
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                junit_path = Path(arg.split("=", 1)[1])
                junit_path.parent.mkdir(parents=True, exist_ok=True)
                junit_path.write_text(
                    '<?xml version="1.0"?><testsuite tests="0" failures="0"></testsuite>',
                    encoding="utf-8",
                )
                captured["junit"] = junit_path

        class _Result:
            returncode = 0

        assert captured["basetemp"].parent.is_dir()
        return _Result()

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    before = set(_tmp_dirs_matching("source-edit-gate-pytest-"))
    result = runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")
    after = set(_tmp_dirs_matching("source-edit-gate-pytest-"))

    assert result is not None
    # No leftover temp dirs from this run.
    assert after - before == set()
    assert not captured["junit"].parent.exists()


def test_run_selected_pytest_removes_temp_dir_on_subprocess_failure(tmp_path, monkeypatch):
    def fake_run(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    before = set(_tmp_dirs_matching("source-edit-gate-pytest-"))
    with pytest.raises(OSError):
        runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")
    after = set(_tmp_dirs_matching("source-edit-gate-pytest-"))

    assert after - before == set()


def test_run_selected_pytest_uses_xdist_when_importable(tmp_path, monkeypatch):
    captured = {}

    def fake_find_spec(name):
        return object() if name == "xdist" else None

    def fake_run(argv, cwd, capture_output, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                junit_path = Path(arg.split("=", 1)[1])
                junit_path.parent.mkdir(parents=True, exist_ok=True)
                junit_path.write_text(
                    '<?xml version="1.0"?><testsuite tests="0" failures="0"></testsuite>',
                    encoding="utf-8",
                )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runner_module.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(runner_module, "_usable_ram_gb", lambda: 16.0)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    argv = captured["argv"]
    assert "-n" in argv
    n_index = argv.index("-n")
    assert argv[n_index + 1] == str(min(8 // 2, int(16.0 * 1024 // 150)))
    assert "-p" in argv
    p_index = argv.index("-p")
    assert argv[p_index + 1] == "no:cacheprovider"


def test_run_selected_pytest_skips_xdist_when_not_importable(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, cwd, capture_output, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                junit_path = Path(arg.split("=", 1)[1])
                junit_path.parent.mkdir(parents=True, exist_ok=True)
                junit_path.write_text(
                    '<?xml version="1.0"?><testsuite tests="0" failures="0"></testsuite>',
                    encoding="utf-8",
                )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    argv = captured["argv"]
    assert "-n" not in argv
    assert "-p" not in argv


def test_run_selected_vitest_removes_temp_dir_on_success(tmp_path, monkeypatch):
    captured = {}

    def fake_which(name):
        return "npx"

    def fake_run(argv, cwd, capture_output, **kwargs):
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--outputFile="):
                out_path = Path(arg.split("=", 1)[1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text('{"testResults": []}', encoding="utf-8")
                captured["out"] = out_path

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(runner_module.shutil, "which", fake_which)
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    before = set(_tmp_dirs_matching("source-edit-gate-vitest-"))
    runner_module.run_selected(str(tmp_path), ["src/x.test.ts"], "vitest")
    after = set(_tmp_dirs_matching("source-edit-gate-vitest-"))

    assert after - before == set()
    assert not captured["out"].parent.exists()
