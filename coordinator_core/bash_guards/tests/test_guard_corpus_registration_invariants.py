"""Registration-completeness invariants for `guard_message_corpus.py`,
moved out of that module's import-time scope into ordinary test bodies.

Spec backlink: pln-install-dogfood-audit-mechanic-ea0784,
chunk C3, F13a. `guard_message_corpus.py` previously carried six bare
`assert` statements at module scope. A failing import-time assert turns one
missing registration into an import failure for EVERY test module that
imports the corpus -- the neighbouring tests never run, and whatever was
broken behind them stays invisible. This already happened: a single missing
`WRITE_GUARD_ROWS` row for `nudge_shell_shaped_spawn` errored out three
unrelated test modules; once the row was added, 18 previously-unrunnable
tests executed and immediately surfaced three genuine pre-existing
failures, one of them a message-cap violation in the very guard whose
registration was missing. The registration-completeness checks themselves
are real and load-bearing (Anti-scope: MOVE, do not drop) -- only their
import-time placement was the defect. Each test below reproduces exactly
one of the six original assertions, now failing on its own rather than
taking the whole suite's collection down with it.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_GUARDS,
    CONFINEMENT_ROWS,
    GUARD_NAMES,
    PLATFORM_CONDITIONED_ROWS,
    WRITE_GUARD_ROWS,
    _FLIPPED_TO_ADVISORY_REWRITE,
    _LIVE_CHAIN_FOR_SANITY,
    _WG_IMPORT_FAILED,
    _WG_NAMES,
)


def test_confinement_guards_bank_subset_of_confinement_rows():
    """Every `CONFINEMENT_GUARDS` name (minus the two live band-flips)
    must appear in `CONFINEMENT_ROWS` at least once -- was
    `guard_message_corpus.py`'s first module-level bare assert (~767)."""
    missing = {name for name, _ in CONFINEMENT_GUARDS} - _FLIPPED_TO_ADVISORY_REWRITE - {
        row.guard for row in CONFINEMENT_ROWS
    }
    assert not missing, (
        "CONFINEMENT_GUARDS name(s) with no CONFINEMENT_ROWS corpus row: %s" % sorted(missing)
    )


def test_guard_names_bank_subset_of_confinement_rows():
    """Every `GUARD_NAMES` entry (minus the two live band-flips) must
    appear in `CONFINEMENT_ROWS` at least once -- was
    `guard_message_corpus.py`'s second module-level bare assert (~770)."""
    missing = set(GUARD_NAMES) - _FLIPPED_TO_ADVISORY_REWRITE - {
        row.guard for row in CONFINEMENT_ROWS
    }
    assert not missing, "GUARD_NAMES entry(ies) with no CONFINEMENT_ROWS corpus row: %s" % sorted(
        missing
    )


def test_live_advisory_rewrite_chain_matches_corpus_rows():
    """Every guard the live `dispatch._build_guard_chain` registers in the
    ADVISORY_REWRITE band must have exactly the corpus rows
    `ADVISORY_REWRITE_ROWS` claims -- was `guard_message_corpus.py`'s third
    module-level bare assert (~1448)."""
    live = {
        e.name for e in _LIVE_CHAIN_FOR_SANITY if e.band == dispatch.GuardBand.ADVISORY_REWRITE
    }
    corpus = {row.guard for row in ADVISORY_REWRITE_ROWS}
    assert live == corpus, (
        "ADVISORY_REWRITE live/corpus mismatch -- live only: %s, corpus only: %s"
        % (sorted(live - corpus), sorted(corpus - live))
    )


def test_live_platform_conditioned_chain_matches_corpus_rows():
    """Every guard the live `dispatch._build_guard_chain` registers in the
    PLATFORM_CONDITIONED_DENY band must have exactly the corpus rows
    `PLATFORM_CONDITIONED_ROWS` claims -- was `guard_message_corpus.py`'s
    fourth module-level bare assert (~1451)."""
    live = {
        e.name
        for e in _LIVE_CHAIN_FOR_SANITY
        if e.band == dispatch.GuardBand.PLATFORM_CONDITIONED_DENY
    }
    corpus = {row.guard for row in PLATFORM_CONDITIONED_ROWS}
    assert live == corpus, (
        "PLATFORM_CONDITIONED_DENY live/corpus mismatch -- live only: %s, corpus only: %s"
        % (sorted(live - corpus), sorted(corpus - live))
    )


def test_write_guards_discovery_import_did_not_fail():
    """`write_guards.engine.discover_guard_names()` must not report any
    import failure among the discovered guard modules -- was
    `guard_message_corpus.py`'s fifth module-level bare assert (~2149)."""
    assert not _WG_IMPORT_FAILED, "write_guards import failure(s): %s" % _WG_IMPORT_FAILED


#: state/bash-guards/known-red.json group "nudge-private-git-fact-resolver-
#: missing-corpus-row" -- `nudge_private_git_fact_resolver` (landed
#: 9ca373fee, D3) and `nudge_outbox_draft_frontmatter_shape` previously had
#: no `WRITE_GUARD_ROWS` corpus entry in guard_message_corpus.py; both now
#: have fire+control rows (see that module) and this test passes for real.
def test_write_guard_names_match_corpus_rows():
    """Every guard `write_guards.engine.discover_guard_names()` reports
    must have exactly the corpus rows `WRITE_GUARD_ROWS` claims -- was
    `guard_message_corpus.py`'s sixth module-level bare assert (~2150).

    NOTE: the "registered write guard has no corpus row" direction of this
    equality is also proven (independently) by `test_ac2_every_reachable_
    guard_has_a_corpus_row` in `guard_message_corpus.py`; that test is kept
    as-is per Anti-scope (module-body test, not a bare assert) rather than
    treated as fully redundant here, since the reverse direction (a corpus
    row naming a guard `discover_guard_names()` no longer reports) is not
    covered there."""
    discovered = set(_WG_NAMES)
    corpus = {row.guard for row in WRITE_GUARD_ROWS}
    assert discovered == corpus, (
        "write_guards discovery/corpus mismatch -- discovered only: %s, corpus only: %s"
        % (sorted(discovered - corpus), sorted(corpus - discovered))
    )
