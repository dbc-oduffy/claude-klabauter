from coordinator_core.ops.dispatch_emit.footprint_breach import (
    MET_ON_FOOTPRINT_BREACH_REASON,
    met_on_breach,
    serving_writes,
)

BREACH = "coordinator_core/session/xs_lane_budget.py"


def _plan(tmp_path, front="", rows=""):
    p = tmp_path / "plan.md"
    p.write_text(f"---\n{front}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8")
    return p


def _row(rid, writes, extra=""):
    w = "".join(f"    - {x}\n" for x in writes)
    return (
        f"- id: {rid}\n  title: t\n  change_kind: code-edit\n  surface: {writes[0]}\n"
        f"  writes:\n{w}  disposition: open\n{extra}  body: b\n"
    )


def test_serving_via_register_rows(tmp_path):
    plan = _plan(tmp_path, rows=_row("C0", ["a.py", "b.py"], '  register_rows: ["R1"]\n')
                 + _row("C1", ["z.py"], '  register_rows: ["R2"]\n'))
    assert serving_writes(plan, ["R1"]) == ["a.py", "b.py"]


def test_serving_via_register_claims(tmp_path):
    plan = _plan(tmp_path, front="register_claims:\n  rows: [R1]\n", rows=_row("C0", ["a.py"]))
    assert serving_writes(plan, ["R1"]) == ["a.py"]


def test_wont_do_excluded(tmp_path):
    plan = _plan(tmp_path, rows=_row("C0", ["a.py"], '  register_rows: ["R1"]\n').replace("open", "wont_do"))
    assert serving_writes(plan, ["R1"]) == []


def test_missing_and_none_and_unparseable(tmp_path):
    assert serving_writes(None, ["R1"]) == []
    assert serving_writes(tmp_path / "nope.md", ["R1"]) == []
    bad = tmp_path / "bad.md"
    bad.write_text("no spine here", encoding="utf-8")
    assert serving_writes(bad, ["R1"]) == []


def _met(**kw):
    return {"id": "L1", "status": "met", **kw}


def test_match_by_path_filename_and_row_id():
    for item in (
        _met(evidence=f"fixed in {BREACH}"),
        _met(met_by=["xs_lane_budget.py"]),
        _met(met_by="C5"),
    ):
        got = met_on_breach([item], [BREACH], {"C5": [BREACH]})
        assert got == [{"id": "L1", "reason": f"{MET_ON_FOOTPRINT_BREACH_REASON}: {BREACH}"}]


def test_non_met_empty_breach_and_non_dict_skipped():
    rows = {"C5": [BREACH]}
    assert met_on_breach([{"id": "L1", "status": "open", "met_by": "C5"}, "x"], [BREACH], rows) == []
    assert met_on_breach([_met(met_by="C5")], [], rows) == []


def test_substring_token_does_not_match():
    rows = {"C5": [BREACH]}
    item = _met(met_by="C55", evidence="my_xs_lane_budget.py and C5-x")
    assert met_on_breach([item], [BREACH], rows) == []
    assert met_on_breach([_met(met_by="C5")], [BREACH], {"C5": ["other.py"]}) == []


def _fence(tmp_path, monkeypatch, widened):
    import json

    from coordinator_core.ops.dispatch_emit import in_run_widen, land_run

    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / land_run.LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": ["a.py"]}), encoding="utf-8")
    for p in widened:
        in_run_widen.record_widen(run, "C1", p)
    monkeypatch.setattr(land_run, "_manifest_declared", lambda m: (["a.py", "t/test_a.py"], None))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", ""))
    monkeypatch.setattr(land_run, "_path_changed", lambda *a: True)
    monkeypatch.setattr("coordinator_core.git.git_dir.resolve_git_common_dir", lambda r: tmp_path)
    return land_run._footprint_fence(tmp_path, run, {"blocking": []}, None, None)


def test_fence_admits_a_recorded_widen_and_still_breaches_an_unrecorded_path(tmp_path, monkeypatch):
    assert _fence(tmp_path, monkeypatch, [])["breach"] == ["t/test_a.py"]
    assert _fence(tmp_path, monkeypatch, ["t/test_a.py"]) == {"recorded": True, "breach": []}


def test_widened_paths_join_the_resume_recorded_paths(tmp_path):
    from coordinator_core.ops.dispatch_emit import in_run_widen, land_run

    in_run_widen.record_widen(tmp_path, "C1", "t/test_a.py")
    assert land_run._resume_recorded_paths(tmp_path, tmp_path, None) == ["t/test_a.py"]
