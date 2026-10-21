"""The ask route composes no standalone read, restage or run-base leg: each engine-owned stage agent returns the
manifest itself, the run base is the manifest's head_sha, and the one conditional `blitz-stage-read` leg exists
only on the plan-blitz arm behind the not-whole / readyOp-did-not-run predicate."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _ask, _ask_scripts
from coordinator_core.win_portability import no_console_creationflags

_RETIRED = ("manifest-read", "ledger-restage", "ready-restage", "run-base")
_BLITZ_LEG = "blitz-stage-read"
_SHA = "e" * 40


def _labels(script: str) -> set[str]:
    return set(re.findall(r"label: '([^']*)'", script))


def test_no_ask_script_composes_a_retired_mechanical_leg(tmp_path):
    for script in _ask_scripts(tmp_path):
        assert not set(_RETIRED) & _labels(script), _labels(script)
        assert "rev-parse HEAD" not in script


def test_the_blitz_stage_read_leg_composes_only_with_the_plan_blitz_fold(tmp_path):
    scripts = _ask_scripts(tmp_path)
    with_leg = [s for s in scripts if _BLITZ_LEG in _labels(s)]
    assert with_leg, "no composed ask script carries the plan-blitz arm's leg"
    for script in scripts:
        has_leg = _BLITZ_LEG in _labels(script)
        assert has_leg == ("const _blitzStageRead" in script)
        assert script.count(f"label: '{_BLITZ_LEG}'") == (1 if has_leg else 0)
    assert _BLITZ_LEG not in _labels(_ask(stop_after=STOP_AFTER_PLAN))


def test_the_leg_is_called_only_behind_the_not_whole_or_not_run_predicate(tmp_path):
    for script in _ask_scripts(tmp_path):
        for call in re.finditer(r"await _blitzStageRead\((\w+)\)", script):
            entry = call.group(1)
            line = script[script.rindex("\n", 0, call.start()) + 1:script.index("\n", call.end())]
            assert "!_stagedManifest && !_stageRefusal" in line, line
            assert (
                f"{entry}.readyOpReply != null || {entry}.readyOpError == null "
                f"|| /did not run/.test(String({entry}.readyOpError))"
            ) in line, line


def test_every_engine_owned_fold_site_returns_the_manifest_under_the_strict_schema():
    strict = ask_compose._MANIFEST_READ_SCHEMA
    assert {"head_sha", "test_targets", "rows", "review_declared_paths"} <= set(strict["required"])
    for schema in (ask_compose._GATE_STAGED_SCHEMA, ask_compose._REWORD_SCHEMA, ask_compose._REVISE_STAGED_SCHEMA,
                   ask_compose._SPINE_REVIEW_STAGED_SCHEMA, ask_compose._REPLAN_STAGED_SCHEMA,
                   ask_compose._ASK_SCOUT_STAGED_SCHEMA, ask_compose._BLITZ_STAGE_READ_SCHEMA):
        manifest = schema["properties"]["manifest"]
        assert manifest["required"] == strict["required"], schema
        assert manifest["properties"] == strict["properties"], schema
        assert "null" in manifest["type"]
    assert ask_compose._PLAN_STAGED_SCHEMA["properties"]["manifest"] == strict


def _node(program: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    out = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, timeout=60, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_run_base_is_the_relayed_manifests_head_sha_and_a_missing_one_is_empty():
    script = _ask()
    line = next(ln.strip() for ln in script.splitlines() if ln.strip().startswith("_runBase = /^[0-9a-f]{40}$/"))
    for manifest, want in (({"head_sha": _SHA}, _SHA), ({"head_sha": None}, ""), ({"head_sha": "HEAD"}, "")):
        program = f"let _runBase = 'emit'; const _manifest = {json.dumps(manifest)}; {line}\nconsole.log(JSON.stringify(_runBase));"
        assert _node(program) == want


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_trimmed_engine_relay_halts_by_name_and_a_whole_one_proceeds():
    from coordinator_core.ops.dispatch_emit.tests.test_ask_fold_reads_disk_manifest_e2e import (
        _TRIMMED_RELAY, _WHOLE, _fold,
    )

    out = _fold(_TRIMMED_RELAY)
    assert out["halted"]["reason"] == "stage manifest relay is not whole: review_declared_paths"
    out = _fold(_WHOLE)
    assert out["halted"] is None and out["manifest"] == _WHOLE and out["seen"] == []
