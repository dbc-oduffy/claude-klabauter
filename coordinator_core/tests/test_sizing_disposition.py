"""coordinator_core.tests.test_sizing_disposition — pins the FK-reading
predicate behind the sizing axis a baton carries.

The wall this replaces was prose: an EM was told, in `plan/SKILL.md`, to
work out whether the baton in hand had been sized. Provenance made that
unanswerable — `spinoff` never enters the sizing lobby and `roadmap-
planning` stamps nothing onto the batons it mints — so an idea baton routed
straight into `plan` on a route nobody computed.

The two failure modes the prose wall could catch NEITHER of, both pinned
below: a baton citing nothing (absence), and a baton citing an FK that
resolves to nothing on disk (non-resolution). The second is the dangerous
one — it reads as sized to anyone matching on key presence.

Cross-repo ask: `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-brief-
should-emit-the-sizing-disposition.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.sizing_disposition import (
    UNSIZED_DANGLING_NEXT_MOVE_PREFIX,
    UNSIZED_NEXT_MOVE_PREFIX,
    compute_sizing_disposition,
    resolve_plan_by_deliverable,
    resolve_plan_id,
    unsized_next_move_prefix,
)


def _write_plan(root: Path, rel: str, plan_id: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: a plan\nplan_id: {plan_id}\nstatus: draft\n---\n\nbody\n", encoding="utf-8")


def _write_plan_with_deliverable(root: Path, rel: str, deliverable_id: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: a plan\nplan_id: pln-whatever-999999\n"
        f"deliverable_id: {deliverable_id}\n---\n\nbody\n",
        encoding="utf-8",
    )


def _write_sizing(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("id: a-sizing\nroute: plan\n", encoding="utf-8")


def test_plan_fk_that_resolves_is_execution(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-a.md", "pln-a-123456")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-a-123456"})

    assert verdict["value"] == "execution"
    assert "pln-a-123456" in verdict["basis"]
    assert "docs/plans/2026-08-20-a.md" in verdict["basis"]
    assert verdict["warning"] is None


def test_plural_plan_ids_resolve_too(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-b.md", "pln-b-123456")

    verdict = compute_sizing_disposition(tmp_path, {"plan_ids": ["pln-missing", "pln-b-123456"]})

    assert verdict["value"] == "execution"
    assert "pln-b-123456" in verdict["basis"]


def test_archived_spec_still_resolves(tmp_path: Path) -> None:
    """An execution baton outlives its plan's archival. If the move to
    `archive/specs/` flipped the verdict to `unsized`, resuming a
    long-running execution would re-enter the sizing lobby on work that was
    planned months earlier — the exact re-litigation this axis exists to
    prevent."""
    _write_plan(tmp_path, "archive/specs/2026-07/2026-07-01-c.md", "pln-c-123456")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-c-123456"})

    assert verdict["value"] == "execution"


def test_sizing_object_that_resolves_is_sized(tmp_path: Path) -> None:
    _write_sizing(tmp_path, "state/sizings/2026-08-20-a.yaml")

    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": "state/sizings/2026-08-20-a.yaml"})

    assert verdict["value"] == "sized"
    assert verdict["basis"] == "sizing_object=state/sizings/2026-08-20-a.yaml"
    assert verdict["warning"] is None


def test_plan_fk_takes_precedence_over_sizing_object(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-d.md", "pln-d-123456")
    _write_sizing(tmp_path, "state/sizings/2026-08-20-d.yaml")

    verdict = compute_sizing_disposition(
        tmp_path,
        {"origin_plan_id": "pln-d-123456", "sizing_object": "state/sizings/2026-08-20-d.yaml"},
    )

    assert verdict["value"] == "execution"


def test_citing_nothing_is_unsized_without_a_warning(tmp_path: Path) -> None:
    """Absence is the ordinary case for a spinoff or roadmap mint, not a
    defect — it earns the trampoline, never a warning."""
    verdict = compute_sizing_disposition(tmp_path, {"title": "an idea"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


@pytest.mark.parametrize(
    "fm,cited",
    [
        ({"origin_plan_id": "pln-nothing-declares-this"}, "origin_plan_id=pln-nothing-declares-this"),
        ({"plan_ids": ["pln-nothing-declares-this"]}, "plan_ids=pln-nothing-declares-this"),
        ({"sizing_object": "state/sizings/absent.yaml"}, "sizing_object=state/sizings/absent.yaml"),
    ],
    ids=["origin_plan_id", "plan_ids", "sizing_object"],
)
def test_dangling_fk_is_unsized_plus_a_named_warning(tmp_path: Path, fm: dict, cited: str) -> None:
    """Non-resolution is the failure mode a key-presence check cannot see.
    The citation stays in `basis` — "we looked, and this is what did not
    resolve" is the diagnostic; dropping it to `None` would make a dangling
    FK indistinguishable from absence."""
    verdict = compute_sizing_disposition(tmp_path, fm)

    assert verdict["value"] == "unsized"
    assert verdict["basis"] == cited
    assert verdict["warning"] is not None
    assert cited in verdict["warning"]


def test_dangling_plan_fk_never_silently_passes_as_execution(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-e.md", "pln-a-different-one")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-e-123456"})

    assert verdict["value"] == "unsized"


def test_blank_and_null_fks_read_as_absent(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(
        tmp_path, {"origin_plan_id": None, "plan_ids": None, "sizing_object": "   "}
    )

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


def test_resolve_plan_id_reads_no_status(tmp_path: Path) -> None:
    """Plan liveness is a separate read with a separate owner. A superseded
    plan still means this baton was planned upstream."""
    path = tmp_path / "docs/plans/2026-08-20-f.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nplan_id: pln-f-123456\nstatus: superseded\n---\n", encoding="utf-8")

    assert resolve_plan_id(tmp_path, "pln-f-123456") == "docs/plans/2026-08-20-f.md"


@pytest.mark.parametrize("prefix", [UNSIZED_NEXT_MOVE_PREFIX, UNSIZED_DANGLING_NEXT_MOVE_PREFIX])
def test_unsized_prefixes_name_the_room_and_not_plan(prefix: str) -> None:
    assert "coordinator:sizing" in prefix
    assert "not `plan`" in prefix


@pytest.mark.parametrize("value", ["execution", "sized"])
def test_sized_arms_get_no_prefix(value: str) -> None:
    """Silence on these arms is the emission, not an omission — the failure
    mode here is an EM re-litigating a baton that WAS sized."""
    assert unsized_next_move_prefix({"value": value, "basis": "x", "warning": None}) == ""


def test_dangling_arm_never_claims_the_baton_cites_nothing(tmp_path: Path) -> None:
    """The two unsized arms are different findings. Telling a baton whose
    FK simply does not resolve that it "cites no plan" is false, and false
    in the direction that hides a broken pointer."""
    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-nope"})

    prefix = unsized_next_move_prefix(verdict)

    assert prefix == UNSIZED_DANGLING_NEXT_MOVE_PREFIX
    assert "cites no plan" not in prefix


def test_absent_arm_gets_the_plain_prefix(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {})

    assert unsized_next_move_prefix(verdict) == UNSIZED_NEXT_MOVE_PREFIX


# ---------------------------------------------------------------------------
# The deliverable-inheritance leg — the execution shape an FK-name reading
# misses. `origin_plan_id` is FORK-SPAWN provenance, so an ordinary /handoff
# fired mid-execute-plan carries neither plan FK. Measured on the real corpus
# when this landed: 1 of 150 live batons carried a plan FK; 36 more inherited
# a plan's deliverable_id and would otherwise have been sent to the lobby for
# work planned days earlier.
# ---------------------------------------------------------------------------


def test_inherited_deliverable_id_is_execution(tmp_path: Path) -> None:
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-g.md", "dlv-shared-abc123")

    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "dlv-shared-abc123"})

    assert verdict["value"] == "execution"
    assert "inherited" in verdict["basis"]
    assert "docs/plans/2026-08-20-g.md" in verdict["basis"]
    assert verdict["warning"] is None


def test_self_minted_deliverable_id_is_not_a_citation(tmp_path: Path) -> None:
    """A spinoff mints its OWN fresh dlv-<slug>-<6hex>, so presence proves
    nothing — inheritance is the discriminant. Adding `deliverable_id` to the
    cited set on presence alone would reopen the hole from the other side."""
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-h.md", "dlv-someone-elses-abc123")

    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "dlv-freshly-minted-def456"})

    assert verdict["value"] == "unsized"


def test_uninherited_deliverable_id_earns_no_warning(tmp_path: Path) -> None:
    """A self-minted key is the ordinary case, not a dangling pointer. Only
    fields whose whole purpose is to point elsewhere warn on non-resolution."""
    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "dlv-freshly-minted-def456"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


def test_explicit_plan_fk_outranks_inheritance(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-i.md", "pln-i-123456")
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-j.md", "dlv-shared-abc123")

    verdict = compute_sizing_disposition(
        tmp_path, {"origin_plan_id": "pln-i-123456", "deliverable_id": "dlv-shared-abc123"}
    )

    assert verdict["value"] == "execution"
    assert "origin_plan_id" in verdict["basis"]


def test_inheritance_outranks_a_resolving_sizing_object(tmp_path: Path) -> None:
    """Planned beats merely sized: a baton whose plan exists is an execution
    baton, not one still owed a route."""
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-k.md", "dlv-shared-abc123")
    _write_sizing(tmp_path, "state/sizings/2026-08-20-k.yaml")

    verdict = compute_sizing_disposition(
        tmp_path,
        {"deliverable_id": "dlv-shared-abc123", "sizing_object": "state/sizings/2026-08-20-k.yaml"},
    )

    assert verdict["value"] == "execution"


def test_inheritance_resolves_against_archived_specs(tmp_path: Path) -> None:
    _write_plan_with_deliverable(tmp_path, "archive/specs/2026-07/2026-07-01-l.md", "dlv-shared-abc123")

    assert resolve_plan_by_deliverable(tmp_path, "dlv-shared-abc123") == (
        "archive/specs/2026-07/2026-07-01-l.md"
    )


def test_dangling_plan_fk_still_warns_even_with_inheritance_absent(tmp_path: Path) -> None:
    """Negative control: adding the inheritance leg must not have swallowed
    the dangling-FK diagnostic."""
    verdict = compute_sizing_disposition(
        tmp_path, {"origin_plan_id": "pln-nope", "deliverable_id": "dlv-freshly-minted-def456"}
    )

    assert verdict["value"] == "unsized"
    assert verdict["basis"] == "origin_plan_id=pln-nope"
    assert verdict["warning"] is not None


# ---------------------------------------------------------------------------
# self_path — a record is never its own upstream plan.
#
# A PLAN carries a deliverable_id and lives in the scanned globs, so without
# this it matches ITSELF and reads `execution` — "already planned", citing
# itself. That waves through the exact wall condition `plan` exists to catch.
# Found by running the real brief() over an unsized plan, not by reading it.
# ---------------------------------------------------------------------------


def test_a_plan_does_not_satisfy_its_own_deliverable_citation(tmp_path: Path) -> None:
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-m.md", "dlv-only-mine-abc123")
    own = tmp_path / "docs/plans/2026-08-20-m.md"

    verdict = compute_sizing_disposition(
        tmp_path, {"deliverable_id": "dlv-only-mine-abc123"}, self_path=own
    )

    assert verdict["value"] == "unsized"
    assert verdict["basis"] is None


def test_self_path_does_not_suppress_a_genuine_upstream_plan(tmp_path: Path) -> None:
    """Negative control: excluding self must not exclude a DIFFERENT plan
    carrying the same key — that is the inheritance leg working."""
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-19-upstream.md", "dlv-shared-xyz789")
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-downstream.md", "dlv-shared-xyz789")
    own = tmp_path / "docs/plans/2026-08-20-downstream.md"

    verdict = compute_sizing_disposition(
        tmp_path, {"deliverable_id": "dlv-shared-xyz789"}, self_path=own
    )

    assert verdict["value"] == "execution"
    assert "2026-08-19-upstream.md" in verdict["basis"]


def test_self_path_omitted_keeps_the_baton_behaviour(tmp_path: Path) -> None:
    """A baton does not live in the scanned globs, so a caller passing one
    may omit `self_path` — the default must stay the pickup seam's behaviour."""
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-08-20-n.md", "dlv-shared-abc123")

    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "dlv-shared-abc123"})

    assert verdict["value"] == "execution"
