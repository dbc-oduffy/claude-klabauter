"""next_action_parts: a gated-off integration stage still carries the zero-slice review record."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.ops.dispatch_emit.wake_digest import INTEGRATION_NOT_RUN_VAR
from coordinator_core.win_portability import no_console_creationflags


def _params_js(with_bookkeeping=True):
    review_vars = {
        "prep": "_reviewPrep",
        "wave": "_reviewWave",
        "delivery": "_deliveryVerdict",
        "integration": "_reviewIntegration",
    }
    if with_bookkeeping:
        review_vars["bookkeeping_stem"] = "'bk-stem'"
        review_vars["plan_id_literal"] = "'plan-x'"
    _kind, _op, params = wd.next_action_parts(
        has_commit_request=True,
        review_vars=review_vars,
        test_var=None,
        falsifier_var=None,
        verification_var="_verifications",
        test_absent_status="not_run",
    )
    return params


def _inline_review(tmp_path, *, not_run, integration, wave="[]"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    harness = tmp_path / "h.js"
    harness.write_text(
        "const _cap = (v) => v;\n"
        "let _incompleteChunks = [];\n"
        "let _notStarted = [];\n"
        f"let {wd.PLAN_HELD_VAR} = {{}};\n"
        "let _reviewPrep = {slices:[]};\n"
        f"let _reviewWave = {wave};\n"
        "let _deliveryVerdict = null;\n"
        f"let _reviewIntegration = {integration};\n"
        f"let {INTEGRATION_NOT_RUN_VAR} = {not_run};\n"
        "let _verifications = [];\n"
        f"const p = {_params_js()};\n"
        "console.log(JSON.stringify(p.inline_review));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.spawns_process
def test_not_run_with_empty_wave_yields_bookkeeping_form(tmp_path):
    ir = _inline_review(tmp_path, not_run="true", integration="null")
    assert ir["integration_stem"] == "bk-stem"
    assert ir["slices"] == 0
    assert ir["fixes"] == 0
    assert "integration" not in ir


@pytest.mark.spawns_process
def test_owed_integration_returning_null_stays_null(tmp_path):
    assert _inline_review(tmp_path, not_run="false", integration="null") is None


@pytest.mark.spawns_process
def test_null_wave_with_not_run_stays_null(tmp_path):
    assert _inline_review(tmp_path, not_run="true", integration="null", wave="null") is None


@pytest.mark.spawns_process
def test_integrated_form_wins_when_integration_present(tmp_path):
    ir = _inline_review(
        tmp_path,
        not_run="false",
        integration="{fixes_applied:3, sidecar_path:'i.md', unresolved:[]}",
    )
    assert ir["integration_stem"] == "bk-stem"
    assert ir["fixes"] == 3
    assert ir["integration"]["sidecar"] == "i.md"


def test_no_bookkeeping_stem_has_no_fallback():
    assert INTEGRATION_NOT_RUN_VAR not in _params_js(with_bookkeeping=False)
