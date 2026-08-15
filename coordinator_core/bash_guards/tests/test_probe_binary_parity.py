"""Tests-package-only drift guard between `_shape_classifier.
_SESSION_FACT_PROBE_BINARIES` and the non-echo binaries
`dispatch_checks._bt_probe_segment_kind` recognizes (Review:
coordinator:code-reviewer, Finding 3).

The duplication itself is legitimate -- `dispatch_checks.py` imports FROM
`_shape_classifier.py`, so importing the other direction would create a
real cycle (confirmed by the reviewer reading both modules' imports). But
nothing in production code enforces the two lists stay in sync; a future
probe binary added to one and not the other would silently reopen the gap
this pairing exists to close. This test module can safely import both
without triggering the production import graph the two guard modules
themselves must avoid.
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards._shape_classifier import _SESSION_FACT_PROBE_BINARIES

#: One minimal recognized invocation per `_SESSION_FACT_PROBE_BINARIES`
#: member, chosen to satisfy `_bt_probe_segment_kind`'s own recognized-form
#: requirements (`git` needs a recognized subcommand form; the rest are
#: bare, no-argument invocations).
_MINIMAL_INVOCATION = {
    "git": ["git", "status"],
    "pwd": ["pwd"],
    "whoami": ["whoami"],
    "date": ["date"],
    "uname": ["uname"],
}


def test_every_session_fact_probe_binary_recognized_by_dispatch_checks():
    """Every binary `_shape_classifier` treats as a session-fact probe must
    also be recognized by `dispatch_checks._bt_probe_segment_kind` -- if
    this fails, a probe binary was added to one module's list and not the
    other's.
    """
    assert set(_SESSION_FACT_PROBE_BINARIES) == set(_MINIMAL_INVOCATION), (
        "this test's _MINIMAL_INVOCATION table has drifted from "
        "_shape_classifier._SESSION_FACT_PROBE_BINARIES -- update both"
    )
    for binary in _SESSION_FACT_PROBE_BINARIES:
        tokens = _MINIMAL_INVOCATION[binary]
        kind = dispatch_checks._bt_probe_segment_kind(tokens)
        assert kind is not None, (
            "%r is in _shape_classifier._SESSION_FACT_PROBE_BINARIES but "
            "dispatch_checks._bt_probe_segment_kind does not recognize %r "
            "-- the two probe-binary lists have drifted" % (binary, tokens)
        )


def test_unrecognized_binary_stays_unrecognized_by_both():
    """Sanity check on the parity assertion's own discriminating power: an
    ordinary non-probe binary is recognized by neither side.
    """
    from coordinator_core.bash_guards._shape_classifier import token_matches_binary

    assert not any(token_matches_binary("ls", b) for b in _SESSION_FACT_PROBE_BINARIES)
    assert dispatch_checks._bt_probe_segment_kind(["ls"]) is None
