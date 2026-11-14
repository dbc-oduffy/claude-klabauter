"""The harness fixture's env makes the client spawn its pair; a pair that never answers is explained."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import publish_under_live_engine as h
from coordinator_core.warm import client

REPO = Path(__file__).resolve().parents[3]


def _settings_home_naming_foreign_root(tmp_path: Path) -> Path:
    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    (home / "machine-local" / ".claude-klabauter-root").write_text(str(tmp_path / "real-engine"), encoding="utf-8")
    return home


def _apply(monkeypatch, env: dict) -> None:
    for key in ("COORDINATOR_CLONE_ROOT", "COORDINATOR_WARM_RUNTIME_BASE", "PYTHONPATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_fixture_env_pins_runtime_base_pythonpath_and_clone_root(tmp_path):
    env = h._env_for(tmp_path / "work", tmp_path / "dest")
    assert env["COORDINATOR_WARM_RUNTIME_BASE"] == str(tmp_path / "work" / "rt")
    assert env["PYTHONPATH"] == str(tmp_path / "dest")
    assert env["COORDINATOR_CLONE_ROOT"] == str(tmp_path / "dest")


def test_the_client_spawned_server_is_launched_with_the_fixture_env(tmp_path):
    dest = tmp_path / "dest"
    env = h._env_for(tmp_path / "work", dest)
    # `_child_env` reads os.environ: run it in a process that carries the harness env (PYTHONPATH
    # swapped to the real repo so the import resolves; the fixture dest does not exist here).
    probe = (
        "import json, sys\n"
        "from coordinator_core.ops.ceremony import detached_spawn as d\n"
        "e = d._child_env(sys.argv[1])\n"
        "print(json.dumps({k: e.get(k) for k in ('COORDINATOR_WARM_RUNTIME_BASE', 'PYTHONPATH', 'COORDINATOR_CLONE_ROOT')}"
        " | {'exe': d._resolve_python_exe()}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(dest)], cwd=str(REPO), capture_output=True, text=True, timeout=60,
        env={**env, "PYTHONPATH": str(REPO)},
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.splitlines()[-1])
    assert seen["COORDINATOR_WARM_RUNTIME_BASE"] == env["COORDINATOR_WARM_RUNTIME_BASE"]
    assert seen["COORDINATOR_CLONE_ROOT"] == str(dest)
    assert seen["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")[0] == str(dest)
    assert Path(seen["exe"]) == Path(sys.executable)


def test_probe_runs_under_the_fixture_env_interpreter_and_cwd(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="boom")

    monkeypatch.setattr(h.subprocess, "run", fake_run)
    dest = tmp_path / "dest"
    env = h._env_for(tmp_path / "work", dest)
    rec = h._probe_once(dest, env, "guard")
    assert seen["argv"][0] == sys.executable
    assert seen["env"] is env and seen["cwd"] == str(dest)
    assert rec == {"probe": "guard", "reply": None, "stderr": "boom"}


def test_a_settings_home_naming_another_root_makes_the_fixture_foreign_only_without_the_pin(tmp_path, monkeypatch):
    home = _settings_home_naming_foreign_root(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    pinned = h._env_for(tmp_path / "work", client._engine_clone_root())
    _apply(monkeypatch, pinned)
    assert client._configured_root_is_foreign() is False
    unpinned = {k: v for k, v in pinned.items() if k != "COORDINATOR_CLONE_ROOT"}
    _apply(monkeypatch, unpinned)
    assert client._configured_root_is_foreign() is True


def test_no_answering_pair_raises_naming_stderr_and_the_spawn_log_paths(tmp_path, monkeypatch):
    work, repo = tmp_path / "work", tmp_path / "repo"
    dest = work / "dest"
    (dest / "state").mkdir(parents=True)
    (dest / "state" / "housekeeping-failures.log").write_text("SPAWN FAILED script=x :: OSError: nope\n", encoding="utf-8")
    monkeypatch.setattr(h, "build_fixture", lambda w, r: (work / "src", dest))
    monkeypatch.setattr(h, "PAIR_UP_WAIT_S", 0.0)
    monkeypatch.setattr(h, "_stop_pair", lambda d, e: None)
    monkeypatch.setattr(
        h, "_probe_once", lambda d, e, kind: {"probe": kind, "reply": None, "stderr": "child exploded: ImportError"}
    )
    monkeypatch.setattr(h, "spawn_diagnosis", lambda d, e: "client view: {\"foreign\": true}")
    with pytest.raises(RuntimeError) as exc:
        h.run_round(work, repo)
    text = str(exc.value)
    assert "never answered a guard probe" in text
    assert "child exploded: ImportError" in text
    assert "client-cold.jsonl" in text
    assert str(dest / "state" / "housekeeping-failures.log") in text
    assert "SPAWN FAILED" in text
    assert '"foreign": true' in text


def test_the_diagnosis_names_a_foreign_configured_root(tmp_path, monkeypatch):
    home = _settings_home_naming_foreign_root(tmp_path)
    env = h._env_for(tmp_path / "work", REPO)
    env.pop("COORDINATOR_CLONE_ROOT")
    env["COORDINATOR_SETTINGS_HOME"] = str(home)
    text = h.spawn_diagnosis(REPO, env)
    assert '"foreign": true' in text
    assert "real-engine" in text
    pinned = h.spawn_diagnosis(REPO, {**env, "COORDINATOR_CLONE_ROOT": str(REPO)})
    assert '"foreign": false' in pinned
