"""Behavioral tests for coordinator_core.write_guards.block_cutover_phase_hand_edit
-- the cutover-record phase advisory guard (see the module's own docstring
for the design decision this is the discharge of).

Mirrors the structure of test_block_consumed_handoff_edit.py: builds a
tmp_path repo layout, monkeypatches `_resolve_git_root` rather than
exercising a real `git init`, and asserts advisory/allow shape plus
advisory-text content (route-first, design-as-offers).

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md (chunk C4d);
CLASS/PRIORITY flip: docs/plans/2026-08-06-apply-guard-class-census.md (C3)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_cutover_phase_hand_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_CUTOVER_PHASE_HAND_EDIT"

_RECORD_BODY = """---
surface: closed_reason
phase: dual-write
confirmed_consumers: []
gate_source:
  kind: value-vocabulary
---

# closed_reason cutover
"""


def _make_repo(tmp_path: Path, record_name: str = "closed-reason-terminal.md",
                body: str = _RECORD_BODY,
                roadmap: str = "lifecycle-vocab") -> tuple[Path, Path]:
    cutovers_dir = tmp_path / "state" / "roadmap" / roadmap / "cutovers"
    cutovers_dir.mkdir(parents=True)
    record_path = cutovers_dir / record_name
    record_path.write_text(body, encoding="utf-8")
    return tmp_path, record_path


def _rel(record_path: Path, repo_root: Path) -> str:
    return str(record_path.relative_to(repo_root)).replace("\\", "/")


def _resolve_root_for(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


class TestFlagsPhaseHandEdit:
    def test_edit_touching_phase_line_flagged(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "old_string": "phase: dual-write",
                "new_string": "phase: retiring",
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "cutover-cli advance" in reason

    def test_advisory_reason_leads_with_route(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "old_string": "phase: dual-write",
                "new_string": "phase: retiring",
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]

        assert "cutover-cli advance" in reason
        assert "override" in reason.lower()

    def test_multiedit_touching_phase_flagged(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        rel = _rel(record_path, repo_root)
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "edits": [
                    {
                        "file_path": rel,
                        "old_string": "phase: dual-write",
                        "new_string": "phase: retiring",
                    }
                ]
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_write_full_rewrite_touching_phase_flagged(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        new_content = _RECORD_BODY.replace("phase: dual-write", "phase: retiring")
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "content": new_content,
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_write_omitting_phase_line_entirely_flagged(self, tmp_path, monkeypatch):
        """Review: code-reviewer — a Write that replaces the whole file with
        content that DROPS the phase: line (rather than changing its value)
        must also be flagged. The old check only inspected the new content
        for a phase:-shaped line, so a deletion went undetected; this uses
        the pre-image to catch "had phase, new content doesn't"."""
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        new_content = "\n".join(
            line for line in _RECORD_BODY.splitlines() if not line.startswith("phase:")
        ) + "\n"
        assert "phase:" not in new_content
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "content": new_content,
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "cutover-cli advance" in reason


class TestDriveRootContainmentGate:
    """Discriminates the drive-root trailing-backslash defect directly:
    a `git_root` of a bare Windows drive root previously composed a
    double-slash `expected_prefix` (`rstrip("/")` does not strip a trailing
    backslash), which never matched the single-slash form `Path.resolve()`
    produces on the candidate side -- the gate went silently inert and
    `_normalize_and_gate` returned `None` for every candidate. Proven to
    fail against the pre-fix `rstrip("/")` spelling before this fix landed.
    """

    def test_drive_root_git_root_still_matches(self):
        drive_root = "X:" + "\\"  # abs-path-ok: synthetic drive-root literal, not a repo path citation
        result = guard._normalize_and_gate(
            "state/roadmap/foo/cutovers/bar.md", drive_root
        )

        assert result is not None


class TestPassThrough:
    def test_edit_of_other_field_passes_through(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "old_string": "confirmed_consumers: []",
                "new_string": "confirmed_consumers: ['archive-stamp-cli']",
            },
            "cwd": str(repo_root),
        }

        assert guard.check(payload) is None

    def test_write_creating_new_record_passes_through(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        cutovers_dir = repo_root / "state" / "roadmap" / "lifecycle-vocab" / "cutovers"
        cutovers_dir.mkdir(parents=True)
        new_record = cutovers_dir / "brand-new.md"
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/roadmap/lifecycle-vocab/cutovers/brand-new.md",
                "content": _RECORD_BODY,
            },
            "cwd": str(repo_root),
        }

        assert not new_record.exists()
        assert guard.check(payload) is None

    def test_non_cutover_file_passes_through(self, tmp_path, monkeypatch):
        repo_root = tmp_path
        other_dir = repo_root / "state" / "roadmap" / "lifecycle-vocab"
        other_dir.mkdir(parents=True)
        other_file = other_dir / "phase.md"
        other_file.write_text("phase: dual-write\n", encoding="utf-8")
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/roadmap/lifecycle-vocab/phase.md",
                "old_string": "phase: dual-write",
                "new_string": "phase: retiring",
            },
            "cwd": str(repo_root),
        }

        assert guard.check(payload) is None

    def test_override_env_set_passes_through(self, tmp_path, monkeypatch):
        repo_root, record_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv(_OVERRIDE_ENV, "1")
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": _rel(record_path, repo_root),
                "old_string": "phase: dual-write",
                "new_string": "phase: retiring",
            },
            "cwd": str(repo_root),
        }

        assert guard.check(payload) is None
