"""coordinator_core.plan_assemble.test_residue_admission — the admission
axis's residue-level tests, homed OFF the cadence tier on purpose.

Split out of `test_residue.py`, which carries a file-level
`cadence` + `spawns_process` marker. None of these six spawns anything:
each either leaves `predicates_requested` false or pins `show_toplevel`,
so the real `git rev-parse` never runs. Left in that module they were
deselected from the fast tier, which mattered most for the AC5 guard —
the regression tripwire on this plan's loudest Anti-scope rule (no
`admitted` boolean, no `*.verdict` field spanning a U/G-typed row). A
guard that only runs on the cadence tier does not guard the commit that
breaks it.

Reported by `coordinator:code-reviewer` against 947c789d9d36, finding 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.plan_assemble import residue as residue_mod
# Fixture helpers are IMPORTED from the sibling module, never copied: two
# drifting definitions of the same residue corpus is a worse failure than
# the import coupling, and importing them does not import its pytestmark.
from coordinator_core.plan_assemble.test_residue import (
    _make_residue_dir,
    _patch_content_root,
)
from coordinator_core.plan_assemble.residue import (
    DEFAULT_ROUTE,
    ResidueAssembleError,
    RouteUsageError,
    brief,
)

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.

def test_admission_resolves_without_sizing_object_ac1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: with NO `--sizing-object`, `brief` resolves the sizing FK
    itself from `--plan`'s own frontmatter, via the shared predicate — the
    row is populated, never `undetermined`."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "---\ntitle: fixture\nscope_mode: spec-dispatch\n---\n\n# fixture\n",
        encoding="utf-8",
    )

    result = brief(explicit_route="plan", plan_path=plan_file)

    admission = result["gates"]["triage"]["admission"]
    assert admission.get("undetermined") is not True
    assert admission == {"value": "unsized", "basis": None, "warning": None}


def test_admission_explicit_sizing_object_wins_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: an explicitly-passed `--sizing-object` still wins over the
    FK the plan's own frontmatter would otherwise resolve to `execution`."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)
    # Pin repo_root to the fixture: without this, brief() resolves it via a real
    # `git rev-parse` from cwd, so the fixture's sizing path sits OUTSIDE the
    # resolved root and `basis` falls back to an absolute form no production
    # invocation produces.
    monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "---\ntitle: fixture\norigin_plan_id: pln-does-not-exist-000000\n---\n\n"
        "# fixture\n",
        encoding="utf-8",
    )
    sizing_file = tmp_path / "state" / "sizings" / "ask.yaml"
    sizing_file.parent.mkdir(parents=True)
    sizing_file.write_text("route: plan\n", encoding="utf-8")

    result = brief(
        explicit_route="plan", plan_path=plan_file, sizing_object_path=sizing_file
    )

    admission = result["gates"]["triage"]["admission"]
    assert admission["value"] == "sized"
    # basis is repo-relative POSIX, never str(Path): the raw form leaked an
    # absolute machine-local root into emitted data and disagreed with the
    # pickup seam's shape for the same field.
    assert admission["basis"] == "sizing_object=state/sizings/ask.yaml"
    assert str(tmp_path) not in admission["basis"]


@pytest.mark.parametrize("route_kw", ["route_arm_unsized", "route_arm_sized", "route_arm_execution"])


def test_admission_matches_direct_predicate_call_ac3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route_kw: str
) -> None:
    """AC3: `gates.triage.admission`'s value matches the predicate called
    directly on the same frontmatter — exercised across all three arms."""
    from coordinator_core.plan_assemble.predicates import triage as triage_mod

    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)
    # `brief`'s own `_default_repo_root()` walks from cwd — pin it to
    # `tmp_path` so the plan-FK/sizing_object resolution this test drives
    # resolves against the same fixture tree the "direct" call below reads.
    monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

    plan_file = tmp_path / "plan.md"
    if route_kw == "route_arm_unsized":
        plan_file.write_text("---\ntitle: fixture\n---\n\n# fixture\n", encoding="utf-8")
        result = brief(explicit_route="plan", plan_path=plan_file)
    elif route_kw == "route_arm_sized":
        sizing_file = tmp_path / "state" / "sizings" / "ask.yaml"
        sizing_file.parent.mkdir(parents=True)
        sizing_file.write_text("route: plan\n", encoding="utf-8")
        plan_file.write_text(
            f"---\ntitle: fixture\nsizing_object: state/sizings/ask.yaml\n---\n\n"
            "# fixture\n",
            encoding="utf-8",
        )
        result = brief(explicit_route="plan", plan_path=plan_file)
    else:
        plan_ref = tmp_path / "docs" / "plans" / "2026-08-20-a.md"
        plan_ref.parent.mkdir(parents=True)
        plan_ref.write_text(
            "---\ntitle: a plan\nplan_id: pln-a-123456\nstatus: draft\n---\n\nbody\n",
            encoding="utf-8",
        )
        plan_file.write_text(
            "---\ntitle: fixture\norigin_plan_id: pln-a-123456\n---\n\n# fixture\n",
            encoding="utf-8",
        )
        result = brief(explicit_route="plan", plan_path=plan_file)

    from coordinator_core.plan_assemble.predicates import PredicateContext

    context = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=plan_file,
        sizing_object_path=None,
        resolved_route="plan",
        caller_flags={},
    )
    direct = triage_mod.admission(context)
    assert result["gates"]["triage"]["admission"] == direct


def test_next_move_three_arms_ac4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the three `next_move` arms, each asserted on real `brief()`
    output — `unsized` names `coordinator:sizing`, `sized`/`execution` name
    the lane/plan to resume, never the literal
    `"Render segments[] in order."` string alone."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)
    monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

    # unsized
    unsized_plan = tmp_path / "unsized.md"
    unsized_plan.write_text("---\ntitle: fixture\n---\n\n# fixture\n", encoding="utf-8")
    unsized_result = brief(explicit_route="plan", plan_path=unsized_plan)
    assert "coordinator:sizing" in unsized_result["next_move"]
    assert "Render segments[] in order." in unsized_result["next_move"]

    # sized
    sizing_file = tmp_path / "state" / "sizings" / "ask.yaml"
    sizing_file.parent.mkdir(parents=True)
    sizing_file.write_text("route: plan\n", encoding="utf-8")
    sized_plan = tmp_path / "sized.md"
    sized_plan.write_text("---\ntitle: fixture\n---\n\n# fixture\n", encoding="utf-8")
    sized_result = brief(
        explicit_route="spec-dispatch",
        plan_path=sized_plan,
        sizing_object_path=sizing_file,
    )
    assert "'spec-dispatch'" in sized_result["next_move"]
    assert "state/sizings/ask.yaml" in sized_result["next_move"]
    assert str(tmp_path) not in sized_result["next_move"]
    assert "Render segments[] in order." in sized_result["next_move"]

    # execution
    # DR-346 (2026-08-21, PM-ratified) retired the corpus walk that used to
    # resolve `origin_plan_id` by search -- `governing_plan` (a repo-relative
    # FK, resolved by the same root-confined stat `sizing_object` uses, never
    # a search) is now the only field that admits `execution`. A citation
    # via `origin_plan_id` alone, with no `governing_plan` stamped, reads
    # `unsized` (`UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX`) -- that stranding arm
    # is not this test's concern.
    plan_ref = tmp_path / "docs" / "plans" / "2026-08-20-a.md"
    plan_ref.parent.mkdir(parents=True)
    plan_ref.write_text(
        "---\ntitle: a plan\nplan_id: pln-a-123456\nstatus: draft\n---\n\nbody\n",
        encoding="utf-8",
    )
    execution_plan = tmp_path / "execution.md"
    execution_plan.write_text(
        "---\ntitle: fixture\ngoverning_plan: docs/plans/2026-08-20-a.md\n---\n\n# fixture\n",
        encoding="utf-8",
    )
    execution_result = brief(explicit_route="plan", plan_path=execution_plan)
    assert "not re-litigated" in execution_result["next_move"]
    assert "docs/plans/2026-08-20-a.md" in execution_result["next_move"]
    assert "Render segments[] in order." in execution_result["next_move"]


def test_admission_never_names_a_verdict_field_ac5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: no `admitted` boolean, and no `*.verdict`/`*.fires`/
    `*.recommended` field spans a `U`- or `G`-typed row — `admission` is
    the only new field this chunk adds, scoped to the SIZING axis."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    def _walk_keys(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from _walk_keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _walk_keys(item)

    all_keys = set(_walk_keys(result))
    assert "admitted" not in all_keys
    assert "verdict" not in all_keys


def test_segments_byte_identical_across_admission_arms_ac6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: `segments[]` is unaffected by which admission arm resolves —
    same route, same segments, regardless of `--plan`/`--sizing-object`."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    bare = brief(explicit_route="plan")

    sizing_file = tmp_path / "state" / "sizings" / "ask.yaml"
    sizing_file.parent.mkdir(parents=True)
    sizing_file.write_text("route: plan\n", encoding="utf-8")
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("---\ntitle: fixture\n---\n\n# fixture\n", encoding="utf-8")
    sized = brief(
        explicit_route="plan", plan_path=plan_file, sizing_object_path=sizing_file
    )

    assert bare["segments"] == sized["segments"]
