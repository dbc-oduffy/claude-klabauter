"""The wave-boundary seam leg of an emitted run: when it is emitted, which rows it holds,
what DRIFT does to them, and how its sidecars reach the terminal commit."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.emit import SeamInputs, compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

P1, P2, P3 = "docs/plans/p1.md", "docs/plans/p2.md", "docs/plans/p3.md"
ABSENT_EDGES = SeamInputs({}, False)


def _row(id_, writes, plan=None, reads=()):
    body = f"Spec: {plan} ({id_})\nSummary.\n" if plan else ""
    return WaveRow(
        id=id_, title=f"t-{id_}", surface="s", writes=writes, reads=list(reads), depends_on=[], body=body
    )


def _compose(waves, seam=ABSENT_EDGES, plan_path="state/mise-inventory/x.spine.md"):
    return compose_script(
        waves,
        name="n",
        description="d",
        plan_path=plan_path,
        expected_branch="work/x",
        run_base_sha="a" * 40,
        seam=seam,
        **REVIEW_KW,
    )


def _two_plan_waves():
    return [
        [_row("P1-C1", ["a.py"], P1), _row("P3-C1", ["z.py"], P3)],
        [_row("P2-C1", ["b.py"], P2, reads=["a.py"]), _row("P3-C2", ["y.py"], P3)],
    ]


def _registration(script: str, row_id: str) -> str:
    return next(line for line in script.splitlines() if line.startswith(f"  _rows['{row_id}'] = _runRow("))


def test_two_plan_run_emits_the_leg_with_the_pinned_params():
    script = _compose(_two_plan_waves())
    assert "plan.seam_record" in script
    assert "phase: 'wave-boundary'" in script
    assert "named_set: true" in script
    assert f'const _seamPlans = ["{P1}", "{P2}", "{P3}"];' in script
    assert "landed_rows: _seamCommitted" in script
    assert "'" + "a" * 40 + "..'" in script
    assert "agentType: 'coordinator:executor'" in script.split("async function _seamLeg", 1)[1]


def test_only_the_row_with_something_to_falsify_waits_on_the_leg():
    script = _compose(_two_plan_waves())
    assert "_seamGates[1]" in _registration(script, "P2-C1")
    for unrelated in ("P1-C1", "P3-C1", "P3-C2"):
        assert "_seamGates" not in _registration(script, unrelated)


def test_a_row_of_a_plan_naming_a_depends_on_plan_edge_waits_on_the_named_plans_wave():
    seam = SeamInputs({P2: (P1,)}, False)
    waves = [
        [_row("P1-C1", ["a.py"], P1)],
        [_row("P2-C1", ["b.py"], P2, reads=["a.py"]), _row("P2-C0", ["c.py"], P2)],
        [_row("P3-C1", ["y.py"], P3)],
    ]
    script = _compose(waves, seam)
    assert "_seamGates[1]" in _registration(script, "P2-C0")
    assert "_seamGates" not in _registration(script, "P3-C1")


def test_a_held_row_never_awaits_its_own_waves_leg():
    waves = [[_row("P1-C1", ["a.py"], P1), _row("P2-C1", ["b.py"], P2, reads=["a.py"])]]
    assert "_seamGates[1]" not in _registration(_compose(waves), "P2-C1")


def test_the_gate_releases_after_the_wave_commit_and_leg_settle():
    script = _compose(_two_plan_waves())
    assert ".then(() => _commitWave(n, ids)).finally(() => _seamRelease[n](" in script
    commit_wave = script.split("async function _commitWave", 1)[1].split("const _seamDrift", 1)[0]
    assert commit_wave.rstrip().endswith("await _seamLeg(n, r.sha);\n  }")


def test_the_digest_names_class_both_plans_and_path():
    script = _compose(_two_plan_waves())
    digest = script.split("decision_required:", 1)[1].splitlines()[0]
    assert "'drifted-contract: ' + d.plans.join(' and ')" in digest
    assert "'seam check failed at wave '" in digest
    assert "d.path" in digest


@pytest.mark.parametrize(
    "waves,seam,plan_path",
    [
        ([[_row("C1", ["a.py"])], [_row("C2", ["b.py"])]], SeamInputs({}, False), "docs/plans/one.md"),
        ([[_row("C1", ["a.py"])], [_row("C2", ["b.py"])]], None, "docs/plans/one.md"),
    ],
)
def test_a_single_plan_edge_free_run_emits_no_leg_and_matches_a_run_without_seam_inputs(waves, seam, plan_path):
    script = _compose(waves, seam, plan_path)
    assert "_seam" not in script
    assert "plan.seam_record" not in script
    assert script == _compose(waves, None, plan_path)


def test_a_single_plan_run_declaring_capabilities_or_an_edge_emits_the_leg_but_holds_nothing():
    waves = [[_row("C1", ["a.py"])], [_row("C2", ["b.py"], reads=["a.py"])]]
    for seam in (SeamInputs({}, True), SeamInputs({"docs/plans/one.md": (P1,)}, False)):
        script = _compose(waves, seam, "docs/plans/one.md")
        assert "plan.seam_record" in script
        assert "_seamGates[1]" not in _registration(script, "C2")


def test_a_wave_with_no_committable_row_gets_no_gate_and_no_leg_call():
    waves = [
        [_row("P1-C1", [], P1), _row("P3-C1", [], P3)],
        [_row("P2-C1", ["b.py"], P2), _row("P3-C2", ["y.py"], P3)],
    ]
    script = _compose(waves)
    assert "_seamGates[1] =" not in script
    assert "_seamGates[2] =" in script


def test_an_all_empty_write_run_emits_no_leg():
    script = _compose([[_row("P1-C1", [], P1), _row("P2-C1", [], P2)]])
    assert "_seam" not in script


def test_the_review_only_compose_emits_no_leg():
    waves = [[_row("P1-C1", ["a.py"], P1), _row("P2-C1", ["b.py"], P2)]]
    script = compose_script(
        waves,
        name="n",
        description="d",
        plan_path="state/mise-inventory/x.spine.md",
        run_base_sha="a" * 40,
        review_only=True,
        seam=ABSENT_EDGES,
        **REVIEW_KW,
    )
    assert "_seam" not in script


_NODE = lambda f: pytest.mark.cadence(pytest.mark.spawns_process(f))


_HARNESS = textwrap.dedent(
    """
    import fs from 'node:fs';
    const src = fs.readFileSync(process.argv[2], 'utf8')
      .replace(/export const meta = \\{[\\s\\S]*?\\n\\};\\n/, '');
    const reply = JSON.parse(process.env.SEAM_REPLY);
    const calls = [];
    const seamParams = [];
    const agent = async (prompt, opts = {}) => {
      const label = opts.label || '';
      calls.push(label);
      if (label.startsWith('commit:')) return { outcome: 'committed', sha: 'c0ffee1' };
      if (label.startsWith('seam:')) { seamParams.push(prompt); return reply; }
      if (label.startsWith('checkpoint-push')) return { pushed: true };
      return label.startsWith('work:') ? 'DONE: ok' : null;
    };
    const body = src + '\\nreturn { notStarted: _notStarted, drift: _seamDrift, haltedPlans: [..._haltedPlans], failures: _seamFailures };';
    const out = await new Function('agent', 'phase', 'log', 'parallel', 'return (async () => {' + body + '})();')(
      agent, () => {}, () => {}, async (xs) => Promise.all(xs.map((f) => f())));
    console.log(JSON.stringify({ calls, seamParams, ...out }));
    """
)

DRIFT_REPLY = {
    "verdict": "DRIFT",
    "per_plan": {P1: "DRIFT", P2: "DRIFT", P3: "CLEAN"},
    "findings": [
        {"class": "drifted-contract", "blocking": True, "plan": P2, "counterpart_plan": P1, "path": "a.py"}
    ],
}
CLEAN_REPLY = {"verdict": "CLEAN", "per_plan": {}, "findings": []}


def _run_script(tmp_path, script: str, reply: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    head = script.split("  await Promise.all(_checkpointPushes);", 1)[0]
    (tmp_path / "script.mjs").write_text(head, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    done = subprocess.run(
        [node, str(tmp_path / "harness.mjs"), str(tmp_path / "script.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        env={"SEAM_REPLY": json.dumps(reply), "PATH": ""},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@_NODE
def test_drift_keeps_the_consuming_row_from_dispatching_and_lets_unimplicated_rows_run(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), DRIFT_REPLY)
    assert "work:P2-C1" not in out["calls"]
    assert "work:P3-C2" in out["calls"]
    assert out["notStarted"] == ["P2-C1"]
    assert sorted(out["haltedPlans"]) == [P1, P2]
    assert out["drift"][0] == {"wave": 1, "plans": [P2, P1], "path": "a.py"}
    params = json.loads(out["seamParams"][0].split("Params, verbatim: `", 1)[1].split("`.", 1)[0])
    assert params["phase"] == "wave-boundary" and params["wave"] == 1
    assert params["landed_range"] == "a" * 40 + "..c0ffee1"
    assert {r["row"] for r in params["landed_rows"]} == {"P1-C1", "P3-C1"}
    assert {r["plan"] for r in params["landed_rows"]} == {P1, P3}


@_NODE
def test_a_clean_leg_dispatches_the_held_row(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), CLEAN_REPLY)
    assert "work:P2-C1" in out["calls"]
    assert out["drift"] == [] and out["notStarted"] == []


@_NODE
@pytest.mark.parametrize("reply", [{"verdict": "REFUSED"}, {"verdict": "CLEAN"}, None])
def test_a_leg_with_no_usable_reply_halts_the_plans_held_behind_it(tmp_path, reply):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), reply)
    assert "work:P2-C1" not in out["calls"]
    assert "work:P3-C2" in out["calls"]
    assert out["notStarted"] == ["P2-C1"]
    assert out["haltedPlans"] == [P2]
    assert out["failures"]
    assert out["drift"][0]["plans"] == [P2] and out["drift"][0]["failed"]


@_NODE
def test_a_finding_plan_is_the_owner_and_per_plan_is_the_fallback(tmp_path):
    reply = {
        "verdict": "DRIFT",
        "per_plan": {P2: "DRIFT"},
        "findings": [{"class": "drifted-contract", "blocking": True, "plan": P2, "counterpart_plan": P1, "path": "a.py"}],
    }
    out = _run_script(tmp_path, _compose(_two_plan_waves()), reply)
    assert out["drift"][0] == {"wave": 1, "plans": [P2, P1], "path": "a.py"}
    assert out["haltedPlans"] == [P2]
    bare = {"verdict": "DRIFT", "per_plan": {P2: "DRIFT"}, "findings": [{"class": "drifted-contract", "blocking": True, "path": "a.py"}]}
    assert _run_script(tmp_path, _compose(_two_plan_waves()), bare)["drift"][0]["plans"] == [P2]


@_NODE
def test_an_edge_free_row_dispatches_before_any_leg_settles(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), DRIFT_REPLY)
    assert "work:P3-C2" in out["calls"]
    assert "_seamGates" not in _registration(_compose(_two_plan_waves()), "P3-C2")


def _seed_plan(root, rel: str, with_sidecar: bool):
    plan = root / rel
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\ntitle: p\n---\n", encoding="utf-8")
    if with_sidecar:
        plan.with_name(plan.stem + ".seam.yaml").write_text("schema: seam-check\n", encoding="utf-8")


def test_seam_sidecars_join_the_terminal_pathspec(tmp_path):
    _seed_plan(tmp_path, "docs/plans/p1.md", True)
    _seed_plan(tmp_path, "docs/plans/p2.md", False)
    found = terminal_commit._seam_sidecars(tmp_path, "docs/plans/p1.md", ["C1"])
    assert found == ["docs/plans/p1.seam.yaml"]
    assert terminal_commit._seam_sidecars(tmp_path, "docs/plans/p2.md", ["C1"]) == []
    assert terminal_commit._seam_sidecars(tmp_path, None, []) == []
