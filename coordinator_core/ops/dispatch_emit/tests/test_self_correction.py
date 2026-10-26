"""Pins self_correction: red-ownership split, progress rule, fix-leg prompt and the JS mirrors."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.ops.dispatch_emit.ledger_recovery import RECOVERY_ATTEMPT_CAP
from coordinator_core.win_portability import no_console_creationflags

_WS = ["coordinator/bin/with-tier-t-slot", "coordinator/bin/tests/test_with_tier_t_slot.py"]

_CASES = [
    {"failing": ["coordinator/tests/test_a.py::t1", "coordinator_core/x/tests/test_b.py::t2"], "own": _WS, "kw": {}},
    {"failing": ["coordinator/bin/tests/test_with_tier_t_slot.py::t"], "own": _WS, "kw": {"pre_existing": ["coordinator/bin/tests/test_with_tier_t_slot.py::t"]}},
    {"failing": ["a/t.py::1", "b/t.py::2"], "own": [], "kw": {"caused": ["a/t.py::1", "b/t.py::2"], "foreign": ["b/t.py::2"]}},
    {"failing": ["a/t.py::1", "b/t.py::2", "c/t.py::3"], "own": [], "kw": {"caused": [], "reverted": [{"tests": ["b/t.py::2"]}], "pre_existing": ["c/t.py::3"]}},
    {"failing": [".\\win\\t.py::x", "./win/u.py::y"], "own": ["win/t.py"], "kw": {}},
    {"failing": [], "own": ["a"], "kw": {}},
]


def _py(case):
    r = sc.classify_reds(case["failing"], case["own"], **case["kw"])
    return {"owned": list(r.owned), "foreign": list(r.foreign), "unverified": list(r.unverified)}


def test_cap_is_the_recovery_cap():
    assert sc.SELF_CORRECTION_CAP == RECOVERY_ATTEMPT_CAP == 2


def test_c04f17_shape_is_all_foreign():
    r = sc.classify_reds(_CASES[0]["failing"], _WS, reverted=[])
    assert r.owned == ()
    assert r.foreign == tuple(sorted(_CASES[0]["failing"]))


def test_write_set_file_is_owned_even_when_pre_existing():
    nid = "coordinator/bin/tests/test_with_tier_t_slot.py::t"
    r = sc.classify_reds([nid], _WS, pre_existing=[nid])
    assert r.owned == (nid,) and r.foreign == ()


def test_caused_and_foreign_is_foreign_and_reverted_is_owned():
    r = sc.classify_reds(["a/t.py::1", "b/t.py::2"], [], caused=["a/t.py::1"], foreign=["a/t.py::1"], reverted=[{"tests": ["b/t.py::2"]}])
    assert r.owned == ("b/t.py::2",) and r.foreign == ("a/t.py::1",)
    assert r.unverified == ()


def test_caused_not_foreign_is_owned():
    r = sc.classify_reds(["a/t.py::1"], [], caused=["a/t.py::1"])
    assert r.owned == ("a/t.py::1",)


def test_unverified_outside_write_set_is_foreign_and_listed():
    r = sc.classify_reds(["u/t.py::1", "p/t.py::2"], [], caused=[], pre_existing=["p/t.py::2"])
    assert r.foreign == ("p/t.py::2", "u/t.py::1")
    assert r.unverified == ("u/t.py::1",)


def test_separators_and_dot_slash_normalise_and_output_is_sorted_unique():
    r = sc.classify_reds(["z\\t.py::1", "./z/t.py::1", "a/t.py::2"], ["z/t.py"])
    assert r.owned == ("z/t.py::1",)
    assert r.foreign == ("a/t.py::2",)


@pytest.mark.parametrize(
    "before,after,expected",
    [({"a", "b"}, {"b"}, True), ({"a"}, {"a"}, False), ({"a"}, {"b"}, True), ({"a"}, {"a", "b"}, False), (set(), {"a"}, False)],
)
def test_progressed(before, after, expected):
    assert sc.progressed(before, after) is expected


def test_halt_fix_prompt_carries_scope_never_delete_and_notes_path():
    p = sc.halt_fix_prompt("gaps", "slices", "fp", "state/warp/r/notes.jsonl")
    assert "${gaps}" in p and "${slices}" in p and "${fp}" in p
    assert "SCOPE" in p and "needs_paths" in p
    assert "Never resolve a finding by deleting the requirement" in p
    assert "state/warp/r/notes.jsonl" in p
    assert "Commit nothing" in p


def test_notes_sentence_is_shared_by_both_fix_prompts():
    from coordinator_core.ops.dispatch_emit import ledger_recovery

    path = "state/warp/r/notes.jsonl"
    sentence = sc.notes_sentence(path)
    assert path in sentence
    assert sentence in ledger_recovery.ledger_fix_prompt("r", "e", "f", "fp", path)
    assert sentence in sc.halt_fix_prompt("g", "s", "fp", path)


def test_schemas_are_closed():
    assert sc.HALT_FIX_SCHEMA["additionalProperties"] is False
    assert sc.HALT_FIX_SCHEMA["properties"]["status"]["enum"] == ["DONE", "BLOCKED", "SCOPE"]
    assert sc.HALT_REVERIFY_SCHEMA["additionalProperties"] is False
    assert set(sc.HALT_REVERIFY_SCHEMA["properties"]) == {"test", "slices"}


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


@pytest.mark.spawns_process
def test_js_mirrors_match_python_reference():
    node = _node()
    prog_cases = [[["a", "b"], ["b"]], [["a"], ["a"]], [["a"], ["b"]], [["a"], ["a", "b"]], [[], ["a"]]]
    program = (
        sc.CLASSIFY_REDS_JS + sc.PROGRESSED_JS
        + f"const cases = {json.dumps(_CASES)};\n"
        + f"const prog = {json.dumps(prog_cases)};\n"
        + "console.log(JSON.stringify({cls: cases.map((c) => classifyReds(c.failing, c.own, c.kw)),"
        " prog: prog.map(([b, a]) => progressed(b, a))}));\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["cls"] == [_py(c) for c in _CASES]
    assert got["prog"] == [sc.progressed(b, a) for b, a in prog_cases]
