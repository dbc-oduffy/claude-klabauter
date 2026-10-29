"""Pins the warp --ask contract names and the StageManifest round trip."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import commit_request


def _manifest():
    rows = (
        c.ManifestRow("C1", "executor", "sonnet", "scratch/warp/r/C1.md", ("a.py", "b.py"), 0),
        c.ManifestRow("C2", "executor", "opus", "scratch/warp/r/C2.md", (), 1, ("C1",)),
    )
    return c.StageManifest("scratch/warp/r", rows, ("a.py",), "scratch/warp/r/req.json")


def test_manifest_round_trip():
    m = _manifest()
    assert c.StageManifest.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_manifest_row_always_serialises_route_source_and_rag_calls():
    row = c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0)
    out = row.to_json()
    assert out["route_source"] == "default" and out["rag_calls"] is None
    set_row = c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0, route_source="overlay", rag_calls=3)
    back = c.ManifestRow.from_json(json.loads(json.dumps(set_row.to_json())))
    assert back == set_row
    legacy = {k: v for k, v in out.items() if k not in ("route_source", "rag_calls")}
    assert c.ManifestRow.from_json(legacy).route_source == "default"
    assert c.ManifestRow.from_json(legacy).rag_calls is None


def test_manifest_row_refuses_unknown_route_source():
    for ok in c.ROUTE_SOURCES:
        c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0, route_source=ok)
    with pytest.raises(ValueError):
        c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0, route_source="guess")


def test_owed_round_trip():
    owed = (c.OwedRow("C3", "em-performed", "EM runs it"), c.OwedRow("C4", "memo-send", "send memo"))
    m = _manifest()
    m = c.StageManifest(m.run_dir, m.rows, m.review_declared_paths, m.marker_path, owed=owed)
    out = json.loads(json.dumps(m.to_json()))
    assert out["owed"][1] == {"id": "C4", "reason": "memo-send", "detail": "send memo"}
    assert c.StageManifest.from_json(out) == m


def test_absent_owed_key_reads_empty():
    data = _manifest().to_json()
    del data["owed"]
    assert c.StageManifest.from_json(data).owed == ()


def test_ask_scout_schema_admits_acceptance_rows_and_grind_scout_does_not():
    rows = c.ASK_SCOUT_SCHEMA["properties"]["acceptance_rows"]
    assert rows["type"] == ["array", "null"]
    assert rows["items"]["required"] == ["statement", "writes", "check"]
    assert "acceptance_rows" not in c.SCOUT_SCHEMA["properties"]


def test_route_preference_record_file_and_override_reason_slot():
    assert "route-preference.json" in c.RUN_RECORD_FILES
    slot = c.ASK_SCOUT_SCHEMA["properties"]["route_override_reason"]
    assert slot["type"] == ["string", "null"]
    assert "route_override_reason" not in c.ASK_SCOUT_SCHEMA["required"]
    assert "route_override_reason" not in c.SCOUT_SCHEMA["properties"]


def test_route_verdict_slot_is_ask_only_and_optional():
    slot = c.ASK_SCOUT_SCHEMA["properties"]["route_verdict"]
    assert slot["type"] == ["object", "null"]
    assert "route_verdict" not in c.ASK_SCOUT_SCHEMA["required"]
    assert "route_verdict" not in c.SCOUT_SCHEMA["properties"]


def test_stop_after_key_and_value_domain():
    assert c.STOP_AFTER_KEY == "stop_after"
    assert c.STOP_AFTER_PLAN == "plan"
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc

    assert fc.ASKS_ITEM_KEYS[-2:] == ("writes", c.STOP_AFTER_KEY)


def test_parked_next_action_text():
    assert c.parked_next_action("docs/plans/x.md") == "Run /mise-prep on docs/plans/x.md, then /mise-en-place."
    expected = "Run /mise-prep on the run's plan, then /mise-en-place."
    assert c.parked_next_action(None) == expected
    assert c.parked_next_action("") == expected


def test_parked_next_action_js_matches_python_form():
    js = c.parked_next_action_js("_planRel")
    plan_branch, none_branch = js[1:-1].split(" ? ", 1)[1].split(" : ", 1)
    assert plan_branch == '"Run /mise-prep on " + _planRel + ", then /mise-en-place."'
    assert js.startswith("(_planRel ? ")
    assert json.loads(none_branch) == c.parked_next_action(None)
    head, tail = plan_branch.split(" + _planRel + ")
    assert json.loads(head) + "X" + json.loads(tail) == c.parked_next_action("X")


def test_phase_order_and_names():
    assert c.ASK_PHASES == ("size", "gate", "accept", "plan", "stage", "execute", "review")
    assert (c.OP_ASK_GATE, c.OP_ASK_STAGE) == ("dispatch.ask_gate", "dispatch.ask_stage")
    assert (c.HALT_ROOM, c.HALT_TOUCHPOINT, c.HALT_REFUSAL) == ("room", "touchpoint", "refusal")


def test_tail_and_grouping_constants_are_pinned():
    assert (c.ASK_VERIFY_LABEL, c.ASK_VERIFY_PHASE) == ("verify", "Verify")
    assert c.ASK_STAGE_PHASE == c.ASK_FALSIFIER_PHASE == "stage"
    assert c.ASK_STAGE_PHASE in c.ASK_PHASES
    assert c.ASK_VERIFY_TEST_KEYS == ("status", "summary")
    assert c.ASK_VERIFY_JUDGE_KEYS == (
        "status", "observation", "differs_from_baseline", "baseline_output", "roots_read",
    )
    assert c.ASK_VERIFY_LEDGER_KEYS == ("items", "all_of_pm_ask", "conflicts")
    assert c.ASK_VERIFY_DOCS_KEYS == ("doc_paths", "note_path", "stale_docs")
    assert c.ASK_VERIFY_RETURN_KEYS == (
        "test", "judge", "items", "all_of_pm_ask", "conflicts", "doc_paths", "note_path", "stale_docs",
    )
    assert c.ASK_VERIFY_PHASE not in c.ASK_PHASES


def test_size_then_plan_names():
    assert c.STOP_AFTER_PLAN == "plan"
    assert c.SIZING_SCOUT_AGENT_TYPE == "coordinator:sizing-scout"
    assert c.NO_PLAN_HAND_OFF_FLAG == "--no-plan-hand-off"
    assert c.WARP_RUN_FLAG == "--warp-run"
    assert c.PM_EXIT_QUESTION_KEY == "pm_exit_question"
    assert c.STOP_AFTER_PLAN in c.ASK_PHASES


def test_marker_distinct_from_commit_request():
    assert c.ASK_MANIFEST_MARKER != commit_request.MARKER_PREFIX
    assert not c.ASK_MANIFEST_MARKER.startswith(commit_request.MARKER_PREFIX)
    assert not commit_request.MARKER_PREFIX.startswith(c.ASK_MANIFEST_MARKER)


def test_gate_verdict_to_json():
    v = c.GateVerdict(None, {"kind": c.HALT_ROOM, "reason": "x"})
    assert v.to_json() == {"arm": None, "halt": {"kind": "room", "reason": "x"}, "baton": None}


def test_gate_verdict_to_json_omits_acceptance_when_unset():
    assert "acceptance" not in c.GateVerdict("s", None).to_json()
    stamp = {"by": "engine-size-rule"}
    assert c.GateVerdict("s", None, acceptance=stamp).to_json()["acceptance"] == stamp


def test_design_brief_absent_from_json_when_empty_and_present_when_set():
    assert "design_brief" not in c.GateVerdict("s", None).to_json()
    assert c.GateVerdict("s", None, design_brief="b").to_json()["design_brief"] == "b"


def test_resume_rows_absent_from_json_when_unset_and_ordered_when_set():
    assert "resume_rows" not in c.GateVerdict("s", None, resume_plan="p.md").to_json()
    out = c.GateVerdict("s", None, resume_plan="p.md", resume_rows=["M1", "A1"]).to_json()
    assert out["resume_rows"] == ["M1", "A1"]


def test_scout_schema_admits_resume_rows_array_or_null():
    prop = c.ASK_SCOUT_SCHEMA["properties"]["resume_rows"]
    assert set(prop["type"]) == {"array", "null"}
    assert prop["items"] == {"type": "string"}


def test_resume_plan_is_a_named_tuple():
    r = c.ResumePlan("docs/plans/p.md", ("A1", "M0"))
    plan_rel, incomplete, ledger, landed_open, _kept, _kept_run, held, *_rest = r
    assert (plan_rel, incomplete) == (r.plan_rel, r.incomplete) == ("docs/plans/p.md", ("A1", "M0"))
    assert ledger is r.ledger is None
    assert landed_open == r.landed_open == ()
    assert held == r.held == ()


def test_record_dir_root_is_state_warp_and_run_dir_root_unchanged():
    assert c.RECORD_DIR_ROOT.endswith("/warp") and c.RECORD_DIR_ROOT.startswith("state")
    assert c.RUN_DIR_ROOT == "scratch/warp"


def test_record_dir_no_directory_on_disk(tmp_path):
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_prefers_state_when_present(tmp_path):
    (tmp_path / c.RECORD_DIR_ROOT / "r1").mkdir(parents=True)
    (tmp_path / "scratch/warp/r1").mkdir(parents=True)
    (tmp_path / "scratch/warp/r1/manifest.json").write_text("{}")
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_scratch_with_only_briefs_and_commit_request_is_state(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    (scratch / "briefs").mkdir(parents=True)
    (scratch / "commit-request.txt").write_text("x")
    assert c.record_dir(tmp_path, "r1") == (tmp_path / c.RECORD_DIR_ROOT / "r1").resolve()


def test_record_dir_scratch_record_file_selects_scratch(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    scratch.mkdir(parents=True)
    (scratch / c.RELAYED_ASK_FILE).write_text("x")
    assert c.record_dir(tmp_path, "r1") == scratch.resolve()


def test_record_dir_scratch_spine_only_selects_scratch(tmp_path):
    scratch = tmp_path / "scratch/warp/r1"
    scratch.mkdir(parents=True)
    (scratch / "r1.spine.md").write_text("x")
    assert c.record_dir(tmp_path, "r1") == scratch.resolve()


def test_record_dir_unsafe_run_id_raises(tmp_path):
    for bad in ("..", "a/b", ""):
        with pytest.raises(ValueError):
            c.record_dir(tmp_path, bad)


def test_scout_and_assemble_schema_required_fields():
    assert set(c.SCOUT_SCHEMA["required"]) == {
        "tshirt", "intent", "name", "premise_provenance", "premise_evidence", "exit_criterion", "writes",
    }
    assert set(c.ASSEMBLE_SCHEMA["required"]) == {"exit_code", "stdout"}


def test_scout_schema_carries_the_closing_act_fields_as_optional():
    props = c.ASK_SCOUT_SCHEMA["properties"]
    for field in (
        "exit_code", "stdout", "arm", "halt", "baton", "resume_plan", "spine_plan", "tshirt", "route",
        "manifest", "falsifier",
    ):
        assert field in props, field
    assert c.ASK_SCOUT_SCHEMA["required"] == [*c.SCOUT_SCHEMA["required"], "exit_code"]
    assert "exit_code" in c.ASK_SCOUT_SCHEMA["required"]
    assert "exit_code" not in c.SCOUT_SCHEMA["required"]
    assert "exit_code" not in c.SCOUT_SCHEMA["properties"]
    assert not {"manifest", "falsifier"} & set(c.SCOUT_SCHEMA["properties"])


def test_ask_scout_schema_requires_integer_exit_code():
    assert c.ASK_SCOUT_SCHEMA["required"] == [*c.SCOUT_SCHEMA["required"], "exit_code"]
    assert c.ASK_SCOUT_SCHEMA["properties"]["exit_code"] == {"type": "integer"}
    assert "exit_code" not in c.SCOUT_SCHEMA["required"]
    jsonschema = pytest.importorskip("jsonschema")
    reply = {
        "tshirt": "S", "intent": "i", "name": "n", "premise_provenance": "read",
        "premise_evidence": "e", "exit_criterion": "x", "writes": [], "exit_code": 0,
    }
    jsonschema.validate(reply, c.ASK_SCOUT_SCHEMA)
    del reply["exit_code"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(reply, c.ASK_SCOUT_SCHEMA)


def test_shim_commands_name_settings_home():
    for cmd, tail in (
        (c.SIZING_ASSEMBLE_CMD, '/bin/sizing-assemble"'),
        (c.DOC_NEW_CMD, '/bin/coordinator-doc-new"'),
    ):
        assert cmd.endswith(tail)
        assert "COORDINATOR_SETTINGS_HOME" in cmd


def test_scout_return_clause_names_every_required_field():
    for field in c.SCOUT_SCHEMA["required"]:
        assert field in c.SCOUT_RETURN_CLAUSE
    assert c.SCOUT_RETURN_CLAUSE.startswith("Return its inputs:")
    assert c.SCOUT_RETURN_CLAUSE.endswith("never omit one.")


def test_scout_return_clause_forbids_suite_tier_names_and_states_the_phrasing():
    assert "never name a suite tier" in c.SCOUT_RETURN_CLAUSE
    assert "'tests covering touched files pass'" in c.SCOUT_RETURN_CLAUSE


def test_research_flags_js_appends_one_quoted_seed_flag_per_seed():
    js = c.research_assemble_flags_js("s", "q")
    assert "(s.research.seeds || []).map(function (v) { return ' --research-seed ' + q(v); })" in js
    assert js.index("--research-target") < js.index("--research-seed") < js.index("--research-question")


def test_manifest_row_group_serialises_only_when_set_and_round_trips():
    solo = c.ManifestRow("C1", "executor", "sonnet", "b.md", (), 0)
    grouped = c.ManifestRow("C2", "executor", "sonnet", "b.md", (), 0, (), "g0-C2")
    assert "group" not in solo.to_json()
    assert grouped.to_json()["group"] == "g0-C2"
    assert c.ManifestRow.from_json(json.loads(json.dumps(grouped.to_json()))) == grouped
    legacy = {k: v for k, v in grouped.to_json().items() if k != "group"}
    assert c.ManifestRow.from_json(legacy).group == ""


def test_stage_manifest_fanout_serialises_only_when_set_and_round_trips():
    base = c.StageManifest("r", (), (), "m")
    assert "fanout" not in base.to_json()
    assert c.StageManifest.from_json(base.to_json()).fanout is None
    fan = {"expectedAgents": 3, "basis": "exact", "units": 6}
    m = c.StageManifest("r", (), (), "m", fanout=fan)
    assert m.to_json()["fanout"] == fan
    assert c.StageManifest.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_stage_manifest_memo_close_serialises_only_when_set_and_round_trips():
    base = c.StageManifest("r", (), (), "m")
    assert "memo_close" not in base.to_json()
    assert c.StageManifest.from_json(base.to_json()).memo_close == ()
    closes = ({"id": "X1", "memo": "state/cross-repo/inbox/a.md"},)
    m = c.StageManifest("r", (), (), "m", memo_close=closes)
    assert m.to_json()["memo_close"] == list(closes)
    assert c.StageManifest.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_scout_schema_seeds_pattern_admits_only_seed_kinds():
    import re

    items = c.ASK_SCOUT_SCHEMA["properties"]["research"]["properties"]["seeds"]["items"]
    pat = re.compile(items["pattern"])
    assert pat.match("repo=https://x/y.git") and pat.match("video=https://v?a=b")
    assert not pat.match("blog=https://x") and not pat.match("doc=") and not pat.match("doc=a b")


def test_kept_work_fields_absent_at_defaults_and_present_when_set():
    assert "resume_ready" not in c.GateVerdict("s", None).to_json()
    assert c.GateVerdict("s", None, resume_ready=True).to_json()["resume_ready"] is True
    row = _manifest().rows[0]
    assert "kept_from" not in row.to_json()
    kept = c.ManifestRow.from_json({**row.to_json(), "kept_from": "ask-1"})
    assert kept.kept_from == "ask-1" and kept.to_json()["kept_from"] == "ask-1"
    assert c.ManifestRow.from_json(row.to_json()).kept_from == ""
    plan = c.ResumePlan("p.md", ("C1",))
    assert plan.kept == () and plan.kept_run is None


def test_measured_rev_constants_and_record_path(tmp_path):
    assert c.MEASURED_REV_OP == "dispatch.measured_rev"
    assert c.MEASURED_REV_ENV == "COORDINATOR_MEASURED_REV"
    assert c.MEASURED_REV_RECORD == "measured-rev.json"
    assert c.MEASURED_REV_KEYS == ("sha", "base_sha", "paths", "removed", "blobs")
    path = c.measured_rev_record_path(tmp_path, "rid")
    assert path.parent == tmp_path / f"{c.RECORD_DIR_ROOT}/rid"
    assert path.name == c.MEASURED_REV_RECORD
    assert c.RUN_DIR_ROOT not in path.relative_to(tmp_path).as_posix()
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            c.measured_rev_record_path(tmp_path, bad)


def test_ask_verify_return_keys_unchanged_by_measured_rev():
    assert not set(c.MEASURED_REV_KEYS) & set(c.ASK_VERIFY_RETURN_KEYS)


def test_parked_halt_carries_one_refire_call_not_a_hand_step():
    from coordinator_core.ops.dispatch_emit import halt_refire

    env = c.parked_refire_next_action("docs/plans/x.md", run_id="ask-1", sizing_rel="state/sizings/s.yaml")
    assert halt_refire.is_refire_next_action(env)
    assert env["resume"]["halted"] == c.HALT_PARKED and env["resume"]["sizing"] == "state/sizings/s.yaml"
    assert "coordinator:engine-ask" in env["call"] and "state/sizings/s.yaml" in env["call"]
    assert env["detail"] == c.parked_next_action("docs/plans/x.md")


def test_dependents_unstarted_halt_carries_one_refire_call_with_the_hold_reason():
    from coordinator_core.ops.dispatch_emit import halt_refire

    env = c.dependents_unstarted_refire_next_action("C2 held on C1", run_id="ask-1", sizing_rel="state/sizings/s.yaml")
    assert halt_refire.is_refire_next_action(env)
    assert env["resume"]["halted"] == c.HALT_DEPENDENTS_UNSTARTED and env["detail"].startswith("C2 held on C1") and c.ACCEPT_PARTIAL_FORM.format(run_id="ask-1") in env["detail"]
