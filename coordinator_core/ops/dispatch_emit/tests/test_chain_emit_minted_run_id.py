"""The chain hop starts the chained run under the run id the emit minted when the script path yields none.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_minted_run_id.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from pathlib import Path

_ASK = "ask-20261106T031723-e2c488"
_ASK_PATH = f"/r/scratch/warp/{_ASK}.workflow.mjs"
_MINTED = "warp-2026-11-06-sizing-f1a54d-20261106T041000-abcdef"


def _drive(handoff: dict, agent_reply: dict | None = None) -> dict:
    """Run the composed chain hop in node; returns {result, started} where started lists the paths `_startLoop` ran."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    values = des._chain_follow_values()
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], chain_js)
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps(_ASK_PATH)};\n"
        f"const first = {{ ok: true, result: {{ {json.dumps(CHAIN_HANDOFF_KEY)}: {json.dumps(handoff)} }} }};\n"
        "const started = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async (p) => { started.push(p); return { ok: true, result: { done: true } }; };\n"
        f"const agent = async () => ({json.dumps(agent_reply)});\n"
        "(async () => {\n" + chain_js + "\n})().then((result) => console.log(JSON.stringify({ result, started })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _relay(path: str, run_id: str) -> dict:
    return _drive({"ask_brief": "x"}, {"path": path, "run_id": run_id, "fire_args": {}})


def test_the_e2c488_failure_text_reproduces_for_an_unusable_path_and_no_run_id() -> None:
    out = _relay("/r/scratch/warp/chained.mjs", "")
    assert out["started"] == []
    assert 'chain emit path run id "" is not a fresh run id' in out["result"]["error"]


def test_a_path_without_a_run_id_starts_under_the_minted_run_id() -> None:
    out = _relay("/r/scratch/warp/chained.mjs", _MINTED)
    assert out["started"] == ["/r/scratch/warp/chained.mjs"]
    assert out["result"]["chained_from"] == _ASK


def test_the_path_basename_stays_the_trusted_run_id_over_a_relayed_ask_id() -> None:
    out = _relay(f"/r/scratch/warp/{_MINTED}.workflow.mjs", _ASK)
    assert out["started"] == [f"/r/scratch/warp/{_MINTED}.workflow.mjs"]


def test_a_minted_run_id_equal_to_the_asks_own_is_still_refused() -> None:
    out = _relay("/r/scratch/warp/chained.mjs", _ASK)
    assert out["started"] == []
    assert "is not a fresh run id" in out["result"]["error"]


def _verify_sequence(replies: list) -> dict:
    """Run `_verifiedScriptPath` in node against a relay whose successive receipt checks answer `replies`."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    block = des._RECEIPT_PATH_JS.replace("__AUTHORITY__", json.dumps("AUTH"))
    block = block.replace("const RECEIPT_RETRY_WAIT_MS = 1000", "const RECEIPT_RETRY_WAIT_MS = 0")
    code = (
        "const REPO_ROOT = '/r';\n"
        f"const REPLIES = {json.dumps(replies)};\n"
        "let calls = 0;\n"
        "const agent = async () => REPLIES[Math.min(calls++, REPLIES.length - 1)];\n"
        + block
        + "_verifiedScriptPath({ path: '/r/scratch/warp/ask-r1.workflow.mjs', run_id: 'ask-r1' })"
        ".then((v) => console.log(JSON.stringify({ v, calls })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_GOOD = {"found": True, "verdict": "MATCH"}
_TORN = {"found": True, "verdict": "MISMATCH", "detail": "MISMATCH receipt sha256 " + "ab" * 32 + " != script sha256 " + "cd" * 32}


def test_a_receipt_read_torn_by_a_retried_emit_is_reread_and_the_run_starts() -> None:
    out = _verify_sequence([_TORN, _GOOD])
    assert out["v"] == {"path": "/r/scratch/warp/ask-r1.workflow.mjs"}
    assert out["calls"] == 2


def test_a_receipt_that_stays_mismatched_is_still_refused() -> None:
    out = _verify_sequence([_TORN])
    assert "emit receipt sha mismatch" in out["v"]["error"] and "path" not in out["v"]
    assert out["calls"] == 3


_HANDOFF_MD = "/r/state/handoffs/2026-11/2026-11-09-x-ccbb6a.md"


def test_a_handoff_md_path_is_never_started_and_the_hop_emits_its_own_run() -> None:
    chained = f"/r/scratch/warp/{_MINTED}.workflow.mjs"
    out = _drive({"path": _HANDOFF_MD, "run_id": "h", "fire_args": {}, "ask_brief": "x"},
                 {"path": chained, "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == [chained]
    assert out["result"]["chained_from"] == _ASK


def test_a_non_script_path_from_the_chain_emit_is_refused_unstarted() -> None:
    out = _relay(_HANDOFF_MD, _MINTED)
    assert out["started"] == []
    assert "non-script path" in out["result"]["error"]


def test_a_handoff_naming_the_asks_own_script_mints_a_fresh_chained_run() -> None:
    chained = f"/r/scratch/warp/{_MINTED}.workflow.mjs"
    out = _drive({"path": _ASK_PATH, "run_id": _ASK, "fire_args": {}, "ask_brief": "x"},
                 {"path": chained, "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == [chained]
    assert out["result"]["chained_from"] == _ASK


def test_a_handoff_naming_the_asks_own_script_is_refused_when_the_fresh_emit_names_it_again() -> None:
    out = _drive({"path": _ASK_PATH, "run_id": _ASK, "fire_args": {}},
                 {"path": _ASK_PATH, "run_id": _ASK, "fire_args": {}})
    assert out["started"] == []
    assert "is not a fresh run id" in out["result"]["error"]


def test_the_handoff_run_id_is_used_when_the_gate_agent_already_emitted() -> None:
    out = _drive({"path": "/r/scratch/warp/chained.mjs", "run_id": _MINTED, "fire_args": {}})
    assert out["started"] == ["/r/scratch/warp/chained.mjs"]


_SIZING = "state/sizings/2026-11-14-slice.yaml"
_PARENT = "ask-20261114T211100-aaaaaa"


@pytest.fixture
def _halted_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose sizing already has a halted ask run: the launcher-minted entry run the chain hop emits from."""
    import yaml

    from coordinator_core.ops import sizing_intake_contract as contract
    from coordinator_core.ops.dispatch_emit import ask_compose, halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n")
    doc = contract.intake_record(name="slice", turn={"text": "plan it", "turn": 4}, today="2026-11-14")
    (tmp_path / _SIZING).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    halted_run.write_halt_marker(record_dir(tmp_path, _PARENT), halted="x", sizing_rel=_SIZING)
    return tmp_path


def _emit(repo, **extra) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    return _dispatch_emit({"ask": True, "sizing_path": _SIZING, "target_root": str(repo), **extra})


def _run_json(repo, run_id: str) -> dict:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    return json.loads((record_dir(repo, run_id) / "run.json").read_text(encoding="utf-8"))


def test_a_chain_emit_never_rebinds_onto_its_halted_chain_parent(_halted_parent) -> None:
    reply = _emit(_halted_parent, chain_parent=_PARENT)
    assert "error" not in reply
    assert reply["run_id"] != _PARENT
    assert not reply["path"].endswith(f"{_PARENT}.workflow.mjs")
    assert _run_json(_halted_parent, reply["run_id"]).get("resumed_from") != _PARENT


def test_a_plain_emit_still_resumes_the_halted_run_for_its_sizing(_halted_parent) -> None:
    reply = _emit(_halted_parent)
    assert "error" not in reply
    assert _run_json(_halted_parent, reply["run_id"])["resumed_from"] == _PARENT
