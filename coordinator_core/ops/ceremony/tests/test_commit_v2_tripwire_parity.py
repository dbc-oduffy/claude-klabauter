"""
coordinator_core.ops.ceremony.tests.test_commit_v2_tripwire_parity

Parity contract: `ceremony.commit_v2` refuses the three hard-deny commit shapes
the Bash commit tripwires refuse -- an incomplete op registration, an over-budget
governed CLAUDE.md, and a machine path in a tracked settings.json. The R cases
are refusals; the P cases are controls that commit.

Every refusal case asserts HEAD is unmoved: a gate that refuses after the commit
landed is not a gate. All git operations run against a throwaway repo under
`tmp_path`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.claude_md_budget import HARD_LIMIT_BYTES
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_CLASSIFICATION = "coordinator_core/authz/classification.py"
_SCOPES = "coordinator_core/op_scopes.py"
_REGISTRY_MAP = "coordinator_core/ops/_registry_map.py"
_OPS_INIT = "coordinator_core/ops/__init__.py"
_QUAD = "coordinator_core/authz/registration_quad.py"
_SURFACES = (_CLASSIFICATION, _SCOPES, _REGISTRY_MAP, _OPS_INIT, _QUAD)

_OLD_OP = "old.op"
_NEW_OP = "x.y"
_NEW_MODULE = "coordinator_core/ops/xy.py"
_OLD_MODULE = "coordinator_core/ops/oldop.py"


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _call(repo: Path, paths: list) -> dict:
    return commit_v2._handler(
        {"paths": paths, "message": "tripwire parity\n"}, repo_root=repo / ".git"
    )


def _surface_sources(ops: dict[str, bool]) -> dict[str, str]:
    """Surface-file text for `ops`; each value says whether the op is fully registered.

    The literal shapes mirror the real files: a MappingProxyType-wrapped dict, an
    AnnAssign dict, a list of tuples, a frozenset and a Mapping ledger.
    """
    complete = [op for op, full in ops.items() if full]
    classified = "".join(f'    "{op}": OpClass.MUTATING,\n' for op in complete)
    scoped = "".join(f'    "{op}": "common_dir",\n' for op in ops)
    mapped = "".join(
        f'    "{op}": "{_module_for(op).removesuffix(".py").replace("/", ".")}",\n'
        for op in complete
    )
    eager = "".join(
        f'    ("{_module_for(op).removesuffix(".py").replace("/", ".")}", "registers"),\n'
        for op in complete
    )
    return {
        _CLASSIFICATION: (
            "import types\n\n"
            "OP_CLASSIFICATION: types.MappingProxyType[str, object] = "
            "types.MappingProxyType({\n" + classified + "})\n"
        ),
        _SCOPES: "from typing import Dict\n\n_OP_KEY_SCOPE: Dict[str, str] = {\n" + scoped + "}\n",
        _REGISTRY_MAP: "from typing import Dict\n\nOP_MODULE_MAP: Dict[str, str] = {\n" + mapped + "}\n",
        _OPS_INIT: "from typing import List, Tuple\n\n_EAGER_OP_MODULES: List[Tuple[str, str]] = [\n" + eager + "]\n",
        _QUAD: (
            "from typing import Mapping\n\n"
            "_KNOWN_UNCLASSIFIED_OPS_DEBT: frozenset[str] = frozenset()\n"
            "_KNOWN_INCOMPLETE_REGISTRATIONS: Mapping[str, tuple[str, ...]] = {\n"
            f'    "{_OLD_OP}": ("OP_CLASSIFICATION", "OP_MODULE_MAP", "_EAGER_OP_MODULES"),\n'
            "}\n"
        ),
    }


def _module_for(op: str) -> str:
    return _OLD_MODULE if op == _OLD_OP else _NEW_MODULE


def _op_source(op: str, body: str = "pass") -> str:
    return f'@register_op("{op}")\ndef handler(params):\n    {body}\n'


def _init_bare(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _commit_all(repo: Path, message: str = "seed") -> None:
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", message], repo)


def _core_repo(tmp_path: Path) -> Path:
    """A repo whose HEAD carries the five baseline surfaces and one baselined op."""
    repo = _init_bare(tmp_path)
    _write(repo, ".coordinator-dev-repo", "")
    for rel, text in _surface_sources({_OLD_OP: False}).items():
        _write(repo, rel, text)
    _write(repo, _OLD_MODULE, _op_source(_OLD_OP))
    _write(repo, "coordinator/CLAUDE.md", "small\n")
    _commit_all(repo)
    return repo


def _assert_refused(repo: Path, before: str, result: dict, *needles: str) -> None:
    assert result["committed"] is False, result
    for needle in needles:
        assert needle in result["error"], result
    assert _head(repo) == before, "refused but the commit landed anyway"


def test_r1_new_op_without_table_entries_is_refused(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, _NEW_MODULE, _op_source(_NEW_OP))
    before = _head(repo)

    result = _call(repo, [_NEW_MODULE])

    _assert_refused(repo, before, result, "registration_quad_gate", _NEW_OP)


def test_r2_table_edits_left_out_of_paths_are_refused(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, _NEW_MODULE, _op_source(_NEW_OP))
    for rel, text in _surface_sources({_OLD_OP: False, _NEW_OP: True}).items():
        _write(repo, rel, text)
    before = _head(repo)

    result = _call(repo, [_NEW_MODULE])

    _assert_refused(repo, before, result, "registration_quad_gate", _NEW_OP)


def test_r3_over_budget_governed_claude_md_is_refused(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, "coordinator/CLAUDE.md", "x" * (HARD_LIMIT_BYTES + 1000) + "\n")
    before = _head(repo)

    result = _call(repo, ["coordinator/CLAUDE.md"])

    _assert_refused(repo, before, result, "claude_md_budget_gate")


def test_r4_machine_path_in_tracked_settings_json_is_refused(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, ".claude/settings.json", '{"hooks": {"cmd": "/home/someone/x"}}\n')
    before = _head(repo)

    result = _call(repo, [".claude/settings.json"])

    _assert_refused(repo, before, result, "machine_path_leak_gate")


def test_r5_syntactically_broken_surface_file_fails_closed(tmp_path: Path):
    repo = _core_repo(tmp_path)
    sources = _surface_sources({_OLD_OP: False, _NEW_OP: True})
    _write(repo, _NEW_MODULE, _op_source(_NEW_OP))
    _write(repo, _CLASSIFICATION, "OP_CLASSIFICATION = MappingProxyType({\n")
    for rel in (_SCOPES, _REGISTRY_MAP, _OPS_INIT):
        _write(repo, rel, sources[rel])
    before = _head(repo)

    result = _call(repo, [_NEW_MODULE, _CLASSIFICATION, _SCOPES, _REGISTRY_MAP, _OPS_INIT])

    _assert_refused(repo, before, result, "registration_quad_gate")


def test_p1_op_complete_on_all_surfaces_commits(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, _NEW_MODULE, _op_source(_NEW_OP))
    for rel, text in _surface_sources({_OLD_OP: False, _NEW_OP: True}).items():
        _write(repo, rel, text)
    before = _head(repo)

    result = _call(repo, [_NEW_MODULE, *_SURFACES])

    assert result["committed"] is True, result
    assert _head(repo) != before


def test_p2_repo_without_coordinator_core_commits_register_op_file(tmp_path: Path):
    repo = _init_bare(tmp_path)
    _write(repo, "seed.md", "seed\n")
    _commit_all(repo)
    _write(repo, "tools/looks_like_op.py", _op_source(_NEW_OP))
    before = _head(repo)

    result = _call(repo, ["tools/looks_like_op.py"])

    assert result["committed"] is True, result
    assert _head(repo) != before


def test_p3_baselined_op_restaged_commits(tmp_path: Path):
    repo = _core_repo(tmp_path)
    _write(repo, _OLD_MODULE, _op_source(_OLD_OP, body="return None"))
    before = _head(repo)

    result = _call(repo, [_OLD_MODULE])

    assert result["committed"] is True, result
    assert _head(repo) != before
