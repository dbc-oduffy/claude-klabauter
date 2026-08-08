"""
coordinator_core.ops.ceremony.tests.test_housekeeping_liveness

Tests for housekeeping_liveness.py -- the per-class last-run timestamp store (NOT a
registry; see module docstring) that backs C17b's "is this housekeeping class still
being called" staleness check, surfaced through the orientation cache's
``## Housekeeping`` section alongside the housekeeping-failures log.

Coverage:
  (a) stamp_liveness writes a fresh, parseable UTC timestamp for the given class, and
      preserves OTHER classes' existing stamps on a read-modify-write.
  (b) check_stale is silent for a class with NO recorded stamp at all (never wired yet --
      not a staleness signal, per the module's negative-spec).
  (c) check_stale flags a class whose recorded stamp IS older than the threshold, and
      stays silent for one that is fresh.
  (d) check_stale never raises on a missing/corrupt liveness file.

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C17b.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator_core.ops.ceremony import housekeeping_liveness as hl


def _git_root(tmp_path: Path) -> str:
    """Stamp a `.git` directory marker into `tmp_path` and return it as a repo_root str.

    `liveness_path` now validates that `repo_root` is absolute, exists, and resolves to a
    git repo (AC1) -- every fixture below stamps/reads through this seam, so each must be
    a real (if minimal) git repo root, not a bare non-git tmp dir.
    """
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


def test_stamp_liveness_writes_parseable_timestamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)

    hl.stamp_liveness(repo_root, hl.ARCHIVE_SWEEPS)

    path = hl.liveness_path(repo_root)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert hl.ARCHIVE_SWEEPS in data
    # Must parse as an ISO-8601 UTC timestamp close to now.
    ts = datetime.fromisoformat(data[hl.ARCHIVE_SWEEPS].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 30


def test_stamp_liveness_preserves_other_classes(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)

    hl.stamp_liveness(repo_root, hl.ARCHIVE_SWEEPS)
    hl.stamp_liveness(repo_root, hl.TRACKER_REGEN)

    data = json.loads(hl.liveness_path(repo_root).read_text(encoding="utf-8"))
    assert hl.ARCHIVE_SWEEPS in data
    assert hl.TRACKER_REGEN in data


def test_check_stale_silent_for_never_stamped_class(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    # No liveness file at all -- every known class is "never wired", not "stale".
    assert hl.check_stale(repo_root) == []


def _write_stamp(repo_root: str, cls: str, age_seconds: float) -> None:
    path = hl.liveness_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[cls] = ts
    path.write_text(json.dumps(data), encoding="utf-8")


def test_check_stale_flags_old_stamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=30 * 24 * 3600)  # 30 days old

    messages = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert len(messages) == 1
    assert hl.ARCHIVE_SWEEPS in messages[0]
    assert "stale" in messages[0]


def test_check_stale_silent_for_fresh_stamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=60)  # 1 minute old

    messages = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert messages == []


def test_check_stale_mixed_classes(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=60)
    _write_stamp(repo_root, hl.TRACKER_REGEN, age_seconds=30 * 24 * 3600)
    # ROADMAP_CALLOUT / COMPLETION_SCAFFOLD / COVERAGE_GATE never stamped at all.

    messages = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert len(messages) == 1
    assert hl.TRACKER_REGEN in messages[0]


def test_check_stale_never_raises_on_corrupt_file(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    path = hl.liveness_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert hl.check_stale(repo_root) == []


def test_check_stale_flags_unparseable_timestamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    path = hl.liveness_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({hl.ARCHIVE_SWEEPS: "not-a-timestamp"}), encoding="utf-8")

    messages = hl.check_stale(repo_root)

    assert len(messages) == 1
    assert "unparseable" in messages[0]


# ---------------------------------------------------------------------------
# C21 leg 2: check_stale_detailed + REMEDY_COMMANDS
# ---------------------------------------------------------------------------


def test_check_stale_detailed_returns_class_key_and_message(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=30 * 24 * 3600)

    results = hl.check_stale_detailed(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert results == [(hl.ARCHIVE_SWEEPS, results[0][1])]
    assert "stale" in results[0][1]


def test_check_stale_delegates_to_detailed_message_only(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=30 * 24 * 3600)
    _write_stamp(repo_root, hl.TRACKER_REGEN, age_seconds=30 * 24 * 3600)

    detailed = hl.check_stale_detailed(repo_root, stale_threshold_s=7 * 24 * 3600.0)
    plain = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert plain == [msg for _cls, msg in detailed]


def test_check_stale_detailed_silent_for_never_stamped_class(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    assert hl.check_stale_detailed(repo_root) == []


def test_remedy_commands_archive_sweeps_names_all_four_sweep_clis() -> None:
    commands = hl.REMEDY_COMMANDS[hl.ARCHIVE_SWEEPS]
    assert any("sweep-consumed-handoffs.py" in cmd for cmd in commands)
    assert any("sweep-shipped-handoffs.py" in cmd for cmd in commands)
    assert any("sweep-terminal-plans.py" in cmd for cmd in commands)
    assert any("sweep-actioned-memos.py" in cmd for cmd in commands)


def test_remedy_commands_classes_with_no_cli_are_empty() -> None:
    for cls in (hl.COMPLETION_SCAFFOLD, hl.TRACKER_REGEN, hl.ROADMAP_CALLOUT, hl.COVERAGE_GATE):
        assert hl.REMEDY_COMMANDS[cls] == ()


def test_remedy_commands_covers_every_known_class() -> None:
    assert set(hl.REMEDY_COMMANDS.keys()) == set(hl.KNOWN_CLASSES)


# ---------------------------------------------------------------------------
# Three-state contract: liveness_status
# ---------------------------------------------------------------------------


def test_liveness_status_never_stamped_for_all_classes_on_empty_repo(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)

    statuses = hl.liveness_status(repo_root)

    assert set(statuses.keys()) == set(hl.KNOWN_CLASSES)
    assert all(v == hl.STATUS_NEVER_STAMPED for v in statuses.values())


def test_liveness_status_fresh_stamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=60)

    statuses = hl.liveness_status(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert statuses[hl.ARCHIVE_SWEEPS] == hl.STATUS_FRESH


def test_liveness_status_stale_stamp(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=30 * 24 * 3600)

    statuses = hl.liveness_status(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert statuses[hl.ARCHIVE_SWEEPS] == hl.STATUS_STALE


def test_liveness_status_mixed_classes_distinguishes_all_three_states(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    _write_stamp(repo_root, hl.ARCHIVE_SWEEPS, age_seconds=60)
    _write_stamp(repo_root, hl.TRACKER_REGEN, age_seconds=30 * 24 * 3600)
    # COMPLETION_SCAFFOLD / ROADMAP_CALLOUT / COVERAGE_GATE never stamped at all.

    statuses = hl.liveness_status(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert statuses[hl.ARCHIVE_SWEEPS] == hl.STATUS_FRESH
    assert statuses[hl.TRACKER_REGEN] == hl.STATUS_STALE
    for cls in (hl.COMPLETION_SCAFFOLD, hl.ROADMAP_CALLOUT, hl.COVERAGE_GATE):
        assert statuses[cls] == hl.STATUS_NEVER_STAMPED


def test_liveness_status_unparseable_timestamp_is_stale(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    path = hl.liveness_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({hl.ARCHIVE_SWEEPS: "not-a-timestamp"}), encoding="utf-8")

    statuses = hl.liveness_status(repo_root)

    assert statuses[hl.ARCHIVE_SWEEPS] == hl.STATUS_STALE


def test_liveness_status_never_raises_on_corrupt_file(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    path = hl.liveness_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    statuses = hl.liveness_status(repo_root)

    assert all(v == hl.STATUS_NEVER_STAMPED for v in statuses.values())


def test_liveness_status_respects_classes_arg(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)

    statuses = hl.liveness_status(repo_root, classes=[hl.COVERAGE_GATE])

    assert set(statuses.keys()) == {hl.COVERAGE_GATE}


# ---------------------------------------------------------------------------
# repo_root validation (liveness_path raises; stamp_liveness/read paths swallow)
# ---------------------------------------------------------------------------


def test_liveness_path_raises_for_non_absolute_root() -> None:
    import pytest

    with pytest.raises(hl.InvalidLivenessRoot):
        hl.liveness_path("relative/junk")


def test_liveness_path_raises_for_nonexistent_root(tmp_path: Path) -> None:
    import pytest

    missing = tmp_path / "does-not-exist"
    with pytest.raises(hl.InvalidLivenessRoot):
        hl.liveness_path(str(missing))


def test_liveness_path_raises_for_existent_non_git_root(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(hl.InvalidLivenessRoot):
        hl.liveness_path(str(tmp_path))


def test_liveness_path_accepts_git_directory_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    # Must not raise.
    path = hl.liveness_path(str(tmp_path))
    assert path == tmp_path / "state" / "housekeeping-liveness.json"


def test_liveness_path_accepts_git_file_root(tmp_path: Path) -> None:
    # Worktrees/submodules use a `.git` FILE pointing elsewhere, not a directory.
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")

    path = hl.liveness_path(str(tmp_path))
    assert path == tmp_path / "state" / "housekeeping-liveness.json"


def test_stamp_liveness_junk_root_creates_no_filesystem_entry(tmp_path: Path) -> None:
    junk_root = tmp_path / "not-a-repo"
    junk_root.mkdir()

    hl.stamp_liveness(str(junk_root), hl.ARCHIVE_SWEEPS)

    # The directory-absence assertion is the one that matters -- an exception-only test
    # proves nothing (the pre-fix bug already "passed" one, per the chunk brief).
    assert not (junk_root / "state").exists()


def test_stamp_liveness_junk_root_raises_nothing() -> None:
    # stamp_liveness stays "Never raises" for a bogus repo_root too.
    hl.stamp_liveness("relative/junk", hl.ARCHIVE_SWEEPS)


def test_stamp_liveness_junk_root_logs_exactly_one_warning(tmp_path: Path, caplog) -> None:
    junk_root = tmp_path / "not-a-repo"
    junk_root.mkdir()

    with caplog.at_level("WARNING", logger="coordinator_core.ops.ceremony.housekeeping_liveness"):
        hl.stamp_liveness(str(junk_root), hl.ARCHIVE_SWEEPS)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert repr(str(junk_root)) in warnings[0].getMessage()
    assert hl.InvalidLivenessRoot.__name__ in warnings[0].getMessage()


def test_liveness_status_junk_root_reports_never_stamped_for_every_class(tmp_path: Path) -> None:
    junk_root = tmp_path / "not-a-repo"
    junk_root.mkdir()

    statuses = hl.liveness_status(str(junk_root))

    assert set(statuses.keys()) == set(hl.KNOWN_CLASSES)
    assert all(v == hl.STATUS_NEVER_STAMPED for v in statuses.values())


def test_check_stale_junk_root_returns_empty_list_and_does_not_raise(tmp_path: Path) -> None:
    junk_root = tmp_path / "not-a-repo"
    junk_root.mkdir()

    assert hl.check_stale(str(junk_root)) == []


def test_check_stale_detailed_junk_root_returns_empty_list_and_does_not_raise(
    tmp_path: Path,
) -> None:
    junk_root = tmp_path / "not-a-repo"
    junk_root.mkdir()

    assert hl.check_stale_detailed(str(junk_root)) == []
