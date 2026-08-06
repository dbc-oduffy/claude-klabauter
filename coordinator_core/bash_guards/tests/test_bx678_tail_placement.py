"""Pins the deliberate TAIL registration of the BX-6/BX-7/BX-8 platform
guards (``guard_grep_via_bash`` / ``guard_multiprobe_banner`` /
``guard_plumbing_and_loops``) in ``dispatch.py``'s ``guard_chain``.

Why this exists (the rationale a future reorder must not silently undo):
these three guards deny on Windows, which makes them LOOK like every other
hard-deny in this chain -- and every other hard-deny sits AHEAD of
``offer-git-c`` and the BX-16 rewrite entries, precisely so a rewrite can
never short-circuit a confinement (see ``test_hard_denies_precede_
rewrites.py``). Moving these three to that same hard-deny cohort was
recommended by an integration report and tested empirically: it inverts the
intended behaviour on both platforms --

  - macOS: the rewrite ahead of the guard is what turns ``grep -rn foo .``
    into the single-process equivalent; short-circuited by an earlier deny,
    that fix never applies and the command is merely advised (or blocked
    outright once ported to Windows below).
  - Windows: the command is denied outright and the auto-rewrite never
    runs at all -- the agent has to hand-type the alternative from the deny
    text instead of the rewrite simply happening.

Placed AFTER every rewrite (where they are registered today), all six
shape/platform combinations correctly auto-rewrite first, and each guard
still falls through to its own deny/advise when the rewrite's own
``COORDINATOR_ALLOW_*`` override has disabled it (or, for the plumbing/loop
guard, when the underlying BX-16 seam confirms no outlet for this exact
command) -- that override/no-outlet case is each guard's actual value, and
it only works correctly at the tail.

These three are deliberately NOT part of ``test_hard_denies_precede_
rewrites.py``'s ``CONFINEMENT_HARD_DENIES`` set -- that invariant exists to
stop a caller EVADING a security boundary (identity gates, git-history
protection, the subagent-commit ban) by reshaping a command around a
rewrite. These three are machine-load guards with no adversarial-evasion
shape; none of their target command shapes are reachable via the
``cd <dir> && git ...`` mechanism that invariant closes. This test asserts
that exclusion explicitly, alongside the tail-placement assertion, so a
future "helpfully" adding them to that set fails loudly here instead of
silently regressing the seam this dispatcher exists to protect.

Deliberately structural (reads the live registration via
``dispatch._build_guard_chain``, like its sibling
``tests/test_guard_band_membership.py``, the M1 2026-07-29 replacement for
the retired source-text-regex ``test_hard_denies_precede_rewrites.py``)
rather than behavioural, so it fires on a bad *registration* even if no
guard's own logic changes.

Spec backlink: coordinator_core/bash_guards/dispatch.py (guard_chain tail
registration of grep-via-bash-guard / multiprobe-banner / plumbing-and-loops)
"""
from __future__ import annotations

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand
from coordinator_core.bash_guards.tests.test_guard_band_membership import (
    ADVISORY_REWRITE_NAMES,
    CONFINEMENT_DENY_NAMES,
    _dummy_chain,
)

PLATFORM_GUARDS = {
    "multiprobe-banner",
    "plumbing-and-loops",
}

#: `grep-via-bash-guard` moved from PLATFORM_CONDITIONED_DENY to
#: ADVISORY_REWRITE (H11(a), 2026-07-30, docs/plans/2026-07-30-os-aware-
#: guard-advisory-defaults.md) -- its own substitutable/deny branch was
#: removed the same day (0 denies on either platform, provably
#: unreachable). Checked against ADVISORY_REWRITE_NAMES separately below
#: (never folded into PLATFORM_GUARDS) so the two band-membership
#: assertions this module makes -- `PLATFORM_GUARDS` is exactly
#: PLATFORM_CONDITIONED_DENY, and `PLATFORM_GUARDS` is disjoint from
#: ADVISORY_REWRITE_NAMES -- both still hold.
GREP_VIA_BASH_GUARD_NAME = "grep-via-bash-guard"

# Every ADVISORY_REWRITE entry that can return allow+updatedInput (a genuine
# rewrite) -- the subset of ADVISORY_REWRITE_NAMES this test cares about
# short-circuiting ahead of the platform guards. `validate-commit`,
# `inprocess-search` and `probe-spray` are advisory/content, not rewrites,
# and are deliberately excluded here (mirrors the retired
# `test_hard_denies_precede_rewrites.py`'s own `REWRITING_GUARDS` set).
REWRITING_GUARDS = {
    "offer-git-c",
    "find-exec-rewrite",
    "grep-via-bash-rewrite",
    "multiprobe-banner-rewrite",
    "head-tail-plumbing-rewrite",
}


def test_platform_guards_are_registered():
    order = {entry.name for entry in _dummy_chain()}
    missing = PLATFORM_GUARDS - order
    assert not missing, (
        f"expected {sorted(PLATFORM_GUARDS)} registered in dispatch.py's guard_chain; "
        f"missing: {sorted(missing)}"
    )


def test_platform_guards_sit_after_every_rewriting_guard():
    order = [entry.name for entry in _dummy_chain()]
    position = {name: index for index, name in enumerate(order)}

    rewriters = {name: position[name] for name in REWRITING_GUARDS if name in position}
    assert rewriters, f"no rewriting guard found in the chain; expected one of {REWRITING_GUARDS}"
    latest_rewrite = max(rewriters.values())
    latest_rewrite_name = max(rewriters, key=rewriters.get)

    ahead = [
        name
        for name in PLATFORM_GUARDS
        if name in position and position[name] < latest_rewrite
    ]
    assert not ahead, (
        f"these BX-6/BX-7/BX-8 platform guards are registered BEFORE {latest_rewrite_name!r} "
        f"and would short-circuit its rewrite (inverting the intended behaviour on both "
        f"platforms -- see this test module's own docstring): {sorted(ahead)}"
    )


def test_platform_guards_are_band_tagged_platform_conditioned_deny_not_confinement():
    """The band model's own answer to the question this test used to ask via
    a hand-maintained ``CONFINEMENT_HARD_DENIES`` exclusion set: these three
    guards must carry ``GuardBand.PLATFORM_CONDITIONED_DENY``, never
    ``GuardBand.CONFINEMENT_DENY`` -- forcing them into the confinement band
    would (by the band model's own contiguous-sequence rule) require them to
    register ahead of every rewrite, which is exactly the regression this
    test module exists to prevent."""
    by_name = {entry.name: entry.band for entry in _dummy_chain()}
    wrong = {name: by_name[name] for name in PLATFORM_GUARDS if by_name[name] is not GuardBand.PLATFORM_CONDITIONED_DENY}
    assert not wrong, (
        f"BX-6/BX-7/BX-8 platform guards must be band-tagged "
        f"GuardBand.PLATFORM_CONDITIONED_DENY, never GuardBand.CONFINEMENT_DENY -- doing so "
        f"forces the ordering invariant onto them (ahead of every rewrite), which is exactly "
        f"the regression this test module exists to prevent. Found with the wrong band: {wrong}"
    )
    assert not (PLATFORM_GUARDS & set(CONFINEMENT_DENY_NAMES))
    assert not (PLATFORM_GUARDS & set(ADVISORY_REWRITE_NAMES))
    # `grep-via-bash-guard` is the mirror-image assertion: it must now be
    # ADVISORY_REWRITE, never PLATFORM_CONDITIONED_DENY or CONFINEMENT_DENY.
    assert by_name[GREP_VIA_BASH_GUARD_NAME] is GuardBand.ADVISORY_REWRITE
    assert GREP_VIA_BASH_GUARD_NAME in ADVISORY_REWRITE_NAMES
    assert GREP_VIA_BASH_GUARD_NAME not in CONFINEMENT_DENY_NAMES
