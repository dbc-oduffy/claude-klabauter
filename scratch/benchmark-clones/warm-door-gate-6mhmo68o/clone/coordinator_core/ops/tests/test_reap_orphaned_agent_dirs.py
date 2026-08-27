"""
Tests for coordinator_core.ops.reap_orphaned_agent_dirs._classify: the
four-rail (liveness / session-dir / dirty-touched-paths / age) classifier
that decides whether an orphaned per-agent bookkeeping dir is an archival
candidate.

Spec backlink: state/audits/2026-08-14-orphan-agent-dir-reap.md.

Negative-spec: does not exercise main()/CLI argument parsing or the audit
file writer — pure unit coverage of the classifier and dirty-path matcher
against a tmp_path tree, matching sibling test files' fixture idiom (no
real .git/, no daemon spawn).

Review: code-reviewer P1 finding (2026-08-14 slice2) — this module had zero
automated test coverage despite already having run once in apply mode
against the real shared tree; the four rails were entirely unexercised.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops import reap_orphaned_agent_dirs as reaper
from coordinator_core.session import touch_record


def _touch(path: Path, *, age_seconds: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def _make_agent_dir(
    agents_base: Path,
    name: str,
    *,
    owner_id: str = "em-owner-1",
    touched: list = None,
    legacy_touched: list = None,
    age_seconds: float = reaper._AGE_THRESHOLD_SECONDS + 3600,
) -> Path:
    adir = agents_base / name
    adir.mkdir(parents=True)
    if owner_id is not None:
        (adir / "em-session-id.txt").write_text(owner_id, encoding="utf-8")
    if touched:
        # The LIVE record, not the retired `touched.txt`. The writers were
        # repointed to the jsonl sink (pln-the-legacy-touched-txt-record-44ce48
        # C7) and the reader's seam no longer unions the legacy file, so a
        # fixture writing `touched.txt` builds an agent dir the reader reports
        # as having touched NOTHING, so the three R3 dirty-rail tests below
        # asserted against a verdict of "cleared all 4 rails". They failed LOUDLY
        # rather than passing vacuously -- they were among this file's reds, and
        # that is how the gap was found. Review: code-reviewer Finding 7 (P3),
        # correcting an earlier "silently" in this comment.
        # `legacy_touched=` exists for the tests that mean the retired file.
        for _p in touched:
            touch_record.append_event(
                adir / reaper._TOUCH_RECORD_FILENAME,
                session_id=owner_id or "em-owner-1",
                agent_id=None,
                verb=touch_record.VERB_TOUCH,
                path=_p,
            )
    if legacy_touched:
        (adir / "touched.txt").write_text(
            "\n".join(legacy_touched) + "\n",
            encoding="utf-8",
        )
    stamp = time.time() - age_seconds
    os.utime(adir, (stamp, stamp))
    return adir


@pytest.fixture
def sessions_dir(tmp_path):
    d = tmp_path / "coordinator-sessions"
    d.mkdir()
    return d


def test_all_rails_clear_is_a_candidate(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-a")
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is True
    assert "cleared all 4 rails" in verdict.reason


def test_r1_missing_owner_id_fails_closed(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-b", owner_id=None)
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is False
    assert "unknown ownership" in verdict.reason


def test_r1_live_owner_is_not_a_candidate(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-c")
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: True)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is False
    assert "LIVE" in verdict.reason


def test_r1_session_live_exception_fails_closed(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-d")

    def _boom(*a, **k):
        raise RuntimeError("simulated liveness failure")

    monkeypatch.setattr(reaper, "session_live", _boom)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is False
    assert "fail-closed" in verdict.reason


def test_r2_owning_session_dir_still_exists(sessions_dir, monkeypatch, tmp_path):
    (sessions_dir / "em-owner-1").mkdir()
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-e")
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is False
    assert "owning session dir still exists" in verdict.reason


def test_r3_dirty_touched_path_exact_match(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-f", touched=["src/foo.py"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, {"src/foo.py"}, time.time(), tmp_path)

    assert verdict.candidate is False
    assert "still dirty" in verdict.reason


def test_r3_dirty_touched_directory_prefix_match(sessions_dir, monkeypatch, tmp_path):
    """A touched.txt entry naming a directory must still catch file-level
    dirt reported beneath it (code-reviewer R3 finding)."""
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-g", touched=["src/subdir"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(
        adir, sessions_dir, {"src/subdir/nested/file.py"}, time.time(), tmp_path
    )

    assert verdict.candidate is False
    assert "directory prefix match" in verdict.reason


def test_r3_dirty_case_insensitive_match(sessions_dir, monkeypatch, tmp_path):
    """A case difference between touched.txt and git-reported dirt must
    still be treated as dirty on this repo's first-class Windows target
    (code-reviewer R3 finding)."""
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-h", touched=["Src/Foo.py"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, {"src/foo.py"}, time.time(), tmp_path)

    assert verdict.candidate is False
    assert "still dirty" in verdict.reason


def test_r3_dirty_star_sentinel_fails_closed(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-i", touched=["src/foo.py"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, {"*"}, time.time(), tmp_path)

    assert verdict.candidate is False
    assert "fail-closed" in verdict.reason


def test_r4_age_within_threshold_is_not_a_candidate(sessions_dir, monkeypatch, tmp_path):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-j", age_seconds=3600)
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)

    assert verdict.candidate is False
    assert "R4 fails" in verdict.reason


def test_legacy_only_record_reads_empty_and_is_refused_by_r3a(sessions_dir, monkeypatch, tmp_path):
    """A sibling `touched.txt` with no `touch-record.jsonl` no longer reads
    through the seam -- the union was dropped when the writers were repointed
    (pln-the-legacy-touched-txt-record-44ce48 C7).

    This test used to assert that union still held. It is kept, inverted, and
    paired with the consequence that actually matters: an empty read from a
    legacy-only dir does NOT mean the agent touched nothing, and R3 alone would
    clear its dirty rail and let R4 archive it on age -- which a pre-migration
    dir passes by construction. R3a refuses that shape instead. The action
    behind this verdict is `rm -rf`, so fail-closed is the only admissible
    direction.
    """
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-k", legacy_touched=["src/foo.py"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    assert reaper._read_touched_paths(adir) == []

    verdict = reaper._classify(adir, sessions_dir, {"src/foo.py"}, time.time(), tmp_path)
    assert verdict.candidate is False
    assert "fail-closed" in verdict.reason


def test_read_touched_paths_jsonl_only(sessions_dir):
    """C2 — an agent dir with only a `touch-record.jsonl` family (no
    `touched.txt`) is read via the seam's agent-dir dialect adapter."""
    adir = sessions_dir / ".agents" / "agent-l"
    adir.mkdir(parents=True)
    touch_record.append_event(
        adir / reaper._TOUCH_RECORD_FILENAME,
        session_id="em-owner-1",
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path="src/bar.py",
    )

    assert reaper._read_touched_paths(adir) == ["src/bar.py"]


def test_jsonl_is_the_sole_source_when_both_records_exist(sessions_dir):
    """With both records present the jsonl is authoritative and the retired
    `touched.txt` contributes nothing.

    Verified against the live corpus 2026-08-27 before this test was inverted:
    across the 123 agent dirs carrying both records, ZERO live TOUCH claims
    appear in `touched.txt` and not in the jsonl -- the 1,257 legacy-only lines
    are all RELEASE events, which the seam drops by design. Ignoring the legacy
    file here therefore loses no claim. R3a covers the legacy-ONLY shape, which
    is the one that would.
    """
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-m", legacy_touched=["src/legacy.py"]
    )
    touch_record.append_event(
        adir / reaper._TOUCH_RECORD_FILENAME,
        session_id="em-owner-1",
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path="src/jsonl.py",
    )

    assert reaper._read_touched_paths(adir) == ["src/jsonl.py"]


def test_read_touched_paths_absent_record_is_empty(sessions_dir):
    """No `touched.txt` and no `touch-record.jsonl` family — silence, not
    an error, matching the pre-C2 OSError-catch posture."""
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-n")

    assert reaper._read_touched_paths(adir) == []


def test_read_touched_paths_degraded_read_is_empty(sessions_dir, monkeypatch):
    """A degraded read (unreadable/malformed family member) is treated as
    silence — the same safe direction as the prior bare OSError catch: an
    empty touched-paths list only ever clears R3's "still dirty" rail, it
    never widens candidacy."""
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-o", touched=["src/foo.py"])

    monkeypatch.setattr(
        reaper, "_read_agent_touch_record_as_legacy_lines", lambda sink_path: (["src/foo.py"], True)
    )

    assert reaper._read_touched_paths(adir) == []


def test_touched_path_is_dirty_directory_and_case_helper_directly():
    """Direct unit coverage of _touched_path_is_dirty's prefix/case handling,
    independent of _classify's plumbing."""
    dirty = {"src/subdir/nested/file.py", "OTHER/File.PY"}

    # Exact case-insensitive match.
    assert reaper._touched_path_is_dirty(["other/file.py"], dirty) is not None
    # Directory-prefix match.
    assert reaper._touched_path_is_dirty(["src/subdir"], dirty) is not None
    # Clean — no match.
    assert reaper._touched_path_is_dirty(["totally/unrelated.py"], dirty) is None


def test_unstattable_legacy_record_fails_closed(sessions_dir, monkeypatch, tmp_path):
    """`Path.exists()` answers False on ANY stat failure, PermissionError
    included, so an unreadable legacy record reported identically to an absent
    one -- reopening R3a's own hole by a different route. Review: code-reviewer
    Finding 1 (P2). No permission-denied fixture existed before this."""
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-perm")
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    real_exists = reaper.Path.exists
    real_stat = reaper.Path.stat

    def _blind_exists(self, *a, **k):
        if self.name in ("touched.txt", reaper._TOUCH_RECORD_FILENAME):
            return False
        return real_exists(self, *a, **k)

    def _denied_stat(self, *a, **k):
        if self.name == "touched.txt":
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(reaper.Path, "exists", _blind_exists)
    monkeypatch.setattr(reaper.Path, "stat", _denied_stat)

    assert reaper._has_unreadable_legacy_record(adir) is True

    verdict = reaper._classify(adir, sessions_dir, set(), time.time(), tmp_path)
    assert verdict.candidate is False
    assert "fail-closed" in verdict.reason


def test_genuinely_absent_record_is_not_treated_as_unreadable(sessions_dir):
    """The other half: no record at all must NOT trip R3a, or every fresh agent
    dir becomes permanently unreapable."""
    adir = sessions_dir / ".agents" / "agent-bare"
    adir.mkdir(parents=True)

    assert reaper._has_unreadable_legacy_record(adir) is False
