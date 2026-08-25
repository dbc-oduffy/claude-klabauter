"""
coordinator_core.ops.tests.conftest

Shared fixture for the C1 (spec_backlink_resolve)/C2 (backfill_deliverable_spine)/
C3 (rewrite_spec_backlinks) chunk trio of
pln-spec-backlinks-cite-a-stable-d-451b3e — one
on-disk corpus fixture, authored once here rather than re-derived three times.

Mirrors real corpus shape verified against this repo's own
docs/plans/2026-08-10-chunk-commit-liveness-becomes-a-nameable.md (plan
frontmatter: `plan_id`, `deliverable_id`, both `pln-<slug>-<6hex>` /
`dlv-<slug>-<6hex>`) and state/sizings/*.yaml (plain YAML, no markdown fence).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def build_spec_backlink_corpus(root: Path) -> dict[str, Path]:
    """Populate `root` with a small corpus resembling this repo's real one.

    Layout built under `root`:
      - docs/plans/<dated-slug>.md        — plan records, frontmatter variants
      - archive/specs/YYYY-MM/<dated-slug>.md — archived spec records, same variants
      - state/sizings/<dated-slug>.yaml   — plain-YAML sizing records
      - a citing file with a path-form spec-backlink citation (in-line, with a
        `§ <anchor>` suffix) plus a decoy prose mention of a docs/plans/ path
        outside any backlink line

    Frontmatter variants covered, per the C1/C2/C3 stub bodies:
      - a real `plan_id` only
      - a real `deliverable_id` only
      - both `plan_id` and `deliverable_id`
      - a literal `null` value for one or both keys
      - no key at all
      - one `dlv-` id deliberately shared by two plan records (the ambiguity
        case), each of those two also carrying a distinct `plan_id`

    Returns a dict of named paths callers commonly need:
      "root", "docs_plans", "archive_specs_dir", "state_sizings",
      "plan_full", "plan_dlv_only", "plan_pln_only", "plan_null_ids",
      "plan_no_ids", "plan_ambiguous_a", "plan_ambiguous_b",
      "archived_full", "archived_no_ids",
      "sizing_with_dlv", "sizing_without_dlv",
      "citing_file".
    """
    docs_plans = root / "docs" / "plans"
    archive_specs_dir = root / "archive" / "specs" / "2026-08"
    state_sizings = root / "state" / "sizings"
    docs_plans.mkdir(parents=True, exist_ok=True)
    archive_specs_dir.mkdir(parents=True, exist_ok=True)
    state_sizings.mkdir(parents=True, exist_ok=True)

    def _plan_frontmatter(
        title: str,
        plan_id: str | None,
        deliverable_id: str | None,
        *,
        plan_id_present: bool = True,
        deliverable_id_present: bool = True,
    ) -> str:
        lines = [
            "---",
            f'title: "{title}"',
            "created: 2026-08-13",
            "author: project-makima-em",
            "status: draft",
        ]
        if plan_id_present:
            value = "null" if plan_id is None else f'"{plan_id}"'
            lines.append(f"plan_id: {value}")
        if deliverable_id_present:
            value = "null" if deliverable_id is None else f'"{deliverable_id}"'
            lines.append(f"deliverable_id: {value}")
        lines.append("scope_mode: feature")
        lines.append("---")
        lines.append("")
        lines.append(f"# {title}")
        lines.append("")
        return "\n".join(lines)

    # -- docs/plans/ variants -------------------------------------------------

    plan_full = docs_plans / "2026-08-13-fixture-plan-full.md"
    plan_full.write_text(
        _plan_frontmatter(
            "fixture plan full",
            "pln-fixture-plan-full-aaaaaa",
            "dlv-fixture-plan-full-bbbbbb",
        ),
        encoding="utf-8",
    )

    plan_dlv_only = docs_plans / "2026-08-13-fixture-plan-dlv-only.md"
    plan_dlv_only.write_text(
        _plan_frontmatter(
            "fixture plan dlv only",
            None,
            "dlv-fixture-plan-dlv-only-cccccc",
            plan_id_present=False,
        ),
        encoding="utf-8",
    )

    plan_pln_only = docs_plans / "2026-08-13-fixture-plan-pln-only.md"
    plan_pln_only.write_text(
        _plan_frontmatter(
            "fixture plan pln only",
            "pln-fixture-plan-pln-only-dddddd",
            None,
            deliverable_id_present=False,
        ),
        encoding="utf-8",
    )

    plan_null_ids = docs_plans / "2026-08-13-fixture-plan-null-ids.md"
    plan_null_ids.write_text(
        _plan_frontmatter("fixture plan null ids", None, None),
        encoding="utf-8",
    )

    plan_no_ids = docs_plans / "2026-08-13-fixture-plan-no-ids.md"
    plan_no_ids.write_text(
        _plan_frontmatter(
            "fixture plan no ids",
            None,
            None,
            plan_id_present=False,
            deliverable_id_present=False,
        ),
        encoding="utf-8",
    )

    # Ambiguity case: two plan records sharing one dlv- id, each also
    # carrying a distinct plan_id (per C1's stub body: "duplicate-id
    # conflict ... resolves via pln- instead").
    shared_dlv = "dlv-fixture-shared-eeeeee"
    plan_ambiguous_a = docs_plans / "2026-08-13-fixture-plan-ambiguous-a.md"
    plan_ambiguous_a.write_text(
        _plan_frontmatter(
            "fixture plan ambiguous a",
            "pln-fixture-plan-ambiguous-a-ffffff",
            shared_dlv,
        ),
        encoding="utf-8",
    )
    plan_ambiguous_b = docs_plans / "2026-08-13-fixture-plan-ambiguous-b.md"
    plan_ambiguous_b.write_text(
        _plan_frontmatter(
            "fixture plan ambiguous b",
            "pln-fixture-plan-ambiguous-b-111111",
            shared_dlv,
        ),
        encoding="utf-8",
    )

    # -- archive/specs/YYYY-MM/ variants --------------------------------------

    archived_full = archive_specs_dir / "2026-08-01-fixture-archived-full.md"
    archived_full.write_text(
        _plan_frontmatter(
            "fixture archived full",
            "pln-fixture-archived-full-222222",
            "dlv-fixture-archived-full-333333",
        ),
        encoding="utf-8",
    )

    archived_no_ids = archive_specs_dir / "2026-08-01-fixture-archived-no-ids.md"
    archived_no_ids.write_text(
        _plan_frontmatter(
            "fixture archived no ids",
            None,
            None,
            plan_id_present=False,
            deliverable_id_present=False,
        ),
        encoding="utf-8",
    )

    # -- state/sizings/*.yaml (plain YAML, no markdown fence) -----------------

    sizing_with_dlv = state_sizings / "2026-08-13-fixture-sizing-with-dlv.yaml"
    sizing_with_dlv.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                'intent: "fixture sizing with a deliverable_id"',
                "intent_source: pm-verbatim",
                'deliverable_id: "dlv-fixture-sizing-with-dlv-444444"',
                "estimate:",
                "  tshirt: XS",
                "  provisional: true",
                "route: dispatch",
                "",
            ]
        ),
        encoding="utf-8",
    )

    sizing_without_dlv = state_sizings / "2026-08-13-fixture-sizing-without-dlv.yaml"
    sizing_without_dlv.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                'intent: "fixture sizing without a deliverable_id"',
                "intent_source: pm-verbatim",
                "estimate:",
                "  tshirt: S",
                "  provisional: true",
                "route: dispatch",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # -- a citing file with a real backlink line + a decoy prose mention -----

    citing_file = root / "coordinator_core" / "fixture_citer.py"
    citing_file.parent.mkdir(parents=True, exist_ok=True)
    citing_file.write_text(
        "\n".join(
            [
                '"""',
                "Fixture module carrying one real spec-backlink citation line and",
                "one decoy prose mention that must NOT be treated as a citation.",
                '"""',
                "",
                "# Spec backlink: docs/plans/2026-08-13-fixture-plan-full.md § AC1",
                "",
                "def note() -> str:",
                '    """See docs/plans/2026-08-13-fixture-plan-full.md for background',
                "    (mentioned here in prose, not as a backlink citation line).",
                '    """',
                '    return "fixture"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    return {
        "root": root,
        "docs_plans": docs_plans,
        "archive_specs_dir": archive_specs_dir,
        "state_sizings": state_sizings,
        "plan_full": plan_full,
        "plan_dlv_only": plan_dlv_only,
        "plan_pln_only": plan_pln_only,
        "plan_null_ids": plan_null_ids,
        "plan_no_ids": plan_no_ids,
        "plan_ambiguous_a": plan_ambiguous_a,
        "plan_ambiguous_b": plan_ambiguous_b,
        "archived_full": archived_full,
        "archived_no_ids": archived_no_ids,
        "sizing_with_dlv": sizing_with_dlv,
        "sizing_without_dlv": sizing_without_dlv,
        "citing_file": citing_file,
    }


@pytest.fixture
def spec_backlink_corpus(tmp_path: Path) -> dict[str, Path]:
    """pytest fixture wrapping `build_spec_backlink_corpus` over a fresh tmp_path."""
    return build_spec_backlink_corpus(tmp_path)
