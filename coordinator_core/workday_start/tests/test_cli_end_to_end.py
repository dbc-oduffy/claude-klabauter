"""workday-start-run end to end: fixture repo under tmp_path, fake dispatch table, fixture template."""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
from pathlib import Path

import pytest

from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.ceremony_common.run import ledger, single_flight
from coordinator_core.ceremony_common.run.runner import ChildOutcome
from coordinator_core.ceremony_common.run.contract import StepSpec
from coordinator_core.workday_start import cli, manifest, run_spec

DAY1, DAY2, DAY3 = "2026-10-12", "2026-10-13", "2026-10-14"
FIXTURE_ENVELOPE = "fixture-envelope"
_FIXTURE_ENVELOPE_STEP = StepSpec(
    id=FIXTURE_ENVELOPE, stage="orient", kind="inproc", target="fixture:envelope", args=(),
    depends_on=None, already_satisfied=False, detail="Test-only envelope producer.",
    heading="Observations", cost_class="engine",
)


def _fixture_spec():
    """The real spec with a test-only envelope step spliced in after Step 0's stage."""
    base = run_spec.SPEC.front_door

    def front_door(**kw):
        steps = list(base(**kw))
        at = 1 + max(
            (i for i, s in enumerate(steps) if isinstance(s, StepSpec) and s.stage == "step0"), default=-1
        )
        steps.insert(at, _FIXTURE_ENVELOPE_STEP)
        return steps

    return dataclasses.replace(run_spec.SPEC, front_door=front_door, envelope_step=FIXTURE_ENVELOPE)

TEMPLATE = (
    "export const meta = {\n  name: 'fixture',\n}\n"
    "export default async function run() { return args.digest.run_id }\n"
)
CONFLICT = "RECONCILE-CONFLICT branch=work/test"
NEUTRAL_SOURCES = Path(ledger.__file__).parent


class Box:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/work/test\n", encoding="utf-8")
        self.state = tmp_path / "state"
        monkeypatch.setenv("CWS_TEST_STATE_ROOT", str(self.state))
        monkeypatch.setattr(manifest, "_cost_rows", lambda: {})
        monkeypatch.setattr(cli, "SPEC", _fixture_spec())
        self.plugin = tmp_path / "plugin"
        (self.plugin / "workflows").mkdir(parents=True)
        self.capsys = capsys
        self.outputs: dict = {}
        self.ran: list[str] = []
        self.day = DAY1

    def with_template(self):
        (self.plugin / cli.TEMPLATE_RELPATH).write_text(TEMPLATE, encoding="utf-8")
        return self

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s):
            self.ran.append(step.id)
            out = self.outputs.get(step.id, (0, "", ""))
            if step.id == FIXTURE_ENVELOPE and step.id not in self.outputs:
                out = (0, json.dumps({"judgment_points": [], "directives": []}), "")
            if step.id == cli.INBOX_STEP and step.id not in self.outputs:
                out = (0, json.dumps({"state": "inventory"}), "")
            if step.id == "marker-write-workday-start-marker":
                self.state.mkdir(parents=True, exist_ok=True)
                (self.state / ".workday-start-marker").write_text(self.day, encoding="utf-8")
            if step.id == cli.FRESHNESS_STEP and step.id not in self.outputs:
                out = (0, json.dumps({"doc_commit_delta": 1}), "")
            code, stdout, stderr = out
            return ChildOutcome(exit_code=code, process_ms=1.0, spawns=1, stdout=stdout, stderr=stderr)

        return {
            "cli": run, "op": run, "inproc": run,
            "rule-child": lambda argv, *, repo_root, deadline_s: ChildOutcome(0, 1.0, 1),
        }

    def go(self, argv, *, day=None, plugin=True):
        self.day = day or self.day
        code = cli.main(
            argv, dispatch=self.table(), plugin_root=self.plugin if plugin else None,
            repo_root=self.root, today=self.day,
        )
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def launch(self, **kw):
        return self.go(["launch"], **kw)

    def envelope(self, points=(), directives=()):
        self.outputs[FIXTURE_ENVELOPE] = (0, json.dumps({
            "judgment_points": list(points), "directives": list(directives), "next_move": "",
        }), "")

    def inbox(self, state="escalate", memos=("state/cross-repo/inbox/a.md",), report_path=None):
        dispatch = {"id": "triage-all", "stage": "triage", "memos": list(memos), "brief": "triage them"}
        if report_path:
            dispatch["report_path"] = report_path
        dispatches = [dispatch]
        self.outputs[cli.INBOX_STEP] = (0, json.dumps({"state": state, "dispatches": dispatches}), "")

    def runs(self):
        base = ledger.run_dir(self.root, cli.CEREMONY, "x").parent
        return sorted(p for p in base.iterdir() if p.is_dir())

    def rows(self, run_dir=None):
        return ledger.read(run_dir or self.runs()[-1])

    def digest_run_id(self, out):
        script = re.search(r'scriptPath: "([^"]+)"', out).group(1)
        return Path(script).parent.name

    def marker(self):
        return (self.state / ".workday-start-marker").is_file()

    def lock(self):
        return (single_flight._lock_dir(self.root, cli.CEREMONY) / "lock.json")


@pytest.fixture
def box(tmp_path, monkeypatch, capsys):
    return Box(tmp_path, monkeypatch, capsys)


def _point(pid, dispositions, recommendation=None):
    return build_judgment_point(
        recommendation, id=pid, question=f"question {pid}",
        dispositions=[build_disposition(v) for v in dispositions],
        evidence="ev", reason="insufficient-evidence" if recommendation is None else "",
    )


def _results(**groups):
    return {"groups": groups}


def _finish(box, run_id, results):
    path = box.runs()[-1] / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    return box.go(["finish", "--run-id", run_id, "--agent-results", str(path)])


def test_launch_without_agent_work_prints_briefing_and_no_workflow(box):
    code, out, _ = box.launch()
    assert code == 0
    assert "Workflow(" not in out
    assert out.startswith("# Workday-start briefing: 2026-10-12 (work/test)")
    assert box.marker() and not box.lock().exists()
    assert {r["id"] for r in box.rows()} >= {"step0-branch-setup", "marker-write-workday-start-marker"}


def test_a_launch_with_no_envelope_step_composes_and_decides_with_no_points(box, monkeypatch):
    monkeypatch.setattr(cli, "SPEC", dataclasses.replace(run_spec.SPEC, envelope_step=None))
    code, out, _ = box.launch()
    rows = {r["id"]: r for r in box.rows()}
    assert code == 0 and out.startswith("# Workday-start briefing")
    assert FIXTURE_ENVELOPE not in rows
    assert rows["decide-resolve"]["status"] == "ran"
    assert rows["decide-resolve"]["stdout"].startswith("0 judgment point(s) decided by rule, 0 routed")
    assert not (box.runs()[-1] / "decisions.jsonl").exists()


REBUILT_STEPS = (
    "hyg-watch-state-ignore", "orient-day-envelope", "triage-stale-executing-plans",
    "memo-outbox-stale-drafts", "rollup-local-ahead-unpublished", "rollup-recheck-due-markers",
)


def test_the_real_spec_names_the_day_envelope_step_and_launch_composes_each_rebuilt_step(box, monkeypatch):
    monkeypatch.setattr(cli, "SPEC", run_spec.SPEC)
    assert run_spec.SPEC.envelope_step == run_spec.ENVELOPE_STEP == "orient-day-envelope"
    box.outputs[run_spec.ENVELOPE_STEP] = (0, json.dumps({"judgment_points": [], "directives": []}), "")
    code, _, _ = box.launch()
    rows = {r["id"]: r for r in box.rows()}
    assert code == 0
    assert all(rows[s]["status"] == "ran" for s in REBUILT_STEPS)


def test_inbox_dispatch_writes_script_with_matching_receipt_and_one_workflow_line(box):
    box.with_template().inbox()
    code, out, _ = box.launch()
    assert code == 0
    assert out.count("Workflow(") == 1
    script = Path(re.search(r'scriptPath: "([^"]+)"', out).group(1))
    receipt = json.loads(Path(str(script) + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    assert "const args = " in script.read_text(encoding="utf-8")
    digest = json.loads((script.parent / cli.DIGEST_FILE).read_text(encoding="utf-8"))
    inbox = [g for g in digest["agent_work"]["groups"] if g["id"] == "inbox"][0]
    assert len(inbox["entries"]) == 1 and inbox["entries"][0]["brief"] == "triage them"
    assert box.lock().exists() and not box.marker()


def test_inventory_state_with_one_dispatch_yields_one_inbox_entry(box):
    box.with_template().inbox(state="inventory")
    code, out, _ = box.launch()
    digest = json.loads((box.runs()[-1] / cli.DIGEST_FILE).read_text(encoding="utf-8"))
    assert code == 0 and out.count("Workflow(") == 1
    assert [len(g["entries"]) for g in digest["agent_work"]["groups"] if g["id"] == "inbox"] == [1]


def test_missing_template_exits_4_with_ledger_briefing_marker_and_lock_released(box):
    box.inbox()
    box.envelope([_point("j-x-1", ["a", "b"])])
    code, out, err = box.launch()
    assert code == 4 and "workflows/workday-start.mjs" in err
    assert "### Failed" in out and "agent:inbox" in out and "agent:judgment" in out
    assert "j-x-1" in out
    assert box.marker() and not box.lock().exists()
    ids = {r["id"] for r in box.rows()}
    assert {"step0-branch-setup", "agent:inbox", "agent:judgment"} <= ids
    filed = [r["id"] for r in box.rows() if r["id"].startswith(cli.BACKLOG_PREFIX)]
    assert filed == [cli.BACKLOG_PREFIX + "agent:inbox"]


def test_failures_past_the_cap_file_one_row_for_the_run(box):
    sentinels = [s.id for s in manifest.steps_for(["sentinels"], repo_root=box.root, today=datetime.date(2026, 10, 12))
                 if s.cost_class != "over-bar"]
    assert len(sentinels) > cli.FAILURE_ROWS_FILED_MAX
    for step_id in sentinels:
        box.outputs[step_id] = (1, "", "broke")
    code, _, _ = box.launch()
    filed = [r["id"] for r in box.rows() if r["id"].startswith(cli.BACKLOG_PREFIX)]
    assert code == 0 and filed == [cli.BACKLOG_PREFIX + sentinels[0]]


def test_an_open_row_with_the_same_title_is_cited_not_refiled(box):
    box.outputs["sentinel-engine-drift"] = (1, "", "broke")
    backlog = box.root / "state" / "bug-backlog"
    backlog.mkdir(parents=True)
    title = "workday-start step sentinel-engine-drift failed: exit 1: broke"
    (backlog / "2026-10-11-x.yaml").write_text(f'title: "{title}"\nstatus: open\n', encoding="utf-8")
    box.launch()
    row = [r for r in box.rows() if r["id"] == cli.BACKLOG_PREFIX + "sentinel-engine-drift"][0]
    assert row["reason"] == "open-row-cited" and row["stdout"].endswith("2026-10-11-x.yaml")
    assert cli.BACKLOG_PREFIX + "sentinel-engine-drift" not in box.ran


def test_step0_conflict_is_deferred_once_then_fails_the_second_run(box):
    box.outputs["step0-branch-setup"] = (3, CONFLICT + "\n", "CONFLICT-PATHS\nsrc/a.py\nsrc/b.py\n")
    code, out, _ = box.launch(day=DAY1)
    assert code == 0 and box.marker()
    assert not (box.root / "tasks" / ".deferred-branches.md").exists()
    decided = out.split("### Decided\n", 1)[1].splitlines()[0]
    assert "step0-reconcile-outcome" in decided and "deferred-once" in decided

    (box.state / ".workday-start-marker").unlink()
    code, out, _ = box.launch(day=DAY2)
    assert code == 5 and not box.marker()
    assert out.split("\n", 2)[2].lstrip().startswith("### Failed")
    assert "src/a.py,src/b.py" in out


OPEN = "RECONCILE-CONFLICT-OPEN branch=work/test"


def _open_merge_launch(box):
    box.with_template()
    box.outputs["step0-branch-setup"] = (3, OPEN + "\n", "CONFLICT-PATHS\nsrc/a.py\nsrc/b.py\n")
    code, out, _ = box.launch()
    return code, out


def test_open_merge_halts_committing_stages_and_emits_one_reconcile_group(box):
    code, out = _open_merge_launch(box)
    assert code == 0 and out.count("Workflow(") == 1
    rows = {r["id"]: r for r in box.rows()}
    assert rows["step0-branch-setup"]["status"] == "ran" and rows["step0-branch-setup"]["reason"] == "held:reconcile-open"
    halted = rows["step0-normalize-consumed-frontmatter"]
    assert halted["status"] == "halted-by-gate" and halted["reason"] == "held:reconcile-open"
    assert "step0-normalize-consumed-frontmatter" not in box.ran and FIXTURE_ENVELOPE in box.ran
    digest = json.loads((box.runs()[-1] / cli.DIGEST_FILE).read_text(encoding="utf-8"))
    (group,) = [g for g in digest["agent_work"]["groups"] if g["id"] == "reconcile"]
    (entry,) = group["entries"]
    assert group["phase"] == 0 and entry["argv"] is None
    assert entry["inputs"]["paths"] == ["src/a.py", "src/b.py"]
    assert "Run no git command" in entry["brief"]
    assert not box.marker()


def test_finish_concludes_the_open_merge_before_the_briefing_and_the_tail(box):
    _, out = _open_merge_launch(box)
    box.outputs["reconcile-conclude"] = (0, "RECONCILED-MERGE-CONCLUDED branch=work/test\n", "")
    code, _, _ = _finish(box, box.digest_run_id(out), _results(reconcile=[{"key": "reconcile", "outcome": "done"}]))
    ids = [r["id"] for r in box.rows()]
    assert code == 0
    row = next(r for r in box.rows() if r["id"] == "reconcile-conclude")
    assert row["status"] == "ran" and "RECONCILED-MERGE-CONCLUDED" in row["stdout"]
    assert ids.index("reconcile-conclude") < ids.index("marker-write-workday-start-marker")
    assert box.marker()


def test_conclude_refusal_is_a_failed_row_carrying_the_refusal_string(box):
    _, out = _open_merge_launch(box)
    box.outputs["reconcile-conclude"] = (3, "RECONCILE-CONCLUDE-MARKERS\nsrc/a.py\n", "")
    _finish(box, box.digest_run_id(out), _results(reconcile=[{"key": "reconcile", "outcome": "done"}]))
    row = next(r for r in box.rows() if r["id"] == "reconcile-conclude")
    assert row["status"] == "failed" and "RECONCILE-CONCLUDE-MARKERS" in row["reason"]


def test_conclude_step_runs_the_conclude_cli_and_leads_with_its_refusal_text(box, monkeypatch):
    seen = {}

    def fake(target, args, *, repo_root, deadline_s):
        seen.update(target=target, args=tuple(args), repo_root=repo_root)
        return ChildOutcome(3, 1.0, 2, "", "RECONCILE-CONCLUDE-UNMERGED\na.py\n")

    monkeypatch.setattr("coordinator_core.workday_start.run_spec.run_bin_cold", fake)
    from coordinator_core.workday_start import run_spec

    code, text = run_spec.conclude_open_merge(box.root)
    assert code == 3 and text.splitlines()[0] == "RECONCILE-CONCLUDE-UNMERGED"
    assert seen == {"target": "reconcile-merge-conclude", "args": (), "repo_root": box.root}


def test_a_clean_run_has_no_conclude_step(box):
    box.with_template().inbox()
    _, out, _ = box.launch()
    _finish(box, box.digest_run_id(out), _results(inbox=[
        {"key": "triage-all", "outcome": "done", "memos": ["state/cross-repo/inbox/a.md"]}]))
    assert "reconcile-conclude" not in [r["id"] for r in box.rows()]


def test_conflict_after_a_clean_step0_is_deferred_again(box):
    code, _, _ = box.launch(day=DAY1)
    assert code == 0
    (box.state / ".workday-start-marker").unlink()
    box.outputs["step0-branch-setup"] = (3, CONFLICT + "\n", "")
    code, out, _ = box.launch(day=DAY2)
    assert code == 0 and "deferred-once" in out and box.marker()


def test_step0_failure_exits_5_with_failed_first_and_no_marker(box):
    box.outputs["step0-branch-setup"] = (1, "CRASH\n", "traceback")
    code, out, _ = box.launch()
    assert code == 5 and not box.marker() and not box.lock().exists()
    assert out.split("\n", 2)[2].lstrip().startswith("### Failed")
    assert "marker-write-workday-start-marker" not in box.ran


def test_finish_with_memo_missing_from_results_writes_a_failed_row(box):
    box.with_template().inbox(memos=("state/cross-repo/inbox/a.md", "state/cross-repo/inbox/b.md"))
    _, out, _ = box.launch()
    run_id = box.digest_run_id(out)
    code, out, _ = _finish(box, run_id, _results(inbox=[
        {"key": "triage-all", "outcome": "done", "memos": ["state/cross-repo/inbox/a.md"]}]))
    failed = {r["id"]: r["reason"] for r in box.rows() if r["status"] == "failed"}
    assert code == 0 and failed.get("inbox:b.md") == "no-result" and "inbox:a.md" not in failed
    assert box.marker() and not box.lock().exists()


def _triaged_run(box, report_path, written):
    box.with_template().inbox(
        memos=("state/cross-repo/inbox/a.md", "state\\cross-repo\\inbox\\b.md"), report_path=report_path)
    _, out, _ = box.launch()
    if written:
        target = box.root / report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("report", encoding="utf-8")
    return box.digest_run_id(out)


def test_triaged_dispatch_with_report_and_no_memos_key_is_credited(box):
    run_id = _triaged_run(box, "state/audits/2026-10-17-inbox-blitz-all.md", written=True)
    _finish(box, run_id, _results(inbox=[{"key": "triage-all", "outcome": "triaged"}]))
    assert not [r for r in box.rows() if r["status"] == "failed"]


def test_triaged_dispatch_without_its_report_still_fails_its_memos(box):
    run_id = _triaged_run(box, "state/audits/2026-10-17-inbox-blitz-all.md", written=False)
    _finish(box, run_id, _results(inbox=[{"key": "triage-all", "outcome": "triaged"}]))
    failed = {r["id"]: r["reason"] for r in box.rows() if r["status"] == "failed"}
    assert failed == {"inbox:a.md": "no-result", "inbox:b.md": "no-result"}


def test_failed_dispatch_with_report_present_still_fails_its_memos(box):
    run_id = _triaged_run(box, "state/audits/2026-10-17-inbox-blitz-all.md", written=True)
    _finish(box, run_id, _results(inbox=[{"key": "triage-all", "outcome": "failed"}]))
    failed = {r["id"]: r["reason"] for r in box.rows() if r["status"] == "failed"}
    assert failed == {"inbox:a.md": "no-result", "inbox:b.md": "no-result"}


def test_acting_ruling_without_a_directive_writes_decision_and_failed_row(box):
    box.with_template().envelope([_point("j-act-1", ["close_stubs", "leave"])])
    _, out, _ = box.launch()
    run_id = box.digest_run_id(out)
    _finish(box, run_id, _results(judgment=[{
        "key": "j-act-1", "outcome": "decided",
        "decision": {"disposition": "close_stubs", "rationale": "stale"}}]))
    decisions = [json.loads(x) for x in (box.runs()[-1] / "decisions.jsonl").read_text().splitlines()]
    assert [(d["point_id"], d["decision"], d["decided_by"]) for d in decisions] == [
        ("j-act-1", "close_stubs", "agent:coordinator:apm")]
    failed = {r["id"]: r["reason"] for r in box.rows() if r["status"] == "failed"}
    assert failed["decision:j-act-1"] == "no-directive-carries:close_stubs"


def test_leave_ruling_writes_the_decision_alone(box):
    box.with_template().envelope([_point("j-lv-1", ["close_stubs", "leave"])])
    _, out, _ = box.launch()
    _finish(box, box.digest_run_id(out), _results(judgment=[{
        "key": "j-lv-1", "outcome": "decided", "decision": {"disposition": "leave", "rationale": "fine"}}]))
    decisions = (box.runs()[-1] / "decisions.jsonl").read_text().splitlines()
    assert len(decisions) == 1
    assert not [r for r in box.rows() if r["status"] == "failed"]


def test_point_without_recommendation_is_a_judgment_entry_and_no_ruling_fails(box):
    box.with_template().envelope([_point("j-q-1", ["leave", "pin"])])
    _, out, _ = box.launch()
    digest = json.loads((box.runs()[-1] / cli.DIGEST_FILE).read_text(encoding="utf-8"))
    judgment = [g for g in digest["agent_work"]["groups"] if g["id"] == "judgment"][0]
    assert [e["key"] for e in judgment["entries"]] == ["j-q-1"]
    _finish(box, box.digest_run_id(out), _results())
    assert not (box.runs()[-1] / "decisions.jsonl").exists()
    failed = {r["id"]: r["reason"] for r in box.rows() if r["status"] == "failed"}
    assert failed["judgment:j-q-1"] == "no-agent-result"


def test_inbox_skipped_with_an_open_memo_point_fails_the_assembler_step(box):
    box.outputs[cli.INBOX_STEP] = (0, json.dumps({"state": "skipped"}), "")
    box.envelope([_point("j-memo-1", ["accept", "decline"])])
    box.launch()
    row = [r for r in box.rows() if r["id"] == cli.INBOX_STEP][0]
    assert row["status"] == "failed" and row["reason"] == "skipped-with-open-memos"


def test_finish_twice_runs_the_tail_once(box):
    box.with_template().inbox()
    _, out, _ = box.launch()
    run_id = box.digest_run_id(out)
    results = _results(inbox=[{"key": "triage-all", "outcome": "done", "memos": ["state/cross-repo/inbox/a.md"]}])
    _, first, _ = _finish(box, run_id, results)
    ran_once = box.ran.count("marker-write-workday-start-marker")
    _, second, _ = box.go(["finish", "--run-id", run_id])
    assert first == second and box.ran.count("marker-write-workday-start-marker") == ran_once == 1


def test_dropped_tail_is_finished_by_the_next_launch(box):
    box.with_template().inbox()
    _, out, _ = box.launch()
    run_dir = box.runs()[-1]
    (run_dir / cli.AGENT_RESULTS_FILE).write_text(json.dumps(_results(inbox=[
        {"key": "triage-all", "outcome": "done", "memos": ["state/cross-repo/inbox/a.md"]}])), encoding="utf-8")
    code, out, _ = box.launch()
    assert code == 0 and out.startswith("# Workday-start briefing") and box.marker()
    assert not box.lock().exists()


def test_in_flight_launch_exits_3_without_results(box):
    box.with_template().inbox()
    box.launch()
    code, out, _ = box.launch()
    assert code == 3 and json.loads(out)["state"] == "in-flight"


def test_usage_errors_and_help(box):
    assert box.go(["resume"])[0] == 2
    assert box.go(["launch", "--decisions", "x"])[0] == 2
    assert box.go(["finish"])[0] == 2
    assert box.go(["finish", "--run-id", "../../escape"])[0] == 2
    assert box.go(["--help"])[0] == 0


def test_no_output_carries_parked_for_you(box):
    box.with_template().inbox()
    box.envelope([_point("j-q-1", ["leave", "pin"])])
    outputs = [box.launch()[1]]
    outputs.append(box.go(["finish", "--run-id", box.digest_run_id(outputs[0])])[1])
    assert all("### Parked for you" not in o for o in outputs)


@pytest.mark.parametrize("name", ["runner.py", "ledger.py", "single_flight.py", "emit.py", "decide.py"])
def test_neutral_modules_name_no_ceremony(name):
    text = (NEUTRAL_SOURCES / name).read_text(encoding="utf-8")
    assert "workday-start" not in text and "workday_start" not in text
