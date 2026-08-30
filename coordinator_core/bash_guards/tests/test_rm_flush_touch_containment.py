"""Containment pin for ``dispatch_checks._rm_flush_touch``.

Plan: docs/plans/2026-08-30-the-guard-s-own-remediation-route-hides.md, C2.

``_rm_flush_touch`` used to relpath every deletion target against ``root``
with no containment check -- its write-side twin
(``write_claim_record.record_write_claims``) has always skipped a target
outside ``root`` via that module's own local ``_is_within`` twin. Closing
that asymmetry matters because ``claim_index.commit_set`` filters neither
dirtiness nor containment (NEGATIVE SPEC, PM ruling 2026-08-21): an
out-of-repo claim that slips through reaches ``safe_commit_offer``'s commit
pathspec, and a PRESENT out-of-repo path there is not benign (see the
function's own docstring for the sighted incident).

The observable this class pins is the ``rels`` list handed to
``append_touch_claims`` -- specifically that no ``../``-prefixed entry ever
reaches it, since that shape is exactly what would reach the commit
pathspec.
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.bash_guards import dispatch_checks


def _capture(monkeypatch):
    calls = []

    def fake_append_touch_claims(paths, session_id, root):
        calls.append(list(paths))

    monkeypatch.setattr(
        "coordinator_core.session.touch_record.append_touch_claims",
        fake_append_touch_claims,
    )
    return calls


class TestInRepoTargetStillRecords:
    def test_in_repo_target_is_recorded(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "sub" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("x\n")

        dispatch_checks._rm_flush_touch([str(target)], "sess", str(root))

        assert calls == [["sub/file.txt"]]


class TestOutOfRepoSameDriveIsSkipped:
    def test_out_of_repo_target_records_nothing(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "outside" / "holder.json"
        outside.parent.mkdir(parents=True)
        outside.write_text("{}\n")

        dispatch_checks._rm_flush_touch([str(outside)], "sess", str(root))

        assert calls == [[]]
        for call in calls:
            for entry in call:
                assert not entry.startswith("../")


class TestCrossDriveTargetSkipped:
    def test_cross_drive_target_records_nothing(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()

        other_drive_root = None
        if sys.platform == "win32":
            import string

            current_drive = os.path.splitdrive(str(root))[0].upper()
            for letter in string.ascii_uppercase:
                candidate = f"{letter}:\\"
                if candidate.upper() != current_drive + "\\" and os.path.isdir(candidate):
                    other_drive_root = candidate
                    break
            if other_drive_root is None:
                pytest.skip("no second drive available on this host")
            target = os.path.join(other_drive_root, "holder.json")
        else:
            pytest.skip("cross-drive relpath ValueError is a Windows-only shape")

        dispatch_checks._rm_flush_touch([target], "sess", str(root))

        assert calls == [[]]


class TestRecordingFailureNeverRaises:
    def test_appender_exception_is_swallowed(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "file.txt"
        target.write_text("x\n")

        def boom(paths, session_id, root):
            raise RuntimeError("sink unavailable")

        monkeypatch.setattr(
            "coordinator_core.session.touch_record.append_touch_claims",
            boom,
        )

        dispatch_checks._rm_flush_touch([str(target)], "sess", str(root))
