"""The terminal ledger gate lets a recovery-withheld chunk's unmet row pass, only when recovery.json backs it."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261023T000000-abc123"
REL = "state/sizings/2026-10-23-recovery.yaml"
VERDICT = {"items": [{"id": "R1", "status": "not_met", "withheld_by": "C7", "remaining": ["x"]}]}


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "deliver sizing.intake_mint in the engine"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


def _record(run_dir, **entry):
    (run_dir / lr.RECOVERY_RECORD_FILE).write_text(
        json.dumps({"cap": lr.RECOVERY_ATTEMPT_CAP, "chunks": {"C7": entry}}), encoding="utf-8"
    )


def _gate(run, withheld=("C7",)):
    return tc._ledger_gate(run[0], run[1], VERDICT, recovery_withheld=list(withheld))


def test_exhausted_at_cap_passes_and_trailer_carries_reason(run):
    _record(run[1], status="exhausted", attempts=lr.RECOVERY_ATTEMPT_CAP, last_reason="test still red")
    refusal, _, open_rows = _gate(run)
    assert refusal is None
    assert open_rows == [{"id": "R1", "reason": "recovery-cap-exhausted (2 attempts): test still red"}]


def test_held_and_blocked_pass_with_their_reasons(run):
    _record(run[1], status="held", attempts=0, last_reason="not staged")
    assert _gate(run)[2][0]["reason"] == "recovery-held: not staged"
    _record(run[1], status="blocked", attempts=1, last_reason="needs other path")
    assert _gate(run)[2][0]["reason"] == "recovery-fix-blocked (1 attempts): needs other path"


@pytest.mark.parametrize("entry", [
    {"status": "exhausted", "attempts": 1, "last_reason": "x"},
    {"status": "fixed", "attempts": 1, "last_reason": ""},
])
def test_unbacked_param_refuses(run, entry):
    _record(run[1], **entry)
    assert _gate(run)[0]["refused"] == "recovery-withheld-unbacked"


def test_missing_record_refuses_and_param_absent_still_fails_the_row(run):
    assert _gate(run)[0]["refused"] == "recovery-withheld-unbacked"
    refusal = tc._ledger_gate(run[0], run[1], VERDICT)[0]
    assert refusal["refused"] == "ledger-unmet"


def test_param_is_a_declared_field():
    assert any(f.name == lr.RECOVERY_WITHHELD_PARAM for f in tc._PARAM_FIELDS)
    assert tc._recovery_withheld_param({"recovery_withheld": ["C7", 3]}) == ["C7"]
