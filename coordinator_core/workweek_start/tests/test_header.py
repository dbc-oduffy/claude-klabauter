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

    assert h.reset_week(tmp_path, "2026-10-05") == "2026-09-28"

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
