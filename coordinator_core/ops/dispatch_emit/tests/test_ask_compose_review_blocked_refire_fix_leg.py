"""A review_blocked refire whose review phase is reused still runs _reviewRecover's fix leg."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose_halt_self_correction import _FOREIGN, _HARNESS, _OWN, _OWN_TEST, _OWNED, _SIZING, _done, _green, _red

pytestmark = [pytest.mark.spawns_process]


def _drive(*, reverify, replies, wave=None, reused=True) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = _HARNESS.replace("const _reviewReused = false;", f"const _reviewReused = {json.dumps(reused)};")
    program = harness % {
        "sizing": json.dumps(_SIZING),
        "arm": json.dumps("s"),
        "own": ", ".join(json.dumps(p) for p in (_OWN, _OWN_TEST)),
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


def _labels(out) -> list[str]:
    return [c["label"] for c in out["calls"]]


def test_a_reused_review_with_an_owned_red_runs_the_fix_leg_and_clears_the_halt():
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert out["halted"] is None


def test_a_reused_review_with_a_blocked_slice_runs_the_fix_leg():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0}]
    out = _drive(
        reverify=_green(),
        wave=wave,
        replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert _labels(out)[:1] == [HALT_FIX_LABEL]


def test_a_reused_review_with_only_foreign_or_pre_existing_reds_runs_no_leg():
    out = _drive(reverify=_red(_FOREIGN, _OWNED, pre_existing=[_OWNED]), replies={})

    assert out["calls"] == []
    assert out["halted"] is None


def test_a_reused_clean_review_runs_no_leg():
    out = _drive(reverify=_green(), replies={})

    assert out["calls"] == []
    assert out["halted"] is None
