"""An empty verify-leg verdict is re-asked once in-run, then halts as verify-leg-empty and is never recorded not_met."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import VERIFY_EMPTY_RETRY_JS, VERIFY_LEG_EMPTY, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_SIZING = "state/sizings/x.yaml"


def _script(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=_SIZING,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _halt_region(script: str) -> str:
    """The composed text from the owed-id derivation through the ledger-check halt clause."""
    start = script.index("const _rulable = ")
    end = script.index("}; }", script.index("{ _halted = { halted: 'ledger-check'", start)) + len("}; }")
    return script[start:end]


def _drive(monkeypatch: pytest.MonkeyPatch, verdicts: list, ledger_ids: list, withheld_ids: list | None = None) -> dict:
    """Runs the composed retry and halt JS under node with a stub verify leg answering `verdicts` in order."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    region = _halt_region(_script(monkeypatch))
    source = (
        "let _halted = null; let _ledger = null; let _verify = null; let _calls = 0; const _logs = [];\n"
        f"const log = (m) => _logs.push(m); const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _planRel = 'docs/plans/p.md';\n"
        f"const _manifest = {json.dumps({'ledger_item_ids': ledger_ids, 'withheld_ids': withheld_ids if withheld_ids is not None else []})};\n"
        "const _gate = {}; const _verifyLateIds = []; const _lateIds = []; const _recoveryWithheld = [];\n"
        "const _blockedChunks = []; const _incompleteChunks = []; const _lateUnplanned = []; const _ledgerSc = null;\n"
        "const _ledgerBlocking = (l) => (l?.items ?? []).filter((i) => i && i.status === 'not_met');\n"
        f"const _verdicts = {json.dumps(verdicts)};\n"
        "const _runVerify = async () => { _calls += 1; _verify = _verdicts[Math.min(_calls - 1, _verdicts.length - 1)]; return _verify; };\n"
        "(async () => {\n"
        "_verify = await _runVerify();\n"
        + VERIFY_EMPTY_RETRY_JS
        + "\n_ledger = (_verify && Array.isArray(_verify.items)) ? { all_of_pm_ask: false, items: _verify.items, conflicts: [] } : null;\n"
        + region
        + "\nconsole.log(JSON.stringify({ halted: _halted, calls: _calls, ledger: _ledger }));\n"
        "})();\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_two_empty_verdicts_retry_once_then_halt_verify_leg_empty_never_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], ["R1"])
    assert out["calls"] == 2
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert halted[VERIFY_LEG_EMPTY.replace("-", "_")] is True
    assert [i["id"] for i in halted["items"]] == ["R1"]
    assert all(i["status"] == VERIFY_LEG_EMPTY for i in halted["items"])
    assert VERIFY_LEG_EMPTY in halted["next_action"]
    assert "not_met" not in json.dumps(halted)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_every_owed_id_carries_verify_leg_empty_when_the_leg_returns_nothing_for_several_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    owed = ["R1", "R2", "R3"]
    out = _drive(monkeypatch, [{"items": []}], owed)
    assert out["calls"] == 2
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert [i["id"] for i in halted["items"]] == owed
    assert [i["status"] for i in halted["items"]] == [VERIFY_LEG_EMPTY] * len(owed)
    assert "not_met" not in json.dumps(halted)
    assert all(f"{i}" in halted["next_action"] for i in owed)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_verdict_on_the_retry_proceeds_without_a_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}, {"items": [{"id": "R1", "status": "met"}]}], ["R1"])
    assert out["calls"] == 2
    assert out["halted"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_returned_verdict_is_never_re_asked_and_a_genuine_not_met_stays_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": [{"id": "R1", "status": "not_met", "remaining": ["x"]}]}], ["R1"])
    assert out["calls"] == 1
    assert [i["status"] for i in out["halted"]["items"]] == ["not_met"]
    assert "verify_leg_empty" not in out["halted"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_all_ruled_out_empty_verdict_is_not_re_asked_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], [], withheld_ids=["R1"])
    assert out["calls"] == 1
    assert out["halted"] is None


def test_the_retry_is_one_bounded_re_ask_with_no_loop() -> None:
    assert VERIFY_EMPTY_RETRY_JS.count("_runVerify()") == 1
    assert "while" not in VERIFY_EMPTY_RETRY_JS and "for (" not in VERIFY_EMPTY_RETRY_JS


def test_the_retry_sits_after_the_verify_join_and_before_the_overlay_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    at = script.index(VERIFY_EMPTY_RETRY_JS)
    assert script.rindex("_verify = await _runVerify(); }", 0, at) < at
    assert at < script.index("_ledger = (_verify && Array.isArray(_verify.items))")
