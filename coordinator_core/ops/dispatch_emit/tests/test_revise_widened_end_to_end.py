"""A row halted on ROW-LOCAL-BLOCK, revised with `widened`, then refired: the widen record carries the
path, the halt-time resume record is not served, the footprint fence admits the path, and the refired
row's brief names it.

Each case drives the production entry point of its site: the `workflow.revise` handler, the resume
record's `matches` over `fingerprint_for_run`, `land_run._footprint_fence`, and `compose_script`.
"""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import in_run_widen, land_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

RUN = "ask-20261029T000000-abc123"
REL = "state/sizings/2026-10-29-widen-e2e.yaml"
PATH = "pkg/scripts/install_plugin.py"


@pytest.fixture
def halted(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (tmp_path / "plan.md").write_text("plan v1", encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "halted.json").write_text(
        json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": None}), encoding="utf-8"
    )
    return tmp_path, rd


def _revise(root):
    return rl._handler({"run_id": RUN, "widened": [{"row": "C1", "path": PATH}]}, repo_root=root)


def test_revise_widened_records_and_invalidates_the_halt_resume_record(halted):
    root, rd = halted
    at_halt = hrr.fingerprint_for_run(root, rd, REL, "plan.md")
    record = hrr.build_record(
        composed_script_rel="resume-script.workflow.mjs", hoisted_fragments={}, gate_verdict={},
        landed_state_sha=None, fingerprint=at_halt, resume_plan_rel="plan.md",
    )
    assert hrr.matches(record, hrr.fingerprint_for_run(root, rd, REL, "plan.md"))

    out = _revise(root)

    assert out["ok"] is True and out["widened"] == 1 and out["refused"] == []
    assert in_run_widen.widened_paths(rd) == [PATH]
    assert not hrr.matches(record, hrr.fingerprint_for_run(root, rd, REL, "plan.md"))


def test_the_refired_run_footprint_fence_admits_the_widened_path_only_after_the_revise(halted, monkeypatch):
    root, rd = halted
    (rd / land_run.LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": ["a.py"]}), encoding="utf-8")
    monkeypatch.setattr(land_run, "_manifest_declared", lambda m: (["a.py", PATH], None))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", ""))
    monkeypatch.setattr(land_run, "_path_changed", lambda *a: True)

    assert land_run._footprint_fence(root, rd, {"blocking": []}, None, None)["breach"] == [PATH]
    _revise(root)
    assert land_run._footprint_fence(root, rd, {"blocking": []}, None, None) == {"recorded": True, "breach": []}


def test_the_refired_row_brief_names_the_widened_path(halted):
    """`compose_script` seeds `_widenOf` from the widen record, so a refired row's prompt carries the
    revised path."""
    root, rd = halted
    _revise(root)
    row = WaveRow(id="C1", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[], body="")
    script = compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md",
        repo_root=root, widen_run_dir=rd, **REVIEW_KW
    )
    assert f'"C1": "{PATH}"' in script


def test_a_second_revised_path_for_the_same_row_is_not_dropped(halted):
    root, rd = halted
    second = "pkg/scripts/other.py"
    rl._handler(
        {"run_id": RUN, "widened": [{"row": "C1", "path": PATH}, {"row": "C1", "path": second}]},
        repo_root=root,
    )
    row = WaveRow(id="C1", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[], body="")
    script = compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md",
        repo_root=root, widen_run_dir=rd, **REVIEW_KW
    )
    assert PATH in script and second in script


def test_a_non_terminal_sibling_script_still_seeds_the_widened_path(halted):
    """A split run's non-carrier script gets the widen seed even though it splices no absorb tail."""
    from coordinator_core.ops.dispatch_emit.emit import emit_script
    from coordinator_core.ops.dispatch_emit.tests.test_sibling_split import _plan_file

    root, _ = halted
    rl._handler({"run_id": RUN, "widened": [{"row": "A", "path": PATH}]}, repo_root=root)
    script = emit_script(
        _plan_file(root, [("A", ())]),
        repo_root=root,
        absorb_run_id=RUN,
        sibling={
            "index": 1, "count": 2, "stage": 1, "stages": 1, "cap": 2, "plan_width": 1,
            "achieved_width": 1, "keep_rows": ["A"], "terminal_commit": False, "commit_scope": [],
        },
        **REVIEW_KW,
    )
    assert f'"A": "{PATH}"' in script
