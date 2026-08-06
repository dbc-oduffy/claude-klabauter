"""Regression test for the 2026-08-05 unguarded-frontmatter-read-in-adopt-
path break-class fix (`state/sizings/2026-08-05-unguarded-frontmatter-read-
in-adopt-prio.yaml`).

Reproduces the live crash: `_adopt_prior_attempt_scaffold_path`'s
per-candidate loop over `state/handoffs/*.md` called `_read_frontmatter`
outside any `try/except`, so a single unreadable (mode 000) or non-UTF8 live
handoff crashed the whole `/handoff` cascade -- `baton-assemble brief` and
`apply` both -- with a raw traceback and exit 1, before any directive could
run. `_scan_deliverable_collision` (same module) already wraps the identical
call in `except (OSError, UnicodeDecodeError): continue` and documents that
the scan never raises -- this test asserts the adopt path is now consistent
with that established pattern, not a new shape.

Spec backlink: `state/sizings/2026-08-05-unguarded-frontmatter-read-in-adopt-
prio.yaml`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _write_artifact

_THIS_RUN_SESSION = "sid-this-run-adopt-unreadable"
_PRED_REL = "state/handoffs/predecessor.md"


@pytest.fixture(autouse=True)
def _this_run_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _pred(root: Path) -> None:
    _write_artifact(
        root / _PRED_REL,
        [
            "handoff_id: HID-PRED",
            "claimed_by: test-session",
            "deployment_state: in_flight",
        ],
    )


def _valid_child(root: Path, name: str) -> Path:
    return _write_artifact(
        root / "state" / "handoffs" / name,
        [
            "handoff_id: HID-SUCC",
            f"predecessor: {_PRED_REL}",
            "predecessor_id: HID-PRED",
            f"authoring_session: {_THIS_RUN_SESSION}",
        ],
    )


class TestAdoptPathSurvivesUnreadableCandidate:
    """Mode-000 candidate alongside a valid one -- the valid candidate must
    still be identified and adopted, not lost to an unguarded crash."""

    def test_unreadable_candidate_is_skipped_valid_one_still_adopted(self, tmp_path):
        _pred(tmp_path)
        valid = _valid_child(tmp_path, "2026-08-05-succ.md")
        unreadable = _write_artifact(
            tmp_path / "state" / "handoffs" / "unreadable.md",
            ["handoff_id: HID-UNREADABLE"],
        )
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip(
                "permission bits do not block reads for root or on this platform"
            )
        os.chmod(unreadable, 0o000)
        try:
            result = ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "HID-PRED", tmp_path)
        finally:
            os.chmod(unreadable, 0o644)

        assert result == str(Path("state") / "handoffs" / valid.name)


class TestAdoptPathSurvivesNonUtf8Candidate:
    """A non-UTF8 byte in a live candidate must not crash the scan either --
    same guard, different exception arm."""

    def test_non_utf8_candidate_is_skipped_valid_one_still_adopted(self, tmp_path):
        _pred(tmp_path)
        valid = _valid_child(tmp_path, "2026-08-05-succ.md")
        bad_bytes = tmp_path / "state" / "handoffs" / "bad-bytes.md"
        bad_bytes.write_bytes(b"---\nhandoff_id: HID-BAD\n---\n\xff\xfe not utf8\n")

        result = ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "HID-PRED", tmp_path)

        assert result == str(Path("state") / "handoffs" / valid.name)
