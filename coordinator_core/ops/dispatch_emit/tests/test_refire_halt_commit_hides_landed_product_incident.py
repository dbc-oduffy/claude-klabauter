"""Incident replay: a ledger-check refire writes its own halt commit before the since-halt check, so
product commits that landed between fires never count; and a resume re-lists an em-performed row the
EM already discharged. The correct outcome is asserted: the since-halt base is the chain's first halt
and a discharged em-performed row owes no re-plan."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_REMAINING_FIELD,
    RUN_DIR_ROOT,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger, gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    _met_out_of_band,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    PLAN,
    RUN,
    SIZING,
    WAVE_REVIEW,
    _git,
    _write,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    _MET,
    _REVIEWED,
    _SID,
    _refire,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: (a) each fire halts under its own run id; (b) fire 2 rebinds fire 1's run id.
FIRE2_RUN_IDS = {"own-run-ids": "run-fire2", "rebound-run-id": RUN}
_HALT = {"halted": "ledger-check", "plan": PLAN, "items": [{"id": "R1"}]}


def _terminal(root, token: str, resume: dict) -> dict:
    """Lands the refire carrying a real review verdict (sidecar on disk) for the run's code edit."""
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": {**_REVIEWED, "wave_reviews": [WAVE_REVIEW]}, "session_id": _SID,
            "ledger_verdict": _MET, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def _fire2_halt(root, run_id: str) -> str:
    """Fire 2 halts again at the ledger check after product commit P changed `a.py`; returns H2."""
    _write(root, "a.py", "a = 2\n")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "review-finding fix to a.py")
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING, "fire": 2}) + "\n", encoding="utf-8")
    (rec / "manifest.json").write_text(json.dumps({"rows": [{"id": "C2"}], "rulable_ids": ["R1"]}), encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": run_id, HALTED_RECORD_PARAM: {**_HALT, "fire": 2}}, repo_root=root
    )
    assert out["committed"] is True, out
    return _git(root, "rev-parse", "HEAD").strip()


def _fire3(root, fire2_run: str):
    """Fire 3: an open plan row keeps `unlanded` non-empty, the ledger is met out of band, the gate
    emits the resume, and the new run declares `a.py` but changes nothing itself."""
    plan_text = (root / PLAN).read_text(encoding="utf-8")
    open_row = "    b\n- id: C2\n  title: owed row\n  disposition: open\n  body: |\n    b\n```"
    _write(root, PLAN, plan_text.replace("    b\n```", open_row))
    h1 = _git(root, "rev-parse", "HEAD").strip()
    h2 = _fire2_halt(root, fire2_run)
    assert h1 != h2
    _met_out_of_band(root)
    verdict = gate(root, SIZING)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    refire = _refire(root, edit=False)
    return h1, h2, dict(verdict.resume_ledger), refire


@pytest.mark.parametrize("shape", sorted(FIRE2_RUN_IDS))
def test_land_run_commits_a_met_refire_whose_product_landed_between_fires(halted, shape):  # noqa: F811
    h1, h2, resume, refire = _fire3(halted, FIRE2_RUN_IDS[shape])
    head = _git(halted, "rev-parse", "HEAD").strip()

    out = _terminal(halted, refire["token"], resume)

    assert out.get("refused") != "ledger-unmet", out
    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    if "since_halt" in out:
        assert out["since_halt"].get("base") == h1, (h1, h2, out["since_halt"])
    assert _git(halted, "rev-parse", "HEAD").strip() != head or out.get("already_landed")


@pytest.mark.parametrize("shape", sorted(FIRE2_RUN_IDS))
def test_terminal_commit_since_halt_check_measures_from_the_first_halt(halted, shape):  # noqa: F811
    h1, h2, resume, _ = _fire3(halted, FIRE2_RUN_IDS[shape])
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True}]}
    detail: dict = {}

    failures = terminal_commit._unchanged_since_halt_failures(
        halted, resume, verdict, halted / RUN_DIR_ROOT / RUN2 / "manifest.json", PLAN, detail,
    )

    assert failures == [], (failures, detail, h1, h2)
    assert detail.get("base", h1) == h1


def test_a_resume_after_an_em_discharged_row_owes_no_replan_for_it(tmp_path):
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\ntitle: p\nregister_claims:\n  rows: [R1]\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: E1\n  title: run the live check\n  disposition: open\n  performer: em\n  register_rows: [R1]\n"
        "  writes: []\n  body: |\n    Perform the check.\n```\n",
        encoding="utf-8", newline="\n",
    )
    plan.with_suffix(".evidence.yaml").write_text(
        yaml.safe_dump({"em_discharged": {"E1": {"recorded_at": "2026-10-23T00:00:00Z", "text": "check passes at HEAD"}}}),
        encoding="utf-8", newline="\n",
    )
    halt = LedgerHalt(
        run_id="run-fire2", plan_rel="docs/plans/p.md", sizing_rel="state/sizings/s.yaml", blocking=("R1",),
        remaining={"R1": {"id": "R1", "remaining": ["perform the live check again"], "evidence": "e"}},
    )
    sizing = {"requirement_register": {"rows": [{"id": "R1", "status": "open"}]}}

    resume = _resume_ledger(sizing, halt, tmp_path)

    assert resume["em_discharged"] == {"E1": "check passes at HEAD"}
    assert resume[RESUME_LEDGER_REMAINING_FIELD]["R1"]["remaining"] == []
