"""Peer-window withhold: own paths pass, paths older than every live peer pass, newer or undatable
paths are withheld. Everything under `tmp_path` with `os.utime`."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from coordinator_core.session.blanket_withhold import read_peer_starts, withheld_paths
from pathlib import Path

START = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
START_TS = int(START.timestamp())


def _file(root, rel, mtime) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n", encoding="utf-8")
    os.utime(p, (mtime, mtime))


def test_path_written_after_peer_start_is_withheld_older_is_captured(tmp_path: Path) -> None:
    _file(tmp_path, "new.txt", START_TS + 5)
    _file(tmp_path, "old.txt", START_TS - 5)

    got = withheld_paths(tmp_path, ["new.txt", "old.txt"], [], {"peer": START})

    assert got == ["new.txt"]


def test_own_path_is_never_withheld(tmp_path: Path) -> None:
    _file(tmp_path, "mine.txt", START_TS + 5)

    assert withheld_paths(tmp_path, ["mine.txt"], ["mine.txt"], {"peer": START}) == []


def test_boundary_second_is_withheld_with_subsecond_mtime(tmp_path: Path) -> None:
    _file(tmp_path, "edge.txt", START_TS + 0.4)

    assert withheld_paths(tmp_path, ["edge.txt"], [], {"peer": START}) == ["edge.txt"]


def test_earliest_peer_start_sets_the_window(tmp_path: Path) -> None:
    _file(tmp_path, "mid.txt", START_TS + 100)
    later = datetime.fromtimestamp(START_TS + 500, tz=timezone.utc)

    assert withheld_paths(tmp_path, ["mid.txt"], [], {"a": later}) == []
    assert withheld_paths(tmp_path, ["mid.txt"], [], {"a": later, "b": START}) == ["mid.txt"]


def test_deleted_path_withheld_only_when_a_peer_is_live(tmp_path: Path) -> None:
    assert withheld_paths(tmp_path, ["gone.txt"], [], {"peer": START}) == ["gone.txt"]
    assert withheld_paths(tmp_path, ["gone.txt"], [], {}) == []


def test_no_peers_withholds_nothing(tmp_path: Path) -> None:
    _file(tmp_path, "a.txt", START_TS + 5)

    assert withheld_paths(tmp_path, ["a.txt"], [], {}) == []


def test_backslash_separators_normalise(tmp_path: Path) -> None:
    _file(tmp_path, "d/a.txt", START_TS + 5)

    assert withheld_paths(tmp_path, ["d\\a.txt"], ["d/a.txt"], {"peer": START}) == []


def test_read_peer_starts_skips_self_and_parses(tmp_path: Path) -> None:
    for sid, text in (("self", "2026-11-14T12:00:00Z"), ("p1", "2026-11-14T12:00:00Z\n")):
        (tmp_path / sid).mkdir()
        (tmp_path / sid / "started_at").write_text(text, encoding="utf-8")

    got = read_peer_starts(tmp_path, ["self", "p1"], "self")

    assert got == {"p1": START}


def test_unreadable_started_at_counts_as_epoch_and_widens_window(tmp_path: Path) -> None:
    (tmp_path / "missing").mkdir()
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "started_at").write_text("not a date", encoding="utf-8")

    starts = read_peer_starts(tmp_path, ["missing", "junk"], "self")

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert starts == {"missing": epoch, "junk": epoch}
    _file(tmp_path, "ancient.txt", 86400)
    assert withheld_paths(tmp_path, ["ancient.txt"], [], starts) == ["ancient.txt"]
