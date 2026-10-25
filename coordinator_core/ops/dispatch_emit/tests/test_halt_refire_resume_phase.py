"""resume.phase on the halt envelope: carried for a mapped kind, optional for validation."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.phase_ledger import HALT_PHASE
from coordinator_core.win_portability import no_console_creationflags

CALL = 'Workflow({script: "x"})'


def _env(halted, **kw):
    return hr.refire_next_action(call=CALL, run_id="r1", sizing="s", halted=halted, **kw)


def test_mapped_kind_carries_phase_and_validates():
    env = _env("review_blocked", stage={"k": 1})
    assert env["resume"]["phase"] == HALT_PHASE["review_blocked"] == "review"
    assert env["resume"]["stage"] == {"k": 1}
    assert hr.is_refire_next_action(env)


def test_unmapped_kind_omits_phase_and_validates():
    env = _env("some_new_kind")
    assert "phase" not in env["resume"]
    assert hr.is_refire_next_action(env)


def test_older_halt_without_phase_still_accepted():
    env = _env("review_blocked")
    del env["resume"]["phase"]
    assert hr.is_refire_next_action(env)


def test_bogus_phase_or_extra_key_refused():
    env = _env("review_blocked")
    env["resume"]["phase"] = "nope"
    assert not hr.is_refire_next_action(env)
    env = _env("review_blocked")
    env["resume"]["extra"] = 1
    assert not hr.is_refire_next_action(env)
    env = _env("review_blocked")
    del env["resume"]["stage"]
    assert not hr.is_refire_next_action(env)


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_js_carries_phase_for_mapped_kind_only(tmp_path):
    js = hr.refire_next_action_js("'CALL'", "'why'", "_halted.halted", "'d'")
    script = (
        "let _runId='r', _sizingRel='s';\nlet _halted;\n"
        + js
        + "\nfor (const k of ['review_blocked','totally_unknown','toString']) {"
        "_halted={halted:k}; _refireNextAction(); console.log(JSON.stringify(_halted.next_action.resume));}"
    )
    f = tmp_path / "t.js"
    f.write_text(script)
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.splitlines()
    rev, unk, proto = (json.loads(x) for x in out)
    assert rev["phase"] == "review"
    assert "phase" not in unk and "phase" not in proto
