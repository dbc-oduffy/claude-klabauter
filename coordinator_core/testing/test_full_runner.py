
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing.full_runner import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "coordinator_core.testing.full_runner", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        **no_console_creationflags(),
        cwd=_REPO_ROOT,
    )


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_subprocess_exit_zero_on_all_pass(fixture_tree) -> None:
    tree = fixture_tree()
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_subprocess_exit_nonzero_on_injected_failure(fixture_tree) -> None:
    tree = fixture_tree(
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),)
    )
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_inprocess_main_supplement_matches_subprocess_contract(fixture_tree) -> None:
    tree = fixture_tree()
    assert main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0

    failing_tree = fixture_tree(
        root=tree.repo_root.parent / "repo-failing",
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),),
    )
    assert main(["--repo", str(failing_tree.repo_root), "--jobs", "1"]) != 0


def test_expect_named_empty_family_fails(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    proc = _run_cli(
        ["--repo", str(repo_root), "--expect", "js-suffix", "--jobs", "1"]
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "js-suffix" in proc.stderr
    assert "WARN" in proc.stderr


def test_expect_all_fails_when_any_family_empty(fixture_tree) -> None:
    tree = fixture_tree()
    tree.family_files["js-suffix"].unlink()

    proc = _run_cli(["--repo", str(tree.repo_root), "--expect", "all", "--jobs", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "js-suffix" in proc.stderr


def test_no_expect_empty_family_stays_green(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WARN" in proc.stderr


def test_repo_dot_self_run_smoke_no_expect(tmp_path: Path) -> None:
    repo_root = tmp_path / "self-run-repo"
    repo_root.mkdir()
    _write(
        repo_root / "test_smoke.py",
        "def test_smoke():\n    assert True\n",
    )
    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_repo_dot_family_warn_semantics_no_expect(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    rc = main(["--repo", str(repo_root), "--families", "js-prefix", "--jobs", "1"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "js-prefix" in captured.err
    assert "WARN" in captured.err


def test_tally_line_shape(fixture_tree) -> None:
    tree = fixture_tree()
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    lines = proc.stdout.splitlines()
    tally_lines = [
        line for line in lines if "passed" in line and "failed" in line and "across" in line
    ]
    assert len(tally_lines) == 1, proc.stdout
    tally = tally_lines[0]
    assert " passed, " in tally
    assert " failed across " in tally
    assert " families (" in tally
    assert tally.rstrip().endswith("s)")


def test_per_suite_pass_fail_streamed(fixture_tree) -> None:
    tree = fixture_tree(
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),)
    )
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert "[PASS]" in proc.stdout
    assert "[FAIL]" in proc.stdout


def test_timeout_surfaces_as_failed_in_tally(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "slow.test.py", "import time\ntime.sleep(5)\n")

    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1", "--timeout", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "[FAIL]" in proc.stdout


def test_run_holds_the_suite_mutex_for_the_duration_of_the_run(fixture_tree, monkeypatch):
    from coordinator_core.testing import full_runner, suite_mutex

    observed = {}
    real_run_suites = full_runner.run_suites

    def spy(*args, **kwargs):
        observed["holder_during_run"] = suite_mutex.holder()
        return real_run_suites(*args, **kwargs)

    tree = fixture_tree()
    monkeypatch.setattr(full_runner, "run_suites", spy)
    assert full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0

    assert observed["holder_during_run"] is not None, (
        "suite mutex was not held during the run — layer 6 is inert again"
    )
    assert suite_mutex.holder() is None, "mutex not released after the run"


def test_mutex_released_when_the_run_raises(fixture_tree, monkeypatch):
    from coordinator_core.testing import full_runner, suite_mutex

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    tree = fixture_tree()
    monkeypatch.setattr(full_runner, "run_suites", boom)
    with pytest.raises(RuntimeError):
        full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"])

    assert suite_mutex.holder() is None


def test_contended_run_proceeds_unserialized_rather_than_failing(fixture_tree, monkeypatch):
    from coordinator_core.testing import full_runner

    tree = fixture_tree()
    monkeypatch.setattr(full_runner.suite_mutex, "MUTEX_WAIT_SECS", 0.0)
    monkeypatch.setattr(full_runner.suite_mutex, "acquire", lambda *a, **k: False)
    assert full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0
