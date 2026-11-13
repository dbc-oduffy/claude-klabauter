"""A node id a run-edited test file no longer holds (pytest: `not found:`) is no blocker, in the
reverify op, the Python blocker and the emitted JS; a real failure in that file still blocks."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.review_test_blocker import from_reverify, own_diff_blockers
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.win_portability import no_console_creationflags

GONE = "tests/test_plain.py::test_old_name"
REAL = "tests/test_plain.py::test_p"
NOT_FOUND = "ERROR: not found: tests/test_plain.py::test_old_name\n(no match in any of [<Module test_plain.py>])"


def _result(**over):
    return {
        "failing": [GONE, REAL],
        "failing_detail": [
            {"node_id": GONE, "message": NOT_FOUND},
            {"node_id": REAL, "message": "assert False"},
        ],
        **over,
    }


def test_renamed_node_key_is_excluded_and_the_real_failure_still_blocks():
    r = _result(renamed_node=[GONE])
    assert [b.node_id for b in from_reverify(r)] == [REAL]
    assert [b.node_id for b in own_diff_blockers(r, ["tests/test_plain.py"])] == [REAL]


def test_not_found_message_in_an_own_file_is_excluded_even_when_green_at_base():
    r = _result(passed_at_base=[GONE])
    assert [b.node_id for b in own_diff_blockers(r, ["tests/test_plain.py"])] == [REAL]


def test_not_found_node_in_a_file_the_run_did_not_edit_still_blocks():
    r =_result(passed_at_base=[GONE])
    assert [b.node_id for b in own_diff_blockers(r, ["pkg/other.py"])] == [GONE]


def test_a_missing_fixture_is_a_real_failure_not_a_rename():
    r = _result(failing_detail=[{"node_id": GONE, "message": "fixture 'x' not found"}])
    assert GONE in [b.node_id for b in own_diff_blockers(r, ["tests/test_plain.py"])]


def test_op_classes_not_found_node_in_edited_file_as_renamed_and_names_its_file(world):
    root, _orig, _declared = world
    (root / "tests/test_plain.py").write_bytes(b"def test_p():\n    assert True\n# edited\n")
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[GONE, REAL],
        failing_output={GONE: NOT_FOUND, REAL: "assert False"},
    )
    assert out["renamed_node"] == [GONE] and out["recollect"] == ["tests/test_plain.py"]
    assert GONE not in out["unattributed"] and GONE not in out["pre_existing"]
    assert [b.node_id for b in from_reverify({"failing": [GONE, REAL], "renamed_node": out["renamed_node"]})] == [REAL]


def test_op_does_not_class_not_found_in_an_unedited_file(world):
    root, _orig, _declared = world
    node = "tests/test_plain.py::test_old_name"
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[node],
        failing_output={node: NOT_FOUND},
    )
    assert out["renamed_node"] == [] and out["recollect"] == []


def _js_failing(src: dict, own: list) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    frag = ask_compose._test_blockers_js("_r", "_t", "_own")
    program = f"const _r = {json.dumps(src)}; const _own = {json.dumps(own)};\n{frag}\nconsole.log(JSON.stringify(_tFailing.map((t) => t.node_id)));"
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_emitted_js_drops_the_renamed_node_and_keeps_the_real_failure():
    assert _js_failing(_result(renamed_node=[GONE]), ["tests/test_plain.py"]) == [REAL]
    assert _js_failing(_result(), ["tests/test_plain.py"]) == [REAL]


def test_emitted_js_keeps_a_not_found_node_outside_the_edited_files():
    assert _js_failing(_result(passed_at_base=[GONE]), ["pkg/other.py"]) == [GONE]
