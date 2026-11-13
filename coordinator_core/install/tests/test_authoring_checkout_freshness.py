"""Decision table, roster filter and advisory line of authoring_checkout_freshness."""

from __future__ import annotations

import pytest

from coordinator_core.install import authoring_checkout_freshness as acf


def _lag(branch="work/x/day", main_sha="b" * 40, ahead=0, behind=3):
    return acf.CheckoutLag("repos.claude-klabauter", "/r", branch, "a" * 40, main_sha, ahead, behind)


def test_current_when_not_behind_even_if_dirty_or_ahead():
    assert acf.decide(_lag(behind=0), ("f.py",), ("run1",)).action == acf.ACTION_CURRENT
    assert acf.decide(_lag(behind=0, ahead=2), (), ()).action == acf.ACTION_CURRENT


def test_fast_forward_when_behind_clean_idle():
    v = acf.decide(_lag(), (), ())
    assert v.action == acf.ACTION_FAST_FORWARD
    assert "3" in v.reason


def test_dirty_but_non_overlapping_fast_forwards():
    # overlap_paths is only the dirty paths the update touches; none means safe.
    assert acf.decide(_lag(), (), ()).action == acf.ACTION_FAST_FORWARD


def test_dirty_overlap_blocks_and_names_paths():
    v = acf.decide(_lag(), ("a.py", "b.py"), ())
    assert v.action == acf.ACTION_BLOCKED_DIRTY_OVERLAP
    assert "a.py" in v.reason and "b.py" in v.reason and "2" in v.reason


def test_overlap_reason_caps_named_paths_and_counts_rest():
    paths = tuple(f"p{i:02d}.py" for i in range(15))
    v = acf.decide(_lag(), paths, ())
    assert "p09.py" in v.reason
    assert "p10.py" not in v.reason
    assert "15" in v.reason and "+5 more" in v.reason


def test_live_run_defers_naming_run_ids():
    v = acf.decide(_lag(), (), ("run-a", "run-b"))
    assert v.action == acf.ACTION_DEFERRED_LIVE_RUN
    assert "run-a" in v.reason and "run-b" in v.reason


def test_live_run_outranks_dirty_overlap():
    assert acf.decide(_lag(), ("a.py",), ("run-a",)).action == acf.ACTION_DEFERRED_LIVE_RUN


def test_diverged_names_counts_and_outranks_live_run_and_overlap():
    v = acf.decide(_lag(ahead=2, behind=5), ("a.py",), ("run-a",))
    assert v.action == acf.ACTION_DIVERGED
    assert "ahead 2" in v.reason and "behind 5" in v.reason


def test_no_main_when_ref_absent():
    assert acf.decide(_lag(main_sha="", behind=0), (), ()).action == acf.ACTION_NO_MAIN


def test_detached_checked_before_no_main_and_never_ff():
    assert acf.decide(_lag(branch="", main_sha=""), (), ()).action == acf.ACTION_DETACHED
    assert acf.decide(_lag(branch=""), (), ()).action == acf.ACTION_DETACHED


@pytest.mark.parametrize(
    "lag,overlap,runs",
    [
        (_lag(), (), ()),
        (_lag(), ("a",), ()),
        (_lag(ahead=1), (), ()),
        (_lag(), (), ("r",)),
        (_lag(main_sha=""), (), ()),
        (_lag(branch=""), (), ()),
        (_lag(behind=0), (), ()),
    ],
)
def test_action_always_in_table(lag, overlap, runs):
    assert acf.decide(lag, overlap, runs).action in acf.ACTIONS


def test_roster_excludes_klabauter_and_sorts():
    reg = {
        "repos.zeta": "/z",
        "repos.claude_klabauter": "/k",
        "repos.alpha": "/a",
    }
    assert acf.authoring_roster(reg) == [("repos.alpha", "/a"), ("repos.zeta", "/z")]


def test_roster_empty_registry():
    assert acf.authoring_roster({}) == []


def test_advisory_none_for_current_and_moved_ff():
    assert acf.advisory_line("h", {"key": "k", "action": "current", "reason": "r"}) is None
    assert (
        acf.advisory_line(
            "h", {"key": "k", "action": "fast_forward", "reason": "r", "moved": True}
        )
        is None
    )


def test_advisory_for_unmoved_ff_and_blockers():
    line = acf.advisory_line(
        "box1", {"key": "repos.m", "action": "fast_forward", "reason": "refused", "moved": False}
    )
    assert line and "box1" in line and "repos.m" in line and "refused" in line
    for action in acf.ACTIONS:
        if action in (acf.ACTION_CURRENT, acf.ACTION_FAST_FORWARD):
            continue
        assert acf.advisory_line("h", {"key": "k", "action": action, "reason": "why"})


def test_advisory_carries_fetch_error():
    line = acf.advisory_line(
        "h",
        {"key": "k", "action": "diverged", "reason": "why", "fetch_error": "fetch failed: x"},
    )
    assert "fetch failed: x" in line
