"""Point 4's by-sink narrowing: the assignment-indirection leg must deny every
real indirected write and stop denying reads whose write lands elsewhere.

Purpose: point 4 denied on "a segment assigns a governed path to a variable"
AND "a write marker exists anywhere in the whole command". The second conjunct
never asked what the write TARGETS, so reading a governed file through a
variable while writing to an unrelated path was denied. Three sessions tripped
it independently on 2026-08-28 (see
`state/bug-backlog/2026-08-29-the-doctrine-surface-port-denies-a-read-f93a6164ec3f.yaml`),
and the session that landed this fix tripped it a fourth time trying to write
the patch itself -- the docstring quotes the governed name.

The DENY corpus below is the load-bearing half. A narrowing of a security guard
is only as good as the attacks it still refuses, so every indirection shape that
motivated point 4 is pinned here explicitly -- including the alias chain
(`q=$p`), which the narrowing must follow rather than treat as an unbound name.

Ported from DoE-claude `9d1404fa6`'s
`coordinator/tests/test_guard_doctrine_surface_point4_by_sink.py`. The one
divergence: DoE reads its governed surface off an import-time constant, while
this port takes the caller's resolved identifier tuple per call (module
docstring, "GOVERNED IDENTIFIER SOURCE"), so the corpus supplies its own
surfaces and a separate case pins that choice against the live manifest
whenever a DoE-claude sibling checkout is resolvable -- rather than skipping
the whole corpus on an install that has none.

Negative-spec: this file does NOT assert on deny TEXT (that is the message
composer's own concern, and `test_folded_guard_transport_parity.py` compares it
against the cold oracle separately), and it does NOT cover point 3 -- a
single-segment interpreter payload is a DIFFERENT leg, still denying on both
sides, and `_has_write_marker_for_point3` already narrowed the redirect shape
there. The two must not be folded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

#: The governed surfaces this corpus runs against. Held here rather than read
#: from disk so the DENY half never skips: an install with no sibling checkout
#: still gets the full regression cover. `test_the_corpus_surface_is_live`
#: below is what stops this list rotting silently.
SURFACES = [
    "global-doctrine/CLAUDE.md",
    "CLAUDE.md",
    "coordinator/snippets/em-operating-doctrine.md",
    "coordinator/snippets/agent-role-dispatched.md",
]
IDENTIFIERS = guard._governed_identifiers_lower(SURFACES)
GOV = SURFACES[1]


DENY_CASES = [
    ("direct redirect", f"echo x > {GOV}"),
    ("quoted redirect", f'echo x > "{GOV}"'),
    ("assign then redirect through the name", f"p={GOV} ; echo corrupted > $p"),
    ("brace deref", f"p={GOV} ; echo corrupted > ${{p}}"),
    ("quoted deref", f'p={GOV} ; echo corrupted > "$p"'),
    ("alias chain", f"p={GOV} ; q=$p ; echo x > $q"),
    ("append through the name", f"p={GOV} ; echo x >> $p"),
    ("tee through the name", f"p={GOV} ; echo x | tee $p"),
    ("cp onto the name", f"p={GOV} ; cp /tmp/evil $p"),
    ("mv onto the name", f"p={GOV} ; mv /tmp/evil $p"),
    ("sed -i through the name", f"p={GOV} ; sed -i s/a/b/ $p"),
    ("tee direct", f"echo x | tee {GOV}"),
    ("xargs stdin indirection", f"echo {GOV} | xargs tee"),
    ("unanalysable marker rides along a benign redirect", f"p={GOV} ; echo x | tee $p > /tmp/o"),
    ("interpreter write", f"python3 -c \"open('{GOV}','w').write('x')\""),
    ("interpreter write through the name", f"p={GOV} ; python3 -c \"open('$p','w').write('x')\""),
]

#: EXACTLY ONE of these is regression cover; the other three are CONTROLS that
#: were already allowed before the narrowing. Measured against a reconstruction
#: of the old predicate, not assumed -- the claim this corpus arrived with was
#: that all four flipped, which was a generalization from the two
#: assignment-bearing shapes and was wrong. The controls still earn their place:
#: they pin that the narrowing did not disturb the shapes point 4 never engaged
#: on. But do not read four green ticks as four regressions caught.
ALLOW_CASES = [
    ("read through a variable, write to scratch", f"p={GOV} ; cat $p ; echo x > /tmp/probe.txt"),
    ("read directly, write to scratch", f"cat {GOV} ; echo x > /tmp/probe.txt"),
    ("pure read", f"grep -n heading {GOV}"),
    ("governed name as quoted prose, scratch redirect", f'printf "%s" "{GOV} holds the budget" > /tmp/msg.txt'),
]

#: The subset point 4 actually engaged on before the fix -- the only entries whose
#: green proves the narrowing did something.
REGRESSION_COVER = {"read through a variable, write to scratch"}


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_real_governed_writes_still_deny(label: str, cmd: str) -> None:
    """The regression half. Every one of these writes a governed surface, and
    the by-sink narrowing must not have opened any of them."""
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, (
        f"{label}: the by-sink narrowing opened a real governed write -- {cmd!r}"
    )


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_reads_whose_write_lands_elsewhere_are_allowed(label: str, cmd: str) -> None:
    """The fix half. The governed surface is only READ; the sole write targets
    an unrelated path."""
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, (
        f"{label}: denied a read whose write target is not governed -- {cmd!r}"
    )


def test_the_minimal_pair_differs_only_by_the_assignment() -> None:
    """The discriminator that isolated the defect: identical governed mention,
    identical unrelated write, differing only in whether the path passes through
    a variable. Before the fix these disagreed; they must now agree."""
    through_var = f"p={GOV} ; cat $p ; echo x > /tmp/probe.txt"
    direct = f"cat {GOV} ; echo x > /tmp/probe.txt"
    assert guard.is_denied_bash_write(through_var, IDENTIFIERS) == guard.is_denied_bash_write(
        direct, IDENTIFIERS
    )


def test_alias_chain_is_followed_not_ignored() -> None:
    """`_governed_bound_variables` must reach `q` through `p`. If it stopped at
    direct assignments, the alias-chain DENY case above would pass for the wrong
    reason -- fail-closed on an unresolved name rather than resolution -- so this
    asserts the binding set directly."""
    segments = guard._split_top_level_segments(f"p={GOV} ; q=$p ; echo x > $q")
    assert guard._governed_bound_variables(segments, IDENTIFIERS) == {"p", "q"}


def test_the_regression_cover_is_labelled_honestly() -> None:
    """Guards the corpus against its own worst failure mode: a green suite that
    reads as more coverage than it has. Reconstructs the pre-fix point-4 leg and
    asserts that exactly the cases named in `REGRESSION_COVER` were denied by it.
    Adding an ALLOW case without classifying it fails here rather than quietly
    padding the count."""
    flipped = set()
    for label, cmd in ALLOW_CASES:
        stripped_cmd = guard._strip_heredoc_bodies(cmd)
        old_leg_b = guard._has_var_assignment_indirection(
            guard._split_top_level_segments(stripped_cmd), IDENTIFIERS
        ) and guard._has_write_marker(stripped_cmd)
        if old_leg_b:
            flipped.add(label)
    assert flipped == REGRESSION_COVER, (
        f"ALLOW cases denied by the OLD point-4 leg were {sorted(flipped)}, but "
        f"REGRESSION_COVER names {sorted(REGRESSION_COVER)} -- classify every new "
        "case as regression cover or control rather than leaving the count ambiguous"
    )


def test_unanalysable_write_families_stay_fail_closed() -> None:
    """The narrowing covers the plain-redirect shape only. A `tee`/`cp`/`sed -i`
    segment cannot be told from its target cheaply, so point 4 keeps its original
    broad behaviour there -- pinned so a later widening is a deliberate act."""
    segments = guard._split_top_level_segments(f"p={GOV} ; cat $p ; echo x | tee /tmp/other.txt")
    assert guard._assignment_indirection_reaches_a_write(segments, IDENTIFIERS) is True


def _live_governed_surfaces() -> Optional[list]:
    doe_root = coordinator_doe_root()
    if not doe_root:
        return None
    return dispatch.resolve_governed_authoring_surfaces(str(Path(doe_root) / "coordinator"))


def test_the_corpus_surface_is_live() -> None:
    """`SURFACES` is hand-held so the DENY half runs everywhere, which means it
    can rot the moment the manifest changes. When a DoE-claude sibling checkout
    is resolvable, compare against the live
    `<plugin_root>/governed-authoring-surfaces.json` -- never a second
    hand-written copy of it."""
    live = _live_governed_surfaces()
    if live is None:
        pytest.skip(
            "no DoE-claude sibling checkout resolved by coordinator_doe_root(); "
            "the governed-surfaces manifest cannot be read to check this corpus"
        )
    assert GOV in live, (
        f"the corpus runs against {GOV!r}, which the live manifest no longer "
        f"governs ({live!r}) -- re-pick a surface rather than deleting this test"
    )
