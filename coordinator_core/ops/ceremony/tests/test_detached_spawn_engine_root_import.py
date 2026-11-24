"""A detached engine child imports coordinator_core, and runs its script, from the engine root."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

from coordinator_core.ops.ceremony import detached_spawn

_PROBE = (
    "import sys\n"
    "import coordinator_core\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(coordinator_core.__file__ + '|' + __file__)\n"
)


def _tree(root: Path, marker: str) -> Path:
    pkg = root / "coordinator_core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    return root


def _spawn_and_wait(engine: Path, repo_root: Path, script: str, out: Path, monkeypatch) -> str:
    monkeypatch.setattr(detached_spawn, "_ENGINE_ROOT", str(engine))
    monkeypatch.setattr(detached_spawn, "_warm_spawn_refused_for_test", lambda _p: False)
    real_popen = subprocess.Popen
    procs: list = []

    def capture(*a, **k):
        p = real_popen(*a, **k)
        procs.append(p)
        return p

    with mock.patch.object(detached_spawn.subprocess, "Popen", side_effect=capture):
        assert detached_spawn.spawn_detached(str(repo_root), script, [str(out)])
    assert procs[0].wait(timeout=60) == 0
    return out.read_text(encoding="utf-8")


def test_child_with_consumer_cwd_and_inherited_pythonpath_imports_engine_root(tmp_path, monkeypatch) -> None:
    engine = _tree(tmp_path / "engine", "engine")
    consumer = _tree(tmp_path / "consumer", "consumer")
    script = engine / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(consumer))

    got = _spawn_and_wait(engine, consumer, str(script), tmp_path / "out.txt", monkeypatch)

    imported, ran = got.split("|")
    assert Path(imported).resolve().is_relative_to(engine.resolve())
    assert Path(ran).resolve() == script.resolve()


def test_relative_script_runs_the_engine_copy_not_the_consumers(tmp_path, monkeypatch) -> None:
    engine = _tree(tmp_path / "engine", "engine")
    consumer = _tree(tmp_path / "consumer", "consumer")
    rel = "coordinator_core/probe_script.py"
    (engine / rel).write_text(_PROBE, encoding="utf-8")
    (consumer / rel).write_text(_PROBE, encoding="utf-8")

    got = _spawn_and_wait(engine, consumer, rel, tmp_path / "out.txt", monkeypatch)

    imported, ran = got.split("|")
    assert Path(ran).resolve() == (engine / rel).resolve()
    assert Path(imported).resolve().is_relative_to(engine.resolve())


def test_relative_script_absent_from_engine_still_runs_from_repo_root(tmp_path, monkeypatch) -> None:
    engine = _tree(tmp_path / "engine", "engine")
    consumer = _tree(tmp_path / "consumer", "consumer")
    rel = "consumer_only.py"
    (consumer / rel).write_text(_PROBE, encoding="utf-8")

    got = _spawn_and_wait(engine, consumer, rel, tmp_path / "out.txt", monkeypatch)

    assert Path(got.split("|")[1]).resolve() == (consumer / rel).resolve()


def test_pythonpath_leads_with_engine_root_before_inherited_consumer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(detached_spawn, "_ENGINE_ROOT", str(tmp_path / "engine"))
    with mock.patch.dict(os.environ, {"PYTHONPATH": str(tmp_path / "consumer")}):
        env = detached_spawn._child_env(str(tmp_path / "consumer"))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "engine")
