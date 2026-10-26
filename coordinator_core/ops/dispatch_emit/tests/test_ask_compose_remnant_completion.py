"""The composed ask script's bounded remnant leg: compose-and-grep pins plus node drives with stubbed replies.

The leg sits after the ledger-fix loop and before the close decision: one writing agent over the
not_met items no chunk could fix, one re-verify, then the same halt codes as before for a gap still open."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ledger_recovery import (
    REMNANT_ATTEMPT_CAP,
    REMNANT_LABEL,
    REMNANT_REVERIFY_LABEL,
)
from coordinator_core.ops.dispatch_emit.leg_register import REGISTER
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_recovery_loop import (
    _DISK,
    _LANDED,
    _NODE_PROGRAM,
    _RESUME,
    _TOKEN,
    _MET,
    _NAMED,
    _fix_labels,
    _labels,
    _run,
    _script,
    _unmet,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ORPHAN = _unmet(["something no chunk owns"])
_DONE = {"status": "DONE", "paths": ["pkg/a.py"], "reason": "completed"}


def _record(out: dict) -> dict:
    prompt = next(c for c in out["calls"] if c["label"] == "remnant-record")["prompt"]
    text = prompt.split("exactly this JSON text: ", 1)[1].split(". Edit nothing else", 1)[0]
    return json.loads(text)


def test_the_leg_composes_once_between_the_recovery_loop_and_the_close(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("const _remnantComplete = async") == 1
    assert script.count("await _remnantComplete()") == 1
    assert script.count(f"label: '{REMNANT_LABEL}'") == 1
    assert script.count(f"label: '{REMNANT_REVERIFY_LABEL}'") == 1
    assert f"cap: {REMNANT_ATTEMPT_CAP}," in script
    assert script.index("await _ledgerRecover()") < script.index("await _remnantComplete()") < script.index("const _rulable = ")
    assert script.index("await _remnantComplete()") < script.index("const _closeable")


def test_an_xs_arm_composes_no_remnant_leg(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_XS)
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_recovery_loop import _BLITZ_FN, _SIZING

    script = ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    assert "_remnantComplete" not in script


def test_every_new_leg_is_registered():
    labels = {leg.label for leg in REGISTER if leg.workflow == "ask"}
    assert {REMNANT_LABEL, REMNANT_REVERIFY_LABEL, "remnant-record"} <= labels


def test_an_unattributed_row_runs_one_remnant_leg_then_one_reverify_before_the_close(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert _fix_labels(out) == []
    assert labels.count(REMNANT_LABEL) == REMNANT_ATTEMPT_CAP == 1
    assert labels.count(REMNANT_REVERIFY_LABEL) == 1
    assert labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL) < labels.index("commit")
    assert out["result"]["halted_by"] is None
    rec = _record(out)
    assert rec["field"] == "remnant" and rec["loop_owner"] == "engine" and rec["status"] == "fixed"
    assert rec["items"] == [{"kind": "ledger", "id": "R1"}]


def test_a_gap_still_open_after_the_reverify_halts_with_todays_code_and_runs_no_second_leg(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_ORPHAN]}},
    )
    labels = _labels(out)
    assert labels.count(REMNANT_LABEL) == 1 and labels.count(REMNANT_REVERIFY_LABEL) == 1
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    rec = _record(out)
    assert rec["status"] == "exhausted" and rec["still_open"] == ["R1"]


def test_a_blocked_remnant_leg_skips_the_reverify_and_halts_as_before(monkeypatch):
    blocked = {"status": "BLOCKED", "paths": [], "reason": "needs a path outside the footprint"}
    out = _run(_script(monkeypatch), items=[_ORPHAN], replies={REMNANT_LABEL: blocked})
    assert REMNANT_REVERIFY_LABEL not in _labels(out)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert _record(out)["status"] == "blocked"


def test_a_write_outside_the_footprint_blocks_the_leg_and_is_recorded(monkeypatch):
    stray = {"status": "DONE", "paths": ["pkg/a.py", "elsewhere/stray.py"], "reason": "done"}
    out = _run(_script(monkeypatch), items=[_ORPHAN], replies={REMNANT_LABEL: stray})
    assert REMNANT_REVERIFY_LABEL not in _labels(out)
    rec = _record(out)
    assert rec["status"] == "blocked" and rec["paths"] == ["elsewhere/stray.py"]
    assert "pkg/a.py" in rec["footprint"]


def test_the_leg_prompt_is_bounded_to_the_footprint(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]}},
    )
    prompt = next(c for c in out["calls"] if c["label"] == REMNANT_LABEL)["prompt"]
    assert '"pkg/a.py"' in prompt and "something no chunk owns" in prompt


def test_a_cap_exhausted_chunk_is_completed_by_the_remnant_leg_and_stays_withheld_when_it_fails(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={
            "ledger-fix:C1": _DONE, "ledger-reverify": {"items": [_NAMED]},
            REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_NAMED]},
        },
    )
    labels = _labels(out)
    assert labels.index("ledger-reverify") < labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL)
    assert out["result"]["halted_by"] is None
    assert "C1" in out["result"]["incomplete"]


def test_a_cap_exhausted_chunk_the_remnant_leg_completes_leaves_no_open_row(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={
            "ledger-fix:C1": _DONE, "ledger-reverify": {"items": [_NAMED]},
            REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]},
        },
    )
    assert out["result"]["halted_by"] is None
    assert _record(out)["status"] == "fixed"


@pytest.mark.parametrize("extra", [{"rulable_ids": ["R1"]}, {"owed": [{"id": "X1", "reason": "em", "detail": "d"}]}])
def test_a_rulable_or_owed_row_runs_no_remnant_leg(monkeypatch, extra):
    item = {**_ORPHAN, **({"owed_by": ["X1"]} if "owed" in extra else {})}
    out = _run(_script(monkeypatch), items=[item], manifest={**_DISK, **extra})
    assert REMNANT_LABEL not in _labels(out)
    assert REMNANT_REVERIFY_LABEL not in _labels(out)


def _run_judged(script: str, *, judge: dict, replies: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": stage_reply(_DISK),
    }
    cfg = {
        "token": _TOKEN, "verify": {"items": [_MET], "judge": judge}, "commit": _LANDED, **readers_cfg(),
        "replies": {"gate": gate, "gate-stage": gate, **replies},
    }
    proc = subprocess.run(
        [node, "-"], input="globalThis.log = (m) => console.error('LOG ' + m);\n" + _NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert " leg threw" not in proc.stderr, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _judged_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_criterion_judge_brief", lambda *a, **k: ("judge brief", {"type": "object"}))
    return _script(monkeypatch)


_NOT_MET = {
    "status": "not_met", "reason": "DELIVERY.md absent", "differs_from_baseline": None, "sidecar_path": None,
    "clauses": [{"status": "not_met"}],
    "remnant": {"gap": "DELIVERY.md absent", "paths": ["DELIVERY.md"], "checks": ["test -f DELIVERY.md"]},
}


def test_a_not_met_criterion_runs_the_remnant_leg_on_its_remnant_paths_then_closes_on_a_met_reverify(monkeypatch):
    out = _run_judged(
        _judged_script(monkeypatch), judge=_NOT_MET,
        replies={
            REMNANT_LABEL: {"status": "DONE", "paths": ["DELIVERY.md"], "reason": "wrote it"},
            REMNANT_REVERIFY_LABEL: {"items": [_MET], "judge": {"status": "met", "reason": "ok"}},
        },
    )
    labels = _labels(out)
    assert labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL) < labels.index("commit")
    leg = next(c for c in out["calls"] if c["label"] == REMNANT_LABEL)["prompt"]
    assert '"DELIVERY.md"' in leg
    reverify = next(c for c in out["calls"] if c["label"] == REMNANT_REVERIFY_LABEL)["prompt"]
    assert "test -f DELIVERY.md" in reverify
    assert reverify.index("(1) TESTS") < reverify.index("(3) VERDICT")
    rec = _record(out)
    assert rec["items"] == [{"kind": "criterion", "id": "criterion"}] and rec["status"] == "fixed"


def test_a_criterion_still_not_met_after_the_reverify_does_not_close(monkeypatch):
    out = _run_judged(
        _judged_script(monkeypatch), judge=_NOT_MET,
        replies={
            REMNANT_LABEL: {"status": "DONE", "paths": ["DELIVERY.md"], "reason": "tried"},
            REMNANT_REVERIFY_LABEL: {"items": [_MET], "judge": _NOT_MET},
        },
    )
    labels = _labels(out)
    assert labels.count(REMNANT_LABEL) == 1 and "commit" not in labels
    assert _record(out)["still_open"] == ["criterion"]


def test_a_met_criterion_runs_no_remnant_leg(monkeypatch):
    out = _run_judged(_judged_script(monkeypatch), judge={"status": "met", "reason": "ok"}, replies={})
    assert REMNANT_LABEL not in _labels(out)


def test_a_held_row_and_a_clean_ledger_run_no_remnant_leg(monkeypatch):
    manifest = {**_DISK, "gated": [{"id": "C9", "reason": "depends_on_plan"}]}
    out = _run(_script(monkeypatch), items=[_unmet(["C9 waits on another plan"])], manifest=manifest)
    assert REMNANT_LABEL not in _labels(out)
    clean = _run(_script(monkeypatch), items=[_MET])
    assert REMNANT_LABEL not in _labels(clean)
