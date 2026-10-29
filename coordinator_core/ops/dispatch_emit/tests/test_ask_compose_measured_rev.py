"""The verify leg builds the measured rev before the scoped tests, and the ledger charter reads a HEAD-measured gate as evidence."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_ENV, MEASURED_REV_OP
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose

_CHARTER_CLAUSE = "candidate commit, so it is evidence about this run"
_REFUSED_CLAUSE = "when the measured-rev op refused, that gate measured HEAD, not the change"


def test_measured_rev_instruction_precedes_the_scoped_test_instruction():
    script = _compose(**_JUDGE_KW)
    # Each verify agent starts with no context, so every leg that runs scoped tests (the review
    # wave's and the remnant re-verify's) carries its own measured-rev instruction.
    measured = [m.start() for m in re.finditer(re.escape(f"`coordinator-invoke {MEASURED_REV_OP}`"), script)]
    scoped = [m.start() for m in re.finditer(re.escape("Run the scoped test targets: ["), script)]
    assert len(measured) == len(scoped) >= 1
    previous_scoped = -1
    for rev_at, scoped_at in zip(measured, scoped):
        assert previous_scoped < rev_at < scoped_at
        previous_scoped = scoped_at
    assert f"{MEASURED_REV_ENV} set to that sha in the test process environment" in script
    assert "If the op refuses, run the tests without it and report the refusal as raw evidence" in script


def test_the_instruction_passes_the_run_id_and_the_manifest_writes():
    script = _compose(**_JUDGE_KW)
    i = script.index(f"coordinator-invoke {MEASURED_REV_OP}")
    window = script[i : i + 400]
    assert "_runId" in window and "_manifest.review_declared_paths" in window


def test_the_ledger_charter_carries_the_measured_gate_clause():
    script = _compose(**_JUDGE_KW)
    assert _CHARTER_CLAUSE in script and _REFUSED_CLAUSE in script
    assert 'never "no change"' in script


def test_the_ledger_check_halt_precedes_the_closeable_terminal_commit():
    script = _compose(**_JUDGE_KW)
    halt = script.index("halted: 'ledger-check'")
    assert halt < script.index("  _nextAction = _closeable")
    assert all(m.start() > halt for m in re.finditer(r"--from-task-output", script))


def test_no_posix_env_prefix_names_the_measured_env():
    script = _compose(**_JUDGE_KW)
    assert not re.search(rf"{MEASURED_REV_ENV}=\S", script)
    assert not re.search(rf"\bexport {MEASURED_REV_ENV}", script)
