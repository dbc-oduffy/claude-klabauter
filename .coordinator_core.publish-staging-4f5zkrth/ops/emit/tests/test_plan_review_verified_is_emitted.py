"""PlanSummary.review_verified is populated at collect time, not left absent.

Why this file exists: `review_verified` landed in the contract at 3.13.0 as a
nullable-REQUIRED property under `additionalProperties: false`, but the caller that
constructs PlanSummary records was not wired to populate it. The schema and the producer
disagreed, and nothing caught it — emit's own malformed-quarantine passed all 306 live plan
records while every one of them omitted a required key.

Do NOT expect a consumer to catch this. That was the first reading, and opticon measured it
false (2026-08-20). Their strict schema alone does reject an absent key — `invalid_union` —
but plan rows never reach it bare: `plansHandler` routes every row through `parseLenient`,
whose `fillAbsentNullableKeys` rescues that exact issue shape by filling the key with null.
All 306 records would have been INGESTED, and a rescued null is byte-identical to a genuine
one. They would have read "not review-verified" for every plan, silently and indefinitely,
leaving only drift rows nobody watches. A hard throw would have been the kinder failure.

That is what makes this file load-bearing rather than decorative, and why the assertion to
protect is `test_every_required_key_is_present` below rather than the two value tests: it
checks the emitted record against the vendored schema's own `required` list rather than a
hand-copied field list, so the next widening that ships an unwired required property reds
here. A consumer's leniency layer exists to absorb precisely this class of producer defect,
which makes it structurally incapable of being our alarm. The alarm has to live on this side.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import plans as plans_section

_VENDORED_PLAN_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "emit"
    / "_vendor"
    / "cockpit-contract"
    / "schema"
    / "plan-summary.schema.json"
)


def _make_ctx(tmp_path: Path) -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-08-20T00:00:00Z",
        hostname="test-host",
        repo_name="test-org/test-repo",
    )


def _plan_rec(path: str, **overrides) -> dict:
    fm = {
        "title": "Test Plan",
        "created": "2026-08-01",
        "author": "test-em",
        "status": "draft",
    }
    fm.update(overrides)
    return {"path": path, "frontmatter": fm}


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_attested_plan_emits_true(mock_qr, tmp_path: Path) -> None:
    """`review_verified_by` present on frontmatter -> the predicate reads True."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [
        _plan_rec("docs/plans/2026-08-01-foo.md", review_verified_by="staff-eng")
    ]

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert records[0]["review_verified"] is True


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_unattested_plan_emits_false_not_absent(mock_qr, tmp_path: Path) -> None:
    """No attest field -> False, and the KEY is still present.

    The distinction is the whole bug: a strict consumer rejects an absent required key, so
    `False` and "not there" are not interchangeable here.
    """
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-08-01-foo.md")]

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert "review_verified" in records[0]
    assert records[0]["review_verified"] is False


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_every_required_key_is_present(mock_qr, tmp_path: Path) -> None:
    """The emitted record satisfies the vendored schema's `required` list in full.

    Reads `required` off the vendored bundle rather than restating it, so the next widening
    that ships a required property without producer wiring fails here — at the producer —
    rather than at a downstream consumer's ingest.

    Scoped to collect-time output, so the three fields the enrich pass fills
    (`last_meaningful_activity`, `workstream_type`, `shipped_sha`, `deliverable_status`) are
    present-as-null here by construction; presence, not value, is what this asserts.
    """
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-08-01-foo.md")]

    records, _ = plans_section.collect(ctx)
    required = set(json.loads(_VENDORED_PLAN_SCHEMA.read_text(encoding="utf-8"))["required"])

    emitted = set(records[0]) - {"_supersedes_raw"}
    assert not required - emitted, f"required keys never emitted: {sorted(required - emitted)}"
