"""An empty target list never yields a zero-test pass: the diff's edited tests are the targets, else a typed error."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (root / "tests" / "test_edited.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "test_sibling.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    _git(root.parent, "init", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    monkeypatch.chdir(root)
    return root


def _main(argv: list[str], out: Path) -> tuple[int, dict]:
    code = tlr.main([*argv[: argv.index("--")], "--out", str(out), *argv[argv.index("--"):]])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_empty_targets_run_the_edited_test_file(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_edited.py").write_text("def test_a():\n    assert True\n\ndef test_c():\n    assert True\n",
                                                   encoding="utf-8")
    _git(repo, "commit", "-am", "edit")
    code, data = _main(["--base-sha", base, "--"], tmp_path / "o.json")
    assert code == tlr.EXIT_OK
    assert data["derived_targets"] == ["tests/test_edited.py"]
    assert data["tests_run"] == 2 and data["status"] == "pass"


def test_empty_targets_with_no_edited_test_is_a_typed_error_not_a_pass(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "doc")
    code, data = _main(["--base-sha", base, "--"], tmp_path / "o.json")
    assert code == tlr.EXIT_USAGE
    assert data["tests_run"] == 0 and data["status"] == "error" and data["status_after"] == "fail"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_EMPTY


def test_empty_targets_without_a_base_is_refused(repo, tmp_path, capsys):
    assert tlr.main(["--out", str(tmp_path / "o.json"), "--"]) == tlr.EXIT_USAGE


def test_a_deadline_with_no_finished_test_names_every_unrun_file(repo, tmp_path):
    for n in ("a", "b"):
        (repo / "tests" / f"test_slow_{n}.py").write_text(
            "import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    code, data = _main(["--budget-s", "2", "--", "tests/test_slow_a.py", "tests/test_slow_b.py"], tmp_path / "o.json")
    assert code == tlr.EXIT_TIMEOUT
    assert data["tests_run"] == 0 and data["zero_run_cause"] == ltt.ZERO_RUN_BUDGET
    assert set(data["unrun_targets"]) == {"tests/test_slow_a.py", "tests/test_slow_b.py"}
