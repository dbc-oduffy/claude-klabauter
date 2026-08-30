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
    def test_cross_drive_target_skipped_by_containment_gate(self, tmp_path, monkeypatch):
        # Review: C2 code-reviewer — this used to pin the (now-deleted)
        # `except ValueError: continue` around `os.path.relpath`, which
        # passed identically whether the skip came from that except clause
        # or from `_is_within` above it, so it pinned nothing that
        # distinguished the two. Repointed to assert `_is_within` itself
        # rejects the cross-drive target — spying on it directly rather
        # than only on the downstream `calls` list, so this fails if the
        # gate stops being consulted even though the net recording
        # behavior would look the same.
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

        gate_calls = []
        real_is_within = dispatch_checks._is_within

        def spying_is_within(path, root_arg):
            result = real_is_within(path, root_arg)
            gate_calls.append((path, root_arg, result))
            return result

        monkeypatch.setattr(dispatch_checks, "_is_within", spying_is_within)

        dispatch_checks._rm_flush_touch([target], "sess", str(root))

        assert calls == [[]]
        assert gate_calls, "the containment gate was never consulted"
        assert gate_calls[0][2] is False, (
            "the cross-drive target must be rejected by _is_within itself, "
            "not merely end up unrecorded"
        )


class TestRootItselfIsRecorded:
    def test_root_itself_is_recorded_as_dot(self, tmp_path, monkeypatch):
        # Review: C2 code-reviewer — pins a case inspection-clean by reading
        # `_is_within` (p == r returns True for root itself) but previously
        # untested: `root` passed as the deletion target relpaths to ".",
        # which is recorded rather than skipped.
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()

        dispatch_checks._rm_flush_touch([str(root)], "sess", str(root))

        assert calls == [["."]]


class TestRootWithTrailingSeparatorTargetIsRecorded:
    def test_target_under_root_with_trailing_separator_is_recorded(
        self, tmp_path, monkeypatch
    ):
        # Review: C2 code-reviewer — pins the trailing-separator shape on
        # `root` itself; `_is_within`'s `r.rstrip(os.sep) + os.sep` handles
        # this by inspection but it was untested here.
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "sub" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("x\n")

        dispatch_checks._rm_flush_touch(
            [str(target)], "sess", str(root) + os.sep
        )

        assert calls == [["sub/file.txt"]]


class TestDotDotNormalizesBackInsideIsRecorded:
    def test_dot_dot_path_normalizing_inside_root_is_recorded(
        self, tmp_path, monkeypatch
    ):
        # Review: C2 code-reviewer — a target spelled with a `..` segment
        # that normalizes back inside `root` (e.g. `root/sub/../file.txt`)
        # must still be RECORDED: it is in-repo once normalized, and
        # `_is_within` normpaths before comparing, so this is not the
        # out-of-repo shape this gate exists to reject.
        calls = _capture(monkeypatch)
        root = tmp_path / "repo"
        root.mkdir()
        (root / "sub").mkdir()
        target_file = root / "file.txt"
        target_file.write_text("x\n")
        spelled = os.path.join(str(root), "sub", "..", "file.txt")

        dispatch_checks._rm_flush_touch([spelled], "sess", str(root))

        assert calls == [["file.txt"]]


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
