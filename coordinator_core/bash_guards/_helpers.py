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
from typing import Dict, List, Optional

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

__all__ = [
    "resolve_git_root",
    "resolve_effective_types",
    "_canonical_agent_id",
    "_read_backpointer_subagent_type",
    "is_confined_findings_agent",
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
    env_var: str, *, reason_placeholder: Optional[str] = None
) -> str:
    """Render the ONE short pointer every guard message that names an escape
    hatch appends -- the SSOT this whole function exists for.

    ``reason_placeholder`` (2026-07-30 P1 fix, this dispatch): renders
    ``env_var`` as a REASON-shaped assignment (``VAR="<placeholder>"``)
    instead of the default flag-shaped ``VAR=1``. Two callers
    (``COORDINATOR_QUEUE_PUNT``, ``COORDINATOR_BATON_BODY_PUNT``) are
    reason-shaped, not flag-shaped: their own ``_is_trivial_reason`` guard
    denylists the literal string ``"1"``, so the default ``VAR=1`` render is
    a remediation their OWN acceptance path would refuse -- confirmed live
    (``COORDINATOR_QUEUE_PUNT=1 (pre-launch only)`` printed by this very
    function, rejected by the guard printing it). Default (``None``) keeps
    the original ``VAR=1`` shape unchanged for the ~40 flag-shaped callers --
    this is an additive parameter, not a behavior change for existing call
    sites. One builder, one SSOT: a reason-shaped call site renders through
    this parameter, never by hand-writing its own pointer text.

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
    case. A call site needing ``VAR="<reason>"`` text passes
    ``reason_placeholder=``; it does not hand-write its own pointer sentence
    (the exact "guard names a remediation that cannot run" defect class this
    function exists to close, reproduced a second time if call sites start
    forking their own render).
    """
    value = '%s="%s"' % (env_var, reason_placeholder) if reason_placeholder is not None else "%s=1" % env_var
    return (
        "Bypass options for a human operator, not this agent: %s "
        "(pre-launch only) -- full list: %s"
        % (value, _resolve_override_keys_doc_display())
    )


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
