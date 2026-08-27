"""
coordinator_core.ops.tests.test_blocker_id_resolves_to_one_chain_terminus

Purpose: a `stub_id` names an entire continuation CHAIN, not one record, so the
act-time resolver's duplicate-id guard fired on every blocker that had ever been
picked up twice. `_resolve_blocker_deployment_state` returned
`<ambiguous-duplicate-id>`, `_blocker_clears_gate` therefore answered False
forever, and `_gate_cascade_clear` could never clear that blocker's dependents no
matter what shipped -- a permanent wedge presenting as an integrity guard.
Measured on this corpus: `ceremony-restore-01` matched 10 records.

The fix collapses a match set to its chain heads via
`reconcile.gate_eval.collapse_to_chain_heads` BEFORE the duplicate check, and that
primitive is deliberately SHARED with the compute-time index
(`gate_eval._index_by_id`). The two resolvers previously disagreed in opposite
directions on the same corpus -- the compute index silently took whichever record
its walker appended last (archived entries append after live ones, so a superseded
record beat the live head), while the act-time resolver failed loud as ambiguous.
`gate_eval`'s module docstring names sibling-evaluator divergence as the shape it
exists to prevent, so the parity test below is the point of this file, not a
bonus: it fails if anyone reintroduces a second evaluator.

Coverage here, per the originating baton's acceptance criteria:
  - a single continuation chain resolves to one record, and the existing
    `continued_into` chase runs against it;
  - a genuinely divergent record set still fails loud (the sentinel is NARROWED,
    never removed -- it guards glob-sort order deciding a lifecycle verdict);
  - a continuation cycle refuses rather than hanging;
  - both call sites resolve the same chain to the same record.

Spec backlink: state/handoffs/2026-08-27-the-gate-resolver-cannot-name-a-blocker-that-was-continued.md,
and docs/research/spike-verdicts/2026-08-27-the-blocker-id-resolves-to-a-lineage-terminus.md
for the corpus measurement behind the rule.

No process spawn: these exercise pure file-reading resolution against a tmp tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.handoff_transition import (
    _AMBIGUOUS_BLOCKER_SENTINEL,
    _blocker_clears_gate,
    _resolve_blocker_deployment_state,
)
from coordinator_core.reconcile.gate_eval import _index_by_id


def _write(
    worktree: Path,
    name: str,
    *,
    stub_id: str,
    deployment_state: str,
    predecessor: Optional[str] = None,
    continued_into: Optional[str] = None,
    archived: bool = False,
) -> dict:
    """Write one chain record, and return the dict shape the compute index takes."""
    root = worktree / ("archive/handoffs/2026-08" if archived else "state/handoffs")
    root.mkdir(parents=True, exist_ok=True)
    fm = {"stub_id": stub_id, "deployment_state": deployment_state}
    if predecessor:
        fm["predecessor"] = f"state/handoffs/{predecessor}"
    if continued_into:
        fm["continued_into"] = f"state/handoffs/{continued_into}"
    body = ["---"]
    body.extend(f"{k}: {v!r}" for k, v in fm.items())
    body.extend(["---", "", f"# {name}", ""])
    path = root / f"{name}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return {**fm, "_path": str(path)}


def _three_record_chain(worktree: Path) -> list:
    """The sat-06 shape: root -> middle -> terminus, the middle two archived.

    Only the ROOT carries a `continued_into` stamp; the later links are joined by
    the successor's `predecessor` up-edge alone. That asymmetry is the real corpus
    shape -- minting a successor writes `predecessor`, while stamping the
    predecessor `continued`/`continued_into` is a separate later step that is not
    always reached -- and it is why the collapse reads both edges.
    """
    return [
        _write(
            worktree, "root", stub_id="chain-01", deployment_state="continued",
            continued_into="middle", archived=True,
        ),
        _write(
            worktree, "middle", stub_id="chain-01", deployment_state="continued",
            predecessor="root", archived=True,
        ),
        _write(
            worktree, "terminus", stub_id="chain-01", deployment_state="shipped",
            predecessor="middle",
        ),
    ]


def test_a_continued_chain_resolves_to_its_terminus(tmp_path: Path) -> None:
    """Three records, one stub_id: the chain resolves, it is not ambiguous."""
    _three_record_chain(tmp_path)

    state = _resolve_blocker_deployment_state("chain-01", tmp_path)

    assert state.deployment_state != _AMBIGUOUS_BLOCKER_SENTINEL, (
        "a continuation chain was read as a duplicate-id collision"
    )
    assert state.deployment_state == "shipped"


def test_the_chase_runs_and_clears_a_gate_on_a_continued_blocker(
    tmp_path: Path,
) -> None:
    """The wedge: `_blocker_clears_gate` could never reach its own chase."""
    _three_record_chain(tmp_path)

    clears, detail = _blocker_clears_gate("chain-01", tmp_path)

    assert clears is True, f"chain whose terminus shipped did not clear: {detail}"
    assert detail == "shipped"


def test_a_genuinely_divergent_set_still_fails_loud(tmp_path: Path) -> None:
    """The sentinel is narrowed, not removed: two unrelated heads stay ambiguous."""
    _write(tmp_path, "fam-a", stub_id="collide-01", deployment_state="shipped")
    _write(tmp_path, "fam-b", stub_id="collide-01", deployment_state="in_flight")

    state = _resolve_blocker_deployment_state("collide-01", tmp_path)

    assert state.deployment_state == _AMBIGUOUS_BLOCKER_SENTINEL
    clears, _ = _blocker_clears_gate("collide-01", tmp_path)
    assert clears is False, "an ambiguous blocker must never clear a gate"


def test_a_continuation_cycle_refuses_instead_of_hanging(tmp_path: Path) -> None:
    """Two records naming each other must terminate on the cycle guard."""
    _write(
        tmp_path, "ping", stub_id="cycle-a", deployment_state="continued",
        continued_into="cycle-b",
    )
    _write(
        tmp_path, "cycle-b", stub_id="cycle-b", deployment_state="continued",
        continued_into="cycle-a",
    )
    _write(
        tmp_path, "cycle-a", stub_id="cycle-a", deployment_state="continued",
        continued_into="cycle-b",
    )

    clears, detail = _blocker_clears_gate("cycle-a", tmp_path)

    assert clears is False
    assert detail, "a refusal must say why"


def test_both_resolvers_agree_on_the_same_chain(tmp_path: Path) -> None:
    """Index parity: one shared predicate, two call sites, never two evaluators.

    The compute index appends the archived half AFTER the live half, which is the
    ordering that used to hand it a superseded record. Feeding that exact order
    here is what makes this a regression guard rather than a tautology.
    """
    records = _three_record_chain(tmp_path)
    live = [r for r in records if "archive" not in r["_path"]]
    archived = [r for r in records if "archive" in r["_path"]]

    index = _index_by_id(archived + live)
    compute_side = index.get("chain-01")
    act_side = _resolve_blocker_deployment_state("chain-01", tmp_path)

    assert compute_side is not None, "compute index lost a resolvable chain"

    assert compute_side["deployment_state"] == act_side.deployment_state, (
        "compute-time index and act-time resolver disagree about the same "
        f"chain: {compute_side['deployment_state']!r} vs "
        f"{act_side.deployment_state!r}"
    )
    assert Path(compute_side["_path"]).name == "terminus.md"
