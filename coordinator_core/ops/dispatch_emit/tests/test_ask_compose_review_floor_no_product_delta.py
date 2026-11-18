"""The composed review floor passes a fresh run whose review prep reports no product delta and still halts one with a delta."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.review_floor import HALT_REVIEW_FLOOR
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_FLOOR_OPEN = "  const _reviewVerdicts = "
_FLOOR_CLOSE = "\n  }\n"
_CODE = ["pkg/declared.py"]
_PREP = {"verdict": "single-reviewer-ok", "slices": [], "product_files": 0}
_VERDICT = {"slice_id": "s1", "verdict": "PASS", "sidecar_path": "x.md"}


def _floor_block() -> str:
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ask_compose, "_known_arm", lambda *_: "xs")
        script = compose_ask_script(
            repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
            wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
            **REVIEW_KW,
        )
    finally:
        mp.undo()
    start = script.index(_FLOOR_OPEN)
    return script[start : script.index(_FLOOR_CLOSE, script.index("_halted = { halted:", start)) + len(_FLOOR_CLOSE)]


def _floor(*, prep, wave=None, ledger=False, changed=None) -> dict | None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = (
        "let _halted = null; const _alreadyDelivered = null; const _metBySkipped = null; const _runId = 'run-1';\n"
        f"const _codeReviewOwed = true; const _manifest = {{ review_code_paths: {json.dumps(_CODE)} }};\n"
        f"const _resumeLedger = {json.dumps(ledger)}; const _firstHaltChanged = {json.dumps(changed)}; const _reviewPrep = {json.dumps(prep)}; "
        f"const _reviewWave = {json.dumps(wave or [])}; const _waveList = () => _reviewWave;\n"
        f"{_floor_block()}\n"
        "process.stdout.write(JSON.stringify(_halted));\n"
    )
    out = subprocess.run(
        [node, "-"], input=harness, capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


def test_no_product_delta_prep_with_declared_code_paths_and_no_verdicts_passes_the_floor() -> None:
    assert _floor(prep=_PREP) is None


@pytest.mark.parametrize(
    "prep",
    [
        {**_PREP, "product_files": 2},
        {**_PREP, "product_files": None},
        {k: v for k, v in _PREP.items() if k != "product_files"},
        {**_PREP, "error": "prep refused"},
        {**_PREP, "product_files": "0"},
        {**_PREP, "product_files": False},
        None,
    ],
)
def test_a_real_or_unreported_delta_with_no_verdicts_still_halts_review_floor(prep) -> None:
    halted = _floor(prep=prep)
    assert halted["halted"] == HALT_REVIEW_FLOOR
    assert _CODE[0] in halted["reason"]


def test_a_ledger_resume_still_halts_because_its_prep_counts_only_the_remaining_leg() -> None:
    assert _floor(prep=_PREP, ledger=True)["halted"] == HALT_REVIEW_FLOOR


def test_a_ledger_resume_whose_code_paths_all_lack_a_delta_since_the_first_halt_passes_the_floor() -> None:
    assert _floor(prep=_PREP, ledger=True, changed=["docs/other.md"]) is None
    assert _floor(prep=_PREP, ledger=True, changed=[]) is None


@pytest.mark.parametrize("changed", [["pkg/declared.py"], ["pkg/declared.py", "x.md"], None, "pkg/declared.py"])
def test_a_ledger_resume_with_a_delta_or_an_unknown_one_still_halts(changed) -> None:
    assert _floor(prep=_PREP, ledger=True, changed=changed)["halted"] == HALT_REVIEW_FLOOR


def test_a_fresh_run_ignores_the_first_halt_changed_set() -> None:
    assert _floor(prep={**_PREP, "product_files": 2}, changed=["docs/other.md"])["halted"] == HALT_REVIEW_FLOOR


def test_a_verdict_still_satisfies_the_floor_with_a_delta() -> None:
    assert _floor(prep={**_PREP, "product_files": 3}, wave=[_VERDICT]) is None
