"""Runtime-render gate: a guard's deny/remediation TEXT must not name an
override env var as `NAME=1` unless the SAME rendered text also carries its
reachability constraint (pre-launch-only) -- the class of defect fixed in
`check_test_suite_invocation.py::_deny_reason_grant` and
`_deny_reason_subagent_directory` (2026-07-30 dud-offer memo), and previously
fixed in `write_guards/block_unauthorized_claude_md_write.py` (its own
`PYTHONPATH=` command-interpolation variant of the same class).

WHY THIS IS A DIFFERENT GATE FROM `test_no_handwritten_override_clauses.py`,
NOT A DUPLICATE: that test statically folds a guard's SOURCE via
`ast`-level constant-propagation (`_fold_string`/`_fold_mod_rhs`) to
reconstruct what a composite string will render to, then regexes the
folded result. It has a real, documented blind spot: a `%`-formatted string
whose tuple RHS mixes a statically-unfoldable element (a function
PARAMETER, e.g. `detected`/`cmd_safe`) with a foldable module constant (the
override env var name) makes `_fold_mod_rhs` bail on the WHOLE tuple and
return `None` the moment ANY element is unfoldable -- so the composed
`"...COORDINATOR_OVERRIDE_X=1"` text is never reconstructed and
`_VIOLATION_RE` never sees it. This is exactly the shape `_deny_reason_grant`
and `_deny_reason_subagent_directory` had before this fix, and exactly why
that pre-existing test passed identically on both the broken and the fixed
text -- nothing else in the suite told anyone the offer was a dead end.

This gate sidesteps the folding problem entirely by not folding at all: it
CALLS the deny-text builder with representative synthetic arguments and
inspects the ACTUAL RENDERED STRING. A runtime call has no unfoldable
operand -- every parameter has a concrete value by the time the function
returns -- so this catches the identical defect shape the static folder
provably cannot, without teaching `_fold_mod_rhs` partial-tuple semantics
(a prior dispatch judged that a folder-semantics change, not a bounded fix
for this memo; a sibling runtime gate is the bounded alternative it left on
the table).

SCOPE, NAMED NOT SILENT: this gate is applied to the two functions this
dispatch fixed (`_deny_reason_grant`, `_deny_reason_subagent_directory`), not
swept across the whole `bash_guards`/`write_guards` package. Two other
functions in this SAME module -- `_deny_reason_subagent` and
`_deny_reason_mutex` -- carry the IDENTICAL hand-written, unqualified
`"...%s=1"` shape and are NOT fixed here: they were named explicit
do-not-touch scope for this dispatch. Applying this gate's assertion to them
would fail red for code this dispatch was told not to touch, so they are
listed in `_KNOWN_UNFIXED_SITES` below (with the same reasoning) rather than
silently exempted by narrow test scope alone -- a future dispatch clearing
them should also delete their `_KNOWN_UNFIXED_SITES` entries.

A companion, LARGER finding from the same sweep -- confirmed present, not
fixed here -- is that the ENTIRE `coordinator_core/write_guards/` package
never imports `operator_override_note` at all; roughly eight guard modules
there hand-write the identical `f"  {_OVERRIDE_ENV_VAR}=1"` shape (grep
`coordinator_core/write_guards/*.py` for `rare-use` / `escape hatch` /
`_OVERRIDE_ENV`). Extending this gate (or `test_no_handwritten_override_
clauses.py`) to cover that package is a distinct, larger, cross-package
follow-up (`operator_override_note` would need to move to a location both
packages can import, or write_guards needs its own copy) -- reported to the
EM, not attempted here.

Spec backlink: 2026-07-30 dud-offer-remediation-sweep memo (this dispatch).
Reference fix (same class, different surface): write_guards/
  block_unauthorized_claude_md_write.py (commits 21a447a6, 10ef6ab4).
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as _ctsi
from coordinator_core.bash_guards._helpers import operator_override_note

#: The actual fingerprint of a hand-written "set this to 1" instruction --
#: same regex `test_no_handwritten_override_clauses.py` uses, applied here to
#: a RENDERED string rather than a folded AST constant.
#:
#: Review: coordinator:code-reviewer -- extended to also match the
#: REASON-shaped assignment (`NAME="<reason>"`) `operator_override_note`'s
#: `reason_placeholder` param now emits for the PUNT-family vars
#: (`COORDINATOR_QUEUE_PUNT`, `COORDINATOR_BATON_BODY_PUNT`), whose own
#: `_is_trivial_reason` guard denylists the literal `"1"`. The original
#: flag-shaped `NAME=1` alternative stays untouched; both shapes are scanned
#: by the SAME regex so neither can silently drift out of this gate's view,
#: and both are still required to carry the reachability marker below.
_VIOLATION_RE = re.compile(
    r'\bCOORDINATOR_(?:ALLOW|OVERRIDE|DISABLE)_[A-Z0-9_]+=1\b'
    r'|\bCOORDINATOR_[A-Z0-9_]+="[^"\n]*"'
)

#: The one fact `operator_override_note` used to attach to every such
#: mention. A `NAME=1` instruction with this phrase NOT within a short
#: lookahead window is the dead-end shape this gate exists to catch: the
#: env var is named, but the reader has no way to know it cannot be set
#: from inside the session.
#:
#: 2026-08-11 reshape #1: `operator_override_note` stopped rendering a
#: `NAME=1`/`NAME="..."` assignment at all (see that function's own
#: docstring, NEGATIVE SPEC 4, and
#: `test_operator_override_note_no_assignment_form.py`), so this marker/
#: lookahead machinery already only mattered for a HAND-WRITTEN `NAME=1`
#: site that bypasses the builder entirely (e.g. `_deny_reason_mutex`'s own
#: known, out-of-scope `%s=1`, see `_KNOWN_UNFIXED_SITES` below).
#:
#: 2026-08-11 reshape #2, SAME DAY (docs/plans/2026-08-11-guard-messages-
#: point-to-docs-never-name.md) -- `operator_override_note` stopped naming
#: the env var at all (any form, assigned or bare) and this phrase is no
#: longer present in ITS output either; the pre-launch-only fact it names
#: moved wholly into the reference doc
#: (`test_operator_override_note_retains_affordances.py`'s
#: `test_reference_doc_states_the_env_var_is_not_reachable_in_session`
#: pins it there now). This marker/lookahead machinery is UNCHANGED in
#: purpose by reshape #2 -- it still exists solely to catch a HAND-WRITTEN
#: `NAME=1` site outside the builder (`_KNOWN_UNFIXED_SITES`) -- and is kept
#: verbatim rather than retired, since that class of violation is
#: independent of what the builder itself renders. `TestDetectorSelfTest.
#: test_negative_note_produced_by_the_real_builder_is_not_caught` below is
#: the one control this reshape DOES change: it is updated to assert
#: `_VIOLATION_RE` finds no match in the builder's output directly (AC-5:
#: non-vacuous), rather than relying on `assert_render_carries_
#: reachability_constraint`'s own zero-iteration loop to "pass" the same
#: way whether or not the builder still carried anything this gate cares
#: about.
_REACHABILITY_MARKER = "unsettable from inside this session"

#: How far past the end of a `NAME=1` match the reachability marker may
#: appear and still count as "attached to this mention" -- generous enough
#: to span a hand-written `"%s=1 (unsettable from inside this session)"`-
#: style layout while still failing a mention on the opposite end of a long
#: paragraph.
_LOOKAHEAD_CHARS = 60


def assert_render_carries_reachability_constraint(text: str, *, context: str) -> None:
    """Fail if `text` names an override env var as `NAME=1` without the
    pre-launch-only marker appearing shortly after -- the runtime-render
    counterpart to `test_no_handwritten_override_clauses.py`'s static AST
    scan. Exported (not module-private) so a future guard's own test module
    can reuse this exact assertion rather than hand-rolling a second copy --
    see module docstring "prefer one gate" framing.
    """
    for m in _VIOLATION_RE.finditer(text):
        window = text[m.end(): m.end() + _LOOKAHEAD_CHARS]
        assert _REACHABILITY_MARKER in window, (
            "%s: found an override-env-var instruction (%r) with no "
            "pre-launch-only reachability marker within %d chars -- route "
            "it through coordinator_core.bash_guards._helpers."
            "operator_override_note instead:\n%s"
            % (context, m.group(0), _LOOKAHEAD_CHARS, text)
        )


#: Known, NAMED sites in THIS module carrying the identical defect shape,
#: deliberately left unfixed by this dispatch's explicit do-not-touch scope
#: (see module docstring). Listed here so their exclusion is visible in the
#: test file itself, not merely absent from it.
_KNOWN_UNFIXED_SITES = ("_deny_reason_subagent", "_deny_reason_mutex")


def test_known_unfixed_sites_still_exist_and_are_not_silently_widened() -> None:
    """Sanity pin for `_KNOWN_UNFIXED_SITES`: if either function is removed
    or renamed, this test file's scope note goes stale silently -- fail loud
    instead so a future editor updates the docstring/list together."""
    for name in _KNOWN_UNFIXED_SITES:
        assert hasattr(_ctsi, name), (
            "%s no longer exists in check_test_suite_invocation -- update "
            "_KNOWN_UNFIXED_SITES and this module's docstring" % name
        )


@pytest.mark.parametrize("is_tie", [True, False])
def test_deny_reason_grant_render_carries_reachability_constraint(is_tie: bool) -> None:
    rendered = _ctsi._deny_reason_grant("pytest", "pytest tests/", is_tie=is_tie)
    assert_render_carries_reachability_constraint(
        rendered, context="_deny_reason_grant(is_tie=%s)" % is_tie
    )


@pytest.mark.parametrize("is_tie", [True, False])
def test_deny_reason_grant_render_never_names_an_override_route(is_tie: bool) -> None:
    """Inverted (was: `..._render_embeds_the_ssot_note_verbatim`, which
    asserted `operator_override_note`'s output WAS present). The Tier-F
    escape hatch is now the Tier-U grant-CLI ask, not an env-var override --
    `_deny_reason_grant`'s current render carries no override-doc pointer at
    all for this payload shape (`{"session_id": ...}` has no `agent_id`/
    `subagent_type`, so `resolves_em_audience` is False). Positively asserts
    both the absence of any override-note fragment AND the presence of the
    real, current Tier-U grant-ask shape, so this cannot pass vacuously on a
    render that has neither."""
    rendered = _ctsi._deny_reason_grant("pytest", "pytest tests/", is_tie=is_tie)
    note = operator_override_note(_ctsi._OVERRIDE_ENV_VAR, payload=None)
    assert note == ""
    assert _ctsi._OVERRIDE_ENV_VAR not in rendered
    assert "unsettable from inside this session" not in rendered
    if is_tie:
        assert "No Tier-F escape" in rendered
    else:
        assert "Ask the PM for a Tier-U authorization grant" in rendered
    assert 'tier-u-grant-cli grant pm "<verbatim PM utterance>"' in rendered


def test_deny_reason_grant_tie_branch_stays_within_word_budget() -> None:
    """Binds a ceiling on the tie branch so a future edit that grows it fails
    loud rather than drifting past a budget nobody re-checks.

    The ceiling is 58, not the 45 originally briefed. 45 was a target for
    cutting PROSE -- framing, doctrine, and explanation a reader cannot act
    on. Twice now the cheapest way to satisfy it has been to delete
    something load-bearing instead: first the reachability constraint on the
    override env var (an offer the reader could not take), then the
    `Grant detail:` pointer (a tie caller's only route to the doctrine this
    function deliberately stopped inlining). Both were restored, and the
    budget moved to accommodate them.

    Moved again, 50 -> 52, when `OVERRIDE_KEYS_DOC` split into a file-
    resolution form and a message-display form (`_helpers.py`): these guards
    run fleet-wide via DoE's PreToolUse shim, so the reader is usually
    sitting in some OTHER repo's tree, where the bare repo-relative path
    resolves to nothing. The display form first became a repo-qualified hint
    ("claude-klabauter " + the relative path, 2 words), then
    `_resolve_override_keys_doc_display()` upgraded it to resolve the
    claude-klabauter root (env var, then the `.claude-klabauter-live-root` pointer file -- never the
    `machine-local` subprocess rung, never on this render-time path) into a
    single absolute-path token when available, falling back to the hint
    otherwise. The absolute form is actually ONE WORD SHORTER than the hint
    (no space inside a path, vs. "claude-klabauter " + the relative path being
    two whitespace-split tokens) -- so a resolving machine renders 50 words,
    a non-resolving one 51. The ceiling stays at 52 to cover BOTH outcomes
    with a word of headroom, since which rung fires is a property of the
    host, not of this function's own text, and the test must pass either
    way.

    Moved a third time, 52 -> 58 (2026-08-04, plan tier-f-is-grant-gated
    C3). The PM ruled that the grant ask IS the escape hatch for Tier F, so
    the lede has to LEAD with that ask and name whose utterance goes in the
    quotes -- content this budget had never priced, because until that
    ruling a refused caller still had the fast suite to fall back on. Prose
    paid what it could (the "(their exact words go in the quotes)"
    parenthetical went, since the `grant pm "<verbatim PM utterance>"`
    example line shows it structurally). The rest was first bought by
    deleting the node-id and `-k` example lines down to a single worked
    example -- the third time this budget has been paid in offers, and
    reverted for the same reason as the first two. A caller refused
    mid-session is being told to run something NARROWER than what they
    tried, and one file-path example does not teach narrower-than-a-file.
    Both lines restored; 56 rendered words, two of headroom.

    So the rule this file enforces is ordered: an offer the reader can take,
    and a route to what was moved out, both outrank the number. If a future
    edit needs room, take it from prose or raise this ceiling deliberately --
    do not buy it by dropping the override note, the pointer, or the worked
    scoped-test examples.

    Moved a fourth time, 58 -> 62 (2026-08-11, this dispatch, PM-raised
    break-class fix): `operator_override_note`'s reshape -- dropping the
    disclaimer register that two independently-dispatched agents classified
    as prompt injection, and the pasteable `KEY=1` assignment shape that was
    the other half of that tell -- grew the note itself by a few words (it
    now states the key's unusability as a plain fact, plus an explicit
    "unsettable from inside this session" clause, instead of a shorter
    disclaimer clause). That growth is exactly the "load-bearing" case this
    docstring's own rule names: the override note is one of the three things
    this budget is forbidden to buy room by dropping. Ceiling raised with 2
    words of headroom over the observed 60.
    """
    rendered = _ctsi._deny_reason_grant("pytest", "pytest tests/", is_tie=True)
    word_count = len(rendered.split())
    assert word_count <= 62, (
        "tie-branch deny text grew to %d words (budget: 62) -- see this "
        "test's docstring and _deny_reason_grant's; take the words back from "
        "prose, not from the override note, the Grant detail pointer, or the "
        "worked scoped-test examples, each of which has already been deleted "
        "once to buy budget" % word_count
    )


def test_deny_reason_subagent_directory_render_never_names_an_override_route() -> None:
    """Inverted (was: `..._render_carries_reachability_constraint`, which
    asserted `operator_override_note`'s output WAS present). The current
    render is the directory-arg-refusal shape (DR-088 R9) with no
    override-doc pointer at all -- positively asserts both the absence of
    any override-note fragment AND the presence of the real, current
    refusal text, so this cannot pass vacuously on a render carrying
    neither."""
    rendered = _ctsi._deny_reason_subagent_directory(
        ["tests/some_dir"], "pytest tests/some_dir", []
    )
    note = operator_override_note(_ctsi._OVERRIDE_ENV_VAR, payload=None)
    assert note == ""
    assert _ctsi._OVERRIDE_ENV_VAR not in rendered
    assert "unsettable from inside this session" not in rendered
    assert "Directory arguments are refused for dispatched agents (DR-088 R9" in rendered


class TestDetectorSelfTest:
    """Positive/negative controls against `assert_render_carries_
    reachability_constraint` itself -- the runtime-scan analog of
    `test_no_handwritten_override_clauses.py`'s own `TestDetectorSelfTest`,
    proving this gate fires on the exact defect shape it exists to catch,
    including the specific partial-tuple-% shape the static folder misses."""

    def test_positive_bare_env_var_instruction_is_caught(self) -> None:
        text = "Override (rare-use — read the guard source before invoking):\n  COORDINATOR_OVERRIDE_FOO=1"
        with pytest.raises(AssertionError):
            assert_render_carries_reachability_constraint(text, context="synthetic")

    def test_positive_partial_tuple_percent_format_shape_is_caught(self) -> None:
        """Reproduces the EXACT shape `_deny_reason_grant` had before this
        fix: a `%`-tuple mixing runtime-only values with a module constant,
        already RENDERED (not folded) -- the shape the static AST gate
        cannot see through, proven caught here via runtime text instead."""
        detected, cmd_safe = "pytest", "pytest tests/"
        override_env_var = "COORDINATOR_OVERRIDE_SYNTHETIC_CASE"
        rendered = (
            "Detected: %s\nCommand: %s\n\n"
            "Override (rare-use — read the guard source before invoking):\n"
            "  %s=1" % (detected, cmd_safe, override_env_var)
        )
        with pytest.raises(AssertionError):
            assert_render_carries_reachability_constraint(rendered, context="synthetic")

    def test_negative_note_produced_by_the_real_builder_is_not_caught(self) -> None:
        """2026-08-11 second reshape (see `_REACHABILITY_MARKER`'s own
        comment above): the builder no longer names an env var at all, so
        `_VIOLATION_RE` can never match its output -- asserted directly,
        not merely inferred from `assert_render_carries_reachability_
        constraint` passing over zero matches (AC-5: a gate that passes
        vacuously proves nothing)."""
        text = "Something happened.\n\n" + operator_override_note(
            "COORDINATOR_OVERRIDE_FOO", payload={"session_id": "sess-c1d-em"}
        )
        assert not _VIOLATION_RE.search(text), (
            "the real builder's output unexpectedly matched the hand-written-assignment "
            "violation pattern: %r" % text
        )
        assert_render_carries_reachability_constraint(text, context="synthetic")

    def test_negative_bare_var_name_with_no_equals_one_is_not_caught(self) -> None:
        text = "The env var is COORDINATOR_OVERRIDE_FOO, mentioned in prose only."
        assert_render_carries_reachability_constraint(text, context="synthetic")

    def test_negative_marker_present_but_too_far_away_is_still_caught(self) -> None:
        """A marker present SOMEWHERE in a long message, but not attached to
        the specific instruction, must not launder the violation -- proves
        this is a proximity check, not a whole-text substring check."""
        padding = "x" * 200
        text = (
            "Override: COORDINATOR_OVERRIDE_FAR_AWAY=1\n" + padding
            + " (unsettable from inside this session)"
        )
        with pytest.raises(AssertionError):
            assert_render_carries_reachability_constraint(text, context="synthetic")
