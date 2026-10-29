"""Tests for coordinator_core.workweek_start.header."""

from __future__ import annotations

import pytest

from coordinator_core.workweek_start import header as h


def test_bootstrap_writes_once(tmp_path):
    assert h.bootstrap(tmp_path) is True
    path = tmp_path / "state" / "week-changelog" / "HEADER.md"
    first = path.read_bytes()
    assert b"Week starting:** (not yet set" in first
    path.write_bytes(first + b"extra\n")
    assert h.bootstrap(tmp_path) is False
    assert path.read_bytes() == first + b"extra\n"


def test_reset_moves_dailies_and_fragments_keeps_header_and_pending(tmp_path):
    h.bootstrap(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    (d / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-09-28 (ISO W40)\n"
        "**Prior week released:** v0.6.0 (commit abcdef1, 2026-09-06)\n"
        "**Last /workweek-start:** 2026-09-28\n",
        encoding="utf-8",
    )
    (d / "2026-09-28-host.md").write_text("a", encoding="utf-8")
    (d / "2026-09-29.md").write_text("b", encoding="utf-8")
    (d / "2026-09-30-pending-release.md").write_text("p", encoding="utf-8")
    h.write_priorities_fragment(tmp_path, "abc123", [("T", "state/goals/g.yaml")])

    assert h.reset_week(tmp_path, "2026-10-05") == ("2026-09-28",)

    arch = tmp_path / "archive" / "week-changelogs" / "2026-09-28"
    assert sorted(p.name for p in arch.iterdir()) == [
        "2026-09-28-host.md",
        "2026-09-29.md",
        "HEADER.priorities.abc123.md",
    ]
    assert (d / "2026-09-30-pending-release.md").exists()
    text = (d / "HEADER.md").read_text(encoding="utf-8")
    assert "**Week starting:** 2026-10-05" in text
    assert "**Prior week released:** v0.6.0 (commit abcdef1, 2026-09-06)" in text
    assert not list(d.glob("HEADER.priorities.*"))


def test_reset_archives_each_missed_week_and_keeps_current_week(tmp_path):
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-09-22\n"
        "**Prior week released:** (none recorded)\n**Last /workweek-start:** 2026-09-22\n",
        encoding="utf-8",
    )
    for name in ("2026-09-22-a.md", "2026-09-28-a.md", "2026-10-06-a.md", "2026-10-13-a.md"):
        (d / name).write_text("x", encoding="utf-8")
    h.write_priorities_fragment(tmp_path, "abc123", [("T", "g.yaml")])

    assert h.missed_weeks(tmp_path, "2026-10-14") == ("2026-09-22", "2026-10-06")
    assert h.reset_week(tmp_path, "2026-10-14") == ("2026-09-22", "2026-10-06")

    arch = tmp_path / "archive" / "week-changelogs"
    assert sorted(p.name for p in (arch / "2026-09-22").iterdir()) == [
        "2026-09-22-a.md", "2026-09-28-a.md", "HEADER.priorities.abc123.md"]
    assert [p.name for p in (arch / "2026-10-06").iterdir()] == ["2026-10-06-a.md"]
    assert (d / "2026-10-13-a.md").exists()
    assert "**Week starting:** 2026-10-13" in (d / "HEADER.md").read_text(encoding="utf-8")


def _dailies(tmp_path, *names):
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_text("x", encoding="utf-8")
    return d


def test_missed_weeks_unset_anchor_lists_each_unarchived_completed_week(tmp_path):
    h.bootstrap(tmp_path)
    _dailies(
        tmp_path, "2026-09-29-a.md", "2026-10-07-a.md", "2026-10-08-pending-release.md",
        "2026-10-14-a.md",
    )
    assert h.missed_weeks(tmp_path, "2026-10-15") == ("2026-09-28", "2026-10-05")


def test_missed_weeks_unset_anchor_skips_archived_week_and_uses_its_chain(tmp_path):
    _dailies(tmp_path, "2026-09-29-a.md", "2026-10-07-a.md")
    (tmp_path / "archive" / "week-changelogs" / "2026-09-29").mkdir(parents=True)
    assert h.missed_weeks(tmp_path, "2026-10-20") == ("2026-10-06",)


def test_missed_weeks_unset_anchor_without_dated_files_is_empty(tmp_path):
    h.bootstrap(tmp_path)
    assert h.missed_weeks(tmp_path, "2026-10-15") == ()


def test_missed_weeks_stale_anchor_adds_unarchived_pre_anchor_weeks_only(tmp_path):
    d = _dailies(tmp_path, "2026-09-15-a.md", "2026-09-22-a.md")
    (d / "HEADER.md").write_text("**Week starting:** 2026-09-28\n", encoding="utf-8")
    (tmp_path / "archive" / "week-changelogs" / "2026-09-21").mkdir(parents=True)
    assert h.missed_weeks(tmp_path, "2026-10-14") == ("2026-09-14", "2026-09-28")


def test_reset_inside_header_week_archives_nothing(tmp_path):
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "HEADER.md").write_text("**Week starting:** 2026-10-05\n", encoding="utf-8")
    (d / "2026-10-06.md").write_text("x", encoding="utf-8")
    before = (d / "HEADER.md").read_bytes()
    assert h.reset_week(tmp_path, "2026-10-08") == ()
    assert (d / "HEADER.md").read_bytes() == before
    assert (d / "2026-10-06.md").exists()
    assert "**Week starting:** 2026-10-05" in (d / "HEADER.md").read_text(encoding="utf-8")


def test_current_week_start_uses_header_then_archive_chain(tmp_path):
    h.bootstrap(tmp_path)
    assert h.current_week_start(tmp_path, "2026-10-08") == "2026-10-08"
    (tmp_path / "archive" / "week-changelogs" / "2026-09-28").mkdir(parents=True)
    assert h.current_week_start(tmp_path, "2026-10-08") == "2026-10-05"


def test_reset_on_sentinel_raises(tmp_path):
    h.bootstrap(tmp_path)
    with pytest.raises(h.WeekUnsetError):
        h.reset_week(tmp_path, "2026-10-05")
    assert not (tmp_path / "archive").exists()


def test_update_touches_one_line(tmp_path):
    h.bootstrap(tmp_path)
    path = tmp_path / "state" / "week-changelog" / "HEADER.md"
    before = path.read_text(encoding="utf-8").splitlines()
    h.update_in_place(tmp_path, "2026-10-12")
    after = path.read_text(encoding="utf-8").splitlines()
    diff = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after)
    assert diff == [("**Last /workweek-start:** (none)", "**Last /workweek-start:** 2026-10-12")]


def test_update_appends_line_when_absent(tmp_path):
    path = tmp_path / "state" / "week-changelog" / "HEADER.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Week Changelog\n**Week starting:** 2026-10-05")
    h.update_in_place(tmp_path, "2026-10-12")
    assert path.read_text(encoding="utf-8").splitlines()[-1] == "**Last /workweek-start:** 2026-10-12"


def test_fragment_writer_rejects_path_like_sid(tmp_path):
    for bad in ("../x", "a/b", "", "a.b"):
        with pytest.raises(ValueError):
            h.write_priorities_fragment(tmp_path, bad, [])


def test_fragment_writer_leaves_peer_untouched(tmp_path):
    peer = h.write_priorities_fragment(tmp_path, "peer01", [("P", "g/p.yaml")])
    before = peer.read_bytes()
    h.write_priorities_fragment(tmp_path, "mine01", [("M", "g/m.yaml")])
    assert peer.read_bytes() == before


def test_unfinished_priorities_round_trip(tmp_path):
    assert h.unfinished_priorities(tmp_path) == []
    frag = h.write_priorities_fragment(
        tmp_path, "abc123", [("One", "g/1.yaml"), ("Two — dash", "g/2.yaml"), ("Three", "g/3.yaml")]
    )
    lines = frag.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("- [ ]", "- [x]", 1)
    frag.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert h.unfinished_priorities(tmp_path) == [
        ("One", "g/1.yaml", frag, 1),
        ("Three", "g/3.yaml", frag, 3),
    ]
