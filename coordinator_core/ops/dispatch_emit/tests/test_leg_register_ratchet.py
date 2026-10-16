"""Ratchet: every LLM leg an emitted workflow composes is declared in leg_register.REGISTER, and a
leg whose whole job is one op or a verdict read is on the closed haiku floor."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

import yaml

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit import ask_compose, emit, grind_compose as gc, grind_profile as gp
from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_M_PLUS, ARM_ROADMAP, ARM_S, ARM_XS
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import parse_execute_review

Key = tuple[str, str]

_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_SESSION = "9d860631-c114-462a-a6ee-f685cc671ced"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROADMAP_TEXT = (
    "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\n"
    "return { gate_report_path: 'g.json' };"
)
_ARM_SIZING = {
    ARM_XS: ("XS", "dispatch"),
    ARM_S: ("S", "spec-dispatch"),
    ARM_M_PLUS: ("M", "plan"),
    ARM_ROADMAP: ("XL", "roadmap"),
}
#: Labels whose suffix names a row, a wave or a reviewer index: one declaration covers the family.
_FAMILY_PREFIXES = ("work:", "falsifier-integrity:", "seam:wave-", "verify:", "continue:")

#: Op-wrapper legs with no preceding agent to fold into, haiku-bound. Shrink-only.
_RUNTIME_FLOOR: frozenset[Key] = frozenset(
    {
        ("ask", "gate"),
        ("ask", "commit"),
        ("ask", "halt-commit"),
        ("ask", "commit:wave-"),
        ("ask", "stage"),
        ("emit", "run-base"),
        ("emit", "commit:wave-"),
        ("grind", "commit"),
        ("grind", "review-fix-commit"),
        ("grind", "verify-op"),
        ("grind", "ledger-sweep:drain"),
    }
)
_RUNTIME_FLOOR_MAX = 11

#: Judgment legs that sit on the mechanical haiku type; every other such leg is refused.
_JUDGMENT_ON_MECHANICAL_TYPE: frozenset[Key] = frozenset({("grind", "triage")})
_MECHANICAL_TYPE, _MECHANICAL_MODEL = "general-purpose", "haiku"


def _key(workflow: str, label: str) -> Key:
    for prefix in _FAMILY_PREFIXES:
        if label.startswith(prefix):
            return workflow, prefix
    return workflow, label


def _ask(**over) -> str:
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        roadmap_blitz_text=_ROADMAP_TEXT, **REVIEW_KW,
    )
    kw.update(over)
    return ask_compose.compose_ask_script(**kw)


def _put_sizing(repo: Path, arm: str) -> str:
    tshirt, route = _ARM_SIZING[arm]
    rel = f"state/sizings/{arm}.yaml"
    doc = {
        "schema": "sizing-object", "name": arm, "intent": "a job",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": route, "detents": [], "fork": None,
        "xl_exit": None, "status": "sized", "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-02", "mode": "pm"}},
    }
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return rel


def _ask_scripts(repo: Path) -> list[str]:
    scripts = [
        _ask(),
        _ask(in_run_commit=True),
        _ask(stop_after=STOP_AFTER_PLAN),
        _ask(in_run_commit=True, stop_after=STOP_AFTER_PLAN),
    ]
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW

    scripts += [_ask(**_JUDGE_KW), _ask(in_run_commit=True, **_JUDGE_KW)]
    for arm in _ARM_SIZING:
        rel = _put_sizing(repo, arm)
        scripts.append(_ask(repo_root=repo.as_posix(), prompt=None, sizing_rel=rel))
        scripts.append(_ask(repo_root=repo.as_posix(), prompt=None, sizing_rel=rel, in_run_commit=True))
        scripts.append(_ask(repo_root=repo.as_posix(), prompt=None, sizing_rel=rel, in_run_commit=True, **_JUDGE_KW))
    return scripts


def _wave_row(row_id: str, writes: list[str], depends_on: Optional[list[str]] = None) -> WaveRow:
    return WaveRow(
        id=row_id, title=f"title-{row_id}", surface="dispatch_emit", writes=writes, reads=[],
        depends_on=depends_on or [], agent_type=None, agent_model=None,
    )


_GOLDEN_SCRIPT = (
    Path(__file__).resolve().parents[3] / "benchmarks" / "fixtures" / "emit_full_path_plan.expected.script.mjs"
)


_REPO_ROOT = Path(__file__).resolve().parents[4]
_JUDGE_ARM_SURFACE = "coordinator_core/ops/dispatch_emit/leg_register.py"


def _judge_arm_script() -> str:
    """A plan-route compose whose roster declares a judge and whose row writes a testable `.py` surface."""
    from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "judge",
            "agents": [
                {
                    "agentType": "coordinator:exit-criterion-judge", "model": "sonnet", "effort": "low",
                    "schema": "judge-result",
                }
            ],
        }
    )
    return emit.compose_script(
        [[_wave_row("T1", [_JUDGE_ARM_SURFACE])]], name="wf", description="d",
        review_roster_fragment=fragment, review_stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path="docs/plans/p.md", run_base_sha="a" * 40, repo_root=_REPO_ROOT,
    )


def _emit_scripts(*, golden: bool = True) -> list[str]:
    """`golden=False` drops the checked-in script, whose model hunks follow its own regeneration, not the register."""
    from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row
    from coordinator_core.ops.dispatch_emit.tests.test_emit_predispatch_gating import _review_spec_for, _row

    waves = [[_wave_row("C1", ["a.py"])], [_wave_row("C2", ["b.py"], ["C1"])]]
    return [
        *([_GOLDEN_SCRIPT.read_text(encoding="utf-8")] if golden else []),
        _judge_arm_script(),
        emit.compose_script(waves, name="wf", description="d", **REVIEW_KW),
        emit.compose_script(
            [[_wave_row("A", ["a.py"])]], name="wf", description="d", run_base_sha="a" * 40,
            review_only=True, review_terminal_sha="b" * 40, **REVIEW_KW,
        ),
        emit.compose_script(
            [[_write_row("C1"), _write_row("C2")]], name="wf", description="d", session_id=_SESSION,
            expected_branch="work/run", plan_path="docs/plans/p.md", run_base_sha="a" * 40, **REVIEW_KW,
        ),
        emit.compose_script(
            [[_row("A", ["a.py"])], [_row("B", ["b.py"], deps=["A"])]], name="wf", description="d",
            plan_path="docs/plans/p.md", predispatch=True, review_specs=_review_spec_for(), **REVIEW_KW,
        ),
        emit.compose_script(waves, name="wf", description="d", chatty=True, **REVIEW_KW),
        emit.compose_script(
            [[_wave_row("C1", ["a.py"])], [_wave_row("C2", ["b.py"], ["C1"])]], name="wf", description="d",
            plan_path="docs/plans/p.md", run_base_sha="a" * 40, seam=emit.SeamInputs({}, False), **REVIEW_KW,
        ),
    ]


def _grind_scripts() -> list[str]:
    profile = gp.load_profile("fixture", _PROFILE_DIR)
    gp.validate_graph(profile)
    entries = tuple(
        ManifestEntry(row_id=f"row{i}", path=f"state/bug-backlog/row{i}.yaml", digest=f"{i:064x}", batch_key="P0")
        for i in range(7)
    )
    manifest = Manifest(entries=entries, batch_sizes={"P0": 4}, source=None, digest="deadbeef")
    return [
        gc.compose_grind_script(
            manifest, profile, gp.resolve_appetite(profile, "standard"),
            run_dir=Path("state/queue-grind/fixture/run-1"), appetite="standard", agent_type_host=None,
            queue_dirs=["state/bug-backlog"], stage_models=None, targeted_runner=targeted, **REVIEW_KW,
        )
        for targeted in (False, True)
    ]


def _review_block(fragment: dict, schemas: dict) -> str:
    phases = compose_execute_review(
        parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]}),
        stage_schemas=schemas, plan_path="docs/plans/example.md", run_base_sha="a" * 40,
        declared_paths=["x.py"],
    )
    return "\n".join(block for _, block in phases)


def _review_scripts() -> dict[str, list[str]]:
    from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

    fragment, schemas = REVIEW_KW["review_roster_fragment"], REVIEW_KW["review_stage_schemas"]
    return {
        "review": [
            _review_block(fragment, schemas),
            _review_block(_v5_fragment(), _STAGE_SCHEMAS),
        ],
    }


def _composed(repo: Path, *, golden: bool = True) -> dict[str, list[str]]:
    return {
        "ask": _ask_scripts(repo),
        "emit": _emit_scripts(golden=golden),
        "grind": _grind_scripts(),
        **_review_scripts(),
    }


def _emitted(repo: Path, *, golden: bool = True) -> dict[Key, list[lr.EmittedLeg]]:
    found: dict[Key, list[lr.EmittedLeg]] = {}
    for workflow, scripts in _composed(repo, golden=golden).items():
        for script in scripts:
            for leg in lr.emitted_legs(script):
                found.setdefault(_key(workflow, leg.label), []).append(leg)
    return found


def _undeclared(keys: Iterable[Key], register: Iterable[lr.LegDecl]) -> list[Key]:
    declared = {(d.workflow, d.label) for d in register}
    return sorted(k for k in keys if k not in declared)


def _earns_its_cost_refusal(key: Key, leg: lr.EmittedLeg) -> Optional[str]:
    if leg.agent_type == OP_RUNNER_AGENT_TYPE:
        return f"{key} runs on the op-runner, whose whole job is one op"
    mechanical = leg.agent_type == _MECHANICAL_TYPE and leg.model == _MECHANICAL_MODEL
    if mechanical and key not in _JUDGMENT_ON_MECHANICAL_TYPE:
        return f"{key} runs on the mechanical haiku type, which only an op-wrapper may use"
    return None


def test_every_emitted_leg_is_declared(tmp_path):
    missing = _undeclared(_emitted(tmp_path), lr.REGISTER)
    assert not missing, f"emitted leg(s) {missing} are not declared: add a LegDecl for each to leg_register.REGISTER"


def test_a_deleted_register_entry_names_its_leg(tmp_path):
    emitted = _emitted(tmp_path)
    for victim in (("ask", "gate"), ("grind", "verify-op"), ("emit", "work:")):
        register = [d for d in lr.REGISTER if (d.workflow, d.label) != victim]
        assert _undeclared(emitted, register) == [victim]


def test_the_composed_set_reaches_every_workflow_and_the_hidden_legs(tmp_path):
    emitted = _emitted(tmp_path)
    assert {wf for wf, _ in emitted} == {"ask", "emit", "grind", "review"}
    for key in (("ask", "size-reword"), ("ask", "gate"), ("ask", "commit"), ("emit", "run-base"), ("grind", "verify-op")):
        assert key in emitted, f"{key} is no longer composed: the ratchet fixtures lost it"


#: Plan-blitz and roadmap-blitz titles are DoE's, passed through as given.
_PASSED_THROUGH_TITLES = frozenset({"Size", "Plan", "Synthesize", "roadmap-blitz"})
_RETIRED_ASK_LEGS = ("test:terminal", "docs", "ledger-check", "review:coordinator:review-prep")
#: The push belongs to the DR-329 cadence, never to a leg.
_RETIRED_EMIT_LEGS = ("checkpoint-push:", "check:")


def test_no_emit_script_composes_a_retired_leg():
    for script in _emit_scripts():
        labels = {leg.label for leg in lr.emitted_legs(script)}
        assert not any(label.startswith(_RETIRED_EMIT_LEGS) for label in labels), labels
        assert "checkpoint-push" not in script
    for retired in _RETIRED_EMIT_LEGS:
        assert lr.lookup("emit", retired) is None, retired


def _meta_titles(script: str) -> list[str]:
    import re

    return re.findall(r"'([^']*)'", script.split("phases: [", 1)[1].split("]", 1)[0])


def _parallel_blocks(script: str) -> list[str]:
    scrubbed = lr.scrub(lr._mask_regex_literals(script))
    blocks = []
    for m in re.finditer(r"\bawait\s+parallel\s*\(", scrubbed):
        open_paren = m.end() - 1
        blocks.append(script[open_paren : lr._find_matching_paren(scrubbed, open_paren)])
    return blocks


def test_the_ask_tail_composes_one_verify_leg_and_none_of_the_legs_it_absorbed(tmp_path):
    for script in _ask_scripts(tmp_path):
        labels = [leg.label for leg in lr.emitted_legs(script)]
        assert not set(labels) & set(_RETIRED_ASK_LEGS), set(labels) & set(_RETIRED_ASK_LEGS)
        if "Verify" in _meta_titles(script):
            assert labels.count("verify") == 1
    retired = {("ask", label) for label in _RETIRED_ASK_LEGS} | {("review-mechanical-prep", "review:coordinator:review-prep")}
    assert not retired & {(d.workflow, d.label) for d in lr.REGISTER}
    assert lr.lookup("ask", "verify").cls is lr.LegClass.EARNS_ITS_COST


def test_phase_titles_show_concurrency_truthfully(tmp_path):
    for script in _ask_scripts(tmp_path):
        for block in _parallel_blocks(script):
            phases = {leg.phase for leg in lr.emitted_legs(block)}
            assert len(phases) <= 1, f"one parallel() spans phase titles {sorted(phases)}"
        carried = {leg.phase for leg in lr.emitted_legs(script)}
        listed = [t for t in _meta_titles(script) if t not in _PASSED_THROUGH_TITLES]
        assert len(set(listed)) == len(listed), f"a title is listed twice: {listed}"
        assert [t for t in listed if t not in carried] == [], f"titles no agent carries: {listed}"


def _refused_outside_floor(register: Iterable[lr.LegDecl]) -> list[Key]:
    return [
        (d.workflow, d.label)
        for d in register
        if d.cls in lr.REFUSED_CLASSES and (d.workflow, d.label) not in _RUNTIME_FLOOR
    ]


def test_every_refused_class_leg_is_on_the_floor():
    offenders = _refused_outside_floor(lr.REGISTER)
    assert not offenders, (
        f"{offenders} are op-wrapper or verdict-reader legs: fold them into the agent before, "
        "or put one with no preceding agent on _RUNTIME_FLOOR"
    )
    for key in _RUNTIME_FLOOR:
        decl = lr.lookup(*key)
        assert decl is not None and decl.cls in lr.REFUSED_CLASSES, f"{key} is on the floor but not a refused-class leg"


def test_an_op_wrapper_entry_outside_the_floor_is_refused():
    stray = lr.LegDecl("ask", "stray-op", lr.LegClass.OP_WRAPPER, "runs one op")
    reader = lr.LegDecl("ask", "stray-read", lr.LegClass.VERDICT_READER, "reads one verdict")
    assert _refused_outside_floor(lr.REGISTER) == []
    assert _refused_outside_floor([*lr.REGISTER, stray, reader]) == [("ask", "stray-op"), ("ask", "stray-read")]


def test_the_floor_only_shrinks():
    assert len(_RUNTIME_FLOOR) <= _RUNTIME_FLOOR_MAX


def test_every_floor_member_cites_the_ruling_and_emits_haiku(tmp_path):
    emitted = _emitted(tmp_path, golden=False)
    for key in sorted(_RUNTIME_FLOOR):
        assert lr.lookup(*key).runtime_floor == lr.RUNTIME_FLOOR_RULING, f"{key} does not cite the floor ruling"
        legs = emitted[key]
        assert {leg.model for leg in legs} == {"haiku"}, f"{key} is on the floor but does not emit haiku"


def test_only_floor_legs_carry_a_runtime_floor():
    for decl in lr.REGISTER:
        assert bool(decl.runtime_floor) == ((decl.workflow, decl.label) in _RUNTIME_FLOOR), decl


def test_earns_its_cost_is_refused_on_a_mechanical_leg(tmp_path):
    for key, legs in _emitted(tmp_path).items():
        if lr.lookup(*key).cls is lr.LegClass.EARNS_ITS_COST:
            for leg in legs:
                assert _earns_its_cost_refusal(key, leg) is None


def test_the_refusal_still_refuses_what_the_exemption_does_not_cover():
    mechanical = lr.EmittedLeg("x", "p", _MECHANICAL_TYPE, _MECHANICAL_MODEL)
    assert _earns_its_cost_refusal(("ask", "gate"), mechanical)
    assert _earns_its_cost_refusal(("grind", "triage"), mechanical) is None
    assert _earns_its_cost_refusal(("grind", "verify-op"), lr.EmittedLeg("x", "p", OP_RUNNER_AGENT_TYPE, "haiku"))
    assert _earns_its_cost_refusal(("grind", "fix"), lr.EmittedLeg("x", "p", _MECHANICAL_TYPE, "sonnet")) is None
