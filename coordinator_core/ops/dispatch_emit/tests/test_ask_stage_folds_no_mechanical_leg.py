"""The ask route composes no standalone read, restage or run-base leg: each engine-owned stage agent returns the
stage reply itself, the run base is the manifest's head_sha, and the one conditional `blitz-stage-read` leg exists
only on the plan-blitz arm behind the readyOp-did-not-run predicate."""

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


def test_the_blitz_stage_read_leg_composes_only_with_the_plan_blitz_fold(tmp_path):  # noqa: D103
    scripts = _ask_scripts(tmp_path)
    with_leg = [s for s in scripts if _BLITZ_LEG in _labels(s)]
    assert with_leg, "no composed ask script carries the plan-blitz arm's leg"
    for script in scripts:
        has_leg = _BLITZ_LEG in _labels(script)
        assert has_leg == ("const _blitzStageRead" in script)
        assert script.count(f"label: '{_BLITZ_LEG}'") == (1 if has_leg else 0)
    assert _BLITZ_LEG not in _labels(_ask(stop_after=STOP_AFTER_PLAN))


def test_the_leg_is_called_only_when_readyop_never_ran(tmp_path):
    for script in _ask_scripts(tmp_path):
        for call in re.finditer(r"await _blitzStageRead\(\)", script):
            lines = script[: call.end()].splitlines()
            guard, branch = lines[-3], lines[-1]
            assert "!_stagedManifest && !_stageRefusal && !_diskHalt && !_relayGap" in guard, guard
            assert "readyOpReply != null" in lines[-2], lines[-2]
            assert (
                "readyOpError == null || /did not run/.test(String(" in branch and branch.lstrip().startswith("else if")
            ), branch


def test_every_engine_owned_fold_site_returns_the_stage_reply_under_the_stage_reply_schema():
    schema_of = ask_compose.STAGE_REPLY_SCHEMA
    assert set(schema_of["properties"]) == {"manifest_handle", "error", "owed_em_ids", "owed_plan"}
    for schema in (ask_compose._GATE_STAGED_SCHEMA, ask_compose._REWORD_SCHEMA, ask_compose._REVISE_STAGED_SCHEMA,
                   ask_compose._SPINE_REVIEW_STAGED_SCHEMA, ask_compose._REPLAN_STAGED_SCHEMA,
                   ask_compose._ASK_SCOUT_STAGED_SCHEMA, ask_compose._BLITZ_STAGE_READ_SCHEMA):
        reply = schema["properties"]["stage_reply"]
        assert {k: v for k, v in reply.items() if k != "type"} == {k: v for k, v in schema_of.items() if k != "type"}
        assert "null" in reply["type"]
        assert "manifest" not in schema["properties"] and "stage_error" not in schema["properties"]
    assert ask_compose._PLAN_STAGED_SCHEMA["properties"]["stage_reply"] == schema_of
    assert "stage_reply" in ask_compose._PLAN_STAGED_SCHEMA["required"]


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
def test_a_trimmed_engine_manifest_halts_by_name_and_a_whole_one_proceeds():
    from coordinator_core.ops.dispatch_emit.tests.test_ask_fold_reads_disk_manifest_e2e import (
        _TRIMMED, _WHOLE, fold_manifest,
    )

    out = fold_manifest(_TRIMMED)
    assert out["halted"]["reason"] == "stage manifest relay is not whole: review_declared_paths"
    out = fold_manifest(_WHOLE)
    assert out["halted"] is None and out["manifest"] == _WHOLE and set(out["seen"]) == {"stage-reply-page"}
