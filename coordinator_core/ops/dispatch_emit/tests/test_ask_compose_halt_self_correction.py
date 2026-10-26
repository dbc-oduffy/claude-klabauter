"""The composed ask script closes the run's own review gaps in-run before the review_blocked halt."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL, SELF_CORRECTION_CAP
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose import _JUDGE_KW, _compose

_SIZING = "state/sizings/x.yaml"
_OWN = "pkg/mod.py"
_OWN_TEST = "pkg/tests/test_mod.py"
_OWNED = f"{_OWN_TEST}::test_a"
_FOREIGN = "other/tests/test_peer.py::test_b"


def test_review_recovery_sits_between_the_review_wave_and_the_blocked_halt():
    script = _compose(**_JUDGE_KW)
    halt = ask_compose._review_blocked_halt_js(None)

    recover_at = script.index("const _reviewRecover = async")
    assert script.index("phase('Review wave')") < recover_at < script.index(halt)
    assert script.index("await _reviewRecover();") < script.index(halt)
    assert f"label: '{HALT_FIX_LABEL}'" in script and f"label: '{HALT_REVERIFY_LABEL}'" in script
    assert f"attempt <= {SELF_CORRECTION_CAP}" in script


_HARNESS = """
const log = () => {};
const _USAGE_LIMIT_RE = /quota/i;
const _haltOnUsageLimit = (e) => { throw e; };
let _halted = null;
const _runId = 'r1';
const _sizingRel = %(sizing)s, _planRel = 'p.md';
const _gate = { arm: %(arm)s };
const _reviewReused = false;
const _manifest = { review_declared_paths: [%(own)s], ledger_item_ids: ['R1'] };
let _reviewWave = %(wave)s;
let _reviewReverify = %(reverify)s;
const _calls = [];
const _script = %(replies)s;
const agent = async (prompt, opts) => {
  _calls.push({ label: opts.label, prompt });
  const queue = _script[opts.label] || [];
  const next = queue.shift();
  return next === undefined ? null : next;
};
%(refire)s
(async () => {
%(recover)s
%(halt)s
_refireNextAction();
console.log(JSON.stringify({ halted: _halted, calls: _calls, reverify: _reviewReverify, foreign: _foreignReds, wave: _reviewWave }));
})();
"""


def _drive(*, reverify, replies, wave=None, arm="s", own=(_OWN, _OWN_TEST)) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = _HARNESS % {
        "sizing": json.dumps(_SIZING),
        "arm": json.dumps(arm),
        "own": ", ".join(json.dumps(p) for p in own),
        "wave": json.dumps(wave or []),
        "reverify": json.dumps(reverify),
        "replies": json.dumps(replies),
        "refire": ask_compose._refire_next_action_helper_js(_SIZING),
        "recover": ask_compose._review_recover_js(
            head="H", anchor="A", session_tail="", record_dir="state/warp/r1", run_id="r1"
        ),
        "halt": ask_compose._review_blocked_halt_js(_SIZING),
    }
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _red(*ids, **kw) -> dict:
    rec = {
        "status_after": "fail",
        "failing": list(ids),
        "failing_detail": [{"node_id": i, "message": f"AssertionError in {i}\nsecond"} for i in ids],
        "pre_existing": [],
        "reverted": [],
    }
    rec.update(kw)
    return rec


def _green() -> dict:
    return _red(status_after="pass")


def _done(*paths) -> dict:
    return {"status": "DONE", "paths": list(paths), "reason": "ok", "needs_paths": []}


def _labels(out) -> list[str]:
    return [c["label"] for c in out["calls"]]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_owned_red_fixed_on_attempt_one_runs_no_halt():
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert out["halted"] is None
    assert _OWNED in out["calls"][0]["prompt"] and "AssertionError in" in out["calls"][0]["prompt"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_fix_that_swaps_the_gap_each_leg_stops_at_the_cap_as_exhausted():
    swap = [{"test": _red(f"{_OWN_TEST}::test_{n}"), "slices": []} for n in "bc"]
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN), _done(_OWN)], HALT_REVERIFY_LABEL: swap},
    )

    assert _labels(out).count(HALT_FIX_LABEL) == SELF_CORRECTION_CAP
    assert out["halted"]["halted"] == "review_blocked"
    assert out["halted"]["self_correction"]["status"] == "exhausted"
    assert out["halted"]["self_correction"]["attempts"] == SELF_CORRECTION_CAP


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unchanged_gaps_after_one_leg_stop_as_no_progress():
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN), _done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_OWNED), "slices": []}]},
    )

    assert _labels(out).count(HALT_FIX_LABEL) == 1
    assert out["halted"]["self_correction"]["status"] == "no-progress"
    assert "ended no-progress" in json.dumps(out["halted"]["next_action"])
    assert "executor fix leg against these failing tests" in json.dumps(out["halted"]["next_action"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_foreign_only_reds_run_no_leg_and_no_halt_and_are_recorded():
    out = _drive(reverify=_red(_FOREIGN), replies={})

    assert out["calls"] == []
    assert out["halted"] is None
    assert out["foreign"] == [_FOREIGN]
    assert out["reverify"]["foreign"] == [_FOREIGN]
    assert out["reverify"]["foreign_unverified"] == [_FOREIGN]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_mixed_run_briefs_the_fix_leg_with_the_owned_red_only_and_halts_without_the_foreign_one():
    out = _drive(
        reverify=_red(_OWNED, _FOREIGN),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _red(_OWNED, _FOREIGN), "slices": []}]},
    )

    assert _FOREIGN not in out["calls"][0]["prompt"]
    assert out["halted"]["failing"] == [{"node_id": _OWNED, "message": "AssertionError in " + _OWNED + "\nsecond"}]
    assert _FOREIGN not in json.dumps(out["halted"]["gaps"])


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "reply",
    [
        {"status": "SCOPE", "paths": [], "reason": "needs a new file", "needs_paths": ["tools/new_cli.py"]},
        _done("tools/new_cli.py"),
    ],
)
def test_a_scope_reply_or_a_write_outside_the_footprint_halts_naming_the_path_after_one_leg(reply):
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [reply, reply]})

    assert _labels(out) == [HALT_FIX_LABEL]
    sc = out["halted"]["self_correction"]
    assert sc["status"] == "scope" and sc["needs_paths"] == ["tools/new_cli.py"]
    assert "tools/new_cli.py" in json.dumps(out["halted"]["next_action"])
    assert "add-task" in json.dumps(out["halted"]["next_action"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_blocked_slice_without_a_red_is_briefed_by_its_sidecar_and_cleared_by_the_reverify():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0, "sidecar_path": "rev/s1.md"}]
    clear = {"test": _green(), "slices": [{"slice_id": "s1", "findings": [], "applied": True}]}
    out = _drive(
        reverify=_green(), wave=wave, replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [clear]}
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert "rev/s1.md" in out["calls"][0]["prompt"]
    assert out["halted"] is None
    assert out["wave"][0]["applied"] == 2


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_xs_arm_runs_no_self_correction_leg():
    out = _drive(reverify=_red(_OWNED), replies={}, arm=ask_compose.ARM_XS)

    assert out["calls"] == []
    assert out["halted"]["halted"] == "review_blocked"
    assert out["halted"]["self_correction"] is None
