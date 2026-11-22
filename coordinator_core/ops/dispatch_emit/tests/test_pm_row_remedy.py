"""A PM-origin ledger row left unmet is delivered, never dropped: the halt remedy leads with delivery,
and `workflow.revise` out_of_scope needs a recorded PM ruling row, not any other PM row's words."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.halted_run_route import ROUTE_OP
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

RUN = "warp-2026-11-21-9d0c1c-3-dense-c-likeness-swaps-20261121T144429-637f7c"
REL = "state/sizings/2026-11/2026-11-21-pm-row.yaml"
TURN_ONE = "swap the dense c likeness meshes for the casper rig"
TURN_TWO = "also keep the overlay passes consistent across shots"
RULING = "skip the overlay passes entirely, they are out of scope for this run"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    doc = {
        "schema": "sizing-object", "intent": "swap likeness", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": TURN_ONE}, {"turn": 2, "text": TURN_TWO}],
    }
    (tmp_path / REL).parent.mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _ruling_row(ref: str = "T3", rid: str = "R9") -> dict:
    return {
        rr.ROW_ID: rid, rr.ROW_TEXT: RULING, rr.ROW_ANCHOR: "pm_verbatims turn 3", rr.ROW_SURFACE: rl.ROW_SURFACE,
        rr.ROW_STATUS: rr.STATUS_DEFERRED, rr.ROW_CLAIMED_BY: [],
        rr.ROW_RULING: {rr.RULING_SOURCE: "pm", rr.RULING_QUOTE: RULING, rr.RULING_REF: ref, rr.RULING_ON: "2026-11-21"},
    }


def _revise(repo: Path, verbatim: str, rows: list[str]) -> dict:
    return rl._handler({"run_id": RUN, "verbatim": verbatim, "out_of_scope": rows}, repo_root=repo)


def _status(repo: Path, rid: str) -> str:
    rows = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    return next(r for r in rows if r["id"] == rid)["status"]


def test_out_of_scope_on_a_pm_row_with_no_ruling_is_refused_naming_row_and_missing_ruling(repo: Path) -> None:
    out = _revise(repo, RULING, ["R1"])
    assert not out.get("ok")
    assert "R1" in str(out) and "no PM verbatim ruling" in str(out)
    assert _status(repo, "R1") != rr.STATUS_DEFERRED


def test_another_pm_requirement_row_is_not_a_ruling(repo: Path) -> None:
    out = _revise(repo, TURN_TWO, ["R1"])
    assert not out.get("ok") and "no PM verbatim ruling" in str(out)
    assert _status(repo, "R1") != rr.STATUS_DEFERRED


def test_a_recorded_pm_ruling_row_verbatim_accepts_the_out_of_scope(repo: Path) -> None:
    rl._mutate_register(repo, REL, lambda rows: [*rows, _ruling_row()])
    out = _revise(repo, RULING, ["R1"])
    assert out["ok"] and out["withheld"] == ["R1"]
    assert _status(repo, "R1") == rr.STATUS_DEFERRED


def test_a_ruling_stamped_by_a_prior_out_of_scope_does_not_rule_out_another_row(repo: Path) -> None:
    stamped = f"{rl.REVISE_REF}:{RUN}:{rl.OUT_OF_SCOPE_REF}"
    rl._mutate_register(repo, REL, lambda rows: [*rows, _ruling_row(ref=stamped)])
    out = _revise(repo, RULING, ["R1"])
    assert not out.get("ok") and "no PM verbatim ruling" in str(out)


def test_pm_rows_without_a_ruling_are_the_unruled_ids_the_remedy_withholds_out_of_scope_from(repo: Path) -> None:
    assert ask_compose._pm_unruled_row_ids(str(repo), REL) == ["R1", "R2"]
    rl._mutate_register(repo, REL, lambda rows: [*rows, _ruling_row()])
    assert ask_compose._pm_unruled_row_ids(str(repo), REL) == ["R9"]


def _halt_text(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs", **REVIEW_KW,
    )
    start = script.index("halted: 'ledger-check'")
    return script[start : script.index("}; }", start)]


def test_the_halt_remedy_leads_with_delivery_for_a_pm_row_and_never_names_out_of_scope_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _halt_text(monkeypatch)
    assert text.count("_pmRemedy") == 2
    assert text.count("_blkRulableOk") >= 4 and "_blkRulable.length" not in text and "_blkRulable.join" not in text
    for use in ("+ _pmRemedy ", ):
        assert text.index(use) < text.index("out_of_scope: [")


def test_the_pm_remedy_text_names_delivery_and_another_runs_sha_and_not_out_of_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs", **REVIEW_KW,
    )
    start = script.index("const _pmRemedy")
    remedy = script[start : script.index(";\n", start)]
    assert "delivered, never dropped" in remedy and "deliver them" in remedy
    assert "another run" in remedy and ROUTE_OP in remedy and "<the sha that delivered it>" in remedy
    assert "out_of_scope" not in remedy
    assert "const _pmUnruledIds" in script
