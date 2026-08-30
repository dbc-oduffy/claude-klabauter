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

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C17b.
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
    hl.stamp_liveness(repo_root, hl.ROADMAP_CALLOUT)

    data = json.loads(hl.liveness_path(repo_root).read_text(encoding="utf-8"))
    assert hl.ARCHIVE_SWEEPS in data
    assert hl.ROADMAP_CALLOUT in data


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
    _write_stamp(repo_root, hl.ROADMAP_CALLOUT, age_seconds=30 * 24 * 3600)
    # COMPLETION_SCAFFOLD never stamped at all.

    messages = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert len(messages) == 1
    assert hl.ROADMAP_CALLOUT in messages[0]


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
    _write_stamp(repo_root, hl.ROADMAP_CALLOUT, age_seconds=30 * 24 * 3600)

    detailed = hl.check_stale_detailed(repo_root, stale_threshold_s=7 * 24 * 3600.0)
    plain = hl.check_stale(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert plain == [msg for _cls, msg in detailed]


def test_check_stale_detailed_silent_for_never_stamped_class(tmp_path: Path) -> None:
    repo_root = _git_root(tmp_path)
    assert hl.check_stale_detailed(repo_root) == []


def test_remedy_commands_classes_with_no_cli_are_empty() -> None:
    for cls in (hl.ARCHIVE_SWEEPS, hl.COMPLETION_SCAFFOLD, hl.ROADMAP_CALLOUT):
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
    _write_stamp(repo_root, hl.ROADMAP_CALLOUT, age_seconds=30 * 24 * 3600)
    # COMPLETION_SCAFFOLD never stamped at all.

    statuses = hl.liveness_status(repo_root, stale_threshold_s=7 * 24 * 3600.0)

    assert statuses[hl.ARCHIVE_SWEEPS] == hl.STATUS_FRESH
    assert statuses[hl.ROADMAP_CALLOUT] == hl.STATUS_STALE
    assert statuses[hl.COMPLETION_SCAFFOLD] == hl.STATUS_NEVER_STAMPED


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

    statuses = hl.liveness_status(repo_root, classes=[hl.COMPLETION_SCAFFOLD])

    assert set(statuses.keys()) == {hl.COMPLETION_SCAFFOLD}


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


# --------------------------------------------------------------------------
# C6: git_maintenance -- the first class with a shipped CLI, 10-day threshold
# --------------------------------------------------------------------------


def test_git_maintenance_is_a_known_class():
    assert hl.GIT_MAINTENANCE in hl.KNOWN_CLASSES


def test_git_maintenance_is_the_first_class_with_a_real_remedy():
    """The module's negative-spec forbids INVENTING commands for classes with
    no CLI. This entry is legitimate because coordinator-git-maintenance is on
    disk before the entry names it -- the case that spec anticipates, not an
    exception to it."""
    assert hl.REMEDY_COMMANDS[hl.GIT_MAINTENANCE]
    for cls in hl.KNOWN_CLASSES:
        if cls != hl.GIT_MAINTENANCE:
            assert hl.REMEDY_COMMANDS[cls] == (), cls


def test_git_maintenance_uses_the_ten_day_threshold_not_the_uniform_default():
    """7 days equals the weekly cadence exactly, so the class would read stale
    the instant it came due, every cycle -- a permanently-amber signal nobody
    reads."""
    assert hl._threshold_for(hl.GIT_MAINTENANCE, None) == 10 * 24 * 3600.0
    assert hl._threshold_for(hl.ARCHIVE_SWEEPS, None) == hl._DEFAULT_STALE_THRESHOLD_S


def test_an_explicit_threshold_beats_the_per_class_map():
    """A caller that named a number meant it."""
    assert hl._threshold_for(hl.GIT_MAINTENANCE, 42.0) == 42.0


def test_explicitly_passing_the_default_value_is_still_an_explicit_opt_out():
    """THE BUG THIS PINS, which a value comparison cannot express: every caller
    of check_stale/check_stale_detailed/liveness_status that passes the
    argument at all passes exactly 7*24*3600.0. Detecting "supplied" by
    comparing against _DEFAULT_STALE_THRESHOLD_S reads all of them as "left at
    the default" and silently re-points them at the per-class override they
    were explicitly opting out of. A public keyword argument that is quietly
    ignored when you pass it is worse than any duplicated threshold."""
    assert (
        hl._threshold_for(hl.GIT_MAINTENANCE, hl._DEFAULT_STALE_THRESHOLD_S)
        == hl._DEFAULT_STALE_THRESHOLD_S
    )


def test_an_explicit_seven_day_threshold_reaches_both_accessors(tmp_path):
    """The end-to-end shape of the same bug: a caller asking for 7 days must
    get 7 days from BOTH threshold-consuming accessors, not the 10-day
    per-class override."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state").mkdir()
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    (repo / "state" / "housekeeping-liveness.json").write_text(
        json.dumps({hl.GIT_MAINTENANCE: eight_days_ago}), encoding="utf-8"
    )
    seven_days = 7 * 24 * 3600.0

    detailed = dict(
        hl.check_stale_detailed(
            str(repo), [hl.GIT_MAINTENANCE], stale_threshold_s=seven_days
        )
    )
    status = hl.liveness_status(
        str(repo), [hl.GIT_MAINTENANCE], stale_threshold_s=seven_days
    )

    assert hl.GIT_MAINTENANCE in detailed, detailed
    assert status[hl.GIT_MAINTENANCE] == hl.STATUS_STALE, status


def test_both_accessors_agree_on_git_maintenance_at_day_eight(tmp_path):
    """THE DIVERGENCE THIS GUARDS. Reaching only one threshold-consuming
    accessor would let check_stale_detailed and liveness_status report
    different statuses for the same class on the same day. At day 8 both must
    read FRESH under the 10-day threshold; under the old uniform 7-day default
    both would have read STALE."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state").mkdir()
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    (repo / "state" / "housekeeping-liveness.json").write_text(
        json.dumps({hl.GIT_MAINTENANCE: eight_days_ago}), encoding="utf-8"
    )

    detailed = dict(hl.check_stale_detailed(str(repo), [hl.GIT_MAINTENANCE]))
    status = hl.liveness_status(str(repo), [hl.GIT_MAINTENANCE])

    assert hl.GIT_MAINTENANCE not in detailed, detailed
    assert status[hl.GIT_MAINTENANCE] == hl.STATUS_FRESH, status
