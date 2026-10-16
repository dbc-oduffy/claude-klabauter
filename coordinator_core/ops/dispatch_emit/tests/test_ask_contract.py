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
    plan_rel, incomplete, ledger = r
    assert (plan_rel, incomplete) == (r.plan_rel, r.incomplete) == ("docs/plans/p.md", ("A1", "M0"))
    assert ledger is r.ledger is None


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
    assert c.ASK_SCOUT_SCHEMA["required"] == c.SCOUT_SCHEMA["required"]
    assert "exit_code" not in c.SCOUT_SCHEMA["properties"]
    assert not {"manifest", "falsifier"} & set(c.SCOUT_SCHEMA["properties"])


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
