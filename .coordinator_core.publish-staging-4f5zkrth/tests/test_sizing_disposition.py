"""coordinator_core.tests.test_sizing_disposition — pins the FK-reading
predicate behind the sizing axis a baton carries.

The wall this replaces was prose: an EM was told, in `plan/SKILL.md`, to
work out whether the baton in hand had been sized. Provenance made that
unanswerable — `spinoff` never enters the sizing lobby and `roadmap-
planning` stamps nothing onto the batons it mints — so an idea baton routed
straight into `plan` on a route nobody computed.

DR-346 (2026-08-21, PM-ratified): the corpus-walk resolution legs this
module used to carry (`resolve_plan_id`, `resolve_plan_by_deliverable`, the
`docs/plans/*.md` / `archive/specs/*/*.md` glob hunt) are RETIRED outright.
The baton now carries its plan link directly as a stamped `governing_plan`
frontmatter field, checked with a single stat under `root` — never a
search. A baton citing `origin_plan_id` with no `governing_plan` stamped is
a deliberate STRANDING, PM-accepted, and reads `unsized` without any
"dangling"/"does not resolve on disk" language, because this module no
longer knows whether the plan exists — only that the pointer was never
written.

Cross-repo ask: `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-brief-
should-emit-the-sizing-disposition.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.sizing_disposition import (
    UNSIZED_DANGLING_NEXT_MOVE_PREFIX,
    UNSIZED_NEXT_MOVE_PREFIX,
    UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX,
    compute_sizing_disposition,
    unsized_next_move_prefix,
)


def _write_plan(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: a plan\nstatus: draft\n---\n\nbody\n", encoding="utf-8")


def _write_sizing(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("id: a-sizing\nroute: plan\n", encoding="utf-8")


def test_governing_plan_that_resolves_is_execution(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-a.md")

    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": "docs/plans/2026-08-20-a.md"})

    assert verdict["value"] == "execution"
    assert "docs/plans/2026-08-20-a.md" in verdict["basis"]
    assert verdict["warning"] is None


def test_governing_plan_takes_precedence_over_sizing_object(tmp_path: Path) -> None:
    _write_plan(tmp_path, "docs/plans/2026-08-20-d.md")
    _write_sizing(tmp_path, "state/sizings/2026-08-20-d.yaml")

    verdict = compute_sizing_disposition(
        tmp_path,
        {
            "governing_plan": "docs/plans/2026-08-20-d.md",
            "sizing_object": "state/sizings/2026-08-20-d.yaml",
        },
    )

    assert verdict["value"] == "execution"
    assert "governing_plan" in verdict["basis"]


def test_governing_plan_absent_but_present_on_disk_never_matters(tmp_path: Path) -> None:
    """No corpus walk remains -- a plan existing on disk that the baton
    never cited via `governing_plan` must not be discovered by any other
    means."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-b.md")

    verdict = compute_sizing_disposition(tmp_path, {"title": "an idea"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


def test_sizing_object_that_resolves_is_sized(tmp_path: Path) -> None:
    _write_sizing(tmp_path, "state/sizings/2026-08-20-a.yaml")

    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": "state/sizings/2026-08-20-a.yaml"})

    assert verdict["value"] == "sized"
    assert verdict["basis"] == "sizing_object=state/sizings/2026-08-20-a.yaml"
    assert verdict["warning"] is None


def test_citing_nothing_is_unsized_without_a_warning(tmp_path: Path) -> None:
    """Absence is the ordinary case for a spinoff or roadmap mint, not a
    defect — it earns the trampoline, never a warning."""
    verdict = compute_sizing_disposition(tmp_path, {"title": "an idea"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


def test_dangling_governing_plan_is_unsized_plus_a_named_dangling_warning(tmp_path: Path) -> None:
    """A STAMPED `governing_plan` that does not exist under root is a
    genuine broken link — this stays checkable, cheaply, via one stat."""
    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": "docs/plans/absent.md"})

    assert verdict["value"] == "unsized"
    assert verdict["basis"] == "governing_plan=docs/plans/absent.md"
    assert verdict["warning"] is not None
    assert "governing_plan=docs/plans/absent.md" in verdict["warning"]
    assert "dangling" in verdict["warning"].lower()


def test_dangling_sizing_object_is_unsized_plus_a_named_warning(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": "state/sizings/absent.yaml"})

    assert verdict["value"] == "unsized"
    assert verdict["basis"] == "sizing_object=state/sizings/absent.yaml"
    assert verdict["warning"] is not None
    assert "sizing_object=state/sizings/absent.yaml" in verdict["warning"]


def test_blank_and_null_fields_read_as_absent(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(
        tmp_path, {"governing_plan": None, "plan_ids": None, "sizing_object": "   "}
    )

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


@pytest.mark.parametrize("prefix", [UNSIZED_NEXT_MOVE_PREFIX, UNSIZED_DANGLING_NEXT_MOVE_PREFIX, UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX])
def test_unsized_prefixes_name_the_room_and_not_plan(prefix: str) -> None:
    assert "coordinator:sizing" in prefix
    assert "not `plan`" in prefix


@pytest.mark.parametrize("value", ["execution", "sized"])
def test_sized_arms_get_no_prefix(value: str) -> None:
    """Silence on these arms is the emission, not an omission — the failure
    mode here is an EM re-litigating a baton that WAS sized."""
    assert unsized_next_move_prefix({"value": value, "basis": "x", "warning": None}) == ""


def test_dangling_governing_plan_gets_the_dangling_prefix_not_unstamped(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": "docs/plans/absent.md"})

    prefix = unsized_next_move_prefix(verdict)

    assert prefix == UNSIZED_DANGLING_NEXT_MOVE_PREFIX


def test_absent_arm_gets_the_plain_prefix(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {})

    assert unsized_next_move_prefix(verdict) == UNSIZED_NEXT_MOVE_PREFIX


# ---------------------------------------------------------------------------
# DR-346 stranding arm — origin_plan_id cited, governing_plan never stamped.
# PM ruling 2026-08-21: "retire the walk immediately, stranding accepted."
# ---------------------------------------------------------------------------


def test_origin_plan_id_without_governing_plan_is_unsized_stranding(tmp_path: Path) -> None:
    """The stranding arm: a plan link that was never stamped is unsized,
    full stop -- no corpus walk resolves it and none may be added back."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-e.md")

    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-e-123456"})

    assert verdict["value"] == "unsized"


def test_stranding_arm_never_uses_dangling_language(tmp_path: Path) -> None:
    """This is the load-bearing negative assertion for DR-346: the module no
    longer knows whether the cited plan exists on disk, so it must not
    claim the citation is broken or unresolved -- only that it was never
    stamped."""
    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-e-123456"})

    assert verdict["value"] == "unsized"
    assert verdict["warning"] is not None
    assert "dangling" not in verdict["warning"].lower()
    assert "does not resolve on disk" not in verdict["warning"]
    assert "governing_plan" in verdict["warning"]


def test_stranding_arm_gets_its_own_third_prefix(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "pln-e-123456"})

    prefix = unsized_next_move_prefix(verdict)

    assert prefix == UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX
    assert prefix != UNSIZED_DANGLING_NEXT_MOVE_PREFIX
    assert prefix != UNSIZED_NEXT_MOVE_PREFIX


def test_governing_plan_present_outranks_the_stranding_arm(tmp_path: Path) -> None:
    """A baton that carries BOTH origin_plan_id and a stamped, resolving
    governing_plan is execution -- the stranding check only fires when
    governing_plan is entirely absent."""
    _write_plan(tmp_path, "docs/plans/2026-08-20-f.md")

    verdict = compute_sizing_disposition(
        tmp_path,
        {"origin_plan_id": "pln-f-123456", "governing_plan": "docs/plans/2026-08-20-f.md"},
    )

    assert verdict["value"] == "execution"


def test_null_origin_plan_id_does_not_trigger_stranding(tmp_path: Path) -> None:
    """A null-sentinel origin_plan_id is an absent citation, not a
    stranding -- it must fall through to the plain unsized arm."""
    verdict = compute_sizing_disposition(tmp_path, {"origin_plan_id": "null"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


# ---------------------------------------------------------------------------
# The deliverable_id-inheritance leg is GONE. DR-346: deliverable_id
# resolves to batons, NEVER to plans. This was the defect, not a feature.
# ---------------------------------------------------------------------------


def test_deliverable_id_matching_a_plan_is_no_longer_a_citation(tmp_path: Path) -> None:
    """The whole behavioural point of this chunk: a baton whose
    deliverable_id matches a plan's must now be unsized. No plan-carrying
    deliverable_id is ever consulted -- there is no glob left to walk."""
    path = tmp_path / "docs/plans/2026-08-20-g.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: a plan\ndeliverable_id: dlv-shared-abc123\n---\n\nbody\n",
        encoding="utf-8",
    )

    verdict = compute_sizing_disposition(tmp_path, {"deliverable_id": "dlv-shared-abc123"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


def test_resolve_plan_by_deliverable_symbol_no_longer_exists() -> None:
    import coordinator_core.sizing_disposition as module

    assert not hasattr(module, "resolve_plan_by_deliverable")
    assert not hasattr(module, "resolve_plan_id")
    assert not hasattr(module, "_resolve_plan_by")
    assert not hasattr(module, "_plan_frontmatter_head")
    assert not hasattr(module, "_PLAN_ID_RE")
    assert not hasattr(module, "_PLAN_DELIVERABLE_RE")
    assert not hasattr(module, "PLAN_ID_SEARCH_GLOBS")


# ---------------------------------------------------------------------------
# The null sentinel — governing_plan side
# ---------------------------------------------------------------------------


def test_null_sentinel_governing_plan_reads_as_absent(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": "null"})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}


@pytest.mark.parametrize("sentinel", ["null", "NULL", "Null", "~", "  null  ", ""])
def test_every_null_sentinel_spelling_is_an_absent_governing_plan(tmp_path: Path, sentinel: str) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": sentinel})

    assert verdict["value"] == "unsized"


# ---------------------------------------------------------------------------
# sizing_object / governing_plan — Path.__truediv__ does not confine to root
#
# `root / sizing_ref` is not a containment check: an ABSOLUTE `sizing_ref`
# replaces `root` outright, and a `..`-laden relative one walks past it.
# Either shape lets `sizing_object`/`governing_plan` name ANY file that
# happens to exist on disk -- not a sizing artifact at all -- and still earn
# `sized`/`execution`.
# ---------------------------------------------------------------------------


def test_absolute_sizing_object_does_not_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-sizing-escape.yaml"
    outside.write_text("not a sizing object", encoding="utf-8")

    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": str(outside)})

    assert verdict["value"] == "unsized"


def test_traversal_sizing_object_does_not_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-sizing-traversal.yaml"
    outside.write_text("not a sizing object", encoding="utf-8")

    verdict = compute_sizing_disposition(
        tmp_path, {"sizing_object": f"../{outside.name}"}
    )

    assert verdict["value"] == "unsized"


def test_absolute_governing_plan_does_not_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-plan-escape.md"
    outside.write_text("not a plan", encoding="utf-8")

    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": str(outside)})

    assert verdict["value"] == "unsized"
    assert verdict["warning"] is not None


def test_traversal_governing_plan_does_not_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-plan-traversal.md"
    outside.write_text("not a plan", encoding="utf-8")

    verdict = compute_sizing_disposition(tmp_path, {"governing_plan": f"../{outside.name}"})

    assert verdict["value"] == "unsized"


def test_sizing_object_within_root_still_resolves(tmp_path: Path) -> None:
    _write_sizing(tmp_path, "state/sizings/2026-08-20-o.yaml")

    verdict = compute_sizing_disposition(
        tmp_path, {"sizing_object": "state/sizings/2026-08-20-o.yaml"}
    )

    assert verdict["value"] == "sized"


# ---------------------------------------------------------------------------
# `plan_ids` is NOT read as a plan citation -- DR-346 §5 (Correction,
# 2026-08-21) named the read side as the actual defect. Retained here
# unchanged: this leg was not touched by the C4(a) retirement.
# ---------------------------------------------------------------------------


def test_plan_ids_is_still_not_read_as_a_citation_dr346(tmp_path: Path) -> None:
    verdict = compute_sizing_disposition(tmp_path, {"plan_ids": ["pln-p-123456"]})

    assert verdict == {"value": "unsized", "basis": None, "warning": None}
