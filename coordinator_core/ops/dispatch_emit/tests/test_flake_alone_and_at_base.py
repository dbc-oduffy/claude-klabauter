"""Pins classify_flakes: a red is a flake only when it passes alone AND at base; absent evidence blocks."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.win_portability import no_console_creationflags

_A = "pkg/tests/test_a.py::t1"
_B = "pkg/tests/test_b.py::t2"

_CASES = [
    {"test": {"failing": [_A], "passed_alone": [_A], "passed_at_base": [_A]}, "run": "r1"},
    {"test": {"failing": [_A], "passed_alone": [_A], "passed_at_base": []}, "run": "r1"},
    {"test": {"failing": [_A], "passed_alone": [], "passed_at_base": [_A]}, "run": "r1"},
    {"test": {"failing": [_A, _B], "passed_alone": [_A, _B], "passed_at_base": [_B]}, "run": "r2"},
    {"test": {"failing": [_A]}, "run": "r1"},
    {"test": {"failing": [_A], "passed_alone": "x", "passed_at_base": None}, "run": "r1"},
    {"test": {}, "run": "r1"},
    {
        "test": {
            "failing": [".\\pkg\\tests\\test_a.py::t1"],
            "failing_detail": [{"node_id": "pkg/tests/test_a.py::t1", "message": "boom\nsecond line"}],
            "passed_alone": ["./pkg/tests/test_a.py::t1"],
            "passed_at_base": ["pkg\\tests\\test_a.py::t1"],
        },
        "run": "r3",
    },
]


def test_alone_and_at_base_is_a_flake_with_evidence():
    test = {
        "failing": [_A],
        "failing_detail": [{"node_id": _A, "message": "AssertionError: x\nmore"}],
        "passed_alone": [_A],
        "passed_at_base": [_A],
    }
    remaining, flakes = sc.classify_flakes(test, "run9")
    assert remaining == []
    assert flakes == [
        {"node_id": _A, "run": "run9", "evidence": "AssertionError: x; passed alone; passed at base"}
    ]


def test_missing_alone_or_missing_at_base_still_blocks():
    for test in (
        {"failing": [_A], "passed_alone": [_A], "passed_at_base": []},
        {"failing": [_A], "passed_alone": [], "passed_at_base": [_A]},
        {"failing": [_A], "passed_alone": [_A]},
        {"failing": [_A], "passed_at_base": [_A]},
    ):
        assert sc.classify_flakes(test, "r") == ([_A], [])


def test_non_list_fields_mean_no_flakes():
    assert sc.classify_flakes({"failing": [_A], "passed_alone": "x", "passed_at_base": None}, "r") == ([_A], [])
    assert sc.classify_flakes({}, "r") == ([], [])


def test_only_the_proven_id_is_split_out():
    remaining, flakes = sc.classify_flakes(_CASES[3]["test"], "r2")
    assert remaining == [_A]
    assert [f["node_id"] for f in flakes] == [_B]


def test_schema_carries_optional_alone_and_base_lists():
    props = sc.HALT_REVERIFY_SCHEMA["properties"]["test"]["properties"]
    assert props["passed_alone"] == props["passed_at_base"] == {"type": "array", "items": {"type": "string"}}
    assert "passed_alone" not in sc.HALT_REVERIFY_SCHEMA["properties"]["test"]["required"]
    assert "passed_at_base" not in sc.HALT_REVERIFY_SCHEMA["properties"]["test"]["required"]


@pytest.mark.spawns_process
def test_js_mirror_matches_python_reference():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        sc.CLASSIFY_FLAKES_JS
        + f"const cases = {json.dumps(_CASES)};\n"
        + "console.log(JSON.stringify(cases.map((c) => classifyFlakes(c.test, c.run))));\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    want = [list(sc.classify_flakes(c["test"], c["run"])) for c in _CASES]
    assert got == want
