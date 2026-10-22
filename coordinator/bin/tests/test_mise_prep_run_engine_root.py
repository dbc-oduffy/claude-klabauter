"""mise-prep-run, run directly from a tree that is not the engine checkout, binds the engine root
the settings-home launcher resolves (`cc_invoke.resolve_engine_root`) and never dies on a bare
`No module named 'coordinator_core'`. Every tree is built under tmp_path; no machine path is read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]

_FAKE_GATE = '''
from pathlib import Path


class GateReadRefused(Exception):
    pass


def compute_gate_report(repo_root, roadmap_id=None):
    root = Path(__file__).resolve().parents[2]
    return {"batons": [{"id": "b1", "path": "b1.md", "candidate": True,
                        "plan": {"path": "archive/engine-marker-" + root.name + ".md",
                                 "status": "approved"}}]}
'''


def _forwarder(tmp: Path) -> Path:
    """A copy of the CLIs and their lib under a tree with no coordinator_core/pyproject.toml."""
    bin_dir = tmp / "forwarder" / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("mise-prep-run.py", "mise-prep-entry.py"):
        shutil.copy2(_BIN / name, bin_dir / name)
    shutil.copytree(_BIN / "lib", bin_dir / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    return bin_dir


def _engine(tmp: Path) -> Path:
    """A minimal engine checkout: both markers, the published-build stamp, and a fake gate."""
    eng = tmp / "engine-xyz"
    roadmap = eng / "coordinator_core" / "roadmap"
    roadmap.mkdir(parents=True)
    (eng / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (eng / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (eng / "coordinator_core" / "_engine_stamp").write_text("stamp\n", encoding="utf-8")
    (roadmap / "__init__.py").write_text("", encoding="utf-8")
    (roadmap / "gate_v2.py").write_text(_FAKE_GATE, encoding="utf-8")
    return eng


def _env(tmp: Path, engine: Path | None) -> dict:
    home = tmp / "home"
    pointer_dir = home / ".coordinator-claude-settings" / "machine-local"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        (pointer_dir / ".claude-klabauter-root").write_text(str(engine), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("COORDINATOR_ENGINE_ROOT", "CLAUDE_KLABAUTER_ROOT", "PYTHONPATH",
                        "COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME")}
    env.update(HOME=str(home), CLAUDE_HOME=str(home),
               COORDINATOR_SETTINGS_HOME=str(home / ".coordinator-claude-settings"))
    return env


def _run_direct(bin_dir: Path, repo: Path, env: dict):
    return subprocess.run(
        [sys.executable, str(bin_dir / "mise-prep-run.py"), "--repo-root", str(repo)],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
def test_direct_path_reads_plan_set_under_the_launcher_engine_root(tmp_path):
    bin_dir, engine = _forwarder(tmp_path), _engine(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _env(tmp_path, engine)

    proc = _run_direct(bin_dir, repo, env)

    assert "No module named 'coordinator_core'" not in proc.stderr + proc.stdout
    assert proc.returncode == 0, proc.stderr
    assert f"SKIPPED-OUT-OF-TREE archive/engine-marker-{engine.name}.md" in proc.stderr

    launcher_root = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import cc_invoke; "
         "print(cc_invoke.resolve_engine_root(sys.argv[2]))",
         str(bin_dir / "lib"), str(bin_dir / "mise-prep-run.py")],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    assert Path(launcher_root).resolve() == engine.resolve()


def _run_apply_rulings(bin_dir: Path, repo: Path, payload: Path, env: dict):
    return subprocess.run(
        [sys.executable, str(bin_dir / "mise-prep-run.py"), "--repo-root", str(repo),
         "--apply-rulings", str(payload)],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
def test_apply_rulings_imports_coordinator_core_with_no_pythonpath(tmp_path):
    bin_dir, engine = _forwarder(tmp_path), _engine(tmp_path)
    (engine / "coordinator_core" / "roadmap" / "mise_residue.py").write_text(
        'RULINGS_OP = "plan.record_residue_rulings"\n', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = tmp_path / "rulings.json"
    payload.write_text('{"run_id": "r", "rulings": []}', encoding="utf-8")

    proc = _run_apply_rulings(bin_dir, repo, payload, _env(tmp_path, engine))

    assert "coordinator_core unresolvable" not in proc.stderr + proc.stdout


@pytest.mark.spawns_process
def test_apply_rulings_unresolvable_engine_still_refuses(tmp_path):
    bin_dir = _forwarder(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = tmp_path / "rulings.json"
    payload.write_text('{"run_id": "r", "rulings": []}', encoding="utf-8")

    proc = _run_apply_rulings(bin_dir, repo, payload, _env(tmp_path, None))

    assert proc.returncode == 2
    assert "coordinator_core unresolvable" in proc.stderr


@pytest.mark.spawns_process
def test_unresolvable_engine_refuses_naming_root_and_remediation(tmp_path):
    bin_dir = _forwarder(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = _run_direct(bin_dir, repo, _env(tmp_path, None))

    assert proc.returncode == 2
    assert "could not read the approved-plan set" in proc.stderr
    assert "engine root unresolved" in proc.stderr
    assert "COORDINATOR_ENGINE_ROOT" in proc.stderr
