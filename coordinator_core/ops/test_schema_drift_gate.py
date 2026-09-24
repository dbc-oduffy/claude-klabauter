"""Tests for coordinator_core.ops.schema_drift_gate.

Covers the gating reduction of scan_vendored_schema_drift()'s verdict — see
that module's docstring for the op-key/contract: `schema.drift_gate`.

Status -> ok mapping under test (P143-T35: divergence_kind now discriminates
which DRIFT verdicts block):
    DRIFT, any entry divergence_kind == "shape"       -> ok=False (blocking)
    DRIFT, every entry divergence_kind in {"prose-only", None} -> ok=True (advisory)
    MATCH         -> ok=True
    INDETERMINATE -> ok=True (inability to check must never block a merge)
    UNRESOLVED    -> ok=True (no DoE clone on this machine — not applicable)

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
               docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md (P143-T35).
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
    def test_shape_drift_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "improvement-queue.schema.json",
                        "detail": "diverges",
                        "direction": "we-are-behind",
                        "divergence_kind": "shape",
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
                "divergence_kind": "shape",
            }
        ]
        assert "improvement-queue.schema.json" in result["message"]
        assert "we-are-behind" in result["message"]
        assert "re-vendor" in result["message"].lower()

    def test_prose_only_drift_advises_not_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "handoff.schema.json",
                        "detail": "diverges",
                        "direction": "we-are-behind",
                        "divergence_kind": "prose-only",
                    }
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is True
        assert result["status"] == "DRIFT"
        assert "handoff.schema.json" in result["message"]
        assert "prose only" in result["message"]
        assert "not blocking" in result["message"].lower()

    def test_unclassified_divergence_kind_advises_not_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An older advisory build with no `divergence_kind` key must never
        block — absence of evidence is not evidence of shape divergence."""
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "a.schema.json",
                        "detail": "diverges",
                        "direction": "both",
                    }
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is True
        assert result["status"] == "DRIFT"

    def test_mixed_shape_and_prose_only_blocks_and_names_only_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "shape.schema.json",
                        "detail": "d",
                        "direction": "we-are-ahead",
                        "divergence_kind": "shape",
                    },
                    {
                        "schema": "prose.schema.json",
                        "detail": "d",
                        "direction": "both",
                        "divergence_kind": "prose-only",
                    },
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        # Both are still reported in `drifted` (unfiltered pass-through)...
        assert {d["schema"] for d in result["drifted"]} == {"shape.schema.json", "prose.schema.json"}
        # ...but the blocking message names only the shape-drifted entry.
        assert "shape.schema.json" in result["message"]
        assert "prose.schema.json" not in result["message"]

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

    def test_shape_drift_direction_unknown_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A shape-drifted entry with no direction key still renders legibly, not a KeyError."""
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "a.schema.json",
                        "detail": "d",
                        "direction": None,
                        "divergence_kind": "shape",
                    }
                ],
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        assert "direction unknown" in result["message"]

    def test_shape_drift_message_names_a_mirror_fallthrough_degrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Y4 fix — the scan's schemas_dir_degrade_reason must be visible to a
        caller reading only `message`, since a fallthrough and a genuine
        source-tree comparison produce very different drift counts and would
        otherwise look identical (state/bug-backlog/2026-09-09-the-drift-
        scan-has-the-right-rung-and-falls-through-it-silently.yaml)."""
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "handoff.schema.json",
                        "detail": "diverges",
                        "direction": "we-are-behind",
                        "divergence_kind": "shape",
                    }
                ],
                schemas_dir_rung="module-relative",
                schemas_dir_degrade_reason="source-root-unregistered",
            ),
        )

        result = evaluate()

        assert result["ok"] is False
        assert result["schemas_dir_rung"] == "module-relative"
        assert result["schemas_dir_degrade_reason"] == "source-root-unregistered"
        assert "mirror's own copies" in result["message"]
        assert "source-root-unregistered" in result["message"]

    def test_shape_drift_message_silent_on_a_genuine_source_comparison(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[
                    {
                        "schema": "handoff.schema.json",
                        "detail": "diverges",
                        "direction": "we-are-behind",
                        "divergence_kind": "shape",
                    }
                ],
                schemas_dir_rung="engine-source",
                schemas_dir_degrade_reason=None,
            ),
        )

        result = evaluate()

        assert result["schemas_dir_degrade_reason"] is None
        assert "mirror's own copies" not in result["message"]


class TestHandler:
    def test_handler_delegates_to_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_scan(monkeypatch, _report("MATCH"))

        result = _handler({})

        assert result == {
            "ok": True,
            "status": "MATCH",
            "drifted": [],
            "schemas_dir_rung": None,
            "schemas_dir_degrade_reason": None,
            "message": None,
        }
