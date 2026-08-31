"""test_retired_central_receiver_suggestion — a retired central id must be
answered from the successor map, never from edit distance.

THE DEFECT. DoE retired `claude-central-em`, `central-em` and `central` from
`identity.centralReceiverIds` at their b787bf0f0 (2026-08-26). The retirement
is correct and these ids must keep failing at send. What was wrong was the
advisory text AFTER that failure: `suggest_nearest_receiver` fell through to
`difflib.get_close_matches` at cutoff 0.5, and measured against this box's 20
registered repos on 2026-08-31, `claude-central-em` scored closest to
`example-game-workbench-repo-em` and `central-em` to `example-retrieval-repo-em`. The
most-cited dead address in this repo pointed the operator at an unrelated
team — a retirement that reads as a typo, which a tired reader will act on.

WHAT IS DELIBERATELY NOT FIXED HERE, and is the easier thing to get wrong:
nothing in this file makes a retired id RESOLVE. `unique_nearest_receiver`
(the auto-accept surface) does not consult the map at all, because
auto-accepting a retired address would silently redirect the very send the
retirement exists to stop. Only the suggestion changes.

The map is read declaratively from DoE's manifest
(`identity.retiredCentralReceiverIds`, landed their 7f5ff0531) rather than
pinned here, because a second copy of their identity data in this tree is the
drift `identity.redirectAliases` was promoted in 2026-07-21 to prevent.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet import _memo_resolver as R


@pytest.fixture
def repos():
    """A SYNTHETIC registry, not the machine's.

    `read_registry_repos()` returns `{}` under this suite's environment, and a
    suggestion function given no candidates correctly suggests nothing — so
    reading the real registry here would make every assertion below pass or
    fail on whether the harness could see a registry file, which is not the
    subject. The two entries are the minimum this suite needs: the successor
    (so the registered-successor path is reachable) and one unrelated repo (so
    a fuzzy fallthrough has somewhere to land)."""
    return {
        "repos.doe_claude": "/x/DoE-claude",
        "repos.claude_klabauter": "/x/claude-klabauter",
    }


def _map_or_skip():
    retired = R.read_retired_central_receiver_ids()
    if not retired:
        pytest.skip(
            "DoE manifest carries no identity.retiredCentralReceiverIds here — "
            "the reader's documented degradation, not a failure"
        )
    return retired


def test_reader_degrades_to_empty_dict_never_raises(monkeypatch):
    """Same graceful-degradation contract as its two sibling readers: an
    unreadable or field-less manifest yields {}, never an exception, because
    this is consulted on an already-failing resolution path."""
    monkeypatch.setattr(R, "read_doe_identity", lambda: {})
    assert R.read_retired_central_receiver_ids() == {}

    def _boom():
        raise OSError("manifest unreadable")

    monkeypatch.setattr(R, "read_doe_identity", _boom)
    assert R.read_retired_central_receiver_ids() == {}


def test_reader_ignores_malformed_rows(monkeypatch):
    monkeypatch.setattr(
        R,
        "read_doe_identity",
        lambda: {
            "retiredCentralReceiverIds": {
                "Claude-Central-EM": "  DoE-Claude-EM  ",  # normalised
                "blank-successor": "   ",                  # dropped
                "": "doe-claude-em",                       # dropped
                "non-string": 42,                          # dropped
            }
        },
    )
    assert R.read_retired_central_receiver_ids() == {
        "claude-central-em": "doe-claude-em"
    }


def test_reader_tolerates_a_non_mapping_field(monkeypatch):
    monkeypatch.setattr(
        R, "read_doe_identity", lambda: {"retiredCentralReceiverIds": ["a", "b"]}
    )
    assert R.read_retired_central_receiver_ids() == {}


def test_every_retired_id_suggests_its_successor_not_a_fuzzy_match(repos):
    """The regression this file exists for. Asserted against the map's own
    keys rather than a hardcoded list, so adding a fourth retired id upstream
    is covered without editing this test."""
    retired = _map_or_skip()
    for dead, successor in retired.items():
        assert R.suggest_nearest_receiver(dead, repos) == successor, (
            f"{dead!r} should suggest its recorded successor {successor!r}"
        )


def test_a_retired_id_never_auto_accepts_however_small_the_registry(repos):
    """The case that was registry-size dependent before the map was wired in,
    and the reason this is an explicit refusal rather than a happy accident.

    `unique_nearest_receiver` auto-accepts when EXACTLY ONE candidate clears
    the 0.5 cutoff. Against this box's 20 repos, two clear it for
    `claude-central-em`, so the ambiguity gate returned None and the retirement
    looked safe. Against the two-repo registry below — the shape of a
    lightly-registered machine — `doe-claude-em` is the sole match, and the
    gate would have reported it unambiguous and silently accepted a send to an
    id DoE retired. Same code, opposite outcome, decided by how many repos
    happen to be registered."""
    for dead in _map_or_skip():
        assert R.unique_nearest_receiver(dead, repos) is None


def test_a_live_typo_still_auto_accepts(repos):
    """The refusal above must be scoped to retired ids only — the 2026-07-24
    papercut fix (auto-accepting an unambiguous 'claude-klabauter-em' ->
    'claude-klabauter-em') stays intact."""
    assert R.unique_nearest_receiver("claude-klabauter-em", repos) == "claude-klabauter-em"


def test_a_retired_id_is_not_a_central_receiver_id():
    """The map must never be mistaken for a re-alias. Membership in
    `centralReceiverIds` is what makes an id resolve, and no retired id may
    appear there — this is the assertion that fails if someone 'fixes' the
    retirement by promoting the keys back."""
    central = R.read_central_receiver_ids()
    for dead in _map_or_skip():
        assert dead not in central


def test_an_ordinary_typo_still_gets_the_fuzzy_suggestion(repos):
    """The map is consulted first, not instead — non-retired input must reach
    `_nearest_receiver_matches` unchanged."""
    assert R.suggest_nearest_receiver("claude-klabauter-em", repos) == "claude-klabauter-em"


def test_an_unregistered_successor_falls_through_rather_than_being_suggested(
    monkeypatch, repos
):
    """`suggest_nearest_receiver`'s standing invariant is that it never offers
    an id that would ALSO fail to resolve. A successor that is not registered
    on this machine is exactly that, so the map is skipped rather than trading
    a wrong suggestion for an unreachable one."""
    monkeypatch.setattr(
        R,
        "read_retired_central_receiver_ids",
        lambda: {"claude-central-em": "not-registered-anywhere-em"},
    )
    assert R.suggest_nearest_receiver("claude-central-em", repos) != (
        "not-registered-anywhere-em"
    )
