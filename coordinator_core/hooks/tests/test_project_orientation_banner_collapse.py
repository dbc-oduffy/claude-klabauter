"""Collapse of SessionStart banner legs into one count line."""

from __future__ import annotations

from coordinator_core.hooks import project_orientation as po

W1 = "── ⚠ Repo map is stale — run: coordinator-invoke repomap ──\n"
W2 = "── Peer set check is stale (30h) — recheck ──\n"
W3 = "── Corpus currency: docs is behind — refresh: coordinator-invoke remount ──\n"
BLOCK = "── ⚠ Install surface missing on THIS machine: x — run setup ──\n"
INFO = "── Engine: sibling LIVE working tree — this session's hooks resolve here ──\n"
TIER = "── fast: last ran 2h ago ──\n"


def test_no_legs_renders_nothing():
    assert po._collapse_banner([], False) == []


def test_single_warning_passes_through():
    assert po._collapse_banner([W1], False) == [W1]


def test_two_or_more_collapse_to_one_count_line():
    out = po._collapse_banner([W1, "\n", W2, W3], False)
    text = "".join(out)
    assert "3 session warnings" in text
    assert text.count("session warnings") == 1
    assert "coordinator-invoke" not in text
    assert po.BANNER_DETAIL_ENV in text


def test_blocking_and_info_keep_detail_and_are_not_counted():
    out = po._collapse_banner([W1, INFO, BLOCK, W2, TIER], False)
    text = "".join(out)
    assert "2 session warnings" in text
    assert BLOCK in out and INFO in out and TIER in out
    assert W1 not in out and W2 not in out


def test_one_warning_beside_blocking_is_not_collapsed():
    lines = [W1, BLOCK]
    assert po._collapse_banner(lines, False) == lines


def test_identical_warning_lines_are_each_counted():
    out = po._collapse_banner([W1, W1, INFO], False)
    assert "2 session warnings" in "".join(out)
    assert INFO in out and W1 not in out


def test_marker_strings_still_match_their_emitters():
    import inspect

    src = inspect.getsource(po)
    for marker in (*po._BLOCKING_MARKERS, *po._INFO_MARKERS):
        assert marker.replace("── ", "") in src, marker


def test_detail_env_restores_full_text():
    lines = [W1, W2, W3]
    assert po._collapse_banner(lines, True) == lines
