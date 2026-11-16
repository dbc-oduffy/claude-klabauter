"""classify_reds attributes each finding in a node's failure message to its file; the JS twin agrees."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.win_portability import no_console_creationflags

_NODE = "coordinator/tests/test_bin_entrypoint_inertness.py::test_inert"
_OWN = ["coordinator/bin/update-docs.py", "docs/x.md"]
_PEER_MSG = "inert violations:\n  unimatrix-peer-router.py:1304 runs at import\n  coordinator/bin/peer.py:12 too"
_OWN_MSG = "inert violations:\n  coordinator/bin/update-docs.py:40 runs at import\n  coordinator/bin/peer.py:12 too"
_BASENAME_OWN_MSG = "`update-docs.py:40` runs at import"
_NO_FILE_MSG = "AssertionError: 3 != 4"


def test_finding_files_extracts_posix_paths_in_order_deduplicated():
    msg = "a\\b\\c.py:3 then ./d/e.py:9 and `unimatrix-peer-router.py:1304`, again a/b/c.py:7 (x.py::test_y)"
    assert sc.finding_files(msg) == ("a/b/c.py", "d/e.py", "unimatrix-peer-router.py")


def test_finding_files_ignores_text_without_path_line():
    assert sc.finding_files(_NO_FILE_MSG) == ()
    assert sc.finding_files("at 12:30 tests/x.py::test_a") == ()
    assert sc.finding_files("ratio 0.5:3 of v1.2:4") == ()
    assert sc.finding_files(None) == ()


def test_all_findings_outside_own_paths_is_foreign_not_unverified():
    r = sc.classify_reds([_NODE], _OWN, detail={_NODE: _PEER_MSG})
    assert r.owned == () and r.foreign == (_NODE,) and r.unverified == ()


def test_one_finding_in_own_path_is_owned():
    r = sc.classify_reds([_NODE], _OWN, detail={_NODE: _OWN_MSG})
    assert r.owned == (_NODE,) and r.foreign == ()


def test_bare_basename_of_an_own_path_counts_as_owned():
    r = sc.classify_reds([_NODE], _OWN, detail={_NODE: _BASENAME_OWN_MSG})
    assert r.owned == (_NODE,)


def test_absolute_path_into_the_repo_counts_as_owned():
    r = sc.classify_reds([_NODE], _OWN, detail={_NODE: "/work/repo/coordinator/bin/update-docs.py:40 bad"})
    assert r.owned == (_NODE,)


def test_pre_existing_node_is_foreign_whatever_the_findings_name():
    r = sc.classify_reds([_NODE], _OWN, pre_existing=[_NODE], detail={_NODE: _OWN_MSG})
    assert r.owned == () and r.foreign == (_NODE,)


def test_owned_finding_outranks_absence_from_passed_at_base():
    r = sc.classify_reds([_NODE], _OWN, passed_at_base=["other::t"], detail={_NODE: _OWN_MSG})
    assert r.owned == (_NODE,)


def test_foreign_findings_outrank_caused_and_diff_named():
    assert sc.classify_reds([_NODE], _OWN, caused=[_NODE], detail={_NODE: _PEER_MSG}).foreign == (_NODE,)
    assert sc.classify_reds([_NODE], _OWN, diff_named=[_NODE], detail={_NODE: _PEER_MSG}).foreign == (_NODE,)
    assert sc.classify_reds([_NODE], _OWN, caused=[_NODE], detail={_NODE: _PEER_MSG}).unverified == ()


def test_message_naming_no_file_keeps_node_level_rules():
    d = {_NODE: _NO_FILE_MSG}
    assert sc.classify_reds([_NODE], _OWN, detail=d).foreign == (_NODE,)
    assert sc.classify_reds([_NODE], _OWN, detail=d).unverified == (_NODE,)
    assert sc.classify_reds([_NODE], _OWN, caused=[_NODE], detail=d).owned == (_NODE,)
    assert sc.classify_reds([_NODE], [_NODE.split("::")[0]], detail=d).owned == (_NODE,)


def test_missing_detail_entry_keeps_node_level_rules():
    assert sc.classify_reds([_NODE], _OWN, detail={"other::t": _PEER_MSG}).unverified == (_NODE,)


def test_reverted_node_stays_owned_over_foreign_findings():
    r = sc.classify_reds([_NODE], _OWN, reverted=[{"tests": [_NODE]}], detail={_NODE: _PEER_MSG})
    assert r.owned == (_NODE,)


def test_foreign_red_still_wins_over_owned_finding():
    r = sc.classify_reds([_NODE], _OWN, foreign_red=[_NODE], detail={_NODE: _OWN_MSG})
    assert r.foreign == (_NODE,)


def _run(expr):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = sc.CLASSIFY_REDS_JS + f"console.log(JSON.stringify({expr}));\n"
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_MSGS = [
    _PEER_MSG,
    _OWN_MSG,
    _BASENAME_OWN_MSG,
    _NO_FILE_MSG,
    "a\\b\\c.py:3 then ./d/e.py:9 and `unimatrix-peer-router.py:1304`, again a/b/c.py:7 (x.py::test_y)",
    "/work/repo/coordinator/bin/update-docs.py:40 bad",
    "at 12:30 tests/x.py::test_a",
    "ratio 0.5:3 of v1.2:4",
]
_KWS = [
    {},
    {"pre_existing": [_NODE]},
    {"passed_at_base": ["other::t"]},
    {"caused": [_NODE], "diff_named": [_NODE]},
    {"reverted": [{"tests": [_NODE]}]},
    {"foreign_red": [_NODE]},
]


@pytest.mark.spawns_process
def test_js_finding_files_matches_python():
    got = _run(f"{json.dumps(_MSGS)}.map(findingFiles)")
    assert got == [list(sc.finding_files(m)) for m in _MSGS]


@pytest.mark.spawns_process
def test_js_classify_matches_python_with_detail():
    cases = [(m, kw) for m in _MSGS for kw in _KWS]
    calls = [[[_NODE], _OWN, dict(kw, detail={_NODE: m})] for m, kw in cases]
    got = _run(f"{json.dumps(calls)}.map(([f, o, k]) => classifyReds(f, o, k))")
    for (msg, kw), g in zip(cases, got):
        py = sc.classify_reds([_NODE], _OWN, detail={_NODE: msg}, **kw)
        assert g == {"owned": list(py.owned), "foreign": list(py.foreign), "unverified": list(py.unverified)}, (msg, kw)


@pytest.mark.spawns_process
def test_js_accepts_a_map_detail():
    got = _run(f"classifyReds({json.dumps([_NODE])}, {json.dumps(_OWN)}, {{detail: new Map([[{json.dumps(_NODE)}, {json.dumps(_PEER_MSG)}]])}})")
    assert got["foreign"] == [_NODE] and got["unverified"] == []
