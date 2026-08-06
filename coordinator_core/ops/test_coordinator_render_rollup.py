"""
Tests for coordinator_core.ops.coordinator_render_rollup — direct-call render
helper.

Mirrors the five contract cases pinned by the example-doctrine-repo-side bash regression net,
plus the CLI-usage and handler-exception fail-open paths that only exist once
the transport hop moves in-process.

Port of: coordinator-render-rollup.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Oracle: test-render-rollup.sh (example-doctrine-repo 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops import coordinator_render_rollup as m


def _patch_common_dir(monkeypatch, common_dir: Path) -> None:
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: common_dir,
    )


def _patch_handler(monkeypatch, result_or_exc):
    def _fake_handler(params, repo_root=None):
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(
        "coordinator_core.ops.deliverable_rollup._handler",
        _fake_handler,
    )


class TestArgParsing:
    def test_missing_both_args(self):
        assert m.main([]) == 1

    def test_missing_repo_root(self):
        assert m.main(["d-001"]) == 1

    def test_empty_deliverable_id(self):
        assert m.main(["", "/fake/repo"]) == 1

    def test_empty_repo_root(self):
        assert m.main(["d-001", ""]) == 1


class TestRenderContract:
    """Mirrors the five cases in the example-doctrine-repo bash regression net (test-render-rollup.sh)."""

    def test_empty_advances_initiatives_no_stdout(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-001",
                "resolution_mode": "direct",
                "artifacts_matched": 0,
                "advances_initiatives": [],
            },
        )
        rc = m.main(["d-001", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""

    def test_one_initiative_one_line(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-002",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "advances_initiatives": [
                    {"id": "claude-klabauter-strangler", "label": "Claude-Klabauter Strangler", "status": "active"}
                ],
            },
        )
        rc = m.main(["d-002", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == "advances initiative Claude-Klabauter Strangler (claude-klabauter-strangler)\n"

    def test_duplicate_id_deduped(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-003",
                "resolution_mode": "direct",
                "artifacts_matched": 3,
                "advances_initiatives": [
                    {"id": "alpha", "label": "Alpha Initiative", "status": "active"},
                    {"id": "beta", "label": "Beta Initiative", "status": None},
                    {"id": "alpha", "label": "Alpha Initiative", "status": "active"},
                ],
            },
        )
        rc = m.main(["d-003", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == (
            "advances initiative Alpha Initiative (alpha)\n"
            "advances initiative Beta Initiative (beta)\n"
        )

    def test_handler_exception_fail_open_no_stdout(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(monkeypatch, AttributeError("'tuple' object has no attribute 'get'"))
        rc = m.main(["d-001", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""
        assert "d-001" in out.err

    def test_scan_incomplete_absent_unchanged(self, monkeypatch, tmp_path, capsys):
        """No `scan_incomplete` key on the payload (today's frozen v1.0 wire shape)
        renders exactly as before the additive widen -- absent == False, no
        qualifier appended."""
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-007",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "advances_initiatives": [
                    {"id": "claude-klabauter-strangler", "label": "Claude-Klabauter Strangler", "status": "active"}
                ],
            },
        )
        rc = m.main(["d-007", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == "advances initiative Claude-Klabauter Strangler (claude-klabauter-strangler)\n"

    def test_scan_incomplete_true_appends_partial_scan_qualifier(
        self, monkeypatch, tmp_path, capsys
    ):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-008",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "scan_incomplete": True,
                "advances_initiatives": [
                    {"id": "claude-klabauter-strangler", "label": "Claude-Klabauter Strangler", "status": "active"}
                ],
            },
        )
        rc = m.main(["d-008", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == (
            "advances initiative Claude-Klabauter Strangler (claude-klabauter-strangler) (partial scan)\n"
        )

    def test_scan_incomplete_false_explicit_unchanged(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-009",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "scan_incomplete": False,
                "advances_initiatives": [
                    {"id": "claude-klabauter-strangler", "label": "Claude-Klabauter Strangler", "status": "active"}
                ],
            },
        )
        rc = m.main(["d-009", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == "advances initiative Claude-Klabauter Strangler (claude-klabauter-strangler)\n"

    def test_scan_incomplete_truthy_non_bool_does_not_trip_qualifier(
        self, monkeypatch, tmp_path, capsys
    ):
        """Review: code-reviewer -- Finding 8. `scan_incomplete` is `is True`-gated,
        not `bool(...)`-coerced, so a future non-bool truthy sentinel (e.g. a
        string like "false") fails to trip "(partial scan)" rather than
        silently rendering it -- `bool("false")` is `True` in Python, which is
        exactly the footgun this strict check closes."""
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-010",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "scan_incomplete": "false",
                "advances_initiatives": [
                    {"id": "claude-klabauter-strangler", "label": "Claude-Klabauter Strangler", "status": "active"}
                ],
            },
        )
        rc = m.main(["d-010", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == "advances initiative Claude-Klabauter Strangler (claude-klabauter-strangler)\n"

    def test_transitive_resolution_mode_no_stdout(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-005",
                "resolution_mode": "transitive",
                "artifacts_matched": 1,
                "advances_initiatives": [{"id": "alpha", "label": "Alpha", "status": None}],
            },
        )
        rc = m.main(["d-005", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""


class TestFailOpenPaths:
    def test_repo_root_resolution_failure(self, monkeypatch, capsys):
        def _raise(repo_root):
            raise RuntimeError("not a git repo")

        monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", _raise)
        rc = m.main(["d-001", "/nonexistent/path/xyz"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""
        assert "d-001" in out.err

    def test_non_dict_result_fail_open(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(monkeypatch, ("not", "a", "dict"))
        rc = m.main(["d-001", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""

    def test_label_falls_back_to_id_when_absent(self, monkeypatch, tmp_path, capsys):
        _patch_common_dir(monkeypatch, tmp_path)
        _patch_handler(
            monkeypatch,
            {
                "deliverable_id": "d-006",
                "resolution_mode": "direct",
                "artifacts_matched": 1,
                "advances_initiatives": [{"id": "gamma", "label": None, "status": None}],
            },
        )
        rc = m.main(["d-006", "/fake/repo"])
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == "advances initiative gamma (gamma)\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
