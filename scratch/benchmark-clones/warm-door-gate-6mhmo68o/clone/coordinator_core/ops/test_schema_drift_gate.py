"""Tests for coordinator_core.ops.schema_drift_gate.

Covers the gating reduction of scan_vendored_schema_drift()'s verdict — see
that module's docstring for the op-key/contract: `schema.drift_gate`.

Status -> ok mapping under test:
    DRIFT         -> ok=False (the only blocking case)
    MATCH         -> ok=True
    INDETERMINATE -> ok=True (inability to check must never block a merge)
    UNRESOLVED    -> ok=True (no DoE clone on this machine — not applicable)

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.schema_drift_gate import _handler, evaluate

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _report(status: str, **extra) -> dict:
    base = {
        "status": status,
        "doe_repo_path": "/fake/doe",
        "checked": 12,
        "matched": [],
        "drifted": [],
        "indeterminate": [],
        "summary": f"stub summary for {status}",
    }
    base.update(extra)
    return base


def _patch_scan(monkeypatch: pytest.MonkeyPatch, report: dict) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.schema_drift_gate.scan_vendored_schema_drift",
        lambda: report,
    )


class TestEvaluate:
    def test_drift_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "improvement-queue.schema.json",
                        "detail": "diverges",
                        "direction": "we-are-behind",
                    }
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        assert result["status"] == "DRIFT"
        assert result["drifted"] == [
            {
                "schema": "improvement-queue.schema.json",
                "detail": "diverges",
                "direction": "we-are-behind",
            }
        ]
        assert "improvement-queue.schema.json" in result["message"]
        assert "we-are-behind" in result["message"]
        assert "re-vendor" in result["message"].lower()

    def test_match_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(monkeypatch, _report("MATCH"))

        result = evaluate()

        assert result["ok"] is True
        assert result["status"] == "MATCH"
        assert result["drifted"] == []
        assert result["message"] is None

    def test_indeterminate_passes_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inability to check must never block a merge."""
        _patch_scan(
            monkeypatch,
            _report("INDETERMINATE", summary="could not compare 1/12 vendored schema(s)"),
        )

        result = evaluate()

        assert result["ok"] is True
        assert result["status"] == "INDETERMINATE"
        assert "could not compare" in (result["message"] or "")

    def test_unresolved_passes_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No DoE clone on this machine is not-applicable, not a failure."""
        _patch_scan(
            monkeypatch,
            _report("UNRESOLVED", summary="no DoE clone resolved on this machine"),
        )

        result = evaluate()

        assert result["ok"] is True
        assert result["status"] == "UNRESOLVED"
        assert "no DoE clone" in (result["message"] or "")

    def test_multiple_drifted_schemas_all_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {"schema": "a.schema.json", "detail": "d", "direction": "we-are-ahead"},
                    {"schema": "b.schema.json", "detail": "d", "direction": "both"},
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        assert "a.schema.json" in result["message"] and "we-are-ahead" in result["message"]
        assert "b.schema.json" in result["message"] and "[both]" in result["message"]

    def test_drift_direction_unknown_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A drifted entry with no direction key still renders legibly, not a KeyError."""
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[{"schema": "a.schema.json", "detail": "d", "direction": None}],
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        assert "direction unknown" in result["message"]


class TestHandler:
    def test_handler_delegates_to_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(monkeypatch, _report("MATCH"))

        result = _handler({})

        assert result == {"ok": True, "status": "MATCH", "drifted": [], "message": None}
