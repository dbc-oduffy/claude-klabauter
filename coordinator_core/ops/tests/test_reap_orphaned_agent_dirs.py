
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
    adir = _make_agent_dir(
        sessions_dir / ".agents", "agent-k", legacy_touched=["src/foo.py"]
    )
    monkeypatch.setattr(reaper, "session_live", lambda *a, **k: False)

    assert reaper._read_touched_paths(adir) == []

    verdict = reaper._classify(adir, sessions_dir, {"src/foo.py"}, time.time(), tmp_path)
    assert verdict.candidate is False
    assert "fail-closed" in verdict.reason


def test_read_touched_paths_jsonl_only(sessions_dir):
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
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-n")

    assert reaper._read_touched_paths(adir) == []


def test_read_touched_paths_degraded_read_is_empty(sessions_dir, monkeypatch):
    adir = _make_agent_dir(sessions_dir / ".agents", "agent-o", touched=["src/foo.py"])

    monkeypatch.setattr(
        reaper, "_read_agent_touch_record_as_legacy_lines", lambda sink_path: (["src/foo.py"], True)
    )

    assert reaper._read_touched_paths(adir) == []


def test_touched_path_is_dirty_directory_and_case_helper_directly():
    dirty = {"src/subdir/nested/file.py", "OTHER/File.PY"}

    assert reaper._touched_path_is_dirty(["other/file.py"], dirty) is not None
    assert reaper._touched_path_is_dirty(["src/subdir"], dirty) is not None
    assert reaper._touched_path_is_dirty(["totally/unrelated.py"], dirty) is None


def test_unstattable_legacy_record_fails_closed(sessions_dir, monkeypatch, tmp_path):
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
    adir = sessions_dir / ".agents" / "agent-bare"
    adir.mkdir(parents=True)

    assert reaper._has_unreadable_legacy_record(adir) is False
