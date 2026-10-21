"""workflow.pending_revisions, plan.claim_register_rows and absorb_tail_js."""

from __future__ import annotations

import time

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import executor_economy as ee
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import revise_absorb as ra

RUN = "ask-20261016T000000-abc123"
REL = "state/sizings/2026-10-16-absorb.yaml"
PLAN = "docs/plans/2026-10-16-absorb.md"


def _repo(tmp_path, *, pin_text="build the op", claims_sizing=REL, rows_style="flow"):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}], "plan": PLAN,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rows = "[R1]" if rows_style == "flow" else "\n    - R1"
    fm = (
        f"title: p\nregister_claims:\n  sizing: {claims_sizing}\n  rows: {rows}\n"
        f"  pins: {{\"R1\": \"{rr.row_pin(pin_text)}\"}}\nowner: keep-me\n"
    )
    (tmp_path / PLAN).write_text("---\n" + fm + "---\n# body stays\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _pending(root, **over):
    return ra._pending_handler({"run_id": RUN, "plan": PLAN, **over}, repo_root=root)


def _revise(root, text="also cover Windows", **extra):
    out = rl._handler({"run_id": RUN, "verbatim": text, **extra}, repo_root=root)
    assert out["ok"] is True
    return out


def test_private_imports_resolve():
    for name in ("_unclaimed_rows", "_sizing_plan", "_sizing_path", "ledger_ids", "pinned_plan_drift", "run_dir", "read_run"):
        assert callable(getattr(rl, name))
    assert callable(rr._sub_block_end) and callable(rr._indent_of)
    assert ra.PENDING_REVISIONS_OP == ee.PENDING_REVISIONS_OP and ra.CLAIM_ROWS_OP == ee.CLAIM_ROWS_OP


def test_no_new_row_replies_empty(tmp_path):
    out = _pending(_repo(tmp_path))
    assert out["rows"] == [] and out["drift"] == [] and out["plan"] == PLAN and out["sizing"] == REL


def test_unclaimed_row_replies_id_and_text_in_well_under_the_bar(tmp_path):
    root = _repo(tmp_path)
    _revise(root)
    start = time.perf_counter()
    out = _pending(root)
    assert time.perf_counter() - start < 0.5
    assert out["rows"] == [{"id": "R2", "text": "also cover Windows"}]


def test_withheld_row_is_subtracted(tmp_path):
    root = _repo(tmp_path)
    _revise(root)
    _revise(root, "drop that", out_of_scope=["R2"])
    ids = [r["id"] for r in _pending(root)["rows"]]
    assert "R2" not in ids


def test_drift_is_reported_not_absorbed(tmp_path):
    root = _repo(tmp_path, pin_text="build a different op")
    out = _pending(root)
    assert out["drift"] == ["R1"] and out["rows"] == []


def test_plan_mismatch_refuses(tmp_path):
    root = _repo(tmp_path)
    assert "error" in _pending(root, plan="docs/plans/other.md")


def test_claims_naming_another_sizing_refuse_not_empty(tmp_path):
    root = _repo(tmp_path, claims_sizing="state/sizings/other.yaml")
    out = _pending(root)
    assert "error" in out and "rows" not in out


def test_unemitted_run_and_missing_params_refuse(tmp_path):
    root = _repo(tmp_path)
    assert "error" in ra._pending_handler({"run_id": "ask-20261016T000001-zzzzzz", "plan": PLAN}, repo_root=root)
    assert "error" in ra._pending_handler({"run_id": RUN}, repo_root=root)
    assert "error" in ra._pending_handler({"run_id": RUN, "plan": PLAN}, repo_root=None)


def _claim(root, rows):
    return ra._claim_handler({"plan": PLAN, "rows": rows}, repo_root=root)


def test_claim_appends_and_pins_and_keeps_other_bytes(tmp_path):
    root = _repo(tmp_path)
    _revise(root)
    out = _claim(root, ["R2"])
    assert out["ok"] is True and out["claimed"] == ["R2"]
    text = (root / PLAN).read_text(encoding="utf-8")
    sizing_rel, ids = rr.plan_claims(text.split("---\n")[1])
    assert sizing_rel == REL and ids == {"R1", "R2"}
    pins = rr.plan_pins(text.split("---\n")[1])
    assert pins["R2"] == rr.row_pin("also cover Windows") and pins["R1"] == rr.row_pin("build the op")
    assert "owner: keep-me" in text and text.endswith("# body stays\n")
    assert _pending(root)["rows"] == []


def test_claim_handles_block_style_rows(tmp_path):
    root = _repo(tmp_path, rows_style="block")
    _revise(root)
    assert _claim(root, ["R2"])["ok"] is True
    _, ids = rr.plan_claims((root / PLAN).read_text(encoding="utf-8").split("---\n")[1])
    assert ids == {"R1", "R2"}


def test_claim_refuses_unknown_and_already_claimed_and_writes_nothing(tmp_path):
    root = _repo(tmp_path)
    _revise(root)
    before = (root / PLAN).read_bytes()
    assert "R9" in _claim(root, ["R9"])["error"]
    assert "already claimed" in _claim(root, ["R1"])["error"]
    assert "error" in _claim(root, ["R2", "R2"])
    assert (root / PLAN).read_bytes() == before


def test_claim_refuses_plan_outside_docs_plans(tmp_path):
    root = _repo(tmp_path)
    (root / "other.md").write_text("---\n---\n", encoding="utf-8")
    assert "error" in ra._claim_handler({"plan": "other.md", "rows": ["R2"]}, repo_root=root)


def test_claim_preserves_crlf(tmp_path):
    root = _repo(tmp_path)
    _revise(root)
    p = root / PLAN
    p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _claim(root, ["R2"])["ok"] is True
    raw = p.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_tail_js_carries_both_agent_calls_ops_and_the_round_cap():
    js = ra.absorb_tail_js(plan_path=PLAN, run_id=RUN)
    assert js.count("await agent(") == 2
    assert "workflow.pending_revisions" in js and "plan.claim_register_rows" in js
    assert "plan.tasks.mutate" in js and "add-task" in js
    assert f"_absorbRound < {ee.ABSORB_ROUNDS}" in js
    assert "_runRow(" in js and "_absorbPrompt(a.id, a.title)" in js
    assert "coordinator:plan-author" in js
    assert "if (!Array.isArray(_absorbPending.rows) || !_absorbPending.rows.length) break;" in js


def test_tail_js_check_precedes_author_and_rows_and_round_cap_follows_argument():
    js = ra.absorb_tail_js(plan_path=PLAN, run_id=RUN, rounds=3)
    assert "_absorbRound < 3" in js
    assert js.index("absorb:check-") < js.index("absorb:plan-") < js.index("_runRow(")
