"""The reverify test leg resolves the engine root from the stamped install when COORDINATOR_ENGINE_ROOT is unset."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from coordinator_core import engine_root
from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

TARGET = "tests/test_sees_engine_root.py::test_child_env_names_the_engine_root"


@pytest.fixture(autouse=True)
def _no_ambient_engine_root(monkeypatch):
    monkeypatch.delenv(tlr.ENGINE_ROOT_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)


def _stamped(monkeypatch, root: str | None) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(engine_root, "ENGINE_CHECKOUT_ROOT", Path("no-such-checkout"))

    def fake():
        calls.append(1)
        return root, ("resolved-engine" if root else "unresolved")

    monkeypatch.setattr(engine_root, "coordinator_engine_root_with_class", fake)
    return calls


@pytest.mark.parametrize("shape", ["forward", "native"])
def test_an_unset_variable_is_filled_from_the_stamped_install_on_both_path_shapes(monkeypatch, tmp_path, shape):
    root = tmp_path / "engine"
    answer = root.as_posix() if shape == "forward" else str(root)
    _stamped(monkeypatch, answer)
    env = tlr._leg_env(tmp_path)
    assert env[tlr.ENGINE_ROOT_ENV] == str(Path(answer))


def test_an_ambient_variable_wins_and_the_stamped_install_is_not_consulted(monkeypatch, tmp_path):
    calls = _stamped(monkeypatch, str(tmp_path / "stamped"))
    monkeypatch.setenv(tlr.ENGINE_ROOT_ENV, str(tmp_path / "ambient"))
    assert tlr._leg_env(tmp_path)[tlr.ENGINE_ROOT_ENV] == str(tmp_path / "ambient")
    assert calls == []


def test_with_no_stamped_install_the_checkout_the_engine_runs_from_answers(monkeypatch, tmp_path):
    _stamped(monkeypatch, None)
    checkout = tmp_path / "self"
    (checkout / "coordinator_core").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(engine_root, "ENGINE_CHECKOUT_ROOT", checkout)
    assert tlr._leg_env(tmp_path)[tlr.ENGINE_ROOT_ENV] == str(checkout)


def test_an_unresolved_stamped_install_is_refused_naming_the_registry_key(monkeypatch, tmp_path):
    _stamped(monkeypatch, None)
    with pytest.raises(tlr.EngineRootUnresolved) as err:
        tlr._leg_env(tmp_path)
    assert "repos.claude_klabauter" in str(err.value) and tlr.ENGINE_ROOT_ENV in str(err.value)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_sees_engine_root.py").write_text(
        "import os\n\n\ndef test_child_env_names_the_engine_root():\n"
        "    assert os.environ['COORDINATOR_ENGINE_ROOT'] == os.environ['EXPECTED_ENGINE_ROOT']\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def session_temp(tmp_path, monkeypatch):
    """The spawned leg's session Temp, a directory the test removes with tmp_path."""
    root = tmp_path / "session-temp"
    root.mkdir()
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(root))
    monkeypatch.setattr(tempfile, "tempdir", None)
    runs = tmp_path / "pytest-runs"
    monkeypatch.setattr(tlr.scoped_pytest, "per_run_root", lambda _repo_root: runs)
    return root


def _main(repo: Path, out: Path) -> dict:
    tlr.main(["--repo-root", str(repo), "--out", str(out), "--", TARGET])
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.spawns_process
def test_the_default_entry_runs_the_targeted_tests_against_the_stamped_engine_root(
    repo, tmp_path, monkeypatch, session_temp
):
    stamped = tmp_path / "stamped-engine"
    stamped.mkdir()
    _stamped(monkeypatch, str(stamped))
    monkeypatch.setenv("EXPECTED_ENGINE_ROOT", str(stamped))
    data = _main(repo, tmp_path / "o.json")
    assert data["tests_run"] > 0 and data["status"] == "pass", data


@pytest.mark.spawns_process
def test_an_unresolvable_engine_root_is_a_typed_zero_collected_fail_naming_argv_cwd_and_interpreter(
    repo, tmp_path, monkeypatch, session_temp
):
    _stamped(monkeypatch, None)
    data = _main(repo, tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status_after"] == "fail", data
    assert ltt.is_zero_collected(data)
    cause = ltt.zero_collected_cause(data)
    assert "repos.claude_klabauter" in json.dumps(data)
    assert "argv: unrecorded" not in cause and TARGET in cause
    assert f"interpreter: {data['interpreter']}" in cause and f"cwd: {data['cwd']}" in cause
