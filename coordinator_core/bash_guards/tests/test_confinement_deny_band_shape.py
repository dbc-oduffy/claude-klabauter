"""coordinator_core.bash_guards.tests.test_confinement_deny_band_shape --
standing gate pinning `dispatch.GuardBand`'s own class docstring invariant
as an EXECUTABLE property, not merely prose.

Spec backlink: `dispatch.GuardBand`'s docstring, "Band semantics (binding on
every entry, not just the named guards)":

    CONFINEMENT_DENY runs to completion in the sense that none of its
    guards ever return a rewrite verdict or an early "allow" with
    content -- every entry in this band emits either a deny or silent
    None, so "first non-None wins" and "first deny wins" coincide here
    by construction.

`dispatch.evaluate_payload_json` returns on the FIRST non-`None` envelope
(dispatch.py, the `for entry in guard_chain:` loop), so a CONFINEMENT_DENY
guard that returns an allow-with-content envelope or a rewrite silently
disarms every guard registered behind it in the chain -- deny guards it
would otherwise have reached never run. Commit `e63c42e39` shipped a
violation of exactly this invariant with a fully green suite; this file is
the regression gate that commit should have tripped.

DESIGN

  1. Band membership is derived LIVE from `dispatch._build_guard_chain(...)`
     via each entry's own `.band` -- never a hardcoded guard-name list, so a
     newly-registered CONFINEMENT_DENY guard is checked the moment it lands,
     with no companion edit to this file.
  2. Each CONFINEMENT_DENY entry is driven through a real input corpus --
     `guard_message_corpus.CONFINEMENT_ROWS` (already builds firing/
     non-firing cells for 16 of the 21 live CONFINEMENT_DENY guards, reusing
     the same scratch-repo/session-isolation fixtures
     `test_check_destructive_git_revert_stash.py`'s own factories back) plus
     `_EXTRA_FIRING_ROWS` below, five additional rows this file adds ONLY
     for the five guards that corpus's own drift-fix note leaves
     control-only: `block-dev-repo-sentinel-removal`,
     `block-disarm-marker-sentinel-creation`, `block-noncanonical-branch-
     creation`, `block-stash-destruction`, `block-subagent-stash-creation`.
  3. A failure NAMES the offender: guard name, its index in the live chain,
     the input that triggered it, the envelope returned, and the list of
     guards it would have shadowed (every entry registered after it).
  4. FORMER known, single-entry, NAMED exception (RESOLVED 2026-08-05, see
     `state/audits/2026-08-05-confinement-deny-band-return-shapes.md`):
     `block-dev-repo-sentinel-removal` used to return an `allow`+
     `additionalContext` envelope on its own "genuinely unexaminable
     indirection" leg directly from its CONFINEMENT_DENY-registered
     `check()`. Split into `check()` (deny-or-None only, stays in
     CONFINEMENT_DENY) and `check_advisory()` (its own ADVISORY_REWRITE
     entry, `block-dev-repo-sentinel-removal-advisory`, registered after
     every CONFINEMENT_DENY guard) -- mirrors `check_destructive_git_
     revert` / `check_destructive_git_revert_advisory`'s split, commit
     `e63c42e39`. `_KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS` below is now the
     empty frozenset -- kept as a named, typed constant (not deleted
     outright) so a future violator has an obvious, already-wired place to
     register a NEW named exception rather than reaching for a blanket
     allowlist; `test_no_confinement_deny_guard_is_currently_exempted`
     below pins it empty and fails loudly (not vacuously) if it is not.
  5. `test_gate_catches_a_synthetic_confinement_deny_violation` proves the
     gate itself can fail -- modeled on `test_guard_message_size.py`'s own
     AC7 falsifiability tests: it splices a synthetic CONFINEMENT_DENY entry
     (real chain, real neighbouring guards, real shadow list) that returns
     an allow+`updatedInput` envelope, and asserts the checker flags it by
     name and index. A gate that cannot fail reads as coverage while being
     none.

PERFORMANCE / SCOPE (design constraint 6). Each corpus row builds ONE guard
chain (`dispatch._build_guard_chain`, cheap -- closures only, no `fn()`
calls yet) and then calls `.fn()` ONLY for the entries whose `band` is
`CONFINEMENT_DENY` -- this is deliberately NOT
`guard_message_capture.capture_all_guards` (which would also fire every
ADVISORY_REWRITE/PLATFORM_CONDITIONED_DENY guard on every row, band-wide
overhead this file has no use for). 44 rows x ~21 live CONFINEMENT_DENY
guards is the full "every CONFINEMENT_DENY entry against every corpus row"
cross product this dispatch's design constraint 2 asks for -- not a
combinatorial sweep across every SHAPE each guard could ever match (each
guard's own dedicated `test_<guard>.py` file already owns that breadth;
this file only needs each guard's non-None branch reached at least once,
which `test_every_live_confinement_deny_guard_has_a_firing_row_in_corpus`
below pins as its own live-derived coverage assertion).
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    _CMD_OVERRIDE_KEY,
    _CWD_OVERRIDE_KEY,
    CONFINEMENT_ROWS,
    CorpusRow,
)

# ---------------------------------------------------------------------------
# The (now empty) named-exception set (module docstring point 4). Kept as a
# typed constant rather than deleted outright -- a future violator has an
# obvious, already-wired place to register a NEW named exception, and
# `test_no_confinement_deny_guard_is_currently_exempted` below pins it
# empty so this never silently regrows into a blanket allowlist.
# ---------------------------------------------------------------------------

_KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS: frozenset = frozenset()


def _noncanonical_branch_hazard_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`block-noncanonical-branch-creation` needs only the repo-scoping gate
    patched open (`resolve_git_root`/`_is_hazard_repo`) -- same monkeypatch
    shape `guard_message_corpus._branch_set_precedence_setup` and
    `_longlived_branch_naming_hazard_setup` already use for this guard's own
    siblings, reproduced here rather than imported (both are private helpers
    scoped to that module's own ADVISORY_REWRITE rows, not exported for
    reuse)."""
    from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard

    mp.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    return {_CWD_OVERRIDE_KEY: "/repo"}


def _subagent_stash_identity_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`block-subagent-stash-creation` is identity-gated on the raw
    presence of `agent_id` in the payload (see that module's own docstring)
    -- merged flatly into the fired payload, the same shape every other
    identity-gated row in `guard_message_corpus.py` uses."""
    return {"agent_id": "a1", "agent_type": "coordinator:executor"}


#: Five firing rows (module docstring point 2) topping up the five
#: CONFINEMENT_DENY guards `guard_message_corpus.CONFINEMENT_ROWS` leaves
#: control-only, per that module's own "C3c" drift-fix comment. Verified
#: live against this tree before being pinned here (each command below was
#: run directly against the guard's own `check()` and confirmed to deny/
#: advise, not guessed from reading the source alone).
_EXTRA_FIRING_ROWS: List[CorpusRow] = [
    CorpusRow(
        "block-dev-repo-sentinel-removal",
        "block-dev-repo-sentinel-removal-fire-direct",
        "rm .coordinator-dev-repo",
        True,
        GuardBand.CONFINEMENT_DENY,
        False,
    ),
    # The former known exception's own trigger (module docstring point 4,
    # RESOLVED 2026-08-05): a DIRECT match still denies via the row above.
    # This xargs-indirection shape used to resolve to this guard's own
    # ADVISORY posture returned DIRECTLY from `check()` -- allow+
    # additionalContext, a CONFINEMENT_DENY-band shape violation. Since the
    # two-leg split, `check()` (the entry this file's `_classify_chain`
    # exercises) returns bare `None` for this input -- the advisory now
    # lives in `check_advisory()`'s own separate ADVISORY_REWRITE entry,
    # which this file's `_classify_chain` deliberately does not walk (module
    # docstring point 2 -- only CONFINEMENT_DENY entries). No corpus row is
    # needed here any more: this shape is now an ordinary control input for
    # this guard's CONFINEMENT_DENY entry, covered implicitly by every row
    # above that is NOT this guard's own direct-fire row. Guard-level proof
    # of the split (both legs, both the three audit trigger shapes, and the
    # non-shadowing property) lives in `test_block_dev_repo_sentinel_
    # removal.py::TestAdvisoryLegAtItsNewChainPosition`, not here.
    CorpusRow(
        "block-disarm-marker-sentinel-creation",
        "block-disarm-marker-sentinel-creation-fire",
        "echo ok > .coordinator-bash-guards-disarmed",
        True,
        GuardBand.CONFINEMENT_DENY,
        False,
    ),
    CorpusRow(
        "block-noncanonical-branch-creation",
        "block-noncanonical-branch-creation-fire",
        "git checkout -b work/machine-b/2026-07-13-closeout",
        True,
        GuardBand.CONFINEMENT_DENY,
        False,
        setup=_noncanonical_branch_hazard_setup,
    ),
    CorpusRow(
        "block-stash-destruction",
        "block-stash-destruction-fire",
        "git stash drop",
        True,
        GuardBand.CONFINEMENT_DENY,
        False,
    ),
    CorpusRow(
        "block-subagent-stash-creation",
        "block-subagent-stash-creation-fire",
        "git stash",
        True,
        GuardBand.CONFINEMENT_DENY,
        False,
        setup=_subagent_stash_identity_setup,
    ),
]

ALL_ROWS: List[CorpusRow] = list(CONFINEMENT_ROWS) + _EXTRA_FIRING_ROWS


# ---------------------------------------------------------------------------
# Chain construction + shape classification.
# ---------------------------------------------------------------------------


def _dummy_chain() -> List[GuardEntry]:
    """The live registration with harmless dummy call-time arguments -- same
    shape `test_guard_band_membership.py`'s own `_dummy_chain` uses. None of
    the `fn` closures are called here; only `name`/`band` (registration-time
    facts) are inspected."""
    return dispatch._build_guard_chain(
        cmd="echo confinement-deny-band-shape-probe",
        session_id="confinement-deny-band-shape-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


def _classify_chain(
    chain: List[GuardEntry], label: str, exceptions: frozenset = _KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS
) -> List[Dict[str, Any]]:
    """Call `.fn()` for every `CONFINEMENT_DENY` entry in `chain` and flag
    any envelope that is not `None` and not a proper deny-with-no-rewrite.
    `exceptions` is threaded as a parameter (rather than read as a bare
    module global) so `test_no_confinement_deny_guard_is_currently_exempted`
    below can call this with an empty set and fire the former exception's
    own input (the xargs-indirection shape) through the classifier,
    asserting a real `None` from the post-split guard -- non-vacuous
    because it only passes if `check()`'s own behavior is clean, not
    because an empty exemption set trivially yields no violations."""
    names = [entry.name for entry in chain]
    violations: List[Dict[str, Any]] = []
    for idx, entry in enumerate(chain):
        if entry.band is not GuardBand.CONFINEMENT_DENY:
            continue
        envelope = entry.fn()
        if envelope is None:
            continue
        if entry.name in exceptions:
            continue
        hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
        decision = hso.get("permissionDecision") if isinstance(hso, dict) else None
        has_rewrite = isinstance(hso, dict) and "updatedInput" in hso
        if decision != "deny" or has_rewrite:
            violations.append(
                {
                    "guard": entry.name,
                    "chain_index": idx,
                    "label": label,
                    "envelope": envelope,
                    "would_shadow": names[idx + 1 :],
                }
            )
    return violations


def _fire_row_and_classify(row: CorpusRow) -> List[Dict[str, Any]]:
    """Build the full guard chain for one corpus row (same per-cell
    isolation discipline as `guard_message_corpus.fire_row`: fresh session
    id, fresh scratch tempdir, fresh `pytest.MonkeyPatch` context, and the
    `CLAUDE_CODE_SESSION_ID` env var `guard_inprocess_search`'s own latch
    keys off) and classify every `CONFINEMENT_DENY` entry's shape on that
    row's input via `_classify_chain`.

    Deliberately reimplements `fire_row`'s body rather than calling it:
    `fire_row` hardcodes `guard_message_capture.capture_one_guard` (a
    single named entry), and this file needs every `CONFINEMENT_DENY`
    entry's response to each row, not only the row's own targeted guard --
    `guard_message_corpus.py` is outside this dispatch's write scope, so no
    seam can be added there to swap in a different capture function."""
    session_id = "confinement-deny-band-shape-%s" % uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="confinement-deny-band-shape-") as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CLAUDE_CODE_SESSION_ID", session_id)
            extra: Dict[str, Any] = dict(row.setup(scratch_dir, mp)) if row.setup else {}
            cmd = extra.pop(_CMD_OVERRIDE_KEY, row.input)
            cwd = extra.pop(_CWD_OVERRIDE_KEY, str(scratch_dir))
            payload: Dict[str, Any] = {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "session_id": session_id,
                "cwd": cwd,
            }
            payload.update(extra)
            chain = dispatch._build_guard_chain(
                cmd=cmd,
                session_id=session_id,
                cwd=cwd,
                payload=payload,
                policy_file=None,
                host_is_windows=row.host_is_windows,
            )
            return _classify_chain(chain, label="%s guard=%s cmd=%r" % (row.row_id, row.guard, cmd))


def _format_violations(violations: List[Dict[str, Any]]) -> str:
    lines = [
        "%d CONFINEMENT_DENY band-shape violation(s) -- a guard in this band "
        "returned something other than deny-with-no-rewrite or None, which "
        "silently shadows every guard registered after it (dispatch.py's "
        "`for entry in guard_chain:` loop returns on the FIRST non-None "
        "envelope):" % len(violations)
    ]
    for v in violations:
        lines.append(
            "\n  guard=%r chain_index=%d\n    corpus row: %s\n    envelope: %r\n"
            "    would shadow (never reached for this input): %r"
            % (v["guard"], v["chain_index"], v["label"], v["envelope"], v["would_shadow"])
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AC-shaped tests.
# ---------------------------------------------------------------------------


def test_chain_is_readable_and_carries_confinement_deny_entries():
    """Guard the guard: if `_build_guard_chain` stops returning entries, or
    stops carrying any `CONFINEMENT_DENY` entry, every assertion below would
    pass vacuously."""
    chain = _dummy_chain()
    assert len(chain) > 10, chain
    confinement = [e for e in chain if e.band is GuardBand.CONFINEMENT_DENY]
    assert len(confinement) > 10, confinement


def test_every_live_confinement_deny_guard_has_a_firing_row_in_corpus():
    """Every CONFINEMENT_DENY guard the LIVE chain registers must have at
    least one `expected_speaker=True` row in `ALL_ROWS` -- otherwise its
    non-None branch is never exercised by the corpus test below, and a
    shape violation on that guard's own deny path would go undetected.
    Derived from the live chain, never a hardcoded guard list, per this
    file's own design constraint 1."""
    chain = _dummy_chain()
    confinement_names = {e.name for e in chain if e.band is GuardBand.CONFINEMENT_DENY}
    firing_names = {row.guard for row in ALL_ROWS if row.expected_speaker}
    missing = confinement_names - firing_names
    assert not missing, (
        "CONFINEMENT_DENY guards registered in the live chain with no "
        "firing row in this file's corpus -- their non-None branch was "
        "never exercised by test_every_confinement_deny_guard_shape_"
        "across_corpus: %r" % sorted(missing)
    )


def test_every_confinement_deny_guard_shape_across_corpus():
    """The main gate: fire every corpus row, and for each one, classify
    every `CONFINEMENT_DENY` entry's shape (module docstring point 2/3)."""
    violations: List[Dict[str, Any]] = []
    for row in ALL_ROWS:
        violations.extend(_fire_row_and_classify(row))
    assert not violations, _format_violations(violations)


def test_no_confinement_deny_guard_is_currently_exempted():
    """`_KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS` is empty following the
    `block-dev-repo-sentinel-removal` two-leg split (module docstring
    point 4, `state/audits/2026-08-05-confinement-deny-band-return-
    shapes.md`) -- pinned here so a future change cannot silently regrow
    it into a blanket allowlist without this test naming the change.

    The formerly-exempted xargs-indirection shape must now classify CLEAN
    with NO exceptions applied at all: the deny leg (`check()`, the only
    function this file's `_classify_chain` exercises) no longer returns
    the advisory envelope for it -- see `test_block_dev_repo_sentinel_
    removal.py::TestAdvisoryLegAtItsNewChainPosition` for the guard-level
    proof of both legs. Asserting this here (rather than trusting an
    empty-set default to pass vacuously -- the exact `all()`-over-nothing
    hazard `test_guard_message_size.py::test_gate_fails_on_deliberately_
    vacuous_population` exists to catch) proves the clean result comes
    from the guard's own fixed behavior, not from an unguarded empty
    collection silently reporting success."""
    assert _KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS == frozenset()

    session_id = "confinement-deny-band-shape-no-exemption-probe-%s" % uuid.uuid4().hex
    cmd = "echo .coordinator-dev-repo | xargs rm"
    # Wrapped in `pytest.MonkeyPatch.context()` with `CLAUDE_CODE_SESSION_ID`
    # set, matching `_fire_row_and_classify`'s per-cell isolation discipline
    # (module docstring: "load-bearing, not hygiene") -- this only builds
    # ONE chain now (the former two-build/one-session_id shape this test
    # used before the exemption resolved is gone), but keeping the same
    # isolation shape avoids a quiet, unexplained deviation.
    # (Review: code-reviewer, Finding 3, 2026-08-05).
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDE_CODE_SESSION_ID", session_id)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": "/tmp",
        }
        chain = dispatch._build_guard_chain(
            cmd=cmd,
            session_id=session_id,
            cwd="/tmp",
            payload=payload,
            policy_file=None,
            host_is_windows=False,
        )
        violations = _classify_chain(chain, label="no-exemption-probe", exceptions=frozenset())
    assert not violations, violations


def test_extra_firing_rows_actually_fire():
    """Mirror `guard_message_corpus.py`'s own
    `test_expected_speaker_matches_measured_reality`: `_classify_chain`
    treats "no envelope" and "correctly-shaped deny" identically as silent
    passes (module docstring point 2/3), so the shape checks above cannot
    by themselves catch a `_EXTRA_FIRING_ROWS` guard whose non-None branch
    quietly stops firing. Every `expected_speaker=True` row here must
    produce a non-`None` envelope from its own NAMED guard specifically --
    not merely "some CONFINEMENT_DENY entry fired somewhere in the chain".
    (Review: code-reviewer, Finding 2, 2026-08-05).
    """
    for row in _EXTRA_FIRING_ROWS:
        if not row.expected_speaker:
            continue
        session_id = "confinement-deny-band-shape-extra-fire-%s" % uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="confinement-deny-band-shape-extra-fire-") as scratch:
            scratch_dir = Path(scratch)
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("CLAUDE_CODE_SESSION_ID", session_id)
                extra: Dict[str, Any] = dict(row.setup(scratch_dir, mp)) if row.setup else {}
                cmd = extra.pop(_CMD_OVERRIDE_KEY, row.input)
                cwd = extra.pop(_CWD_OVERRIDE_KEY, str(scratch_dir))
                payload: Dict[str, Any] = {
                    "tool_name": "Bash",
                    "tool_input": {"command": cmd},
                    "session_id": session_id,
                    "cwd": cwd,
                }
                payload.update(extra)
                chain = dispatch._build_guard_chain(
                    cmd=cmd,
                    session_id=session_id,
                    cwd=cwd,
                    payload=payload,
                    policy_file=None,
                    host_is_windows=row.host_is_windows,
                )
                entry = next(e for e in chain if e.name == row.guard)
                envelope = entry.fn()
                assert envelope is not None, (
                    "row %r expected guard %r to fire (produce a non-None "
                    "envelope) for input %r, but it returned None -- this "
                    "guard's non-None branch has stopped firing"
                    % (row.row_id, row.guard, cmd)
                )


def test_gate_catches_a_synthetic_confinement_deny_violation():
    """Falsifiability (module docstring point 5, modeled on
    `test_guard_message_size.py`'s own AC7 falsifiability tests): splice a
    synthetic `CONFINEMENT_DENY` entry -- real chain, real neighbouring
    guards, real shadow list -- that returns an allow+`updatedInput`
    envelope (the exact shape `dispatch.GuardBand`'s docstring forbids, and
    the shape commit `e63c42e39` shipped), and assert the checker flags it
    by name, index, and shadow list. A gate that cannot fail here is worse
    than no gate."""
    chain = _dummy_chain()
    target_idx = next(i for i, e in enumerate(chain) if e.band is GuardBand.CONFINEMENT_DENY)
    original = chain[target_idx]
    synthetic = GuardEntry(
        name=original.name,
        fn=lambda: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"command": "echo rewritten-by-synthetic-violation"},
            }
        },
        fail_closed=original.fail_closed,
        band=GuardBand.CONFINEMENT_DENY,
    )
    spliced = list(chain)
    spliced[target_idx] = synthetic

    violations = _classify_chain(spliced, label="synthetic-injection-probe")

    assert len(violations) == 1, violations
    v = violations[0]
    assert v["guard"] == original.name
    assert v["chain_index"] == target_idx
    assert v["would_shadow"] == [e.name for e in chain[target_idx + 1 :]]


def test_gate_catches_a_synthetic_allow_with_additional_context():
    """Sibling falsifiability cell: the invariant also forbids a bare
    allow+`additionalContext` (content, no rewrite) from an UNEXEMPTED
    guard -- only `block-dev-repo-sentinel-removal` is named. A different
    guard returning that exact shape must still be caught.

    Spliced at the LAST non-exempted CONFINEMENT_DENY entry (rather than
    the first, which `test_gate_catches_a_synthetic_confinement_deny_
    violation` already uses) so the two falsifiability tests together
    exercise `_classify_chain`'s shadow-list computation (`names[idx +
    1:]`) at both an interior/terminal position and position 0, not the
    same position twice. (Review: code-reviewer, Finding 4, 2026-08-05).
    """
    chain = _dummy_chain()
    target_idx = max(
        i
        for i, e in enumerate(chain)
        if e.band is GuardBand.CONFINEMENT_DENY and e.name not in _KNOWN_ALLOW_WITH_CONTENT_EXCEPTIONS
    )
    original = chain[target_idx]
    synthetic = GuardEntry(
        name=original.name,
        fn=lambda: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": "synthetic allow-with-content, no rewrite",
            }
        },
        fail_closed=original.fail_closed,
        band=GuardBand.CONFINEMENT_DENY,
    )
    spliced = list(chain)
    spliced[target_idx] = synthetic

    violations = _classify_chain(spliced, label="synthetic-allow-with-context-probe")

    assert len(violations) == 1, violations
    assert violations[0]["guard"] == original.name
    assert violations[0]["chain_index"] == target_idx
