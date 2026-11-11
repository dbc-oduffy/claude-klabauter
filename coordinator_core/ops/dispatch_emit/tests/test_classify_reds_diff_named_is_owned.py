"""A failing id whose test names a symbol the run's diff changed is owned; explicit pre_existing still wins."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.win_portability import no_console_creationflags

_EDITED = "pkg/worker_init.py"
_ID = "tests/test_pool_worker.py::test_worker_init_calls_the_binder_before_anything_else"
_OTHER = "tests/test_pool_worker.py::test_other"


def test_python_diff_named_id_outside_write_set_with_empty_passed_at_base_is_owned():
    r = sc.classify_reds([_ID], [_EDITED], passed_at_base=[], diff_named=[_ID])
    assert r.owned == (_ID,) and r.foreign == () and r.unverified == ()


def test_python_diff_named_outranks_partial_passed_at_base():
    r = sc.classify_reds([_ID], [_EDITED], passed_at_base=[_OTHER], diff_named=[_ID])
    assert r.owned == (_ID,) and r.foreign == ()


def test_python_diff_named_ids_are_posix_normalised():
    r = sc.classify_reds([_ID], [_EDITED], diff_named=[".\\" + _ID.replace("/", "\\")])
    assert r.owned == (_ID,)


def test_python_without_diff_named_the_same_id_stays_foreign_unverified():
    r = sc.classify_reds([_ID], [_EDITED], passed_at_base=[])
    assert r.owned == () and r.foreign == (_ID,) and r.unverified == (_ID,)
    r = sc.classify_reds([_ID], [_EDITED], passed_at_base=[_OTHER], diff_named=[_OTHER])
    assert r.foreign == (_ID,)


def test_python_pre_existing_still_wins_over_diff_named():
    r = sc.classify_reds([_ID], [_EDITED], pre_existing=[_ID], diff_named=[_ID])
    assert r.owned == () and r.foreign == (_ID,)


def _run(failing, kw):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        sc.CLASSIFY_REDS_JS
        + f"console.log(JSON.stringify(classifyReds({json.dumps(failing)}, {json.dumps([_EDITED])}, {json.dumps(kw)})));\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_CASES = [
    {"passed_at_base": [], "diff_named": [_ID]},
    {"passed_at_base": [_OTHER], "diff_named": [_ID]},
    {"diff_named": ["./" + _ID]},
    {"passed_at_base": [], "diff_named": [_OTHER]},
    {"pre_existing": [_ID], "diff_named": [_ID]},
    {"passed_at_base": [_ID], "pre_existing": [_ID], "diff_named": [_ID]},
]


@pytest.mark.spawns_process
@pytest.mark.parametrize("kw", _CASES)
def test_js_matches_python_for_diff_named(kw):
    py = sc.classify_reds([_ID], [_EDITED], **kw)
    got = _run([_ID], kw)
    assert got == {"owned": list(py.owned), "foreign": list(py.foreign), "unverified": list(py.unverified)}


@pytest.mark.spawns_process
def test_js_diff_named_is_owned_and_pre_existing_wins():
    assert _run([_ID], {"passed_at_base": [], "diff_named": [_ID]})["owned"] == [_ID]
    got = _run([_ID], {"pre_existing": [_ID], "diff_named": [_ID]})
    assert got["owned"] == [] and got["foreign"] == [_ID]
