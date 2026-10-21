"""The wave-boundary seam leg of an emitted run: when it is emitted, which rows it holds,
what DRIFT does to them, and how its sidecars reach the terminal commit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts
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


def test_certified_plans_ride_into_the_leg_params_only_when_given():
    assert "certified_plans" not in _compose(_two_plan_waves())
    script = _compose(_two_plan_waves(), seam=SeamInputs({}, False, (P1, "docs/plans/p9.md")))
    assert 'const _seamCertified = ["docs/plans/p1.md", "docs/plans/p9.md"];' in script
    assert "certified_plans: _seamCertified," in script


def _registration(script: str, row_id: str) -> str:
    return next(line for line in script.splitlines() if line.startswith(f"  _rows['{row_id}'] = _runRow("))


def test_two_plan_run_emits_the_leg_with_the_pinned_params():
    script = _compose(_two_plan_waves())
    assert "plan.seam_record" in script
    assert "phase: 'wave-boundary'" in script
    assert "named_set: true" in script
    assert f'const _seamPlans = ["{P1}", "{P2}", "{P3}"];' in script
    assert "landed_rows: _seamCommitted" in script
    assert "landed_range: _runBase + '..' + sha" in script
    assert "agentType: 'coordinator:executor'" in script.split("async function _seamLeg", 1)[1]
    assert "'seam drift at wave ' + n + (finding[p] ? ': ' + finding[p] : '')" in script


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
    assert "d.class + ': ' + d.plans.join(' and ')" in digest
    assert "'seam check failed at wave '" in digest
    assert "d.path" in digest


ONE = "docs/plans/one.md"


def _single_plan_waves(reads=("a.py",)):
    return [[_row("C1", ["a.py"])], [_row("C2", ["b.py"], reads=list(reads))]]


@pytest.mark.parametrize("seam", [SeamInputs({}, False), None])
def test_a_single_plan_one_wave_run_emits_no_leg_and_matches_a_run_without_seam_inputs(seam):
    waves = [[_row("C1", ["a.py"]), _row("C2", ["b.py"])]]
    script = _compose(waves, seam, ONE)
    assert "_seam" not in script
    assert "plan.seam_record" not in script
    assert script == _compose(waves, None, ONE)


def test_a_single_plan_two_wave_run_emits_the_leg_and_holds_its_read_after_write_row():
    script = _compose(_single_plan_waves(), SeamInputs({}, False), ONE)
    assert "plan.seam_record" in script
    assert "'seam:wave-' + n" in script
    assert "_seamGates[1]" in _registration(script, "C2")
    assert "_seamGates" not in _registration(script, "C1")


def test_a_single_plan_two_wave_run_whose_rows_read_nothing_composes_the_leg_and_holds_nothing():
    script = _compose(_single_plan_waves(reads=()), SeamInputs({}, False), ONE)
    assert "plan.seam_record" in script
    assert "_seamGates" not in _registration(script, "C2")


def test_a_single_plan_run_declaring_capabilities_or_an_edge_holds_its_read_after_write_row():
    for seam in (SeamInputs({}, True), SeamInputs({ONE: (P1,)}, False)):
        script = _compose(_single_plan_waves(), seam, ONE)
        assert "plan.seam_record" in script
        assert "_seamGates[1]" in _registration(script, "C2")


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
        review_terminal_sha="b" * 40,
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
    const commitReply = JSON.parse(process.env.COMMIT_REPLY);
    const calls = [];
    const seamParams = [];
    const agent = async (prompt, opts = {}) => {
      const label = opts.label || '';
      calls.push(label);
      if (label.startsWith('commit:')) return commitReply;
      if (label.startsWith('seam:')) { seamParams.push(prompt); return reply; }
      return label.startsWith('work:') ? 'DONE: ok' : null;
    };
    const body = src + '\\nreturn { notStarted: _notStarted, drift: _seamDrift, haltedPlans: [..._haltedPlans], failures: _seamFailures, planHeld: _planHeld, commitFailures: _commitFailures, commitRefused: _commitRefused };';
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


COMMITTED = {"outcome": "committed", "sha": "c0ffee1"}


def _run_script(tmp_path, script: str, reply: dict, commit: dict = COMMITTED) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    anchor = "  await _commitChain;\n"
    assert script.count(anchor) == 1, "wave-commit settle line moved: re-anchor the harness split"
    head = script.split(anchor, 1)[0] + anchor
    # LF pinned: the harness strips `meta` by a `\n};\n` regex that CRLF defeats on Windows.
    (tmp_path / "script.mjs").write_text(head, encoding="utf-8", newline="\n")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [node, str(tmp_path / "harness.mjs"), str(tmp_path / "script.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        env={"SEAM_REPLY": json.dumps(reply), "COMMIT_REPLY": json.dumps(commit), "PATH": "",
             # Windows node aborts CSPRNG init without SystemRoot.
             **{k: os.environ[k] for k in ("SystemRoot",) if k in os.environ}},
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
    assert out["drift"][0] == {"wave": 1, "class": "drifted-contract", "plans": [P2, P1], "path": "a.py"}
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
    assert out["drift"][0] == {"wave": 1, "class": "drifted-contract", "plans": [P2, P1], "path": "a.py"}
    assert out["haltedPlans"] == [P2]
    bare = {"verdict": "DRIFT", "per_plan": {P2: "DRIFT"}, "findings": [{"class": "drifted-contract", "blocking": True, "path": "a.py"}]}
    assert _run_script(tmp_path, _compose(_two_plan_waves()), bare)["drift"][0]["plans"] == [P2]


@_NODE
def test_an_edge_free_row_dispatches_before_any_leg_settles(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), DRIFT_REPLY)
    assert "work:P3-C2" in out["calls"]
    assert "_seamGates" not in _registration(_compose(_two_plan_waves()), "P3-C2")


@_NODE
def test_a_commit_reporting_committed_without_a_sha_records_nothing_and_releases_the_gate(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), DRIFT_REPLY, {"outcome": "committed"})
    assert not any(c.startswith("seam:") for c in out["calls"])
    assert "work:P2-C1" in out["calls"]
    assert out["drift"] == [] and out["failures"] == [] and out["haltedPlans"] == []


@_NODE
def test_a_failed_wave_commit_holds_only_its_dependents_and_records_no_seam_failure(tmp_path):
    refusal = "attribution_gate: path not attributed"
    out = _run_script(
        tmp_path, _compose(_two_plan_waves()), CLEAN_REPLY, {"outcome": "commit-failed", "reason": refusal}
    )
    assert not any(c.startswith("seam:") for c in out["calls"])
    assert "work:P2-C1" not in out["calls"]
    assert "work:P3-C2" in out["calls"]
    assert out["failures"] == [] and out["drift"] == [] and out["haltedPlans"] == []
    assert out["notStarted"] == ["P2-C1"]
    assert out["planHeld"]["P2-C1"] == f"commit-failed: wave 1: {refusal}"
    assert out["commitFailures"][0] == f"wave 1: {refusal}"
    assert out["commitRefused"]["wave 1 commit"] == f"wave 1: {refusal}"


@_NODE
def test_a_failed_wave_commit_with_no_reply_names_it(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), CLEAN_REPLY, None)
    assert out["planHeld"]["P2-C1"] == "commit-failed: wave 1: no committer reply"
    assert out["failures"] == [] and out["drift"] == []


@_NODE
def test_an_op_error_relay_fails_closed_and_an_empty_drift_does_too(tmp_path):
    for reply in (
        {"verdict": "REFUSED", "per_plan": {}, "findings": [{"class": "op-error"}]},
        {"verdict": "DRIFT", "per_plan": {}, "findings": []},
    ):
        out = _run_script(tmp_path, _compose(_two_plan_waves()), reply)
        assert "work:P2-C1" not in out["calls"]
        assert out["haltedPlans"] == [P2] and out["failures"]


def test_the_relay_prompt_says_refused_for_an_op_error():
    assert "REFUSED for any op refusal or error" in _compose(_two_plan_waves())


def test_a_run_without_a_base_sha_does_not_qualify_for_the_leg():
    script = compose_script(
        _two_plan_waves(),
        name="n",
        description="d",
        plan_path="state/mise-inventory/x.spine.md",
        expected_branch="work/x",
        seam=ABSENT_EDGES,
        **REVIEW_KW,
    )
    assert "_seam" not in script


@_NODE
def test_the_digest_renders_the_class_and_collapses_repeats():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _compose(_two_plan_waves())
    grouped = "(_seamDrift.length ? (() =>"
    expr = grouped + script.split(grouped, 1)[1].split("})() : ", 1)[0] + "})() : '')"
    drift = [
        {"wave": 1, "class": "unpromised-export", "plans": [P2, P1], "path": "a.py"},
        {"wave": 1, "class": "unpromised-export", "plans": [P2, P1], "path": "b.py"},
        {"wave": 1, "class": "path-collision", "plans": [P2, P1], "path": "c.py"},
    ]
    done = subprocess.run(
        [node, "-e", f"const _seamDrift = {json.dumps(drift)}; console.log({expr});"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == (
        f"unpromised-export: {P2} and {P1} at a.py x2 (wave 1); path-collision: {P2} and {P1} at c.py (wave 1)"
    )


@_NODE
def test_the_digest_names_a_refused_wave_commit_as_a_commit_failure_never_a_seam_failure():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _compose(_two_plan_waves())
    digest = script.split("decision_required:", 1)[1].splitlines()[0]
    expr = "(Object.keys(_commitRefused).length ? " + digest.split("(Object.keys(_commitRefused).length ? ", 1)[1].split("(_seamDrift.length ? (() =>", 1)[0]
    probe = (
        "const _commitRefused = {'wave 1 commit': 'wave 1: attribution_gate refused'}; const _seamDrift = [];"
        f"const r = {expr} null);console.log(r);"
    )
    done = subprocess.run(
        [node, "-e", probe],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    assert "wave commit failed" in done.stdout and "attribution_gate refused" in done.stdout
    assert "seam check failed" not in done.stdout


def _seed_plan(root, rel: str, with_sidecar: bool):
    plan = root / rel
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\ntitle: p\n---\n", encoding="utf-8")
    if with_sidecar:
        plan.with_name(plan.stem + ".seam.yaml").write_text("schema: seam-check\n", encoding="utf-8")


def _seams(root, plan) -> list:
    """The run's seam sidecars as ``land_run`` enumerates them for its landing pathspec."""
    return list(run_artifacts.candidate_paths(root, "ask-20261016T120000-aaaaaa", plan_rel=plan).by_class.get("seam-sidecar", ()))


def test_seam_sidecars_join_the_terminal_pathspec(tmp_path):
    _seed_plan(tmp_path, "docs/plans/p1.md", True)
    _seed_plan(tmp_path, "docs/plans/p2.md", False)
    found = _seams(tmp_path, "docs/plans/p1.md")
    assert found == ["docs/plans/p1.seam.yaml"]
    assert _seams(tmp_path, "docs/plans/p2.md") == []
    assert _seams(tmp_path, None) == []


def test_terminal_pathspec_carries_prep_and_wave_sidecars_only_of_its_own_plan(tmp_path):
    _seed_plan(tmp_path, "docs/plans/p1.md", True)
    _seed_plan(tmp_path, "docs/plans/p10.md", False)
    d = tmp_path / "docs/plans"
    for name in ("p1.wave-2.seam.yaml", "p1.wave-1.seam.yaml", "p10.wave-1.seam.yaml"):
        (d / name).write_text("schema: seam-check\n", encoding="utf-8")
    assert _seams(tmp_path, "docs/plans/p1.md") == [
        "docs/plans/p1.seam.yaml",
        "docs/plans/p1.wave-1.seam.yaml",
        "docs/plans/p1.wave-2.seam.yaml",
    ]
    _seed_plan(tmp_path, "docs/plans/p3.md", False)
    (d / "p3.wave-1.seam.yaml").write_text("schema: seam-check\n", encoding="utf-8")
    assert _seams(tmp_path, "docs/plans/p3.md") == ["docs/plans/p3.wave-1.seam.yaml"]


def test_run_artifacts_enumerates_wave_sidecars_and_maps_them_to_the_plan_sizing(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_artifacts as ra

    run = "ask-20261016T120000-cccccc"
    plan, sizing = "docs/plans/2026-10-16-x.md", "state/sizings/2026-10-16-x.yaml"
    files = {
        f"state/warp/{run}/run.json": json.dumps({"sizing_rel": sizing}),
        sizing: f"plan: '{plan}'\n",
        plan: f"sizing_object: {sizing}\n",
        "docs/plans/2026-10-16-x.seam.yaml": "x\n",
        "docs/plans/2026-10-16-x.wave-1.seam.yaml": "x\n",
        "docs/plans/2026-10-16-x.wave-2.seam.yaml": "x\n",
        "docs/plans/2026-10-16-xy.wave-1.seam.yaml": "x\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    found = ra.candidate_paths(tmp_path, run).by_class["seam-sidecar"]
    assert found == (
        "docs/plans/2026-10-16-x.seam.yaml",
        "docs/plans/2026-10-16-x.wave-1.seam.yaml",
        "docs/plans/2026-10-16-x.wave-2.seam.yaml",
    )
    assert ra._sizing_of_artifact(tmp_path, "docs/plans/2026-10-16-x.wave-2.seam.yaml") == sizing
    assert ra._sizing_of_artifact(tmp_path, "docs/plans/2026-10-16-x.seam.yaml") == sizing


def test_archive_plans_treats_wave_sidecars_as_sidecars_of_their_plan(tmp_path):
    from pathlib import Path

    from coordinator_core.ops.fleet import archive_plans

    d = tmp_path / "docs/plans"
    d.mkdir(parents=True)
    for name in ("2026-10-16-x.md", "2026-10-16-x.seam.yaml", "2026-10-16-x.wave-1.seam.yaml", "notes.txt"):
        (d / name).write_text("x\n", encoding="utf-8")
    live = [p.name for p in archive_plans.collect_live_plan_paths(tmp_path)]
    assert live == ["2026-10-16-x.md", "2026-10-16-x.seam.yaml", "2026-10-16-x.wave-1.seam.yaml"]
    wave = Path(live[2])
    assert archive_plans._is_sidecar(wave) and not archive_plans._is_sidecar(Path(live[0]))
    assert archive_plans._primary_for_sidecar(d / live[2]).name == "2026-10-16-x.md"


@_NODE
def test_a_single_plan_drift_keeps_the_held_row_out_and_a_clean_leg_dispatches_it(tmp_path):
    script = _compose(_single_plan_waves(), SeamInputs({}, False), ONE)
    drift = {"verdict": "DRIFT", "per_plan": {ONE: "DRIFT"}, "findings": [{"class": "drifted-contract", "blocking": True, "plan": ONE, "path": "a.py"}]}
    out = _run_script(tmp_path, script, drift)
    assert "seam:wave-1" in out["calls"]
    assert "work:C2" not in out["calls"]
    assert out["notStarted"] == ["C2"]
    clean = _run_script(tmp_path, script, CLEAN_REPLY)
    assert "work:C2" in clean["calls"] and clean["notStarted"] == []
