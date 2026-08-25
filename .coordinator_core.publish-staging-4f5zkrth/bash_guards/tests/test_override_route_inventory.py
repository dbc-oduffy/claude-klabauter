"""Property gate over every advisory-emitting guard in `dispatch.py`'s
registered chain: a message RENDERED for a subagent-shaped payload (one
carrying an `agent_id`) may not state that an override/unlock mechanism
exists -- no override key, sentinel/marker dotfile, `touch`/`export`/`rm`
recipe, CLI invocation, doc pointer, or bare "an unlock exists" statement.

Spec backlink: docs/plans/2026-08-13-guard-messages-stop-handing-agents-
the-keys.md, AC-7; this chunk's own stub is
tasks/guard-messages-keys/C7b.md.

WHY THIS FILE FLIPPED TWICE:

Round 1 (pre-2026-08-13) required every advisory guard to CALL
`operator_override_note` or sit on a hand-kept 7-entry allowlist -- the
opposite of the PM's ruling that a dispatched agent is never handed a key
to its own guard's unlock.

Round 2 (C7, `ae43220928`) inverted the call-site check to forbid the
CALL outright (`test_no_advisory_guard_names_an_override_route`). That
gate aimed at the wrong object: `operator_override_note` (C1,
`cbc3f0241`/`ceb68bce2`) is now audience-aware -- it returns `""` for a
subagent, an unresolvable audience, or any exception, and the doc pointer
ONLY for a positively-resolved EM (`bash_guards/_helpers.py`'s own
`operator_override_note` docstring, "AUDIENCE-GATED" section). It is the
SSOT builder every guard should route through. A gate forbidding the
call punished 20 now-correct guards and pushed an author needing an EM
pointer to hand-write one instead -- the exact "fork a second builder"
move `operator_override_note`'s own NEGATIVE SPEC 3 forbids, and how the
2026-08-12 regression happened in the first place (a pointer that would
not resolve, so someone inlined the mechanism by hand).

Round 3 (this file, C7b) drops the call-site question entirely. The
defect was never "a guard calls the composer" -- it was "a rendered
subagent-audience message states that an unlock exists". This file now
gates on RENDERED TEXT: every live ADVISORY_REWRITE/
PLATFORM_CONDITIONED_DENY guard is fired through
`guard_message_corpus.py`'s own trigger corpus (C5's SSOT for "what
command actually makes this guard speak", reused here rather than
re-derived -- a second, parallel trigger fixture per guard would be
exactly the "parallel mechanism" the dispatch brief's "Coordinate with
C6" section forbids) with a subagent-shaped payload laid over each row's
own identity, and the rendered `hookSpecificOutput` prose
(`_message_size.extract_prose_text`, the same seam
`test_write_bump_message.py`'s subagent-render tests already use) is
scanned for leak patterns.

No hand-kept allowlist survives any round: an allowlist naming which
guards are "exempt" from a rule about EVERY guard's rendered text would
be the same defect wearing a new sign (plan Anti-scope).

`GuardBand.ADVISORY_REWRITE` and `GuardBand.PLATFORM_CONDITIONED_DENY`
are the two bands this suite covers -- `CONFINEMENT_DENY` guards are a
different kind of boundary (several are unconditionally
non-suppressible, by dispatch.py's own registration doctrine), and their
subagent-render text is already covered by `test_write_bump_message.py`
et al. where it matters; their absence here is not a gap of the same
shape.

NOT COVERED HERE, BY DESIGN: the EM leg (AC-2's permitted doc-pointer
shape for a positively-resolved EM audience). Firing every corpus row a
SECOND time under an EM-shaped payload would double this file's walk
cost over the same live registries and trigger fixtures C6's own
`audience` axis on `CorpusRow` is built to cover (dispatch brief,
"Coordinate with C6") -- building a second EM-leg walk here would be the
parallel mechanism that section forbids. Left to C6.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Dict, List, Pattern

import pytest

from coordinator_core.bash_guards import dispatch as _dispatch
from coordinator_core.bash_guards._message_size import extract_prose_text
from coordinator_core.bash_guards.dispatch import GuardBand
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    PLATFORM_CONDITIONED_ROWS,
    CorpusRow,
    fire_row,
)

#: Bands whose guards can emit advisory/deny text an agent reads --
#: CONFINEMENT_DENY is deliberately excluded -- see module docstring.
_ADVISORY_BANDS = (GuardBand.ADVISORY_REWRITE, GuardBand.PLATFORM_CONDITIONED_DENY)

#: Baseline subagent identity laid over a corpus row's own `setup` (via
#: `dict.setdefault`, below) -- a row already wiring its own identity
#: (e.g. `block-reviewer-bash-outside-allowlist`'s reviewer legs) keeps
#: that identity untouched; every other row picks this one up. Any
#: non-empty `agent_id` is enough to make
#: `session.identity.resolves_em_audience` return `False` -- the exact
#: leg `operator_override_note` gates its own emission on.
_SUBAGENT_IDENTITY: Dict[str, str] = {
    "agent_id": "deadbeef0123",
    "agent_type": "coordinator:executor",
}

#: What counts as "names an unlock" in RENDERED prose -- the categories
#: the dispatch brief names explicitly: an override key, a sentinel/
#: marker dotfile path, a touch/export/rm recipe, a CLI invocation naming
#: an override/bypass, a doc pointer, or a bare "an unlock exists"
#: statement. Not an allowlist of guards -- a fixed vocabulary of what a
#: leak LOOKS like, checked against every guard's rendered text
#: uniformly.
#:
#: Deliberately NOT a bare "any backticked span" scan for "CLI
#: invocation": live-measured against every ADVISORY_REWRITE/
#: PLATFORM_CONDITIONED_DENY guard's actual rendered text (this file's
#: own first draft), that pattern false-positived on every guard's
#: legitimate REWRITE SUGGESTION -- `git stash push ...`, `git checkout
#: -b work/...`, a Python rewrite snippet -- which is the guard's whole
#: PURPOSE, not a leak. "CLI invocation" here is scoped to the one shape
#: an override/bypass CLI invocation actually takes: naming a bypass/
#: override/disarm subcommand explicitly.
_LEAK_PATTERNS: Dict[str, Pattern[str]] = {
    "override-key(s) phrase": re.compile(r"override key", re.IGNORECASE),
    "guard-override-keys.md doc pointer": re.compile(r"guard-override-keys\.md"),
    "bare unlock statement": re.compile(r"\bunlock\b", re.IGNORECASE),
    "sentinel/marker dotfile path": re.compile(r"\.coordinator-[a-z][a-z0-9-]*"),
    "touch/export/rm recipe": re.compile(r"\b(?:touch|export|rm)\s+\S"),
    "override env-var name": re.compile(r"\bCOORDINATOR_[A-Z_]+\b"),
    "bypass/override/disarm CLI invocation": re.compile(
        r"\b(?:bypass|override|disarm)\b.{0,40}`", re.IGNORECASE
    ),
}


def _find_leaks(text: str) -> List[str]:
    return [label for label, pattern in _LEAK_PATTERNS.items() if pattern.search(text)]


def _as_subagent_row(row: CorpusRow) -> CorpusRow:
    """Same `row`, fired with a subagent-shaped payload laid over whatever
    `row.setup` already returns -- see `_SUBAGENT_IDENTITY`'s own
    docstring for why `setdefault` (not overwrite)."""
    base_setup = row.setup

    def setup(scratch_dir, mp):
        extra: Dict[str, str] = dict(base_setup(scratch_dir, mp)) if base_setup else {}
        extra.setdefault("agent_id", _SUBAGENT_IDENTITY["agent_id"])
        extra.setdefault("agent_type", _SUBAGENT_IDENTITY["agent_type"])
        return extra

    return dataclasses.replace(row, row_id=row.row_id + "-subagent-render", setup=setup)


def _firing_advisory_rows() -> List[CorpusRow]:
    """Every corpus row that actually makes an advisory-band guard speak.

    `PLATFORM_CONDITIONED_DENY`'s two guards (`multiprobe-banner`,
    `plumbing-and-loops`) pin `host_is_windows=False` on their own corpus
    rows -- the always-suppressed/allow leg, not the DENY leg, which only
    fires on the Windows host leg (see
    `guard_message_capture.py::test_seam_pins_host_is_windows_explicitly_
    per_cell`, the same guard, proving the Windows/non-Windows legs
    differ). A Windows-leg variant of each is added here so this
    inventory actually reaches the rendered DENY text, not only the
    always-suppressed one.
    """
    base = [
        row
        for row in (*ADVISORY_REWRITE_ROWS, *PLATFORM_CONDITIONED_ROWS)
        if row.expected_speaker and row.band in _ADVISORY_BANDS
    ]
    windows_legs = [
        dataclasses.replace(row, row_id=row.row_id + "-windows-leg", host_is_windows=True)
        for row in PLATFORM_CONDITIONED_ROWS
        if row.expected_speaker
    ]
    return base + windows_legs


def _live_advisory_guard_names() -> List[str]:
    """Advisory-band guard names read straight off the LIVE
    `dispatch._build_guard_chain` output -- never a hand-kept list (AC-5's
    standard, applied here per the dispatch brief's item 4)."""
    chain = _dispatch._build_guard_chain(
        cmd="echo hi",
        session_id="test-override-route-inventory-live-chain",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        policy_file=None,
        host_is_windows=False,
    )
    return sorted({entry.name for entry in chain if entry.band in _ADVISORY_BANDS})


#: Guards live-registered in an advisory band that structurally never
#: return a non-`None` envelope -- side-effect-only guards
#: (`guard_message_corpus.py`'s own `reap-stale-git-lock` comment: "always
#: returns None") with no message to render at all, so they cannot leak
#: text they never produce. This is NOT an allowlist exempting a guard
#: from the leak-vocabulary check above -- every row that DOES fire is
#: still checked (`test_no_advisory_guard_leaks_an_unlock_to_a_subagent_
#: render`); this only narrows the enumeration proof's "every live guard
#: needs a firing row" requirement for the guard(s) that provably have
#: none to give, and `test_never_speaks_guards_are_verified_silent`
#: (below) checks the claim live rather than trusting the name alone.
_NEVER_SPEAKS_GUARDS = frozenset({"reap-stale-git-lock"})


def test_every_live_advisory_guard_has_a_subagent_render_row():
    """Enumeration proof: every ADVISORY_REWRITE/PLATFORM_CONDITIONED_DENY
    guard registered in the LIVE chain has at least one firing row this
    file's own coverage draws from, or is a verified-silent guard (see
    `_NEVER_SPEAKS_GUARDS`). A guard added tomorrow with no corpus row
    fails HERE (not only in `guard_message_corpus.py::
    test_ac2_every_reachable_guard_has_a_corpus_row`, a different file's
    promise) -- no hand-kept allowlist EXEMPTS a guard from being
    checked; `_NEVER_SPEAKS_GUARDS` narrows only which guards need a
    FIRING row, and is itself verified below."""
    live = set(_live_advisory_guard_names())
    covered = {row.guard for row in _firing_advisory_rows()}
    missing = sorted(live - covered - _NEVER_SPEAKS_GUARDS)
    assert not missing, (
        "the following live advisory-band guards have no firing row in "
        "guard_message_corpus.py -- add one there before this inventory "
        "can vouch for their subagent-render text: %s" % missing
    )


def test_never_speaks_guards_are_verified_silent():
    """Fires every corpus row (firing and non-firing alike) registered
    for each name in `_NEVER_SPEAKS_GUARDS` and asserts every one returns
    `envelope is None` -- a false exemption (a guard that can actually
    speak) fails HERE, not silently in the enumeration proof above."""
    checked = set()
    for row in (*ADVISORY_REWRITE_ROWS, *PLATFORM_CONDITIONED_ROWS):
        if row.guard not in _NEVER_SPEAKS_GUARDS:
            continue
        checked.add(row.guard)
        capture = fire_row(_as_subagent_row(row))
        assert capture.envelope is None, (
            "guard %r is in _NEVER_SPEAKS_GUARDS but row %r produced a "
            "non-None envelope -- it can speak after all; remove it from "
            "the never-speaks set and give this inventory a proper firing "
            "row instead" % (row.guard, row.row_id)
        )
    assert checked == _NEVER_SPEAKS_GUARDS, (
        "_NEVER_SPEAKS_GUARDS names a guard with no corpus row at all to "
        "verify against: %s" % sorted(_NEVER_SPEAKS_GUARDS - checked)
    )


@pytest.mark.parametrize("row", _firing_advisory_rows(), ids=lambda r: r.row_id)
def test_no_advisory_guard_leaks_an_unlock_to_a_subagent_render(row: CorpusRow):
    """The real invariant (PM ruling, 2026-08-13): a dispatched agent is
    never handed a key to its own guard's unlock. Fires `row` through the
    live registry with a subagent-shaped payload and asserts the rendered
    prose (`additionalContext`/`permissionDecisionReason`) names no
    override/unlock mechanism -- see module docstring for the leak
    vocabulary and why this supersedes the call-site check it replaces."""
    capture = fire_row(_as_subagent_row(row))
    text = extract_prose_text(capture.envelope)
    leaks = _find_leaks(text)
    assert not leaks, (
        "guard %r leaked an unlock mechanism to a subagent-shaped render "
        "(%s): %r" % (row.guard, ", ".join(leaks), text)
    )
