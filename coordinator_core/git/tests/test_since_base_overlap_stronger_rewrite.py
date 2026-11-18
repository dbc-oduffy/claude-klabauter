"""since_base_overlap: a landing that rewrites a foreign plan-checkpoint line into stronger lines is not a drop;
a bare delete, a restore of base text and an unrelated nearby insert still are.

Shaped after the d8cd69-2 case: checkpoint 8e4c815b9a added `assert reply.get("advisory"), reply`; the run's
version moved `assert "result" in reply` above it and replaced it with two stronger asserts, which the old
delete+insert classification refused. Repos are written in-process with ``git_objects`` (no spawn).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from coordinator_core.git import since_base_overlap
from coordinator_core.git.tests.test_since_base_overlap import PATH, PLAN, _Repo, _join
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

# Same plan, base off the walked chain: a checkpoint of another run of the plan, so foreign to this run.
_OTHER_CHECKPOINT = f"1b codex C2: land pin\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: deadbeefdeadbeef"
_HEAD = ["def test_nag(entry):\n", "    reply = _invoke(entry)\n"]
_WEAK = '    assert reply.get("advisory"), reply\n'
_RESULT = '    assert "result" in reply\n'
_STRONG = [
    '    assert "past the XS bound" in reply.get("advisory", ""), reply\n',
    '    assert "coordinator-invoke engine_ask.submit" in reply["advisory"]\n',
]
_TAIL = ["\n", "def test_other():\n", "    assert True\n"]


def _scenario(tmp_path: Path, base_body: List[str], checkpoint_body: List[str]) -> str:
    repo = _Repo(tmp_path)
    base = repo.commit({PATH: _join(_HEAD + base_body + _TAIL)}, "base")
    repo.commit({PATH: _join(_HEAD + checkpoint_body + _TAIL)}, _OTHER_CHECKPOINT)
    return base


def _land(tmp_path: Path, body: List[str]) -> None:
    (tmp_path / PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / PATH).write_bytes(_join(_HEAD + body + _TAIL).encode())


def _check(tmp_path: Path, base: str):
    return since_base_overlap.check(tmp_path, [PATH], base, run_id=None, plan=PLAN)


def test_d8cd69_case_reorder_with_stronger_asserts_is_not_a_drop(tmp_path: Path) -> None:
    base = _scenario(tmp_path, [_RESULT], [_WEAK, _RESULT])
    _land(tmp_path, [_RESULT, *_STRONG])

    assert _check(tmp_path, base) == ([], [])


def test_in_place_stronger_rewrite_of_a_checkpoint_line_is_not_a_drop(tmp_path: Path) -> None:
    base = _scenario(tmp_path, [_RESULT], [_WEAK, _RESULT])
    _land(tmp_path, [_STRONG[0], _RESULT])

    assert _check(tmp_path, base) == ([], [])


def test_pure_delete_of_a_checkpoint_line_is_still_a_drop(tmp_path: Path) -> None:
    base = _scenario(tmp_path, [_RESULT], [_WEAK, _RESULT])
    _land(tmp_path, [_RESULT])

    findings, notes = _check(tmp_path, base)

    assert notes == []
    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_silent_revert_to_base_text_is_still_a_drop(tmp_path: Path) -> None:
    old = '    assert reply, "old"\n'
    base = _scenario(tmp_path, [old, _RESULT], [_WEAK, _RESULT])
    _land(tmp_path, [old, _RESULT])

    findings, _notes = _check(tmp_path, base)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_delete_beside_an_unrelated_insert_is_still_a_drop(tmp_path: Path) -> None:
    base = _scenario(tmp_path, [_RESULT], [_WEAK, _RESULT])
    _land(tmp_path, [_RESULT, "    unrelated_setup = 3\n"])

    findings, _notes = _check(tmp_path, base)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
