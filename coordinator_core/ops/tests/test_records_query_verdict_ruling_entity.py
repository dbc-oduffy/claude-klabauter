"""records.query rows for delivery-verdict and ruling carry path + observed_at and
validate against the cockpit DeliveryVerdictSummary / RulingSummary entities."""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 — populates the op registry
from coordinator_core.contract.cockpit_schema.entities.delivery_verdict_summary import (
    DeliveryVerdictSummary,
)
from coordinator_core.contract.cockpit_schema.entities.ruling_summary import RulingSummary
from coordinator_core.ops.records_query import _handler

_VERDICT = """\
---
kind: delivery-verdict
plan_id: pln-x-111111
head_sha: b4f0251f6774ec0b4311615b45bb8aba9fd8fca2
recorded_at: '2026-10-13T12:26:52.469079Z'
session_id: ''
delivery:
  verdict: PASS
  unbacked: []
criterion:
  status: met
  observation: Falsifier passes.
  sidecar: null
foreign_claims: []
---
VERDICT-BODY-SENTINEL
"""

_FENCED_RULING = """\
---
title: "APM ruling: sizing"
date: 2026-10-08
ruled_by: coordinator:apm
from: apm
created: 2026-10-08
---
RULING-BODY-SENTINEL
"""

_FENCELESS_RULING = "No fence here.\nFENCELESS-BODY-SENTINEL\n"


@pytest.fixture()
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "delivery-verdicts" / "2026-10").mkdir(parents=True)
    (root / "state" / "rulings").mkdir(parents=True)
    (root / "state" / "apm-rulings").mkdir(parents=True)
    (root / "state/delivery-verdicts/2026-10/v.md").write_text(_VERDICT, encoding="utf-8")
    (root / "state/rulings/fenced.md").write_text(_FENCED_RULING, encoding="utf-8")
    (root / "state/apm-rulings/fenceless.md").write_text(_FENCELESS_RULING, encoding="utf-8")
    return root / ".git"


def _rows(repo: Path, record_type: str, **extra) -> list[dict]:
    return _handler(
        params={"type": record_type, "format": "json", "limit": 0, **extra}, repo_root=repo
    )["records"]


def _ruling_entity(row: dict) -> RulingSummary:
    fm = row["frontmatter"]
    return RulingSummary.model_validate({
        "path": row["path"], "observed_at": row["observed_at"],
        "title": fm.get("title"), "from": fm.get("from"),
        "created": fm.get("created"), "source": fm.get("source"),
        "decision": fm.get("decision"), "pm_must_see": fm.get("pm_must_see"),
    })


def test_delivery_verdict_row_validates_against_entity(repo):
    rows = _rows(repo, "delivery-verdict")
    assert len(rows) == 1
    row = rows[0]
    fm = row["frontmatter"]
    DeliveryVerdictSummary.model_validate({
        "path": row["path"],
        "observed_at": row["observed_at"],
        "plan_id": fm.get("plan_id"),
        "head_sha": fm.get("head_sha"),
        "recorded_at": fm.get("recorded_at"),
        "session_id": fm.get("session_id"),
        "delivery": fm["delivery"],
        "criterion": fm["criterion"],
        "foreign_claims": fm["foreign_claims"],
    })


def test_fenced_and_fenceless_rulings_both_served_and_validate(repo):
    rows = {Path(r["path"]).name: r for r in _rows(repo, "ruling")}
    assert set(rows) == {"fenced.md", "fenceless.md"}
    fenced = _ruling_entity(rows["fenced.md"])
    assert fenced.title == "APM ruling: sizing" and fenced.from_ == "apm"
    fenceless = _ruling_entity(rows["fenceless.md"])
    assert fenceless.title is None and fenceless.pm_must_see is None


def test_fenceless_file_of_another_type_is_still_skipped(repo):
    handoffs = repo.parent / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "h.md").write_text("no fence\n", encoding="utf-8")
    assert _rows(repo, "handoff") == []


@pytest.mark.parametrize("record_type", ["delivery-verdict", "ruling"])
def test_observed_at_is_iso_utc_and_body_needs_include_body(repo, record_type):
    rows = _rows(repo, record_type)
    assert rows
    for row in rows:
        assert row["observed_at"].endswith("Z") and "T" in row["observed_at"]
        assert "body" not in row["frontmatter"]
    with_body = _rows(repo, record_type, include_body=True)
    assert with_body and all("BODY-SENTINEL" in r["frontmatter"]["body"] for r in with_body)
    assert all(r["observed_at"] for r in with_body)


def test_other_types_do_not_gain_observed_at(repo):
    handoffs = repo.parent / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "h.md").write_text("---\nkind: spinoff-roadmap\nstatus: open\n---\nb\n",
                                   encoding="utf-8")
    rows = _rows(repo, "handoff")
    assert rows and all("observed_at" not in r for r in rows)
