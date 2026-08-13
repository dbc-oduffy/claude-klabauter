"""Regression tests — DR-084 transitional old->new ingest tolerance in the
``handoffs`` emit section.

The P1/P3 old->new coerce shim, briefly retired at C7 (``5372260e``) on the
premise that every live/archived record was already migrated, is RESTORED-
AS-TRANSITIONAL (2026-07-23): that premise held only for claude-klabauter's own corpus,
not for the consumer repos (example-retrieval-repo, example-cockpit-repo, ...) this section
also ingests from. Old-vocabulary input (``status: active``/``consumed``,
``deployment_state: abandoned``, ``consumed_at``/``consumed_by``) is coerced
UP to the new wire vocabulary at ingest (see ``sections/handoffs.py``'s
``_STATUS_RECOGNIZED``/``_DEPLOYMENT_RECOGNIZED`` module docstring for the
named exit condition); a genuinely unrecognized value (neither old nor new
vocabulary) is per-record quarantined into ``malformed``, not raised — see
the "unrecognized values are per-record quarantined" section below.
``status: superseded`` remains
recognized and still maps to ``claimed`` — a SEPARATE, permanently-
grandfathered 2026-06-26 retirement, not part of the DR-084 axis this module
covers.

Spec backlink: docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md § C7
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section
from coordinator_core.ops.emit.sections import handoff_columns

# The tail of this file (`test_compute_handoff_columns_resolves_shipped_in_
# via_git` and its sibling) resolves `shipped_in` via a real `git log`
# lookup against a throwaway repo -- the production behaviour under test is
# that real git resolution, not a mocked stand-in for it. Most tests above
# it mock `_query_records` only, not git, but the module-level marker covers
# the file uniformly per Rule 2(b).
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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
        observed_at="2026-07-22T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-07-22",
        "status": "open",
        "deployment_state": "ready_to_fire",
    }
    fm.update(overrides)
    return fm


def _collect_with_records(mock_qr, tmp_path: Path, records: list[dict]):
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return records
        return []

    mock_qr.side_effect = query_records
    return handoffs_section.collect(ctx)


# ---------------------------------------------------------------------------
# status axis: new vocabulary passes through; superseded is grandfathered
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_status_open_passes_through_unchanged(mock_qr, tmp_path: Path) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm(status="open")}],
    )

    assert malformed == []
    assert records[0]["status"] == "open"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_status_claimed_passes_through_unchanged(mock_qr, tmp_path: Path) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/y.md",
            "frontmatter": _base_handoff_fm(
                status="claimed", claimed_at="2026-07-22T00:00:00Z", claimed_by="agent-y",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["status"] == "claimed"
    assert records[0]["claimed_at"] == "2026-07-22T00:00:00Z"
    assert records[0]["claimed_by"] == "agent-y"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_status_superseded_still_coerces_to_claimed_grandfathered(mock_qr, tmp_path: Path) -> None:
    """``superseded`` is the permanently-grandfathered 2026-06-26 retirement — a
    SEPARATE axis from DR-084 — and stays recognized post-P4."""
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm(status="superseded")}],
    )

    assert malformed == []
    assert records[0]["status"] == "claimed"


# ---------------------------------------------------------------------------
# status axis: old DR-084 vocabulary is TOLERATED (coerced up to the new wire
# vocabulary at ingest) — transitional shim, restored 2026-07-23
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old_status,expected", [("active", "open"), ("consumed", "claimed")])
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_old_status_vocabulary_coerces_to_new(
    mock_qr, tmp_path: Path, old_status: str, expected: str,
) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm(status=old_status)}],
    )

    assert malformed == []
    assert records[0]["status"] == expected


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_consumed_at_and_consumed_by_fall_back_when_new_names_absent(
    mock_qr, tmp_path: Path,
) -> None:
    """The retired ``consumed_at``/``consumed_by`` field names are read as a
    fallback when the new-named field is absent — a record still carrying
    ONLY the old names still projects onto the NEW wire field names."""
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(
                status="claimed",
                consumed_at="2026-07-22T01:02:03Z",
                consumed_by="agent-x",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["claimed_at"] == "2026-07-22T01:02:03Z"
    assert records[0]["claimed_by"] == "agent-x"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_claimed_at_and_claimed_by_preferred_over_consumed_fallback(
    mock_qr, tmp_path: Path,
) -> None:
    """When both old and new field names are present, the new name wins."""
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(
                status="claimed",
                claimed_at="2026-07-22T09:00:00Z",
                claimed_by="agent-new",
                consumed_at="2026-07-22T01:02:03Z",
                consumed_by="agent-old",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["claimed_at"] == "2026-07-22T09:00:00Z"
    assert records[0]["claimed_by"] == "agent-new"


# ---------------------------------------------------------------------------
# deployment_state axis: new vocabulary passes through
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_deployment_state_continued_passes_through_with_continued_into(
    mock_qr, tmp_path: Path,
) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(
                deployment_state="continued", continued_into="state/handoffs/successor.md",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["deployment_state"] == "continued"
    assert records[0]["continued_into"] == "state/handoffs/successor.md"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_deployment_state_closed_passes_through_with_closed_reason(
    mock_qr, tmp_path: Path,
) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(
                deployment_state="closed", closed_reason="stale",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["deployment_state"] == "closed"
    assert records[0]["closed_reason"] == "stale"


@pytest.mark.parametrize(
    "deployment_state", ["in_flight", "shipped", "awaiting_gate", "ready_to_fire"],
)
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_deployment_state_shared_vocabulary_passes_through_unchanged(
    mock_qr, tmp_path: Path, deployment_state: str,
) -> None:
    overrides: dict = {"deployment_state": deployment_state}
    if deployment_state == "shipped":
        overrides["shipped_in"] = None
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm(**overrides)}],
    )

    assert malformed == []
    assert records[0]["deployment_state"] == deployment_state


# ---------------------------------------------------------------------------
# deployment_state axis: old DR-084 vocabulary is TOLERATED — abandoned splits
# into continued/closed via _coerce_legacy_abandoned, restored 2026-07-23
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_deployment_state_abandoned_without_successor_coerces_to_closed_stale(
    mock_qr, tmp_path: Path,
) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm(deployment_state="abandoned")}],
    )

    assert malformed == []
    assert records[0]["deployment_state"] == "closed"
    assert records[0]["closed_reason"] == "stale"
    assert records[0]["continued_into"] is None


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_deployment_state_abandoned_with_successor_coerces_to_continued(
    mock_qr, tmp_path: Path,
) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(
                deployment_state="abandoned", continued_into="state/handoffs/successor.md",
            ),
        }],
    )

    assert malformed == []
    assert records[0]["deployment_state"] == "continued"
    assert records[0]["continued_into"] == "state/handoffs/successor.md"
    assert records[0]["closed_reason"] is None


# ---------------------------------------------------------------------------
# unrecognized values are per-record quarantined (not a whole-emit hard-abort)
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_unrecognized_status_value_quarantines_record(mock_qr, tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{
                "path": "state/handoffs/x.md",
                "frontmatter": _base_handoff_fm(status="not-a-real-status"),
            }]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "state/handoffs/x.md"
    assert "not-a-real-status" in malformed[0]["reason"]


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_unrecognized_deployment_state_value_quarantines_record(mock_qr, tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [{
                "path": "state/handoffs/x.md",
                "frontmatter": _base_handoff_fm(deployment_state="not-a-real-deployment-state"),
            }]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["path"] == "state/handoffs/x.md"
    assert "not-a-real-deployment-state" in malformed[0]["reason"]


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_unrecognized_value_quarantines_only_the_bad_record(mock_qr, tmp_path: Path) -> None:
    """One record with an unrecognized deployment_state must not take out the rest of
    the corpus — the good record is still emitted, the bad one is quarantined."""
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return [
                {
                    "path": "state/handoffs/good.md",
                    "frontmatter": _base_handoff_fm(deployment_state="ready_to_fire"),
                },
                {
                    "path": "state/handoffs/bad.md",
                    "frontmatter": _base_handoff_fm(deployment_state="record"),
                },
            ]
        return []

    mock_qr.side_effect = query_records

    records, malformed = handoffs_section.collect(ctx)

    assert len(records) == 1
    assert records[0]["provenance"]["path"] == "state/handoffs/good.md"
    assert len(malformed) == 1
    assert malformed[0]["path"] == "state/handoffs/bad.md"
    assert "record" in malformed[0]["reason"]


# ---------------------------------------------------------------------------
# C1: the four-column computation, extracted to handoff_columns.py, is
# callable directly on frontmatter — no EmitContext, no envelope required.
# ---------------------------------------------------------------------------

def _run_git_or_raise(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_columns_test_repo(repo_root: Path) -> None:
    """Init a throwaway git repo with a local identity (no reliance on global git config)."""
    _run_git_or_raise(repo_root, "init", "-q")
    _run_git_or_raise(repo_root, "config", "user.email", "test@example.com")
    _run_git_or_raise(repo_root, "config", "user.name", "Test User")
    _run_git_or_raise(repo_root, "config", "commit.gpgsign", "false")


def test_compute_handoff_columns_takes_bare_frontmatter_and_repo_root(tmp_path: Path) -> None:
    """Direct call — no EmitContext, no envelope. Proves AC1's decoupling: the helper
    only ever needed a repo-root path, not the whole EmitContext/envelope machinery."""
    columns = handoff_columns.compute_handoff_columns(
        {"status": "open", "deployment_state": "ready_to_fire"}, tmp_path,
    )

    assert columns == {
        "status": "open",
        "deployment_state": "ready_to_fire",
        "predecessor": "none",
        "shipped_in": None,
    }


def test_compute_handoff_columns_predecessor_passthrough_when_present(tmp_path: Path) -> None:
    columns = handoff_columns.compute_handoff_columns(
        {"status": "open", "deployment_state": "ready_to_fire", "predecessor": "hnd-foo-abc123"},
        tmp_path,
    )

    assert columns["predecessor"] == "hnd-foo-abc123"


def test_compute_handoff_columns_coerces_legacy_abandoned_without_successor(tmp_path: Path) -> None:
    columns = handoff_columns.compute_handoff_columns(
        {"status": "consumed", "deployment_state": "abandoned"}, tmp_path,
    )

    assert columns["deployment_state"] == "closed"


def test_compute_handoff_columns_coerces_legacy_abandoned_with_successor(tmp_path: Path) -> None:
    columns = handoff_columns.compute_handoff_columns(
        {
            "status": "consumed",
            "deployment_state": "abandoned",
            "continued_into": "state/handoffs/successor.md",
        },
        tmp_path,
    )

    assert columns["deployment_state"] == "continued"


def test_compute_handoff_columns_resolves_shipped_in_via_git(tmp_path: Path) -> None:
    """``shipped_in`` resolution is a real ``git log`` call against the repo-root path — no
    EmitContext needed to drive it, only the raw path this test passes directly."""
    _init_columns_test_repo(tmp_path)
    (tmp_path / "file.txt").write_text("v1")
    _run_git_or_raise(tmp_path, "add", "-A")
    _run_git_or_raise(tmp_path, "commit", "-q", "-m", "first commit")
    sha = _run_git_or_raise(tmp_path, "rev-parse", "HEAD")

    columns = handoff_columns.compute_handoff_columns(
        {"status": "claimed", "deployment_state": "shipped", "shipped_in": sha}, tmp_path,
    )

    assert columns["shipped_in"]["sha"] == sha
    assert columns["shipped_in"]["date"]


def test_compute_handoff_columns_shipped_in_null_when_sha_unresolvable(tmp_path: Path) -> None:
    _init_columns_test_repo(tmp_path)

    columns = handoff_columns.compute_handoff_columns(
        {"status": "claimed", "deployment_state": "shipped", "shipped_in": "0" * 40}, tmp_path,
    )

    assert columns["shipped_in"] is None
