
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


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_populates_from_plain_review_sidecar(mock_qr, tmp_path: Path) -> None:
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
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    (tmp_path / "docs/plans").mkdir(parents=True)
    _write(tmp_path / "docs/plans/2026-07-01-foo.md", "---\ntitle: x\n---\n")

    records, malformed = plans_section.collect(ctx)

    assert malformed == []
    assert records[0]["reviewer"] is None


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_ignores_non_review_sidecars(mock_qr, tmp_path: Path) -> None:
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
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = [_plan_rec("docs/plans/2026-07-01-foo.md")]
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.sonnet-review.md",
        "---\nreviewer: sonnet-5\n---\n\nbody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-07-01-foo.patrik-review.md",
        "---\nreviewer: staff-eng\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "staff-eng"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_kind_based_staff_reviewer_outranks_sonnet_review(
    mock_qr, tmp_path: Path
) -> None:
    """A NAMED staff reviewer with no hardcoded-roster
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
        tmp_path / "docs/plans/2026-07-01-foo.camelia-review.md",
        "---\nreviewer: the Data Science Reviewer\nkind: staff-eng-review\n---\n\nbody\n",
    )

    records, malformed = plans_section.collect(ctx)

    assert records[0]["reviewer"] == "camelia"


@patch("coordinator_core.ops.emit.sections.plans._query_plan_records")
def test_reviewer_join_kind_absent_falls_back_to_filename_matching(mock_qr, tmp_path: Path) -> None:
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


def _base_handoff_fm(**overrides) -> dict:
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


def _plan(path: str, created: str = "2026-07-01", supersedes=None, superseded_by=None) -> dict:
    return {
        "path": path,
        "created": created,
        "superseded_by": superseded_by,
        "_supersedes_raw": supersedes,
    }


def test_apply_superseded_by_populates_reverse_edge_scalar() -> None:
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md")

    _apply_superseded_by([a, b])

    assert b["superseded_by"] == "docs/plans/a.md"
    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a
    assert "_supersedes_raw" not in b


def test_apply_superseded_by_populates_reverse_edge_list() -> None:
    a = _plan("docs/plans/a.md", supersedes=["docs/plans/b.md", "docs/plans/c.md"])
    b = _plan("docs/plans/b.md")
    c = _plan("docs/plans/c.md")

    _apply_superseded_by([a, b, c])

    assert b["superseded_by"] == "docs/plans/a.md"
    assert c["superseded_by"] == "docs/plans/a.md"


def test_apply_superseded_by_absent_when_not_superseded() -> None:
    a = _plan("docs/plans/a.md")

    _apply_superseded_by([a])

    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a


def test_apply_superseded_by_authored_value_wins_over_derived() -> None:
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md", superseded_by="docs/plans/manually-authored.md")

    _apply_superseded_by([a, b])

    assert b["superseded_by"] == "docs/plans/manually-authored.md"


def test_apply_superseded_by_multiple_supersession_newest_created_wins() -> None:
    older = _plan("docs/plans/older.md", created="2026-07-01", supersedes="docs/plans/target.md")
    newer = _plan("docs/plans/newer.md", created="2026-07-10", supersedes="docs/plans/target.md")
    target = _plan("docs/plans/target.md")

    _apply_superseded_by([older, newer, target])

    assert target["superseded_by"] == "docs/plans/newer.md"


def test_apply_superseded_by_pops_staging_key_on_every_record_unconditionally() -> None:
    a = _plan("docs/plans/a.md", supersedes="docs/plans/b.md")
    b = _plan("docs/plans/b.md")
    c = _plan("docs/plans/c.md", supersedes=None)

    _apply_superseded_by([a, b, c])

    for record in (a, b, c):
        assert "_supersedes_raw" not in record


def test_apply_superseded_by_empty_list_is_noop() -> None:
    _apply_superseded_by([])


def test_apply_superseded_by_self_supersession_guard() -> None:
    a = _plan("docs/plans/a.md", supersedes="docs/plans/a.md")

    _apply_superseded_by([a])

    assert a["superseded_by"] is None
    assert "_supersedes_raw" not in a


def test_apply_superseded_by_self_supersession_guard_within_list() -> None:
    a = _plan("docs/plans/a.md", supersedes=["docs/plans/a.md", "docs/plans/b.md"])
    b = _plan("docs/plans/b.md")

    _apply_superseded_by([a, b])

    assert a["superseded_by"] is None
    assert b["superseded_by"] == "docs/plans/a.md"
