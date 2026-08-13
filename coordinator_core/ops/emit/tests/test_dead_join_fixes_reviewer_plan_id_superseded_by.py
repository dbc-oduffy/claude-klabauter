"""Regression tests — three dead-join defects in the ``plans``/``handoffs`` emit sections.

Pins the 2026-07-21 fixes (same defect class: the emitter read a key nothing authors, while
the real data sat under a different key or in a file never joined):

  1. ``PlanSummary.reviewer`` — plan frontmatter never authors ``reviewer:`` directly; real
     attribution lives in sibling review-sidecar files (``sections/plans.py::_resolve_reviewer``).
  2. ``HandoffSummary.plan_id`` — handoff frontmatter authors ``origin_plan_id``, never the
     bare ``plan_id`` key the reader used to read (``sections/handoffs.py::collect``).
  3. ``PlanSummary.superseded_by`` — authors write the forward edge ``supersedes:``; the
     backward edge is derived cross-record inside ``sections/plans.py::collect()`` itself, as
     a second pass over the already-built records list (``plans_section._apply_superseded_by``
     — relocated 2026-07-21 from a post-collect ``envelope.py`` enricher; Review: code-reviewer
     Finding 1: this is an intra-section self-join, not a cross-section join, so it belongs in
     the section porter that already has the full plan set in scope).

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P10
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section
from coordinator_core.ops.emit.sections import plans as plans_section
from coordinator_core.ops.emit.sections.plans import _apply_superseded_by


def _make_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-21T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_plan_fm(**overrides) -> dict:
    fm = {
        "title": "Test Plan",
        "created": "2026-07-01",
        "author": "test-em",
        "status": "draft",
    }
    fm.update(overrides)
    return fm


def _plan_rec(path: str, **overrides) -> dict:
    return {"path": path, "frontmatter": _base_plan_fm(**overrides)}


# ---------------------------------------------------------------------------
# FIX 1 — PlanSummary.reviewer joins the review sidecar
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_populates_from_plain_review_sidecar(mock_qr, tmp_path: Path) -> None:
    """A ``.review.md`` sidecar's own ``reviewer:`` field is joined onto the plan record."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.review.md",
        "---\nreviewer: staff-eng\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert records[0]["reviewer"] == "staff-eng"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_absent_when_no_sidecar(mock_qr, tmp_path: Path) -> None:
    """D9 present-as-null: no review sidecar -> reviewer stays null (not quarantined)."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    (tmp_path / "docs/plans").mkdir(parents=True)
    _write(tmp_path / "docs/plans/2026-07-01-foo.md", "---\ntitle: x\n---\n")

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert records[0]["reviewer"] is None


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_ignores_non_review_sidecars(mock_qr, tmp_path: Path) -> None:
    """A ``.plan-coverage-check.md`` / ``.prior-art-check.md`` sidecar (no "review" marker in
    its suffix) must NOT be treated as a reviewer source, even if it happens to carry a
    ``reviewer:``-shaped key."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.plan-coverage-check.md",
        "---\nreviewer: plan-coverage-checker\n---\n\nbody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.prior-art-check.md",
        "---\nreviewer: prior-art-checker\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert records[0]["reviewer"] is None


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_direct_authorship_wins_over_sidecar(mock_qr, tmp_path: Path) -> None:
    """A directly-authored plan-frontmatter ``reviewer:`` always wins over any sidecar join."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md", reviewer="direct-author")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.review.md",
        "---\nreviewer: sidecar-reviewer\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "direct-author"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_precedence_named_reviewer_over_model_reviewer(mock_qr, tmp_path: Path) -> None:
    """When BOTH a the Staff Engineer-review and a sonnet-review sidecar exist (measured: 1/27 plans
    today), the named human/staff reviewer's sidecar wins over the model co-reviewer's."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.sonnet-review.md",
        "---\nreviewer: sonnet-5\n---\n\nbody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.the Staff Engineer-review.md",
        "---\nreviewer: staff-eng\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "staff-eng"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_kind_based_staff_reviewer_outranks_sonnet_review(
    mock_qr, tmp_path: Path
) -> None:
    """Review: code-reviewer Finding 3 — a NAMED staff reviewer with no hardcoded-roster
    marker in its filename (e.g. a the Data Science Reviewer/the UX Reviewer/sid/the Front-End Reviewer-style sidecar) still outranks a
    ``kind: sonnet-review`` sidecar, because ``kind:`` is read directly rather than matched
    against a fixed persona-name list. This is the exact case the old hardcoded
    ``_REVIEWER_SIDECAR_PRIORITY`` roster got wrong."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.sonnet-review.md",
        "---\nreviewer: sonnet-5\nkind: sonnet-review\n---\n\nbody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.the Data Science Reviewer-review.md",
        "---\nreviewer: the Data Science Reviewer\nkind: staff-eng-review\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "the Data Science Reviewer"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_kind_absent_falls_back_to_filename_matching(mock_qr, tmp_path: Path) -> None:
    """A sidecar with frontmatter but no ``kind:`` field falls back to the filename-substring
    tier rather than erroring or silently ranking wrong."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.review.md",
        "---\nreviewer: plain-reviewer\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "plain-reviewer"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_unrecognized_kind_still_outranks_sonnet_review(
    mock_qr, tmp_path: Path
) -> None:
    """An unrecognized non-``sonnet-review`` kind must NOT silently rank below ``sonnet-review``
    (Finding 3's explicit robustness ask)."""
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.sonnet-review.md",
        "---\nreviewer: sonnet-5\nkind: sonnet-review\n---\n\nbody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.review-someone.md",
        "---\nreviewer: someone\nkind: future-staff-kind-nobody-has-seen-yet\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "someone"


# ---------------------------------------------------------------------------
# FIX 2 — HandoffSummary.plan_id reads origin_plan_id (with bare plan_id fallback)
# ---------------------------------------------------------------------------

def _base_handoff_fm(**overrides) -> dict:
    """Contract-valid base frontmatter (2026-07-21 fix): ``status``/``deployment_state``
    are closed Literal enums on ``HandoffSummary`` (``HandoffStatus``/``DeploymentState``)
    and ``created`` is an IsoDate (date-only, no time-of-day) — the previous placeholder
    values here (``status: "open"``, ``deployment_state: "not_shipped"``, a datetime-shaped
    ``created``) were never contract-valid, but went undetected because ``handoffs.collect()``
    built a hand-rolled dict with no shape validation. Now that ``collect()`` routes every
    record through the pydantic model (see ``sections/handoffs.py`` module docstring), an
    invalid fixture like the old one quarantines instead of asserting ``malformed == []``.
    """
    fm = {
        "title": "Test Handoff",
        "created": "2026-07-21",
        "status": "open",
        "deployment_state": "ready_to_fire",
    }
    fm.update(overrides)
    return fm


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_plan_id_reads_origin_plan_id(mock_qr, tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{
                "path": "state/handoffs/x.md",
                "frontmatter": _base_handoff_fm(origin_plan_id="pln-foo-000001"),
            }]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert malformed == []
    assert records[0]["plan_id"] == "pln-foo-000001"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_plan_id_absent_when_neither_key_authored(mock_qr, tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm()}]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert malformed == []
    assert records[0]["plan_id"] is None


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_plan_id_falls_back_to_bare_plan_id_for_legacy_records(mock_qr, tmp_path: Path) -> None:
    """A legacy/future record authoring the bare ``plan_id`` key (no ``origin_plan_id``) is
    still honored — the fallback keeps any such record from silently regressing to null."""
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{
                "path": "state/handoffs/x.md",
                "frontmatter": _base_handoff_fm(plan_id="pln-legacy-000002"),
            }]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert records[0]["plan_id"] == "pln-legacy-000002"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_plan_id_prefers_origin_plan_id_over_bare_plan_id(mock_qr, tmp_path: Path) -> None:
    """When both keys are present, ``origin_plan_id`` (the authoring convention) wins."""
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{
                "path": "state/handoffs/x.md",
                "frontmatter": _base_handoff_fm(
                    origin_plan_id="pln-origin-000003", plan_id="pln-bare-000004"
                ),
            }]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert records[0]["plan_id"] == "pln-origin-000003"


# ---------------------------------------------------------------------------
# FIX 3 — PlanSummary.superseded_by derives the reverse edge of authored `supersedes`
# ---------------------------------------------------------------------------

def _plan(path: str, created: str = "2026-07-01", supersedes=None, superseded_by=None) -> dict:
    return {
        "path": path,
        "created": created,
        "superseded_by": superseded_by,
        "_supersedes_raw": supersedes,
    }


def test_apply_superseded_by_populates_reverse_edge_scalar() -> None:
    """Plan A's scalar ``supersedes: B`` derives B's ``superseded_by`` = A's path."""
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md")

    _apply_superseded_by([a, b])

    assert b["superseded_by"] == "docs/plans/a.md"
    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a
    assert "_supersedes_raw" not in b


def test_apply_superseded_by_populates_reverse_edge_list() -> None:
    """A YAML-list ``supersedes: [B, C]`` derives the reverse edge onto BOTH targets."""
    a = _plan("docs/plans/a.md", supersedes=["docs/plans/b.md", "docs/plans/c.md"])
    b = _plan("docs/plans/b.md")
    c = _plan("docs/plans/c.md")

    _apply_superseded_by([a, b, c])

    assert b["superseded_by"] == "docs/plans/a.md"
    assert c["superseded_by"] == "docs/plans/a.md"


def test_apply_superseded_by_absent_when_not_superseded() -> None:
    """D9 present-as-null: a plan nobody supersedes stays null (not quarantined, not omitted)."""
    a = _plan("docs/plans/a.md")

    _apply_superseded_by([a])

    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a


def test_apply_superseded_by_authored_value_wins_over_derived() -> None:
    """A directly-authored ``superseded_by`` is never overwritten by the derived reverse edge."""
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md", superseded_by="docs/plans/manually-authored.md")

    _apply_superseded_by([a, b])

    assert b["superseded_by"] == "docs/plans/manually-authored.md"


def test_apply_superseded_by_multiple_supersession_newest_created_wins() -> None:
    """When two plans supersede the SAME target, the most-recently-created superseder wins
    (last-supersession-wins); this collision is unexercised in real data today but must
    resolve deterministically."""
    older = _plan("docs/plans/older.md", created="2026-07-01", supersedes="docs/plans/target.md")
    newer = _plan("docs/plans/newer.md", created="2026-07-10", supersedes="docs/plans/target.md")
    target = _plan("docs/plans/target.md")

    _apply_superseded_by([older, newer, target])

    assert target["superseded_by"] == "docs/plans/newer.md"


def test_apply_superseded_by_pops_staging_key_on_every_record_unconditionally() -> None:
    """The ``_supersedes_raw`` staging key must never leak onto the wire — popped from every
    record regardless of whether it produced a reverse edge (strict schema, additionalProperties:
    false)."""
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md")
    c = _plan("docs/plans/c.md", supersedes=None)

    _apply_superseded_by([a, b, c])

    for record in (a, b, c):
        assert "_supersedes_raw" not in record


def test_apply_superseded_by_empty_list_is_noop() -> None:
    _apply_superseded_by([])


def test_apply_superseded_by_self_supersession_guard() -> None:
    """A plan whose own ``supersedes`` names its own path does not set ``superseded_by``
    to itself (Review: code-reviewer Finding 5 — vanishingly unlikely in authored data but
    must not silently corrupt the record)."""
    a = _plan("docs/plans/a.md", supersedes="docs/plans/a.md")

    _apply_superseded_by([a])

    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a


def test_apply_superseded_by_self_supersession_guard_within_list() -> None:
    """A self-target inside a ``supersedes:`` list is dropped; sibling targets still resolve."""
    a = _plan("docs/plans/a.md", supersedes=["docs/plans/a.md", "docs/plans/b.md"])
    b = _plan("docs/plans/b.md")

    _apply_superseded_by([a, b])

    assert a["superseded_by"] is None
    assert b["superseded_by"] == "docs/plans/a.md"
