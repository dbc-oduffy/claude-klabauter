"""An engine run widens or resizes in-run and delivers; a human-needing or out-of-ask block still halts.

Nine cases, each through the production entry point of its site: the emitted run script's row
helper (``emit.compose_script``), ``terminal_commit._terminal_commit`` at default arguments,
``shipped_claims.shipped_gate``, ``sizing_fire.apply_in_run_resize`` and the composed review-recovery
leg. The helpers are the sibling per-site tests' own, so each case drives the same harness.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import emit, in_run_widen, land_run, terminal_commit
from coordinator_core.ops.dispatch_emit import sizing_fire as sf
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL
from coordinator_core.ops.dispatch_emit.shipped_claims import SHIPPED_CLAIM_REFUSED, shipped_gate
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW
from .test_ask_compose_halt_self_correction import _OWN, _drive, _done, _green, _labels
from .test_ask_run_product_commit import RUN, repo  # noqa: F401 -- fixture
from .test_sizing_fire_in_run_resize import REL, _apply, _seed
from .test_terminal_commit_xs_over_budget import _call, _git, _new_files, root  # noqa: F401 -- fixture
from .test_vitest_cap_and_row_local_block import _BLOCK, _go

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.xs_gate_live]

_WARN = [{"slice_id": "s1", "verdict": "WARN", "findings": 4, "applied": 0, "sidecar_path": "rev/s1.md"}]
_CLEAR = {"test": _green(), "slices": [{"slice_id": "s1", "findings": [], "applied": True}]}


def _emitted_script() -> str:
    row = WaveRow(
        id="C1", title="t", surface="dispatch_emit", writes=["a.py"], reads=[], depends_on=[], body="",
    )
    return compose_script([[row]], name="n", description="d", plan_path="docs/plans/example.md", **REVIEW_KW)


def _fence(tmp_path, monkeypatch, widened):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / land_run.LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": ["a.py"]}), encoding="utf-8")
    for row, path in widened:
        in_run_widen.record_widen(run, row, path)
    monkeypatch.setattr(land_run, "_manifest_declared", lambda m: (["a.py", "pkg/helper.py"], None))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", ""))
    monkeypatch.setattr(land_run, "_path_changed", lambda *a: True)
    monkeypatch.setattr("coordinator_core.git.git_dir.resolve_git_common_dir", lambda r: tmp_path)
    return land_run._footprint_fence(tmp_path, run, {"blocking": []}, None, None)


def test_1_an_in_ask_row_local_block_widens_redispatches_and_lands(tmp_path, monkeypatch):
    script = _emitted_script()
    assert "function classifyBlock(" in script and "_widened" in script
    assert f"if ({emit._STOP_RULE_JS_RE}.test(_text) && !{emit._ROW_LOCAL_BLOCK_JS_RE}.test(_text))" in script
    out = _go({"A": [_BLOCK % "pkg/helper.py", "DONE: a"]})

    assert out["calls"] == ["A", "A"]
    assert out["incomplete"] == [] and out["halted"] is None
    assert out["widened"] == [{"row": "A", "path": "pkg/helper.py"}]
    assert "pkg/helper.py" in out["prompts"][1]["widen"]

    unrecorded = _fence(tmp_path, monkeypatch, [])
    assert unrecorded["breach"] == ["pkg/helper.py"]
    recorded = _fence(tmp_path, monkeypatch, [(w["row"], w["path"]) for w in out["widened"]])
    assert recorded == {"recorded": True, "breach": []}


def test_2_an_xs_run_over_budget_resizes_and_lands(root):  # noqa: F811
    head = _git(root, "rev-parse", "HEAD")
    reply, _ = _call(root, _new_files(root, 3, 14))

    assert reply.get("refused") != "xs-over-budget", reply
    assert reply.get("committed") is True, reply
    assert _git(root, "rev-parse", "HEAD") != head


def test_2b_an_unmeasurable_xs_diff_still_refuses(root):  # noqa: F811
    reply, _ = _call(root, _new_files(root, 1, 1), base="0" * 40)

    assert reply["refused"] == "xs-over-budget" and "unmeasurable" in reply["error"]


def test_3_a_review_leg_adjacent_test_edit_is_backed_by_the_recorded_widen(tmp_path, root):  # noqa: F811
    adjacent = "pkg/tests/test_mod.py"
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / adjacent).write_text("def test_a():\n    assert True\n", encoding="utf-8")
    note = f"## What shipped\n- `{adjacent}`\n"
    run = tmp_path / "run"
    in_run_widen.record_widen(run, "X1", adjacent)

    asked = []

    def owners(paths):
        asked.extend(paths)
        return {p: "peer" for p in paths}

    widened = in_run_widen.widened_paths(run)
    refusal, _ = shipped_gate(
        root, note, landing_paths=[adjacent], own_paths={"pkg/mod.py"}, widened=widened, owners=owners
    )
    assert refusal is None and asked == []

    refusal, _ = shipped_gate(root, note, own_paths={"pkg/mod.py"}, widened=widened, owners=owners)
    assert refusal and refusal.startswith(SHIPPED_CLAIM_REFUSED) and asked == []

    refusal, _ = shipped_gate(root, note, own_paths={"pkg/mod.py"}, owners=lambda paths: {})
    assert refusal and refusal.startswith(SHIPPED_CLAIM_REFUSED)


def test_4_an_s_sizing_reported_l_mid_planning_continues(tmp_path):
    repo = _seed(tmp_path, "S")
    arm, amended, _ = _apply(repo, "L")

    assert arm == sf.ARM_M_PLUS and amended["estimate"]["tshirt"] == "L"


def test_5_an_out_of_ask_row_local_block_halts():
    for value in ("state/handoffs/x.md", "../outside.py", ".claude/rules/x.md"):
        out = _go({"A": [_BLOCK % value, "DONE: a"]})
        assert out["calls"] == ["A"] and out["widened"] == [], value
        assert out["incomplete"] == ["A"], value


def test_6_a_stop_rule_reply_halts_without_widening():
    out = _go({"A": ["DONE: x\nSTOP-RULE-FIRED: premise refuted"]})

    assert out["calls"] == ["A"] and out["widened"] == []
    assert out["stoppedBy"] == ["A"]


@pytest.mark.parametrize("old,new", [("S", "XXL"), ("XS", "XL"), ("L", "XXL"), ("M", "XXL")])
def test_7_a_ballooning_resize_flags_to_the_em_and_leaves_the_sizing(tmp_path, old, new):
    repo = _seed(tmp_path, old)
    with pytest.raises(sf.SizingFireRefused, match="possible engine error"):
        _apply(repo, new)

    assert sf.load_sizing(repo, REL)["estimate"]["tshirt"] == old


def test_8_a_review_blocked_run_with_in_ask_warn_findings_is_fixed_in_run_and_lands():
    out = _drive(
        reverify=_green(), wave=_WARN, replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [_CLEAR]}
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert out["halted"] is None
    assert out["wave"][0]["applied"] == 4


def test_9_the_scripts_returned_widened_list_is_persisted_by_terminal_commit_and_admitted_by_the_fence(
    repo, monkeypatch  # noqa: F811
):
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    run_dir = repo / RUN_DIR_ROOT / RUN
    adjacent = "pkg/tests/test_helper.py"
    assert in_run_widen.widened_paths(run_dir) == []

    terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "widened": [{"row": "C1", "path": adjacent}]},
        repo_root=repo / ".git",
    )

    assert in_run_widen.widened_paths(run_dir) == [adjacent]
    (run_dir / land_run.LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": ["a.py"]}), encoding="utf-8")
    monkeypatch.setattr(land_run, "_manifest_declared", lambda m: (["a.py", adjacent], None))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", ""))
    monkeypatch.setattr(land_run, "_path_changed", lambda *a: True)
    fence = land_run._footprint_fence(repo, run_dir, {"blocking": []}, None, None)
    assert fence == {"recorded": True, "breach": []}

    (run_dir / in_run_widen.WIDEN_RECORD_NAME).unlink()
    assert land_run._footprint_fence(repo, run_dir, {"blocking": []}, None, None)["breach"] == [adjacent]
