"""
Tests for `_memo_compose._normalize_supersedes_ref` — the portability of a
`supersedes` reference across the host boundary it is always read over.

WHY THIS HAS ITS OWN FILE. A `supersedes` value is written on the sender's
machine and resolved on the receiver's, which is routinely a different host and
a different platform. An absolute path therefore names nothing where it is
read, and it fails SILENTLY: the superseded memo still sits in the inbox
looking live, so a reader picking by name can action the withdrawn version of a
correction. That is a supersession inverting itself at the point of pickup, and
no other test in this package covers the cross-host direction.

Observed, not hypothetical (doe-claude-em, 2026-08-26): five memos in DoE's
inbox carry a macOS-shaped absolute `supersedes` on a Windows host.

Spec backlink: coordinator_core/ops/fleet/_memo_compose.py ::
_normalize_supersedes_ref.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from coordinator_core.ops.fleet._memo_compose import (  # noqa: E402
    _normalize_supersedes_ref,
    _validate_supersedes_param,
)


# ---------------------------------------------------------------------------
# Portable values pass through untouched. This is the larger obligation of the
# two: the normalizer must be invisible to every value that was already fine.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ref",
    [
        "2026-08-17-example-cockpit-repo-em-settings-home-ladder.md",
        "cross-repo/inbox/2026-08-17-example-cockpit-repo-em-settings-home-ladder.md",
        "cross-repo/archive/2026-08-04-claude-klabauter-em-mint-run-id-verb-shipped.md",
        "state/memo-outbox/sent/split-registration-for-the-bash-guard.md",
        "some-bare-memo-id",
    ],
)
def test_portable_refs_are_returned_verbatim(ref):
    assert _normalize_supersedes_ref(ref) == ref


# ---------------------------------------------------------------------------
# Absolute values are reduced to something the receiver can resolve.
# ---------------------------------------------------------------------------

def test_the_observed_macos_path_on_a_windows_host_becomes_repo_relative():
    """The exact shape doe-claude-em found in DoE's inbox."""
    ref = (
        "/Users/someone/X/DoE-claude/cross-repo/inbox/"
        "2026-08-17-example-cockpit-repo-em-settings-home-ladder-rung-order-is-yours-to-call.md"
    )
    assert _normalize_supersedes_ref(ref) == (
        "cross-repo/inbox/"
        "2026-08-17-example-cockpit-repo-em-settings-home-ladder-rung-order-is-yours-to-call.md"
    )


def test_a_windows_absolute_path_normalizes_the_same_way():
    """Both directions, because either host can be the sender."""
    # abs-path-ok: a synthetic foreign-host path is the fixture under test, not a citation
    ref = r"D:\repos\DoE-claude\cross-repo\inbox\2026-08-17-a-memo.md"
    assert _normalize_supersedes_ref(ref) == "cross-repo/inbox/2026-08-17-a-memo.md"


def test_an_absolute_path_with_no_known_anchor_falls_back_to_the_basename():
    """The basename is what a reader matches on and is recoverable by search."""
    assert _normalize_supersedes_ref("/opt/elsewhere/2026-08-17-a-memo.md") == (
        "2026-08-17-a-memo.md"
    )


def test_the_archive_anchor_is_honoured_not_just_inbox():
    """A supersession can name a memo the receiver has already drained."""
    ref = "/Users/someone/repo/cross-repo/archive/2026-08-04-a-memo.md"
    assert _normalize_supersedes_ref(ref) == "cross-repo/archive/2026-08-04-a-memo.md"


# ---------------------------------------------------------------------------
# The normalizer is reached through the shared validator every memo-composing
# op calls, on both the bare-string and list shapes.
# ---------------------------------------------------------------------------

def test_the_validator_normalizes_a_bare_string():
    value, err = _validate_supersedes_param(
        "draft",
        "/Users/someone/X/DoE-claude/cross-repo/inbox/2026-08-17-a-memo.md",
        dry_run=True,
    )
    assert err is None
    assert value == "cross-repo/inbox/2026-08-17-a-memo.md"


def test_the_validator_normalizes_every_entry_in_a_list():
    value, err = _validate_supersedes_param(
        "draft",
        [
            "/Users/someone/X/DoE-claude/cross-repo/inbox/2026-08-17-first.md",
            "cross-repo/inbox/2026-08-17-second.md",
        ],
        dry_run=True,
    )
    assert err is None
    assert value == [
        "cross-repo/inbox/2026-08-17-first.md",
        "cross-repo/inbox/2026-08-17-second.md",
    ]


def test_normalization_never_rejects():
    """Withholding a correction is worse than an unresolvable reference.

    A `supersedes` the receiver cannot resolve is the bug this function exists
    for; refusing to compose the superseding memo would withhold the correction
    itself, which is strictly worse.
    """
    value, err = _validate_supersedes_param("draft", "/nonsense", dry_run=True)
    assert err is None
    assert value == "nonsense"


def test_absence_and_malformed_entries_keep_their_existing_dispositions():
    """The normalizer must not have changed the rules it sits inside."""
    assert _validate_supersedes_param("draft", "   ", dry_run=True) == (None, None)
    assert _validate_supersedes_param("draft", None, dry_run=True) == (None, None)

    _value, err = _validate_supersedes_param("draft", ["ok", "  "], dry_run=True)
    assert err is not None, "a blank entry inside a list still fails loud"
