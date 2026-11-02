"""A failing test red at the run base never owns a review_blocked halt or a self-correction fix leg."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose_halt_self_correction import _OWN, _OWN_TEST, _OWNED, _drive, _labels, _red

_OTHER = "pkg/tests/test_other.py::test_b"


def test_python_red_at_base_in_the_write_set_is_foreign_both_ways():
    own = [_OWN, _OWN_TEST]
    by_pre = sc.classify_reds([_OWNED], own, pre_existing=[_OWNED])
    assert by_pre.owned == () and by_pre.foreign == (_OWNED,)


def test_python_absence_at_base_never_disowns_a_test_the_run_added():
    # passed_at_base drops ids "not found" at base, so a run's own new failing test is absent
    # from it; only an explicit pre_existing entry may make an own-path id foreign.
    r = sc.classify_reds([_OWNED], [_OWN, _OWN_TEST], passed_at_base=[_OTHER])
    assert r.owned == (_OWNED,) and r.foreign == ()


def test_python_green_at_base_red_with_diff_and_no_evidence_stay_owned():
    own = [_OWN, _OWN_TEST]
    assert sc.classify_reds([_OWNED], own, passed_at_base=[_OWNED]).owned == (_OWNED,)
    assert sc.classify_reds([_OWNED], own, passed_at_base=[]).owned == (_OWNED,)
    assert sc.classify_reds([_OWNED], own).owned == (_OWNED,)


@pytest.mark.spawns_process
def test_js_mirror_agrees_with_python():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    own = [_OWN, _OWN_TEST]
    cases = [
        {"pre_existing": [_OWNED]},
        {"passed_at_base": [_OTHER]},
        {"passed_at_base": [_OWNED]},
        {"passed_at_base": []},
        {"passed_at_base": [_OTHER], "reverted": [{"tests": [_OWNED]}]},
    ]
    program = (
        sc.CLASSIFY_REDS_JS
        + f"const cases = {json.dumps(cases)};\n"
        + f"console.log(JSON.stringify(cases.map((k) => classifyReds([{json.dumps(_OWNED)}], {json.dumps(own)}, k))));\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    want = []
    for k in cases:
        r = sc.classify_reds([_OWNED], own, **k)
        want.append({"owned": list(r.owned), "foreign": list(r.foreign), "unverified": list(r.unverified)})
    assert json.loads(out.stdout) == want


@pytest.mark.spawns_process
def test_a_red_at_base_failure_runs_no_fix_leg_and_does_not_halt_review_blocked():
    out = _drive(reverify=_red(_OWNED, passed_at_base=[_OTHER], pre_existing=[_OWNED]), replies={})

    assert out["halted"] is None
    assert _labels(out) == []


@pytest.mark.spawns_process
def test_a_green_at_base_red_with_the_diff_still_halts_review_blocked():
    out = _drive(reverify=_red(_OWNED, passed_at_base=[_OWNED]), replies={})

    assert out["halted"]["halted"] == "review_blocked"
    assert [t["node_id"] for t in out["halted"]["failing"]] == [_OWNED]
