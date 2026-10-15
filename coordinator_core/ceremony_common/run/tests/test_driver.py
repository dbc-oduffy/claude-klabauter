"""The shared driver end to end: fixture spec, fake dispatch table, fixture template, tmp_path."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.ceremony_common.run import decide, driver, ledger, single_flight
from coordinator_core.ceremony_common.run.contract import StepSpec
from coordinator_core.ceremony_common.run.runner import ChildOutcome
from coordinator_core.ceremony_common.run.spec import CeremonySpec

DAY = "2026-10-12"
TEMPLATE = (
    "export const meta = {\n  name: 'fixture',\n}\n"
    "export default async function run() { return args.digest.run_id }\n"
)
PROG = ("fx-run",)


def _step(step_id: str, stage: str, **kw) -> StepSpec:
    fields = dict(
        id=step_id, stage=stage, kind="inproc", target="m:f", args=(), depends_on=None,
        already_satisfied=False, detail="", heading="Observations", cost_class="engine",
    )
    fields.update(kw)
    return StepSpec(**fields)


def _point(pid, dispositions, resolves=()):
    return build_judgment_point(
        None, id=pid, question=f"question {pid}",
        dispositions=[build_disposition(v, resolves if i == 0 else ()) for i, v in enumerate(dispositions)],
        evidence="ev", reason="insufficient-evidence",
    )


def _directive(did, depends_on):
    return {"id": did, "cli": "whats-next", "args": [], "depends_on": depends_on, "detail": ""}


class Box:
    def __init__(self, tmp_path, capsys):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/work/test\n", encoding="utf-8")
        self.plugin = tmp_path / "plugin"
        (self.plugin / "workflows").mkdir(parents=True)
        self.capsys = capsys
        self.outputs: dict = {}
        self.ran: list[str] = []
        self.tail_calls: list = []
        self.envelope()

    def with_template(self):
        (self.plugin / "workflows" / "fixture.mjs").write_text(TEMPLATE, encoding="utf-8")
        return self

    def envelope(self, points=(), directives=()):
        self.outputs["orient"] = (0, json.dumps({"judgment_points": list(points), "directives": list(directives)}), "")

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s, **_kw):
            self.ran.append(step.id)
            code, stdout, stderr = self.outputs.get(step.id, (0, "", ""))
            return ChildOutcome(exit_code=code, process_ms=1.0, spawns=1, stdout=stdout, stderr=stderr)

        return {
            "cli": run, "op": run, "inproc": run,
            "rule-child": lambda argv, *, repo_root, deadline_s: ChildOutcome(0, 1.0, 1),
        }

    def spec(self, **over) -> CeremonySpec:
        def finish_steps(decisions, ledger_rows, *, run):
            self.tail_calls.append((list(decisions), dict(run)))
            return [_step("tail-1", "tail")]

        fields = dict(
            name="fixture", dispatch_key="workday_start", run_dir_name="fixture",
            template="workflows/fixture.mjs", marker_path=None,
            launch_stages=("prep", "orient", "decide"), finish_stages=("decisions", "briefing", "tail"),
            front_door=[_step("prep-1", "prep"), _step("orient", "orient")],
            envelope_step="orient", stage_of=lambda _id: "decisions", residue=(),
            rules={decide.RECOMMENDATION: decide.recommendation},
            spec_points=lambda envelope, flags, rows: [],
            agent_groups=lambda envelope, rows, resolution: [],
            finish_steps=finish_steps, receipt_sections=(), render=None,
            costs=lambda key: "engine", chain=None, launch_flags=frozenset({"--x"}),
        )
        fields.update(over)
        return CeremonySpec(**fields)

    def launch(self, spec=None, flags=None, session="S-launch"):
        code = driver.launch(
            spec or self.spec(), repo_root=self.root, flags=flags or {}, session_id=session,
            prog_argv=PROG, dispatch=self.table(), plugin_root=self.plugin, today=DAY,
        )
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def finish(self, spec, run_id, results=None):
        path = None
        if results is not None:
            path = self.runs()[-1] / "r.json"
            path.write_text(json.dumps(results), encoding="utf-8")
        code = driver.finish(
            spec, repo_root=self.root, run_id=run_id, agent_results_path=str(path) if path else None,
            prog_argv=PROG, dispatch=self.table(),
        )
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def runs(self):
        base = ledger.run_dir(self.root, "fixture", "x").parent
        return sorted(p for p in base.iterdir() if p.is_dir())

    def rows(self):
        return ledger.read(self.runs()[-1])

    def status(self, step_id):
        return {r["id"]: (r["status"], r["reason"]) for r in self.rows()}.get(step_id)

    def run_id(self, out):
        return Path(re.search(r"^digest: (.+)$", out, re.MULTILINE).group(1)).parent.name

    def lock(self):
        return single_flight._lock_dir(self.root, "fixture") / "lock.json"


@pytest.fixture
def box(tmp_path, capsys):
    return Box(tmp_path, capsys)


def _results(point_id, disposition, **extra):
    decision = {"disposition": disposition, "rationale": "because", **extra}
    return {"groups": {"judgment": [{"key": point_id, "outcome": "decided", "decision": decision}]}}


def test_launch_with_no_agent_work_prints_a_receipt_and_no_workflow(box):
    code, out, _ = box.launch()
    assert code == 0 and "Workflow(" not in out
    assert out.startswith("# fixture receipt: 2026-10-12 (work/test)")
    assert not box.lock().exists()
    assert {r["id"] for r in box.rows()} >= {"prep-1", "orient", "decide-resolve", "tail-1"}


def test_launch_with_a_judgment_entry_prints_one_workflow_line_and_a_receipt_that_verifies(box):
    box.with_template().envelope([_point("j-1", ["a", "b"])])
    code, out, _ = box.launch()
    assert code == 0 and out.count("Workflow(") == 1
    script = Path(re.search(r'scriptPath: "([^"]+)"', out).group(1))
    receipt = json.loads(Path(str(script) + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    digest = json.loads((script.parent / driver.DIGEST_FILE).read_text(encoding="utf-8"))
    judgment = [g for g in digest["agent_work"]["groups"] if g["id"] == "judgment"][0]
    assert [e["key"] for e in judgment["entries"]] == ["j-1"]
    assert digest["finish_argv"][:2] == ["fx-run", "finish"]
    assert box.lock().exists()


def test_finish_hands_the_payload_and_the_launching_session_to_finish_steps(box):
    spec = box.spec()
    box.with_template().envelope([_point("j-1", ["a", "b"])])
    _, out, _ = box.launch(spec, session="S-launch")
    code, _, _ = box.finish(spec, box.run_id(out), _results("j-1", "a", payload={"titles": ["x"]}))
    assert code == 0
    (decisions, run), = box.tail_calls
    assert decisions[0].payload == {"titles": ["x"]}
    assert run["session_id"] == "S-launch" and run["day"] == DAY
    assert not box.lock().exists()


def test_undecided_point_exits_5_and_halts_its_dependants(box):
    spec = box.spec()
    box.with_template().envelope(
        [_point("j-1", ["a", "b"], resolves=["d-1"])], [_directive("d-1", "j-1")]
    )
    _, out, _ = box.launch(spec)
    code, _, _ = box.finish(spec, box.run_id(out), {"groups": {}})
    assert code == 5
    assert box.status("judgment:j-1") == ("failed", "no-agent-result")
    assert box.status("d-1") == ("halted-by-gate", "judgment:j-1")
    assert "d-1" not in box.ran


def test_a_decision_that_does_not_open_a_directive_closes_it_and_an_unknown_dependency_fails_it(box):
    spec = box.spec()
    box.with_template().envelope(
        [_point("j-1", ["a", "b"], resolves=["d-1"])], [_directive("d-1", "j-1"), _directive("d-2", "zz")]
    )
    _, out, _ = box.launch(spec)
    code, _, _ = box.finish(spec, box.run_id(out), _results("j-1", "b"))
    assert code == 0
    assert box.status("d-1") == ("not-composed", "closed-by:j-1=b")
    assert box.status("d-2") == ("failed", "unknown-dependency:zz")


def test_a_decision_that_opens_a_directive_runs_it_once(box):
    spec = box.spec()
    box.with_template().envelope([_point("j-1", ["a", "b"], resolves=["d-1"])], [_directive("d-1", "j-1")])
    _, out, _ = box.launch(spec)
    code, _, _ = box.finish(spec, box.run_id(out), _results("j-1", "a"))
    assert code == 0 and box.ran.count("d-1") == 1
    assert box.status("d-1") == ("ran", "")


def test_a_chained_spec_runs_it_last_and_returns_its_digest(box):
    digest_file = box.root / "child-digest.json"
    digest_file.write_text(json.dumps({"ceremony": "other"}), encoding="utf-8")
    box.outputs["chain:whats-next"] = (0, f'digest: {digest_file.as_posix()}\nWorkflow({{scriptPath: "child.mjs"}})\n', "")
    spec = box.spec(chain=("whats-next", "launch"))
    code, out, _ = box.launch(spec)
    assert code == 0 and out.count("Workflow(") == 1 and 'scriptPath: "child.mjs"' in out
    ids = [r["id"] for r in box.rows()]
    assert ids.index("chain:whats-next") > ids.index("tail-1")
    assert ids[-1] == "chain:whats-next"
    stored = json.loads((box.runs()[-1] / driver.DIGEST_FILE).read_text(encoding="utf-8"))
    assert stored["chained"] == {"ceremony": "other"}


def test_a_flag_outside_launch_flags_exits_2_by_name_and_takes_no_lock(box):
    code, _, err = box.launch(flags={"--nope": "1"})
    assert code == 2 and "--nope" in err
    assert not box.lock().exists() and not box.ran


def test_a_declared_flag_reaches_the_spec(box):
    seen = []
    spec = box.spec(spec_points=lambda envelope, flags, rows: seen.append(dict(flags)) or [])
    code, _, _ = box.launch(spec, flags={"--x": "1"})
    assert code == 0 and seen == [{"--x": "1"}]


def test_missing_template_exits_4_and_releases_the_lock(box):
    box.envelope([_point("j-1", ["a", "b"])])
    code, out, err = box.launch()
    assert code == 4 and "workflows/fixture.mjs" in err and "### Failed" in out
    assert box.status("agent:judgment")[0] == "failed"
    assert not box.lock().exists()


def test_finish_twice_runs_the_tail_once(box):
    spec = box.spec()
    box.with_template().envelope([_point("j-1", ["a", "b"])])
    _, out, _ = box.launch(spec)
    run_id = box.run_id(out)
    _, first, _ = box.finish(spec, run_id, _results("j-1", "a"))
    _, second, _ = box.finish(spec, run_id)
    assert first == second and box.ran.count("tail-1") == 1 and len(box.tail_calls) == 1


def test_a_failing_gate_step_halts_the_rest_and_exits_5(box):
    spec = box.spec(front_door=[_step("prep-1", "prep", gates_next=True), _step("orient", "orient")])
    box.outputs["prep-1"] = (1, "", "boom")
    code, out, _ = box.launch(spec)
    assert code == 5 and "### Failed" in out
    assert box.status("orient") == ("halted-by-gate", "prep-1")
    assert "tail-1" not in box.ran


def _two_failing_tails(box):
    spec = box.spec(finish_steps=lambda decisions, rows, *, run: [_step("tail-1", "tail"), _step("tail-2", "tail")])
    box.outputs["tail-1"] = (1, "", "boom")
    box.outputs["tail-2"] = (1, "", "bang")
    box.outputs["backlog:tail-1"] = (0, "state/bug-backlog/2026-10-12-a.yaml\n", "")
    box.outputs["backlog:tail-2"] = (0, str(box.root / "state/bug-backlog/2026-10-12-b.yaml") + "\n", "")
    return spec


@pytest.fixture
def commits(monkeypatch):
    from coordinator_core.ops.ceremony import git_native

    calls = []

    class _Ok:
        ok, stdout, stderr = True, "sha", ""

    def fake(paths, msg_file, cwd, **kw):
        calls.append((list(paths), Path(msg_file).read_text(encoding="utf-8")))
        return _Ok()

    monkeypatch.setattr(git_native, "commit_scoped", fake)
    return calls


def test_filed_failure_rows_land_in_one_scoped_commit(box, commits):
    code, _, _ = box.launch(_two_failing_tails(box))
    assert code == 0
    (paths, msg), = commits
    assert paths == ["state/bug-backlog/2026-10-12-a.yaml", "state/bug-backlog/2026-10-12-b.yaml"]
    assert msg.startswith("fixture: file failure rows (run ")


def test_a_run_with_no_failures_makes_no_commit(box, commits):
    box.launch()
    assert commits == []


def test_a_cited_open_row_commits_nothing(box, commits):
    directory = box.root / "state" / "bug-backlog"
    directory.mkdir(parents=True)
    spec = box.spec(finish_steps=lambda decisions, rows, *, run: [_step("tail-1", "tail")])
    box.outputs["tail-1"] = (1, "", "boom")
    box.launch(spec)
    reason = box.status("tail-1")[1]
    (directory / "old.yaml").write_text(f"title: fixture step tail-1 failed: {reason}\nstatus: open\n", encoding="utf-8")
    commits.clear()
    box.launch(spec)
    assert box.status("backlog:tail-1") == ("ran", "open-row-cited")
    assert commits == []


def test_a_failed_commit_becomes_a_ledger_row_and_the_finish_completes(box, monkeypatch):
    from coordinator_core.ops.ceremony import git_native

    def boom(*_a, **_k):
        raise RuntimeError("no git")

    monkeypatch.setattr(git_native, "commit_scoped", boom)
    code, out, _ = box.launch(_two_failing_tails(box))
    assert code == 0 and out.startswith("# fixture receipt")
    assert box.status("backlog-commit")[0] == "failed"
    assert (box.runs()[-1] / driver.DONE_FILE).exists()


def test_the_driver_names_no_ceremony():
    text = Path(driver.__file__).read_text(encoding="utf-8").lower()
    assert not [name for name in ("workday", "workweek") if name in text]
