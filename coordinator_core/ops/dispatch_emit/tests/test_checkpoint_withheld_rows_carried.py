"""A checkpoint that withholds a declared path leaves its row carried, never seam-committed."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    _CHECKPOINT_COMMIT_SCHEMA,
    SeamInputs,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

pytestmark = pytest.mark.complete

_HELD = (
    "const _heldBack = _pending.filter((i) => [..._landed[i].paths, ..._landed[i].declared]"
    ".some((p) => _withheld.has(p)));"
)


def _plain():
    return compose_script(
        [[_write_row("C1"), _write_row("C2")]],
        name="wf",
        description="d",
        session_id="9d860631-c114-462a-a6ee-f685cc671ced",
        expected_branch="work/run",
        **REVIEW_KW,
    )


def _seamed():
    rows = [
        WaveRow(id="P1-C1", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[], body="Spec: docs/plans/p1.md (P1-C1)\n"),
        WaveRow(id="P2-C1", title="t", surface="s", writes=["b.py"], reads=["a.py"], depends_on=[], body="Spec: docs/plans/p2.md (P2-C1)\n"),
    ]
    return compose_script(
        [rows[:1], rows[1:]],
        name="n",
        description="d",
        plan_path="state/mise-inventory/x.spine.md",
        expected_branch="work/x",
        run_base_sha="a" * 40,
        seam=SeamInputs({}, False),
        **REVIEW_KW,
    )


def _commit_wave(script: str) -> str:
    return script.split("async function _commitWave(n, ids) {", 1)[1].split("\n  }\n", 1)[0]


def test_committer_schema_carries_withheld_paths():
    assert _CHECKPOINT_COMMIT_SCHEMA["properties"]["withheld_paths"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "withheld_paths" not in _CHECKPOINT_COMMIT_SCHEMA["required"]
    assert json.dumps(_CHECKPOINT_COMMIT_SCHEMA["properties"]["withheld_paths"]) in _plain()


def test_committer_brief_says_to_copy_checkpoint_withheld_paths_verbatim():
    assert "`checkpoint_withheld_paths` verbatim into `withheld_paths`" in _commit_wave(_plain())


@pytest.mark.parametrize("script", [_plain, _seamed], ids=["plain", "seam"])
def test_rows_touching_withheld_paths_stay_carried(script):
    body = _commit_wave(script())
    assert _HELD in body
    assert "_heldBack.forEach((i) => _carried.push(i));" in body
    assert body.index("_carried.length = 0;") < body.index("_heldBack.forEach")


def test_seam_commits_only_rows_not_held_back():
    body = _commit_wave(_seamed())
    assert "done.filter((i) => !_heldBack.includes(i)).forEach((i) => _seamCommitted.push(i));" in body
    assert "done.forEach((i) => _seamCommitted.push(i));" not in body
