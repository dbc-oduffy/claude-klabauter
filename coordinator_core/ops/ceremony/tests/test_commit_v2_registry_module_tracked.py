"""The terminal commit refuses a registry surface naming a module the committed tree lacks.

Drives `commit_v2._handler` at default params on a tmp_path repo: the staged `_registry_map.py`
names a module that exists only as an untracked worktree file.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CLS = "coordinator_core/authz/classification.py"
SCOPE = "coordinator_core/op_scopes.py"
MAP = "coordinator_core/ops/_registry_map.py"
EAGER = "coordinator_core/ops/__init__.py"
LEDGER = "coordinator_core/authz/registration_quad.py"
NEW_MODULE = "coordinator_core/ops/ceremony/newmod.py"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry(ops: dict[str, str]) -> dict[str, str]:
    return {
        CLS: "import types\nOP_CLASSIFICATION = types.MappingProxyType({\n"
        + "".join(f'    "{k}": 1,\n' for k in ops)
        + "})\n",
        SCOPE: "_OP_KEY_SCOPE = {\n" + "".join(f'    "{k}": "none",\n' for k in ops) + "}\n",
        MAP: "OP_MODULE_MAP = {\n" + "".join(f'    "{k}": "{m}",\n' for k, m in ops.items()) + "}\n",
        EAGER: "_EAGER_OP_MODULES = [\n"
        + "".join(f'    ("{m}", "registers"),\n' for m in sorted(set(ops.values())))
        + "]\n",
        LEDGER: "_KNOWN_UNCLASSIFIED_OPS_DEBT = frozenset()\n_KNOWN_INCOMPLETE_REGISTRATIONS = {}\n",
    }


def _seed(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _write(repo, "coordinator_core/ops/ceremony/oldmod.py", "X = 1\n")
    for rel, text in _registry({"a.b": "coordinator_core.ops.ceremony.oldmod"}).items():
        _write(repo, rel, text)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _call(repo: Path, paths: list[str]) -> dict:
    return commit_v2._handler({"paths": paths, "message": "registry edit\n"}, repo_root=repo / ".git")


def _both_ops_registry() -> dict[str, str]:
    return _registry(
        {
            "a.b": "coordinator_core.ops.ceremony.oldmod",
            "c.d": "coordinator_core.ops.ceremony.newmod",
        }
    )


def test_registry_naming_an_untracked_module_refuses_and_head_is_unmoved(tmp_path: Path):
    repo = _seed(tmp_path)
    before = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, NEW_MODULE, "Y = 1\n")  # untracked, not in the commit paths
    registry = _both_ops_registry()
    for rel, text in registry.items():
        _write(repo, rel, text)

    result = _call(repo, list(registry))

    assert result["committed"] is False, result
    assert "registration_quad_gate" in result["error"]
    assert "coordinator_core.ops.ceremony.newmod" in result["error"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == before


def test_registry_naming_a_module_in_the_commit_passes(tmp_path: Path):
    repo = _seed(tmp_path)
    _write(repo, NEW_MODULE, "Y = 1\n")
    registry = _both_ops_registry()
    for rel, text in registry.items():
        _write(repo, rel, text)

    result = _call(repo, [*registry, NEW_MODULE])

    assert result["committed"] is True, result


def test_gate_adds_under_50ms_process_time_with_zero_spawns(tmp_path: Path, monkeypatch):
    from coordinator_core.ops.ceremony.commit_gates import registration_quad_gate

    repo = _seed(tmp_path)
    files = {**_both_ops_registry(), NEW_MODULE: "Y = 1\n", "coordinator_core/ops/ceremony/oldmod.py": "X = 1\n"}
    read = lambda p: files[p].encode() if p in files else None  # noqa: E731
    paths = list(_both_ops_registry())

    registration_quad_gate(repo, paths, read)  # warm: the first call pays one-time import/parse cost

    def _no_spawn(*a, **k):
        raise AssertionError("registration_quad_gate spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    start = time.process_time()
    outcome = registration_quad_gate(repo, paths, read)
    elapsed = time.process_time() - start

    assert outcome.passed, outcome
    assert elapsed < 0.05
