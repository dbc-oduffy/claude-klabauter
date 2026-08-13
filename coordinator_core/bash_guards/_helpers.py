"""coordinator_core.bash_guards._helpers -- shared primitives for the
PreToolUse:Bash guard cohort (W3a/W3b naked-Python hook migration).

Two families of shared primitive live here:

1. Identity resolution -- a thin RE-EXPORT of
   ``coordinator_core.subagent_sandbox.engine``'s resolver functions. Per the
   W3a recipe (bash-to-python-migration/W3a-preuse-bash-recipe.md \xa7(a)),
   the bash-guard cohort MUST reuse that engine's resolver rather than
   authoring a second ``coordinator_core.identity`` module -- Python's import
   system realizes the AC4 "one resolver" reconciliation, not a parallel
   module. This module re-exports:
     - ``resolve_git_root``
     - ``_canonical_agent_id``
     - ``_read_backpointer_subagent_type``
     - ``resolve_effective_types``
   Each per-guard module in this package should call
   ``resolve_effective_types(payload, git_root)`` ONCE (via the dispatcher, or
   locally if run standalone) and then pick whichever leg(s) of the returned
   3-tuple ``(agent_id, agent_type, subagent_type)`` its own bash predecessor
   consumed -- that choice is a per-check *consumption* decision, not a
   resolver change (recipe \xa7(a), "one per-check divergence to preserve").

2. Two new shared predicates that had no Python port before this module
   (recipe \xa7(a) "Confined-findings-agent SSOT gap" + \xa7 Summary items 1-2):

   - ``is_confined_findings_agent(effective_type)`` -- port of example-doctrine-repo's
     ``coordinator/lib/coordinator-session.sh``'s ``_cs_is_confined_findings_agent``.
     SSOT for the guard-before-grant set consumed by
     ``block-reviewer-bash-outside-allowlist.sh`` (fold-candidate #2). A
     hardcoded single-member set (``coordinator:code-reviewer``), mirroring
     the bash lib's CURRENT set byte-for-byte -- NOT a new YAML policy
     surface (recipe: "do not invent a new YAML policy surface for a
     1-member set"). Deliberately lives here, NOT inside
     ``subagent_sandbox`` -- that package's confined/exempt sets are a
     *different* concept (per-``SubagentPolicy``, YAML-driven, keyed for the
     write-sandbox), and conflating the two confinement concepts under one
     policy object would be a regression, not a simplification.

   - ``csn_check(comp)`` -- port of example-doctrine-repo's
     ``coordinator/bin/lib/coordinator-safe-name.sh``'s ``csn_check``. The
     canonical basename-legality predicate: byte-for-byte faithful to the
     bash case-statement order (trailing dot, trailing space, then each
     NTFS-illegal char in the bash's exact case order, then ASCII control
     chars). This is the ONE shared helper the recipe calls for
     (\xa7 Summary item 2: "needed by fold-candidate #5's Bash leg AND its
     Write/Edit sibling -- one shared helper, not two"). The Write/Edit
     sibling (``coordinator_core.write_guards.block_illegal_filename``)
     previously carried its own private inline port (``_csn_check``); that
     module now imports this shared copy instead of re-defining it, closing
     the "one shared helper, not two" gap the recipe flagged. The (still
     unbuilt, W3b-later) Bash-leg guard for ``block-illegal-filename.sh``
     must import THIS function too, not author a third copy.

3. Git subcommand OPTION-inspection primitives (2026-07-25 P0 fix, added by
   this dispatch), extracted from
   ``block_reviewer_bash_outside_allowlist``'s Tier A option-surface
   hardening (that module's own docstring "Divergence 5") and shared with
   ``block_subagent_destructive_action``'s safe-forward git-subcommand gate,
   which carried the IDENTICAL defect: both guards allowlist a set of git
   subcommands by NAME (``show``/``diff``/``log``/... ) and, pre-fix, granted
   the allow for the WHOLE remaining command line without inspecting a single
   option token -- confirmed live: ``git show --output=<path> HEAD`` and
   ``git log --output=<path>`` both create/overwrite an arbitrary file.
   ``prefix_denies``/``find_git_diff_family_write_flag`` are the ONE
   authoritative implementation of that write-flag scan; neither guard
   module may hand-duplicate a second copy -- that is precisely the
   drift-back-open shape both modules' own docstrings warn about for their
   *other* shared primitives.

   - ``prefix_denies(token, prefix)`` -- hyphen-boundary prefix match: denies
     if ``token`` is exactly ``prefix``, or ``prefix`` followed by any
     character OTHER than ``-``. Closes the attached-no-``=`` shape
     (``--output/tmp/x``) that a bare exact-or-``=``-only check misses,
     while the hyphen-boundary exception keeps a real sibling flag family
     (``--output-indicator-{new,old,context}=X``) allowed.
   - ``scan_tokens_until_separator(tokens)`` -- returns ``tokens`` up to
     (excluding) the first bare ``--`` pathspec/option terminator, or all of
     ``tokens`` if none is present. Git option parsing ends at a bare
     ``--``; a token beyond it is a pathspec/positional operand (a file
     literally named ``--output=x``), never an option, and must not be
     flag-matched.
   - ``find_git_diff_family_write_flag(tokens)`` -- scans
     ``scan_tokens_until_separator(tokens)`` for ``--output``/``-o``
     (arbitrary file write) or ``--ext-diff`` (external diff driver, an
     exec vector achievable without the already-denied ``-c`` global
     option) and returns the first offending token, or ``None`` if clean.
     ``tokens`` is expected to be the argv slice AFTER the subcommand token
     (each guard resolves that slice its own way -- Tier A's
     ``_git_command_tokens`` walk vs. the destructive guard's
     ``remaining`` from ``_real_git_subcommand`` -- but both hand this
     function the same shape: post-subcommand option/operand tokens).

4. ``is_trivial_reason(reason)`` / ``TRIVIAL_REASON_MIN_LEN`` (2026-07-30
   triviality-bar consolidation + tightening, PM-authorised) -- the ONE
   non-triviality bar for a human-authored reason/justification string,
   consolidated here from two byte-for-byte duplicate copies in
   ``write_guards/nudge_improvement_queue_write.py`` and
   ``write_guards/nudge_baton_body_bar.py`` per the ``csn_check`` precedent
   above (item 2): a predicate shared across the bash_guards/write_guards
   split lives HERE, not in a second ``write_guards/_helpers.py``, even
   though neither call site is itself a bash guard. Tightened in the SAME
   dispatch that consolidated it -- landing the tightening in two
   already-diverged copies was exactly the risk consolidating first was
   meant to close. Both call sites bind their local ``_is_trivial_reason``
   name to this function via a plain import; see this function's own
   docstring for the character-variety rule and the terse-reason test table
   it was picked against.

Parity oracles:
  Port of: coordinator-session.sh \xa7 _cs_is_confined_findings_agent (example-doctrine-repo e34f2484, 2026-07-22)
  Port of: coordinator-safe-name.sh \xa7 csn_check (example-doctrine-repo 721a71f4, 2026-07-21)
  Ported from the retired example-doctrine-repo bash guard
  ``block-reviewer-bash-outside-allowlist.sh`` (deleted 2026-07-16, example-doctrine-repo
  ``2f8b8450``).
Spec backlink: scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md \xa7(a), \xa7 Summary items 1-2
"""

from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional

# --- (1) Identity resolver: thin re-export, NOT a new module -----------------
# Per recipe \xa7(a): "do NOT author coordinator_core.identity. Extend/reuse
# coordinator_core/subagent_sandbox/engine.py". Importing these names FROM
# this module (rather than requiring every bash-guard module to import
# subagent_sandbox.engine directly) is the "tiny re-export shim" the task
# explicitly permits so the bash_guards package has ONE resolver import site.
from coordinator_core.subagent_sandbox.engine import (  # noqa: F401
    resolve_git_root,
    resolve_effective_types,
    _canonical_agent_id,
    _read_backpointer_subagent_type,
)
from coordinator_core.session.identity import resolves_em_audience

# AC5 (docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md, C2):
# a plain re-use of C1's dispatch-seam roster resolver, NOT a second roster
# implementation. `resolve_roster()` is the one function that already unions
# the three legitimate-dispatch sources (example-doctrine-repo policy map keys, coordinator
# agents, harness built-ins + plugin agents) and fails CLOSED on an
# unreadable roster -- see that module's own docstring. This bash-guard
# package borrows it for the SAME reason it borrows `resolve_effective_types`
# above: one resolver, not a parallel one.
#
# DEFERRED, NOT re-exported at module level (2026-08-13 hot-path import-budget
# fix): a module-level `from coordinator_core.hooks.block_unenumerated_agent_type
# import resolve_roster` drags in `coordinator_core.hooks`'s package `__init__`
# and its full eager registration (18 submodules) on EVERY `write_guards.engine`
# / `bash_guards.dispatch` import -- the exact regression commit `670cf7878`
# (2026-08-10) introduced, doubling `write_guards.engine`'s import cost against
# `coordinator_core/benchmarks/import-budget-manifest.json`'s hot-path budget.
# `_resolve_roster()` below imports lazily, ONLY when
# `is_confined_by_roster_absence` actually needs the roster (the same
# already-documented "walks real disk I/O" fallback path). Mirrors
# `coordinator_core.session.core._psutil()`'s cache-on-the-module-attribute-
# itself shape byte-for-byte, for the SAME reason: it is what keeps
# `monkeypatch.setattr(_helpers, "resolve_roster", ...)` working unmodified
# (see this package's `tests/test_block_reviewer_bash_outside_allowlist_
# roster_absence.py`) -- a private `_resolve_roster_mod` cache would silently
# break that patch point. DO NOT re-flatten this back to a module-level
# import; it will re-open the exact regression this fix closes.
_UNRESOLVED = object()
resolve_roster = _UNRESOLVED  # type: ignore[assignment]


def _resolve_roster_accessor():
    """Lazily import and cache ``resolve_roster`` on this module's own
    attribute (see the negative-spec comment above this cache's
    declaration). Returns the callable; never calls it.
    """
    global resolve_roster
    if resolve_roster is _UNRESOLVED:
        from coordinator_core.hooks.block_unenumerated_agent_type import (
            resolve_roster as _imported_resolve_roster,
        )

        resolve_roster = _imported_resolve_roster
    return resolve_roster

__all__ = [
    "resolve_git_root",
    "resolve_effective_types",
    "_canonical_agent_id",
    "_read_backpointer_subagent_type",
    "is_confined_findings_agent",
    "is_confined_by_roster_absence",
    "csn_check",
    "prefix_denies",
    "scan_tokens_until_separator",
    "find_git_diff_family_write_flag",
    "operator_override_note",
    "emit_kind_resolution_failure_signal",
    "is_trivial_reason",
    "TRIVIAL_REASON_MIN_LEN",
    "resolve_override_keys_doc_display",
]


# ---------------------------------------------------------------------------
# (0) Kind-resolution-failure instrumentation -- measurement only, and the
#     TEXT must report what the CALLING GUARD actually did, never assert a
#     package-wide default OR a hand-passed claim about the outcome. The
#     hard-deny identity family (block_subagent_commit,
#     block_subagent_destructive_action) confine an unresolvable-kind
#     subagent (fail-closed default, 2026-07-30 fix for a fail-open bypass
#     where an unreadable backpointer chain collapsed to "no identity" and
#     was silently allowed through a CLASS = "hard-deny" guard). The two
#     plan-body guards (block_subagent_plan_body_bash_write,
#     write_guards.block_subagent_plan_body_write) do NOT confine on this
#     path -- they are per-kind policy (enricher/review-integrator are
#     legitimate plan-body editors), and their shared 2026-06-09 PM ruling
#     keeps lookup-fail-is-allow deliberately.
#
#     THIS WAS FIRST BUILT with a hand-passed ``disposition`` string
#     argument (2026-07-30, same day) -- and it drifted from the actual
#     verdict on the very next revert (a call site kept asserting CONFINED
#     after its own guard was reverted to allow). A disposition stated IN
#     PARALLEL with the verdict, instead of DERIVED from it, is two sources
#     of truth that can silently disagree -- exactly the guard-message-
#     asserts-an-untrue-thing defect class this whole workstream exists to
#     close, reproduced a third time inside this instrumentation itself.
#     Fixed by removing the parallel channel: the caller passes the ACTUAL
#     return value it is about to emit (the verdict -- ``None`` for allow,
#     the deny envelope dict for deny), and this function reads the
#     disposition off that value. There is nothing left for a future call
#     site to get wrong, because there is no second, independently-typed
#     claim to keep in sync.
#
#     We do not yet know how OFTEN the backpointer read actually fails in
#     practice, so this one-line, non-fatal stderr signal exists purely to
#     measure that before any future decision about the per-kind policy
#     paths -- it changes no ALLOW/DENY outcome itself. Named legs only, no
#     payload contents (no agent_id, no cwd, no command text).
# ---------------------------------------------------------------------------


def emit_kind_resolution_failure_signal(
    guard_name: str,
    agent_id: str,
    git_root: Optional[str],
    verdict: Optional[Dict[str, object]],
) -> None:
    """Print a one-line stderr signal when a subagent's KIND could not be
    resolved -- i.e. ``agent_id`` (raw) was present, so the caller is known
    to be a subagent, but neither ``agent_type`` (payload leg) nor
    ``subagent_type`` (backpointer leg) resolved to a non-empty string.

    Names WHICH leg failed (canonicalization vs. backpointer-read) and
    whether ``git_root`` was empty, matching the level of detail
    ``bash_guards.dispatch``'s own crashed-advisory-guard stderr lines carry
    -- structural facts, not payload contents.

    ``verdict`` MUST be the EXACT value the calling guard's ``check()`` is
    about to return for this call -- ``None`` (allow) or the
    ``hookSpecificOutput``/``permissionDecision: "deny"`` envelope (deny).
    Call this at the guard's own final decision point, after every branch
    that could change the outcome has already run, never earlier -- the
    disposition reported is read off THIS value, not asserted
    independently, so it cannot drift from what the guard actually did.
    An unrecognized shape (neither ``None`` nor a deny envelope) reports no
    disposition rather than guessing one.
    """
    if not agent_id:
        leg = "agent_id-canonicalization (raw agent_id did not match either accepted id shape)"
    elif not git_root:
        leg = "backpointer-read (git_root empty/unresolvable)"
    else:
        leg = "backpointer-read (missing, unreadable, or malformed chain)"

    if verdict is None:
        outcome = "this guard ALLOWS it (verdict: allow)"
    elif (
        isinstance(verdict, dict)
        and isinstance(verdict.get("hookSpecificOutput"), dict)
        and verdict["hookSpecificOutput"].get("permissionDecision") == "deny"
    ):
        outcome = "this guard CONFINES it (denies) (verdict: deny)"
    else:
        outcome = "disposition not reported (unrecognized verdict shape)"

    print(
        "%s: kind-resolution-failed -- leg=%s, git_root=%s. Caller is a "
        "subagent (agent_id present) but its kind is unknown; %s."
        % (guard_name, leg, "present" if git_root else "empty", outcome),
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# (2z) Override-instruction SSOT -- the one place the escape-hatch sentence
#      is written
# ---------------------------------------------------------------------------


#: Reference doc carrying the full bypass-options content this function used
#: to inline on every firing (the two relayable routes, the CONFINEMENT_DENY
#: caveat, the pre-launch-only env-var constraint, and the generated
#: enumeration of every COORDINATOR_OVERRIDE_*/COORDINATOR_ALLOW_* key). This
#: module never reads the file, it only names it.
#:
#: Repo-root-relative. This is the RESOLUTION form -- what a caller joins to a
#: repo root to find the file on disk (the retention suite does exactly that).
OVERRIDE_KEYS_DOC = "docs/reference/guard-override-keys.md"

#: The DISPLAY form -- a repo-qualified hint, e.g.
#: ``"claude-klabauter docs/reference/guard-override-keys.md"``. Not
#: interchangeable with ``OVERRIDE_KEYS_DOC`` above: that constant is the
#: file-resolution form a caller joins to a repo root, this is what a reader
#: of a guard MESSAGE sees.
#:
#: These guards are not claude-klabauter-local: example-doctrine-repo's PreToolUse shim resolves this
#: engine and runs the guard logic in-process for EVERY repo on the machine,
#: so the reader of this pointer is usually sitting in some other repo's
#: tree, where a bare `docs/reference/...` resolves to nothing. Naming the
#: repo matches the convention CLAUDE.md already uses for cross-repo
#: citations ("example-doctrine-repo coordinator/docs/wiki/..."). NEGATIVE SPEC: do not
#: collapse these two constants back into one -- the file-resolution caller
#: and the message reader need different strings.
#:
#: 2026-08-05 (PM-raised, break-class): this used to be only the FALLBACK,
#: with a resolver (``_resolve_override_keys_doc_display``, since reduced to
#: a trivial wrapper below) preferring an absolute, in-process-resolved path
#: instead. That absolute form was wrong on two independent axes at once:
#: it interpolated this operator's home directory and repo name into every
#: guard message the suite emits (a machine-path leak matching the class
#: ``check-machine-path-leak.py`` exists for, though that checker's scope is
#: `settings.json`/`working-repos.yaml` only -- it never scanned
#: runtime-rendered guard text, which is why it missed this), AND it only
#: ever resolved correctly in the SAME process that rendered it -- wrong
#: shape on Windows (a POSIX-joined path there is meaningless) and wrong for
#: any other username or checkout location, i.e. every machine but the one
#: that produced it. A pointer that does not resolve for a stranger on a
#: different OS is not "correct but sensitive", it is simply broken, and
#: this repo ships as an OSS mirror -- every downstream reader would have
#: hit that. The repo-qualified relative form is the only one of the two
#: that is portable AND leaks nothing, so it is now unconditional.
OVERRIDE_KEYS_DOC_DISPLAY = "claude-klabauter " + OVERRIDE_KEYS_DOC


def _resolve_override_keys_doc_display() -> str:
    """Render the override-doc pointer for a guard MESSAGE.

    Reduced (2026-08-05, PM-raised, break-class) from an in-process resolver
    that preferred an absolute, machine-resolved path to a trivial return of
    the repo-qualified ``OVERRIDE_KEYS_DOC_DISPLAY`` constant. The absolute
    form was strictly worse than this hint on every axis that matters for a
    guard MESSAGE (as opposed to a command a reader pastes and runs, e.g.
    ``block_unauthorized_claude_md_write._grant_cli_invocation()``, which
    legitimately still resolves an absolute, in-process root because that
    text is only ever read on the SAME machine that renders it, immediately,
    by the operator about to run it): it leaked this operator's home
    directory and repo name into every guard firing, AND it only resolved
    correctly in the process that rendered it -- wrong on Windows, wrong for
    any other username, wrong for any other checkout location. See
    ``OVERRIDE_KEYS_DOC_DISPLAY``'s own docstring for the full history.

    Kept as a function (rather than inlining the constant at both call
    sites) purely for import-compatibility: ``bash_guards.dispatch`` and
    ``write_guards.engine`` (via the public alias below) both already bind a
    local name to this callable, and changing that shape is out of scope
    here.

    Still never raises -- there is nothing left to raise on, but the
    contract (called on the hot PreToolUse path, on every guard firing) is
    unchanged, and a future edit here must preserve it.
    """
    return OVERRIDE_KEYS_DOC_DISPLAY


def resolve_override_keys_doc_display() -> str:
    """Public alias for ``_resolve_override_keys_doc_display``.

    A second caller package (``write_guards.engine``) needs this pointer
    without importing a leading-underscore, package-internal name across a
    package boundary. This wrapper is the promoted, intentionally-shared
    entry point; the underscore-prefixed original is kept working unchanged
    for ``operator_override_note`` and any other in-package caller.

    # Review: coordinator:code-reviewer -- Finding 2, private cross-package
    # import (write_guards.engine importing bash_guards._helpers's
    # underscore-prefixed name directly, unexposed via __all__).
    """
    return _resolve_override_keys_doc_display()


def operator_override_note(
    env_var: str,
    *,
    payload: Optional[Dict[str, Any]],
    git_root: Optional[str] = None,
    reason_placeholder: Optional[str] = None,
) -> str:
    """Render the ONE short pointer every guard message that names an escape
    hatch appends -- the SSOT this whole function exists for.

    ``reason_placeholder`` (2026-07-30 P1 fix; reshaped again 2026-08-11,
    see NEGATIVE SPEC 4): originally rendered ``env_var`` as a REASON-shaped
    assignment (``VAR="<placeholder>"``) instead of the default flag-shaped
    ``VAR=1``, because two callers (``COORDINATOR_QUEUE_PUNT``,
    ``COORDINATOR_BATON_BODY_PUNT``) are reason-shaped, not flag-shaped:
    their own ``_is_trivial_reason`` guard denylists the literal string
    ``"1"``, so a flag-shaped render was a remediation their OWN acceptance
    path would refuse. As of the 2026-08-11 reshape below, NEITHER branch
    renders an assignment at all -- the parameter now only selects which
    parenthetical describes the key's shape (a bare flag vs. a one-sentence
    reason), never a pasteable ``VAR=1``/``VAR="..."`` literal. Default
    (``None``) renders the flag-shaped parenthetical; passing
    ``reason_placeholder`` (any non-``None`` value -- the placeholder text
    itself is no longer interpolated) renders the reason-shaped one. This is
    still an additive parameter, not a second builder: one builder, one
    SSOT, per NEGATIVE SPEC 3 below.

    Cut a second time (2026-07-30, same day as H12) from a ~50-word inline
    note to a single-line pointer, per PM ruling: the note fired in FULL on
    every guard firing, so its ~50 words were paid by every advisory message
    in the suite regardless of how many times a session had already seen it.
    A session-once dedup mechanism (session-keyed marker file) was drafted
    for this cut and discarded before landing -- a per-firing pointer is
    cheaper than session-keyed state and has nothing to get wrong, since
    there is no marker file, no atomicity concern, and no fail-open leg to
    reason about. The two relayable routes (``!``-prefixed prompt,
    blanket-disarm marker) and the CONFINEMENT_DENY caveat now live in
    ``OVERRIDE_KEYS_DOC`` instead of inline; this function keeps only the ONE
    fact that must survive at decision time -- the env var this specific
    guard names is pre-launch-only, so an agent does not burn a turn trying
    to set it from inside the session. Full prior rationale (subprocess-per-
    event boundary, the ``export``-in-backticks liveness-gate trap, empirical
    unreachability verification) lives in
    ``tests/test_override_unreachability_boundary.py`` and this function's
    git history, not restated here.

    RESHAPED 2026-08-11 (PM-raised, break-class; cross-repo memo from
    example-market-data-repo-em plus two prior siblings): the previous render --
    ``"Bypass options for a human operator, not this agent: VAR=1
    (pre-launch only) -- full list: ..."`` -- carried two independent
    injection tells. (1) A pasteable ``KEY=1`` assignment sitting inside
    tool output reads as an INSTRUCTION, not a fact, to a model scanning for
    actionable text. (2) The ``"...not this agent:"`` disclaimer is exactly
    the deniability-preserving phrasing an attacker writes to make an agent
    feel authorised while covering itself -- a tell, not a safeguard. Two
    independently-dispatched agents (a code-reviewer, a review-integrator,
    no shared context) classified this line as prompt injection and
    declined to act; a prior incident hit the same defect four more times
    (see ``session.guard_unlock_sentinel.annotate_deny``'s own docstring,
    items 1-2, for the sibling banner's parallel history). The fix removes
    the assignment form entirely (the key is now named BARE, never
    ``KEY=1``/``KEY="..."``) and replaces the disclaimer with the load-
    bearing fact that was doing the disclaimer's job badly: this env var is
    read once at hook-process spawn, so it is STRUCTURALLY unusable from
    inside a running session, independent of who is asking. Stating that
    plainly is what stops an agent burning a turn on it -- no disclaimer
    needed to carry that weight. Rendered shape, e.g.: ``"Override key
    (flag), unsettable from inside this session -- read only at
    hook-process spawn: COORDINATOR_ALLOW_X -- key list: claude-klabauter
    docs/reference/guard-override-keys.md"`` -- kept terse (this reshape
    stayed inside the pre-existing byte/word budgets in
    ``test_operator_override_note_retains_affordances.py``, not a new,
    looser one).

    NEGATIVE SPEC -- do not re-add an ``export ... =1`` instruction (the
    exact hand-written shape ``test_no_handwritten_override_clauses.py``
    exists to catch), and do not re-inline the two relayable routes or the
    CONFINEMENT_DENY caveat here -- they belong in ``OVERRIDE_KEYS_DOC`` now,
    and duplicating them back into this per-firing string is the exact
    regression this cut exists to prevent.

    NEGATIVE SPEC 2 -- keep the word "export" and the env-var name OUT of
    backticks. ``_alternative_liveness``'s extractor reads a backticked span
    as an EXECUTABLE alternative and probes it on PATH; neither resolves, so
    backticking either registers as a DEAD alternative and fails the
    liveness gate (found by that gate on this function's first draft).

    NEGATIVE SPEC 3 -- do not fork a second builder for the reason-shaped
    case. A call site needing the reason-shaped parenthetical passes
    ``reason_placeholder=`` (any non-``None`` value); it does not hand-write
    its own pointer sentence (the exact "guard names a remediation that
    cannot run" defect class this function exists to close, reproduced a
    second time if call sites start forking their own render).

    NEGATIVE SPEC 4 (2026-08-11, this reshape) -- no assignment form, ever.
    Neither branch may render ``env_var`` concatenated with ``=``, ``"``, or
    any other character that makes the key plus a value look pasteable and
    runnable. The key is named bare; the parenthetical describes its SHAPE
    in prose ("a bare flag" / "a one-sentence reason") instead of showing a
    literal value. This is the invariant the whole reshape exists to
    enforce, and it is checked by construction in
    ``test_operator_override_note_no_assignment_form.py`` -- do not weaken
    that test to re-permit a ``KEY=`` substring in this function's output.

    RESHAPED AGAIN 2026-08-11 (same day, second reshape; PM-raised,
    break-class; docs/plans/2026-08-11-guard-messages-point-to-docs-never-
    name.md) -- the reshape immediately above still NAMED the override key,
    bare, in every firing. Twelve rounds of rewording this function had not
    closed the "reads as injection" defect (the specimen: dispatched agents
    across the fleet reading "blocked, but here's a key you could try" as an
    offer to bypass the guard) because every round kept the SAME SPEECH ACT
    -- a denial that still names a bypass mechanism at the moment of denial.
    This reshape changes the speech act instead of the wording: the render
    below is a DOC POINTER ONLY. Neither ``env_var`` nor ``reason_placeholder``
    is interpolated into the returned string any more -- the function now
    returns the exact same literal string regardless of what either
    argument is.

    Both parameters are KEPT, not dropped, for two different reasons:
    ``env_var`` is a required positional at 92 call sites across 42 files,
    none of which change in this reshape (an SSOT edit, not a signature
    break); ``reason_placeholder`` is kept per NEGATIVE SPEC 3 below, which
    forbids forking a second builder for the 5 reason-shaped call sites.
    Neither argument currently changes this function's OUTPUT -- with no key
    rendered at all, the flag-shaped/reason-shaped distinction
    ``reason_placeholder`` used to select between has no reader any more.
    This is a live-but-currently-inert knob, stated here rather than left
    for a future reader to discover by diffing two calls: removing the
    parameter now would itself be the "fork a second builder" move the
    moment a future render needs to differentiate again, so it stays,
    documented as inert.

    The load-bearing "read only at hook-process spawn, unsettable from
    inside this session" fact this render used to carry inline moves
    wholly into ``OVERRIDE_KEYS_DOC`` -- the doc this pointer already sends
    the reader to -- rather than surviving in per-firing text.
    ``test_deny_text_reachable_override.py``'s reachability-marker gate and
    ``test_operator_override_note_retains_affordances.py`` are updated in
    the same dispatch to check for that fact in the doc instead of the note.

    NEGATIVE SPEC 5 -- do not re-interpolate ``env_var`` (or any per-guard
    identifier) into this function's return value. That is precisely the
    "offer to bypass the guard" speech act this reshape exists to remove;
    rewording it again without removing it is the exact failure mode twelve
    prior rounds already tried and the source memo (2026-08-11-example-retrieval-repo-
    ue-addon-em-guard-self-narration-reads-as-injection.md) reports as still
    unclosed.

    AUDIENCE-GATED, 2026-08-13 (this dispatch;
    tasks/guard-messages-keys/DECISIONS.md D1/D2; plan
    docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md).
    Every prior reshape above still rendered the doc pointer for EVERY
    audience -- a dispatched subagent included. A doc pointer is itself a
    statement that an unlock exists, which is a banned message shape for a
    subagent audience (plan AC-1) even with no key named and no assignment
    form. ``payload`` is now a REQUIRED keyword argument with NO DEFAULT --
    a call site missed by this migration must raise ``TypeError`` at
    collection, never silently keep the old always-render behaviour.

    Resolution: ``coordinator_core.session.identity.resolves_em_audience(
    payload, git_root)``. False (including on any resolution exception) ->
    return ``""`` -- not a shorter pointer, not "blocked, see docs", the
    empty string. True -> return today's doc-pointer literal, unchanged
    (plan AC-2's permitted shape). See ``resolves_em_audience``'s own
    docstring for the inverted-default rationale, the two-leg-not-three
    divergence from ``_blanket_disarm.py::_is_em_caller``, and why
    forgeability is out of scope here -- this function does not re-derive
    any of that.

    SPLICE CONTRACT for every caller (this module's own two internal call
    sites, below, and the ~106 call sites in other files C1b/C1c own):
    this function's return value must be safe to concatenate/join into a
    guard message with NO special-casing at the call site for the empty
    case -- no dangling connective (`` -- ``), no double space, no orphan
    trailing newline. The two shapes callers must pick between:
      - Append this call's result as the LAST element of a `" -- ".join(...)`
        (or equivalent) list of message parts, filtering out any empty
        string from that list before joining -- the empty case then simply
        contributes nothing.
      - Or, where the pointer is the ONLY content of a trailing sentence,
        gate the whole trailing sentence on ``bool(operator_override_note(...))``
        rather than always appending it.
    Do not hand-roll a third splice shape; both of the above degrade
    cleanly to "no trailing artefact" when this returns ``""``.

    NEGATIVE SPEC 6 (2026-08-13, this reshape) -- do not widen this
    function back to rendering for an unresolved/unknown audience. The
    default here is DENY-EMIT (return ``""``) on anything short of a
    positively-resolved EM audience, including "could not tell" -- that is
    the specific regression this whole plan exists to close, and reverting
    to "emit unless we positively know it's a subagent" reopens it.
    """
    if not resolves_em_audience(payload, git_root):
        return ""
    return "See %s for this guard's override keys." % _resolve_override_keys_doc_display()


# ---------------------------------------------------------------------------
# (2a) Confined-findings-agent SSOT
# ---------------------------------------------------------------------------

#: CONFINED SET -- originally mirrored coordinator-session.sh's
#: _cs_is_confined_findings_agent byte-for-byte as of the 2026-07-01
#: findings-agents-self-persist change (a single member; review personas
#: were removed from this set on 2026-07-01 -- see the bash lib's own
#: comment block for why). ``coordinator:executor`` was added 2026-08-01
#: (docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md, Amendment 1)
#: to close the ``python3 -c "...dispatch_message..."`` commit-bypass
#: incident, then REMOVED again 2026-08-03 on the PM ruling recorded at
#: DR-125 (docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md,
#: chunk C2): Bash-allowlist confinement is the wrong control for a
#: commit-shaped bypass once ``block_subagent_commit`` independently denies
#: the commit/push/stash/reset shape regardless of how it is invoked (C1
#: closed the remaining ``python3 -c``/interpreter-path text-matching hole
#: that motivated the original addition) -- narrowing this set back to
#: ``coordinator:code-reviewer`` removes duplicate, narrower-than-needed
#: confinement of the executor's general Bash surface without reopening the
#: commit bypass. REVISIT TRIGGER: re-adding ``coordinator:executor`` here is
#: warranted only by a NEW harm class reachable from bash that is neither
#: machine-degrading (already governed by the wider bash-allowlist ruleset
#: policy tests) nor commit-shaped (already governed by
#: ``block_subagent_commit`` independently of this set) -- e.g. a
#: qualitatively new bypass vector, not a re-litigation of the commit case
#: this ruling already settled. The SOLE consumer of this set,
#: ``block_reviewer_bash_outside_allowlist``, resolves a DIFFERENT, wider
#: per-type ruleset for ``coordinator:executor`` than for
#: ``coordinator:code-reviewer`` (see that module's own docstring Divergence
#: 9 and ``_DEFAULT_RULESET_TYPE_OVERRIDES``) — membership here is
#: SET-WIDE, but the allowed Bash surface per member is not. Adding a member
#: here is still the single edit point for CONFINEMENT membership itself,
#: matching the bash lib's "single source of truth" intent; it is a
#: SEPARATE, independent hardcoded set from
#: ``coordinator_core.session.core._CONFINED_FINDINGS_AGENTS`` (that
#: module's own SSOT for its own, unrelated consumer -- not re-exported from
#: here, and not affected by this edit).
_CONFINED_FINDINGS_AGENTS = frozenset({"coordinator:code-reviewer"})


def is_confined_findings_agent(effective_type: str) -> bool:
    """Port of ``_cs_is_confined_findings_agent <subagent_type>`` (bash).

    Returns ``True`` iff ``effective_type`` is a member of the confined
    findings-agent SET; ``False`` otherwise (including for ``""``/``None``).
    Callers (e.g. ``block-reviewer-bash-outside-allowlist``'s port) call this
    once per resolver leg (PRIMARY ``agent_type``, SECONDARY back-pointer
    ``subagent_type``) and OR the two booleans -- this function itself does
    not know about the dual-resolver shape, matching the bash predicate's
    single-argument contract.
    """
    return (effective_type or "") in _CONFINED_FINDINGS_AGENTS


def is_confined_by_roster_absence(effective_type: str) -> bool:
    """AC5 (2026-08-10, C2 of
    ``docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md``):
    an ``effective_type`` that is not on C1's dispatch-seam roster is
    UNTRUSTED -- confined -- by default, not exempt.

    Before this function existed, ``block_reviewer_bash_outside_allowlist.
    _is_confined_type`` treated non-membership in
    ``_CONFINED_FINDINGS_AGENTS`` (this module's OWN hardcoded set, above) as
    "not confined" -- i.e. UNRESTRICTED Bash. That default is correct for a
    known, enumerated type this project deliberately chose not to confine
    (``coordinator:enricher``, ``coordinator:executor``'s non-destructive
    siblings, etc. -- confinement per Amendment 2 in that guard's own module
    docstring is reserved for destructive-capable surfaces, not applied
    uniformly). It is WRONG for a type nobody enumerated at all: an invented
    ``subagent_type`` got a WIDER Bash surface than
    ``coordinator:code-reviewer``, this project's own findings agent --
    exactly the "less governed than any agent in the stable" defect this
    plan's Problem section names.

    This predicate distinguishes the two cases the membership check alone
    cannot: it is ``True`` (confine) only when ``effective_type`` is
    non-empty AND absent from ``resolve_roster()``'s union-of-three roster --
    an enumerated-but-not-explicitly-confined type (on the roster, not in
    ``_CONFINED_FINDINGS_AGENTS``) still returns ``False`` here, preserving
    its existing unrestricted Bash surface exactly as before this predicate
    existed.

    An empty/falsy ``effective_type`` returns ``False`` -- same convention as
    ``is_confined_findings_agent`` above: the caller resolves TWO legs
    (``agent_type``, back-pointer ``subagent_type``) and ORs the results, so
    an empty leg must contribute nothing to either direction, not force a
    confinement verdict off the OTHER leg's absence.

    A roster-load FAILURE (``resolve_roster()`` returning ``(None, reason)``
    -- an unreadable/unparseable peer-repo source) fails CLOSED, i.e. this
    returns ``True``: an unreadable roster degrades to "cannot confirm this
    type is legitimate", never to "assume it is fine and leave it
    unconfined" -- the same fail-closed direction C1's own roster-load-
    failure deny already takes, and the opposite of every OTHER roster-keyed
    lookup-miss this plan's census found.

    Called ONLY as a fallback, after the caller has already checked the
    cheaper ``bash_policy:`` key and ``is_confined_findings_agent`` legs --
    those two checks resolve the common case (a known confined type) without
    ever reaching this function's ``resolve_roster()`` call, which walks
    example-doctrine-repo's policy YAML, ``coordinator/agents/*.md``, and the plugin discovery
    tree -- real disk I/O, unlike the two cheap membership checks it
    supplements. This function does not itself defer that call further; the
    call-site ordering in ``block_reviewer_bash_outside_allowlist.
    _is_confined_type`` is what keeps the roster walk off the hot path for
    every already-confined or already-exempt-and-enumerated type.
    """
    if not effective_type:
        return False
    roster, error = _resolve_roster_accessor()()
    if roster is None:
        return True
    return effective_type not in roster


# ---------------------------------------------------------------------------
# (2b) csn_check -- shared basename-legality predicate
# ---------------------------------------------------------------------------

#: NTFS-illegal chars in csn_check's exact case-statement order
#: (Port of: coordinator-safe-name.sh, example-doctrine-repo 721a71f4, 2026-07-21). Order matters only for WHICH
#: hint is returned when a component has more than one illegal char -- the
#: deny/allow verdict (None vs non-None) is order-independent.
_ILLEGAL_CHARS_ORDER = (":", "?", "*", "<", ">", "|", '"', "\\", "/")


def csn_check(comp: str) -> Optional[str]:
    """Port of: coordinator-safe-name.sh's ``csn_check`` (example-doctrine-repo 721a71f4, 2026-07-21).

    Exit-code-0-if-safe / exit-non-zero-plus-reason-if-not becomes: return
    ``None`` if ``comp`` is safe for all target platforms (NTFS, macOS HFS+,
    Linux ext4, Git-Bash checkout); else return the "illegal_char_hint" token
    for the FIRST violation found, checked in this EXACT order (byte-for-byte
    parity with the bash case-statement sequence):
      1. trailing dot
      2. trailing space
      3. each NTFS-illegal char, in order: colon, question-mark, asterisk,
         less-than, greater-than, pipe, double-quote, backslash, slash
      4. any ASCII control character (0x00-0x1F or 0x7F DEL)
    """
    if comp.endswith("."):
        return "trailing dot"
    if comp.endswith(" "):
        return "trailing space"
    for ch in _ILLEGAL_CHARS_ORDER:
        if ch in comp:
            return ch
    for ch in comp:
        code_point = ord(ch)
        if code_point <= 0x1F or code_point == 0x7F:
            return "control character"
    return None


# ---------------------------------------------------------------------------
# (2c) is_trivial_reason -- shared non-triviality bar for a human-authored
# reason/justification string.
#
# Consolidated here (2026-07-30 triviality-bar tightening, PM-authorised)
# from two byte-for-byte duplicate copies that had drifted into
# ``write_guards/nudge_improvement_queue_write.py`` and
# ``write_guards/nudge_baton_body_bar.py`` -- tightening the rule in one
# copy and not the other is exactly the drift this module's own docstring
# (see item 2, ``csn_check``) already warns about for a different pair of
# call sites. Follows the SAME precedent as ``csn_check``: a predicate
# shared across the bash_guards / write_guards package split lives HERE, not
# in a parallel ``write_guards/_helpers.py`` (which would fork the
# shared-helper story into two homes rather than consolidating it), even
# though neither of this predicate's two call sites is itself a bash guard --
# ``csn_check`` sets that precedent for exactly this "module name is a poor
# fit, but the alternative is worse" tradeoff (see ``block_illegal_filename``,
# its Write/Edit-side importer).
#
# Both call sites (``nudge_improvement_queue_write._is_trivial_reason``,
# ``nudge_baton_body_bar._is_trivial_reason``) now bind their local name to
# THIS function via a plain import -- no re-export shim, no second
# definition. Their own module-level ``_TRIVIAL_REASONS``/
# ``_TRIVIAL_REASON_MIN_LEN`` constants are retired along with the local
# copies of the function; a caller needing the length floor imports
# ``TRIVIAL_REASON_MIN_LEN`` from here.
# ---------------------------------------------------------------------------

#: Exact-match denylist -- a known-lazy token, whitespace-collapsed and
#: lowercased before comparison.
_TRIVIAL_REASONS = frozenset({"1", "true", "yes", "y", "ok", "okay", "sure", "fine", "", "-", "x"})

#: Length floor -- unchanged from the pre-consolidation copies.
TRIVIAL_REASON_MIN_LEN = 12

#: Character-variety floor (2026-07-30 tightening): the number of DISTINCT
#: alphabetic characters (case-folded) a reason must contain. A string with
#: fewer than this many distinct letters is degenerate regardless of length
#: -- ``"aaaaaaaaaaaa"`` has 1, ``"abababababab"`` has 2, an all-digit string
#: like ``"123456789012"`` has 0. Set to 3 because every TERSE-but-genuine
#: reason considered while picking this threshold (see the test table this
#: dispatch added) clears it comfortably -- a real short phrase draws from
#: more than two distinct letters as a simple consequence of being English
#: prose, not padding.
_TRIVIAL_REASON_MIN_DISTINCT_LETTERS = 3


def is_trivial_reason(reason: str) -> bool:
    """Shared non-triviality bar for a human-authored reason/justification
    string -- applied identically everywhere a guard needs to tell a real
    reason from a degenerate placeholder, whether that string lives in a
    pre-launch-only operator env var (``COORDINATOR_QUEUE_PUNT``,
    ``COORDINATOR_BATON_BODY_PUNT``) or, since the 2026-07-30
    escape-mechanism rework, a DURABLE content field an agent writes
    directly (the ``justification:`` line on an improvement-queue entry) --
    the latter is a record the next reader relies on, not merely an
    operator-set toggle, which is why the bar below closes the
    pure-length-test gap the pre-tightening version left open (any 12-char
    string, including ``"aaaaaaaaaaaa"``, used to pass).

    Rejects, in this order:
      1. an exact match (whitespace-collapsed, case-insensitive) against a
         known-lazy token in ``_TRIVIAL_REASONS``;
      2. anything under ``TRIVIAL_REASON_MIN_LEN`` characters;
      3. a string whose alphabetic characters span fewer than
         ``_TRIVIAL_REASON_MIN_DISTINCT_LETTERS`` distinct letters -- this
         is what catches degenerate filler that clears the length floor by
         sheer repetition (``"aaaaaaaaaaaa"``, ``"abababababab"``) or by
         having no letters at all (``"123456789012"``).

    THE FAILURE MODE THIS DELIBERATELY AVOIDS is over-tightening: this
    predicate gates a DENY, so a bar that rejects a terse-but-genuine reason
    blocks real work and trains people to pad their justification just to
    get past it -- strictly worse than the gap being closed. It therefore
    does NOT enforce a minimum word count, a dictionary/spell check, or any
    NLP-based genuineness score; it only catches the three degenerate shapes
    named above. Confirmed against a table of realistic terse reasons
    (``"genuinely cross-cutting"``, ``"needs its own plan"``, ``"blocked on
    rag schema"``, ``"upstream fix pending"``) -- every one of those passes;
    see ``tests/test_is_trivial_reason.py`` for the full pass/fail table
    this threshold was picked against.

    Never raises: a non-string-shaped ``reason`` (e.g. ``None``) is treated
    as falsy up front (``if not reason``), same as the pre-consolidation
    copies.
    """
    if not reason:
        return True
    collapsed = re.sub(r"\s+", "", reason).lower()
    if collapsed in _TRIVIAL_REASONS:
        return True
    if len(reason) < TRIVIAL_REASON_MIN_LEN:
        return True
    distinct_letters = {ch for ch in collapsed if ch.isalpha()}
    return len(distinct_letters) < _TRIVIAL_REASON_MIN_DISTINCT_LETTERS


# ---------------------------------------------------------------------------
# (3) Git subcommand OPTION-inspection primitives (2026-07-25 P0 fix).
# Extracted from block_reviewer_bash_outside_allowlist's Tier A hardening;
# shared with block_subagent_destructive_action's safe-forward
# git-subcommand gate, which had the identical unpatched defect. See module
# docstring item 3 above for the full incident context.
# ---------------------------------------------------------------------------


def prefix_denies(token: str, prefix: str) -> bool:
    """Prefix match with a hyphen-boundary exception: ``token`` denies if it
    is exactly ``prefix``, or starts with ``prefix`` followed by any
    character OTHER than ``-``. This is what makes ``--output/tmp/x``
    (attached, no ``=``) deny -- a bare ``startswith(prefix)`` would ALSO
    close the ``--output=<path>``/bare cases, so this closes the last
    unclosed shape (attached long form with no ``=``) -- while the
    hyphen-boundary exception is what keeps ``--output-indicator-new=X``,
    ``--output-indicator-old=X``, and ``--output-indicator-context=X``
    (real, non-write git formatting flags) allowed, since each has a ``-``
    immediately after the ``prefix`` substring.
    """
    if token == prefix:
        return True
    if token.startswith(prefix):
        return token[len(prefix)] != "-"
    return False


def scan_tokens_until_separator(tokens: List[str]) -> List[str]:
    """Return ``tokens`` up to (excluding) the first bare ``--``
    pathspec/option terminator, or all of ``tokens`` if none is present.
    Git option parsing ends at a bare ``--``; a token beyond it is a
    pathspec/positional operand (a file literally named ``--output=x``
    given AFTER ``--``), never an option, and must not be flag-matched.
    """
    out: List[str] = []
    for tok in tokens:
        if tok == "--":
            break
        out.append(tok)
    return out


def find_git_diff_family_write_flag(tokens: List[str]) -> Optional[str]:
    """Scan ``tokens`` (the argv slice AFTER a git subcommand, e.g.
    ``show``/``log``/``diff``) for a write/exec-capable flag -- ``--output``
    and ``--ext-diff`` (bare, ``=``-form, or attached-no-``=``, via
    ``prefix_denies``), or ``-o``/``-o<path>`` (attached form). Stops at a
    bare ``--`` pathspec separator via ``scan_tokens_until_separator``.

    ``--output``/``-o`` write to an arbitrary caller-chosen file (confirmed
    empirically against real git: ``git show --output=<path>`` and
    ``git log --output=<path>`` both create/overwrite the target file).
    ``--ext-diff`` enables an external diff driver -- i.e. arbitrary command
    execution via ``diff.<driver>.command`` config, achievable without going
    through a denied ``-c`` global option.

    Returns the offending token, or ``None`` if the tokens are clean.
    """
    for token in scan_tokens_until_separator(tokens):
        if prefix_denies(token, "--output"):
            return token
        if prefix_denies(token, "--ext-diff"):
            return token
        if token == "-o" or (token.startswith("-o") and not token.startswith("--")):
            return token
    return None
