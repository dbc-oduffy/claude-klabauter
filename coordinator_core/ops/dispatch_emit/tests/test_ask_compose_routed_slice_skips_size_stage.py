"""A routed, non-intake fan-out slice sizing composes no size phase and no accept leg; a draft one does."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

REL = "state/sizings/2026-10-23-slice.yaml"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _put(repo, *, status):
    doc = {
        "status": status,
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "exit_criterion": {"statement": "x", "accepted": {"source": "apm", "ruling_ref": "run-x"}},
        "em_analysis": {"ask_parent": "ask-20261023T000000-aaaaaa"},
    }
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _compose(repo):
    return compose_ask_script(
        repo_root=str(repo), prompt=None, sizing_rel=REL, run_id="ask-20261023T010101-bbbbbb",
        session_id=None, wrap_stage=lambda text: (_BLITZ_FN, ["Plan"]), plan_blitz_text="stub", **REVIEW_KW,
    )


def test_routed_non_intake_slice_composes_no_size_phase_and_no_accept_leg(tmp_path):
    _put(tmp_path, status="routed")
    script = _compose(tmp_path)
    assert "phase('size')" not in script
    assert "phase('accept')" not in script
    assert "label: 'accept'" not in script


def test_draft_slice_still_composes_the_size_phase(tmp_path):
    _put(tmp_path, status="draft")
    assert "phase('size')" in _compose(tmp_path)
