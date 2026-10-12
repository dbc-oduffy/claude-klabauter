"""ceremony-run end to end: a fixture spec registered under tmp_path, fake dispatch, fixture template."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.ceremony_common.run import cli, decide, ledger
from coordinator_core.ceremony_common.run import spec as spec_mod
from coordinator_core.ceremony_common.run.contract import StepSpec
from coordinator_core.ceremony_common.run.runner import ChildOutcome
from coordinator_core.ceremony_common.run.spec import CeremonySpec

DAY = "2026-10-12"
TEMPLATE = (
    "export const meta = {\n  name: 'fixture',\n}\n"
    "export default async function run() { return args.digest.run_id }\n"
)
RUN_SOURCES = Path(cli.__file__).parent


def _step(step_id: str, stage: str) -> StepSpec:
    return StepSpec(
        id=step_id, stage=stage, kind="inproc", target="m:f", args=(), depends_on=None,
        already_satisfied=False, detail="", heading="Observations", cost_class="engine",
    )


def _point(pid):
    return build_judgment_point(
        None, id=pid, question=f"question {pid}",
        dispositions=[build_disposition(v, ()) for v in ("a", "b")],
        evidence="ev", reason="insufficient-evidence",
    )


class Box:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/work/test\n", encoding="utf-8")
        self.plugin = tmp_path / "plugin"
        (self.plugin / "workflows").mkdir(parents=True)
        (self.plugin / "workflows" / "fixture.mjs").write_text(TEMPLATE, encoding="utf-8")
        self.capsys = capsys
        self.points: list = []
        self.spec = CeremonySpec(
            name="fixture", dispatch_key="fixture", run_dir_name="fixture",
            template="workflows/fixture.mjs", marker_path=None,
            launch_stages=("prep", "orient", "decide"), finish_stages=("decisions", "briefing", "tail"),
            front_door=[_step("orient", "orient")], envelope_step="orient",
            stage_of=lambda _id: "decisions", residue=(),
            rules={decide.RECOMMENDATION: decide.recommendation},
            spec_points=lambda envelope, flags, rows: [],
            agent_groups=lambda envelope, rows, resolution: [],
            finish_steps=lambda decisions, rows, *, run: [_step("tail-1", "tail")],
            receipt_sections=(), render=None, costs=lambda key: "engine", chain=None,
            launch_flags=frozenset({"--x"}),
        )
        monkeypatch.setattr(spec_mod, "CEREMONY_SPECS", {"fixture_cer": "unused:SPEC"})
        monkeypatch.setattr(spec_mod, "load_spec", lambda key: self.spec)

    def table(self):
        def run(step, *, repo_root, run_dir, deadline_s, **_kw):
            out = ""
            if step.id == "orient":
                out = json.dumps({"judgment_points": self.points, "directives": []})
            return ChildOutcome(exit_code=0, process_ms=1.0, spawns=1, stdout=out, stderr="")

        return {
            "cli": run, "op": run, "inproc": run,
            "rule-child": lambda argv, *, repo_root, deadline_s: ChildOutcome(0, 1.0, 1),
        }

    def go(self, argv):
        code = cli.main(
            argv, dispatch=self.table(), plugin_root=self.plugin, repo_root=self.root, today=DAY,
        )
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def run_id(self, out):
        script = re.search(r'scriptPath: "([^"]+)"', out).group(1)
        return Path(script).parent.name


@pytest.fixture
def box(tmp_path, monkeypatch, capsys):
    return Box(tmp_path, monkeypatch, capsys)


def test_launch_without_agent_work_prints_a_receipt_and_no_workflow(box):
    code, out, _ = box.go(["fixture-cer", "launch"])
    assert code == 0 and "Workflow(" not in out
    assert out.startswith("# fixture receipt")


def test_launch_with_agent_work_prints_exactly_one_workflow_line(box):
    box.points = [_point("j-1")]
    code, out, _ = box.go(["fixture-cer", "launch"])
    assert code == 0 and out.count("Workflow(") == 1


def test_a_flag_outside_the_spec_is_refused_by_the_driver(box):
    code, _, err = box.go(["fixture-cer", "launch", "--nope", "1"])
    assert code == 2 and "--nope" in err


def test_a_declared_flag_passes_through(box):
    code, out, _ = box.go(["fixture-cer", "launch", "--x", "1"])
    assert code == 0 and "Workflow(" not in out


def test_resume_is_an_unknown_verb(box):
    assert box.go(["fixture-cer", "resume"])[0] == 2


def test_an_unknown_ceremony_exits_2_with_one_line(box):
    code, out, err = box.go(["nonesuch", "launch"])
    assert code == 2 and out == "" and len(err.strip().splitlines()) == 1


def test_finish_needs_a_valid_run_id(box):
    assert box.go(["fixture-cer", "finish"])[0] == 2
    assert box.go(["fixture-cer", "finish", "--run-id", "../../escape"])[0] == 2


def test_finish_twice_renders_once(box):
    box.points = [_point("j-1")]
    _, out, _ = box.go(["fixture-cer", "launch"])
    run_id = box.run_id(out)
    results = ledger.run_dir(box.root, "fixture", run_id) / "r.json"
    results.write_text(json.dumps({"groups": {"judgment": [{
        "key": "j-1", "outcome": "decided", "decision": {"disposition": "a", "rationale": "because"},
    }]}}), encoding="utf-8")
    first = box.go(["fixture-cer", "finish", "--run-id", run_id, "--agent-results", str(results)])
    second = box.go(["fixture-cer", "finish", "--run-id", run_id])
    assert first[0] == 0 and second[0] == 0
    assert first[1].startswith("# fixture receipt") and second[1] == first[1]
    rows = [r["id"] for r in ledger.read(ledger.run_dir(box.root, "fixture", run_id))]
    assert rows.count("tail-1") == 1


def test_help_lists_the_registered_ceremonies(box, capsys):
    code, out, _ = box.go(["--help"])
    assert code == 0 and "fixture-cer" in out


def test_only_spec_py_names_a_ceremony():
    named = []
    for path in sorted(RUN_SOURCES.glob("*.py")):
        if path.name == "spec.py":
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "workday" in text or "workweek" in text:
            named.append(path.name)
    assert not named
