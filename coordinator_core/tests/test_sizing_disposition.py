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


def test_plural_plan_ids_never_resolve(tmp_path: Path) -> None:
    """DR-346 §5 (Correction, C3 2026-08-21): `plan_ids` is a fan-in AUDIT
    list, never a plan FK -- `cited_plan_fks` no longer joins it at all, so
    a populated `plan_ids` matching a real on-disk plan still reads unsized
    (absent, not dangling: the field is never cited, so it never earns a
    warning either)."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-b.md", "pln-b-123456")

    verdict = compute_sizing_disposition(tmp_path, {"plan_ids": ["pln-missing", "pln-b-123456"]})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


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
        ({"sizing_object": "state/sizings/absent.yaml"}, "sizing_object=state/sizings/absent.yaml"),
    ],
    ids=["origin_plan_id", "sizing_object"],
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


# ---------------------------------------------------------------------------
# The null sentinel — both legs of the join
# ---------------------------------------------------------------------------


def test_null_sentinel_fk_does_not_match_a_null_stamped_plan(tmp_path: Path) -> None:
    """The regression this module's `_real_id` exists for.

    `docs/plans/*.md` really does contain a plan reading `plan_id: null
    # stamped by a real /plan run`, and 80 live batons really do carry
    `origin_plan_id` surviving frontmatter parsing as the string "null".
    Matching those two by string equality stamped `execution` — "sized
    upstream, re-litigate nothing" — citing an unrelated July draft. The
    truthful answer is `unsized`, with no dangling-citation warning: a
    null FK is an ABSENT citation, not a broken one.
    """
    _write_plan(tmp_path, "docs/plans/2026-07-19-unrelated-draft.md", "null")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "null"})

    assert verdict["value"] == "unsized"
    assert verdict["basis"] is None
    assert verdict["warning"] is None


@pytest.mark.parametrize("sentinel", ["null", "NULL", "Null", "~", "  null  ", ""])
def test_every_null_sentinel_spelling_is_an_absent_citation(tmp_path: Path, sentinel: str) -> None:
    """`~` is YAML null too, and case is not load-bearing in a sentinel."""
    _write_plan(tmp_path, "docs/plans/2026-07-19-unrelated-draft.md", "null")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": sentinel})

    assert verdict["value"] == "unsized"
    assert verdict["basis"] is None


def test_null_stamped_plan_is_not_an_inheritance_target(tmp_path: Path) -> None:
    """Target-side leg: a plan carrying `deliverable_id: null` must not be
    inherited by a baton whose own id failed to parse into anything real."""
    _write_plan_with_deliverable(tmp_path, "docs/plans/2026-07-19-unstamped.md", "null")

    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "null"})

    assert verdict["value"] == "unsized"
    assert verdict["basis"] is None


def test_a_real_fk_still_resolves(tmp_path: Path) -> None:
    """Negative control — the sentinel filter must not blunt the real join."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-real.md", "pln-real-abc123")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-real-abc123"})

    assert verdict["value"] == "execution"
    assert "docs/plans/2026-08-20-real.md" in verdict["basis"]


# ---------------------------------------------------------------------------
# sizing_object — Path.__truediv__ does not confine to root
#
# `root / sizing_ref` is not a containment check: an ABSOLUTE `sizing_ref`
# replaces `root` outright, and a `..`-laden relative one walks past it.
# Either shape lets `sizing_object` name ANY file that happens to exist on
# disk -- not a sizing artifact at all -- and still earn `sized`, purchasing
# the same "already routed, do not re-litigate" authorization the null
# sentinel purchased for `execution`.
# ---------------------------------------------------------------------------


def test_absolute_sizing_object_does_not_escape_root(tmp_path: Path) -> None:
    """`root / <absolute path>` discards `root` entirely (see
    `_sizing_object_within_root`'s docstring) -- an absolute `sizing_object`
    must not be able to name a file outside the repo and still read as
    `sized`."""
    outside = tmp_path.parent / "outside-sizing-escape.yaml"
    outside.write_text("not a sizing object", encoding="utf-8")

    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": str(outside)})

    assert verdict["value"] == "unsized"


def test_traversal_sizing_object_does_not_escape_root(tmp_path: Path) -> None:
    """A relative `sizing_object` laden with `..` must not resolve past
    `root` either -- containment, not merely "not absolute"."""
    outside = tmp_path.parent / "outside-sizing-traversal.yaml"
    outside.write_text("not a sizing object", encoding="utf-8")

    verdict = compute_sizing_disposition(
        tmp_path, {"sizing_object": f"../{outside.name}"}
    )

    assert verdict["value"] == "unsized"


def test_sizing_object_within_root_still_resolves(tmp_path: Path) -> None:
    """Negative control -- the containment check must not blunt a genuine
    in-repo sizing artifact."""
    _write_sizing(tmp_path, "state/sizings/2026-08-20-o.yaml")

    verdict = compute_sizing_disposition(
        tmp_path, {"sizing_object": "state/sizings/2026-08-20-o.yaml"}
    )

    assert verdict["value"] == "sized"


# ---------------------------------------------------------------------------
# `plan_ids` is NOT read as a plan citation -- DR-346 §5 (Correction,
# 2026-08-21) named the read side as the actual defect: `resolve_lineage`
# writes `plan_ids` as a fan-in DISAGREEMENT AUDIT (only entered when >=2
# distinct `origin_plan_id` values are seen across a funnel), never as an FK
# to follow, and `cited_plan_fks` used to read it as a citation regardless --
# live, not dormant: 4 of 797 records carried a populated `plan_ids`, and
# `2026-08-21-guards-under-the-brightline.md` resolved `execution` SOLELY
# through it with no `origin_plan_id` at all. C3 (2026-08-21) closed the
# mismatch on the READ side: `cited_plan_fks` now joins `origin_plan_id`
# only. The two write sites (`resolve_lineage`'s `_ordered_unique` and
# `baton_assemble/apply.py :: baton-stamp-carried-ids`) both still emit
# `plan_ids` as a >=2-distinct-id audit trail, now consumed by nothing that
# treats it as a citation -- see `coordinator_core.baton_assemble.apply.
# _dispatch_baton_stamp_carried_ids`'s own PLAN_IDS THRESHOLD docstring note
# for the writer-side half of this fix.
# ---------------------------------------------------------------------------


def test_plan_ids_is_still_read_as_a_citation_dr346(tmp_path: Path) -> None:
    """DR-346 §5 (Correction, C3 2026-08-21): `plan_ids` is a fan-in audit
    list, never a plan FK. This test used to pin the DEFECTIVE behaviour
    (a populated `plan_ids` matching an on-disk plan stamped `execution`)
    as a tripwire naming exactly what had to change -- it is rewritten here,
    not deleted, to pin the FIXED behaviour instead: `plan_ids` no longer
    contributes to `execution` at all, even when it names a real plan and
    even when `origin_plan_id` is entirely absent, matching the corpus
    record (`2026-08-21-guards-under-the-brightline.md`) DR-346 named."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-p.md", "pln-p-123456")

    verdict = compute_sizing_disposition(tmp_path, {"plan_ids": ["pln-p-123456"]})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}
