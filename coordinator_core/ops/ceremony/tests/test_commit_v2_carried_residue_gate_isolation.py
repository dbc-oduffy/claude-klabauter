"""`commit_v2` re-judges a gate-refused commit over the caller's own paths and withholds the carry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import stopped_runs
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cri-self"
RUN = "ask-20260101T000000-aaaaaa"
HALF = "coordinator_core/ops/half_peer.py"
OWN_HALF = "coordinator_core/ops/half_own.py"
CLEAN_RESIDUE = "notes/residue.md"

_SURFACES = {
    "coordinator_core/authz/classification.py": "OP_CLASSIFICATION = {}\n",
    "coordinator_core/op_scopes.py": "_OP_KEY_SCOPE = {}\n",
    "coordinator_core/ops/_registry_map.py": "OP_MODULE_MAP = {}\n",
    "coordinator_core/ops/__init__.py": "_EAGER_OP_MODULES = []\n",
    "coordinator_core/authz/registration_quad.py": (
        "_KNOWN_UNCLASSIFIED_OPS_DEBT = frozenset()\n_KNOWN_INCOMPLETE_REGISTRATIONS = {}\n"
    ),
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _op(key: str) -> str:
    return f'@register_op("{key}")\ndef _h(params):\n    return {{}}\n'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _write(root, "a.py", "v1\n")
    for rel, text in _SURFACES.items():
        _write(root, rel, text)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _write(root, "a.py", "v2\n")
    return root


def _residue(monkeypatch, *paths: str) -> None:
    monkeypatch.setattr(
        stopped_runs, "residue",
        lambda *a, **k: stopped_runs.StoppedResidue({RUN: tuple(paths)}, {}),
    )


def _files(repo: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())


def test_stopped_residue_that_half_registers_an_op_is_withheld_and_the_own_path_lands(repo, monkeypatch):
    _write(repo, HALF, _op("half.peer"))
    _residue(monkeypatch, HALF)

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True, result
    assert _files(repo) == {"a.py"}
    assert result["stopped_run_residue"] == {"carried": {}, "withheld": {HALF: "gate-refused"}}
    assert any(w.startswith("carried residue withheld: registration_quad_gate") for w in result["warnings"])
    assert "Stopped-Run-Residue" not in _git(["log", "-1", "--format=%B"], repo)
    assert HALF in _git(["status", "--porcelain", "-uall"], repo)


def test_own_left_behind_that_half_registers_an_op_is_left_not_carried(repo):
    _write(repo, HALF, _op("half.peer"))
    for rel in ("a.py", HALF):
        session_scope.touch(SELF, rel, cwd=str(repo))

    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_own_left_behind": True})

    assert result["committed"] is True, result
    assert _files(repo) == {"a.py"}
    assert result.get("own_left_behind") == {"carried": [], "left": [HALF]}, result
    assert any(w.startswith("carried residue withheld:") for w in result["warnings"])


def test_an_own_half_registration_still_refuses_and_names_only_the_own_op(repo, monkeypatch):
    _write(repo, HALF, _op("half.peer"))
    _write(repo, OWN_HALF, _op("half.own"))
    _residue(monkeypatch, HALF)

    result = _call(repo, {"paths": ["a.py", OWN_HALF], "message": "m"})

    assert result["committed"] is False
    assert "half.own" in result["error"]
    assert "half.peer" not in result["error"]
    assert _git(["log", "--format=%s"], repo).split() == ["seed"]


def test_gates_passing_with_residue_carries_it_unchanged(repo, monkeypatch):
    _write(repo, CLEAN_RESIDUE, "note\n")
    _residue(monkeypatch, CLEAN_RESIDUE)
    calls = []
    real = commit_v2._pre_commit_gates
    monkeypatch.setattr(commit_v2, "_pre_commit_gates", lambda *a, **k: calls.append(1) or real(*a, **k))

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True, result
    assert _files(repo) == {"a.py", CLEAN_RESIDUE}
    assert {k: list(v) for k, v in result["stopped_run_residue"]["carried"].items()} == {
        RUN: [CLEAN_RESIDUE]
    }
    assert result["stopped_run_residue"]["withheld"] == {}
    assert not any("withheld" in w for w in result["warnings"])
    assert len(calls) == 1


def test_the_success_path_pays_no_second_gate_pass_and_no_extra_spawns(repo, monkeypatch):
    _write(repo, "notes/r1.md", "1\n")
    _write(repo, "notes/r2.md", "2\n")
    spawns = []
    real_popen = subprocess.Popen

    class Counting(real_popen):
        def __init__(self, *a, **k):
            spawns.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    calls = []
    real = commit_v2._pre_commit_gates
    monkeypatch.setattr(commit_v2, "_pre_commit_gates", lambda *a, **k: calls.append(1) or real(*a, **k))

    _residue(monkeypatch, "notes/r1.md")
    first = _call(repo, {"paths": ["a.py"], "message": "m"})
    first_spawns, first_calls = len(spawns), len(calls)
    _write(repo, "a.py", "v3\n")
    spawns.clear()
    calls.clear()
    _residue(monkeypatch, "notes/r2.md")
    second = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert first["committed"] and second["committed"], (first, second)
    assert first_calls == len(calls) == 1
    assert first_spawns == len(spawns)
