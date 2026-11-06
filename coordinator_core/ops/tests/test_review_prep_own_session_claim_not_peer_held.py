"""Review prep counts a claim by the run's lead/firing session as own; a distinct live peer's claim still withholds."""

import json

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir
from coordinator_core.session import claim_index, liveness

RUN = "ask-20261106T045617-66b431"
PROD = "tools/pkg/mod.py"
TEST = "tools/pkg/tests/test_mod.py"
LEAD = "78201803-89ff-433f-a193-faa7b122ef52"
PEER = "456dea75-0000-4000-8000-000000000001"


class _Found(dict):
    recorded_kind = None


def _stage(tmp_path, emitter=None):
    base = record_dir(tmp_path, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": [PROD, TEST]}), encoding="utf-8")
    if emitter:
        script = tmp_path / RUN_DIR_ROOT / f"{RUN}.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        (script.parent / f"{script.name}.emitted.json").write_text(json.dumps({"session_id": emitter}), encoding="utf-8")


def _claims(monkeypatch, touch=None, run_claims=None):
    touch = touch or {}

    def lookup(paths, **kw):
        found = _Found({p: touch.get(p, []) for p in paths})
        found.recorded_kind = {p: {s: "write" for s in touch.get(p, [])} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        warp_run_deliverable, "live_run_write_claims",
        lambda *a, **kw: {p: list(s) for p, s in (run_claims or {}).items()},
    )


def test_lead_session_run_declaration_is_own_when_it_is_the_caller(tmp_path, monkeypatch):
    _stage(tmp_path)
    _claims(monkeypatch, run_claims={PROD: [LEAD], TEST: [LEAD]})
    assert rp.declared_paths_for_run(tmp_path, RUN, own_session=LEAD) == [PROD, TEST]


def test_lead_session_is_own_at_default_arguments_via_the_emission_receipt(tmp_path, monkeypatch):
    _stage(tmp_path, emitter=LEAD)
    _claims(monkeypatch, touch={PROD: [LEAD]}, run_claims={PROD: [LEAD], TEST: [LEAD]})
    assert rp.declared_paths_for_run(tmp_path, RUN) == [PROD, TEST]


def test_real_peer_run_declaration_still_withholds(tmp_path, monkeypatch):
    _stage(tmp_path, emitter=LEAD)
    _claims(monkeypatch, run_claims={PROD: [PEER], TEST: [LEAD]})
    assert rp.declared_paths_for_run(tmp_path, RUN) == [TEST]


def test_real_peer_holding_every_path_raises_naming_the_peer_not_the_lead(tmp_path, monkeypatch):
    _stage(tmp_path, emitter=LEAD)
    _claims(monkeypatch, touch={PROD: [PEER], TEST: [PEER]}, run_claims={PROD: [LEAD, PEER]})
    with pytest.raises(ValueError, match="no reviewable paths") as exc:
        rp.declared_paths_for_run(tmp_path, RUN)
    assert PEER in str(exc.value) and LEAD not in str(exc.value)
