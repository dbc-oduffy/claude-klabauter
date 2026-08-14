"""
coordinator_core.session.tier_u_gate -- shared Tier-U shape gate for
resolve-and-execute CLIs.

Purpose: give every CLI that resolves a repo's configured test command and
then executes it in-process (``validate-fast-and-packageability.py``'s
``fast`` subcommand, ``workday-complete-step1-validate.py``'s Gate 2) ONE
shared decision -- classify the resolved command string's SHAPE with the
Bash-guard classifier already used at the PreToolUse layer, and refuse to
execute when the shape is Tier U (unscoped/full-suite) and the calling
session holds no live Tier-U grant.

This module exists so the algorithm is written exactly once. Two CLIs
independently re-implementing "classify, check a grant, refuse" is the
"two classifiers that disagree" failure the ruling memo warns against --
see the module-level negative spec below.

Spec backlink: cross-repo/archive/2026-07-25-doe-claude-em-validate-tier-u-
shape-ruling.md (R3, R4) -- amendment to
docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md.
R6/R7 amendment: cross-repo/archive/2026-07-25-doe-claude-em-dr088-marker-
scope-ruling.md.

R3 -- the process-boundary bypass (a resolver materializing a command
inside a CLI's own process, never passing through a ``PreToolUse(Bash)``
text match) is a gap in DR-088's Layer 3, not an accepted limit. This
module is the resolver-execution-seam check that closes it.

R4 -- a gate built on this module classifies and REFUSES; it never grants.
See the negative spec below -- this is enforced, not merely documented.

R6 -- a repo may DECLARE its fast tier legitimately unscoped via a
non-empty ``fast_tier_unscoped_reason`` prose value in
``coordinator.local.md`` frontmatter. This is the AUTHORITY question ("is
this caller authorized to run this shape here"), so it is read HERE, in
the shared caller-side helper -- never in the classifier
(``check_test_suite_invocation.classify_command``), which only answers
"what shape is this command." The declaration's reach is deliberately
narrow: it discharges the authority check for EXACTLY ONE string -- the
literal resolved ``fast_test_cmd`` for this repo -- never for any other
Tier-U command, and it is not a Tier-U grant (writes nothing, does not
touch ``check_tier_u_grant``, does not authorize ``full_test_cmd``). See
``_fast_tier_unscoped_declaration_covers`` below.

BEHAVIOR CONTRACT CHANGE (PM ruling, 2026-07-28) -- fail-closed default on
an UNCLASSIFIABLE command. Previously, a command that ``classify_command``
returned ZERO matches for (an opaque wrapper -- ``pnpm run tier:fast``,
``bash scripts/run-tests.sh --tier fast``, ``python
example_retrieval_repo_scripts/run_tier_tests.py --tier sufficient``) fell through
the same "no Tier-U matches" branch as a classified Tier-T/F command and
proceeded unconditionally -- a fail-OPEN hole: the whole ladder silently
bypassed for any repo whose fast tier is fronted by a wrapper the
classifier doesn't recognise. This is now split into two distinct cases:

  - Classified, non-U (at least one ``classify_command`` match, none of
    them Tier U) -- unaffected, still proceeds without touching any of
    the logic below.
  - UNCLASSIFIABLE (zero matches at all) -- now REFUSES unless the repo
    declares its breadth via a new ``fast_tier_shape:`` key in
    ``coordinator.local.md`` frontmatter, or the command carries no
    full-suite-capable runner footprint at all (see the FOOTPRINT NARROWING
    below):
      - ``fast_tier_shape: scoped`` -- the repo asserts its wrapper runs a
        bounded subset; proceeds.
      - ``fast_tier_shape: unscoped`` -- treated EXACTLY like a detected
        Tier-U command: falls through to the existing
        ``fast_tier_unscoped_reason``/``check_tier_u_grant`` path (that
        logic is reused, not duplicated, for this case).
      - absent, empty, whitespace-only, or any other value -- refuses,
        naming the exact key, both legal values, and this repo's own
        resolved ``fast_test_cmd`` string in the refusal message.

  Reach note: unlike ``fast_tier_unscoped_reason`` (scoped to exactly one
  literal command string -- see ``_fast_tier_unscoped_declaration_covers``
  above), ``fast_tier_shape: scoped`` has repo-wide reach over the
  unclassifiable case -- it asserts a general shape property of the
  repo's wrapper, not an authorization of one specific string, because an
  unclassifiable command has no Tier-U match to scope against in the
  first place.

FOOTPRINT NARROWING (2026-08-02) -- the fail-closed default above was
written for the opaque-wrapper case and applied to every unclassifiable
command, which is strictly broader than the hole it closes. Measured before
this narrowing, with ``repo_root`` = this repo: ``exit 0`` refused, ``python3
-m pytest coordinator/tests/test_claude_doe.py`` (one file) refused, and only
the verbatim configured ``fast_test_cmd`` proceeded -- via the R6 declaration
path, not via classification. Two consequences: the diff-scoped invocation
``validate-fast-and-packageability.py`` builds from the changed-test-file set
is refused by the same gate it is handed to for any repo whose scoped command
does not also containment-match its configured tier, and a fast tier
configured as a non-runner command (``true``) could never run at all.

So the unclassifiable-with-no-declaration case now consults
``check_test_suite_invocation.classify_runner_footprint`` before refusing:

  - ``RUNNER_FOOTPRINT_NONE`` -- every segment is provably incapable of
    spawning a test run (``true``, ``exit 3``, ``echo ok``). A command that
    runs no test runner cannot be an unscoped suite run, so refusing it was
    never this gate's business. Proceeds.
  - ``RUNNER_FOOTPRINT_SCOPED`` -- every runner invocation present was
    POSITIVELY parsed and found scoped (a path, node id, or ``-k``
    footprint). Scoped by construction, and exactly the shape the Bash-layer
    guard's own refusal message advertises as always allowed ("Run the tests
    you actually touched: ``pytest path/to/your/test_file.py``"). Two
    enforcement layers must not disagree about one command shape. Proceeds.
  - ``RUNNER_FOOTPRINT_UNPROVEN`` -- the opaque wrapper (``pnpm run
    tier:fast``, ``bash scripts/run-tests.sh --tier fast``, ``python dev.py
    test``). REFUSES, unchanged. This is the fail-OPEN hole the 2026-07-28
    ruling closed and it stays closed.

This narrows a default; it does not weaken a check. Nothing that
``classify_command`` classifies Tier U is touched (a Tier-U match never
reaches this branch at all), the declaration paths are consulted FIRST and
are entirely unchanged, and the wrapper class the fail-closed default was
written for still refuses.

One companion fix landed a layer down, in the classifier itself, because the
same conflation lived there too: ``_tier_for_cfg_match``'s ``full_test_cmd``
leg read ``_runner_recognized() is False`` as "could be the whole suite" and
so classified a repo whose fast tier resolved to ``exit 3`` as Tier U ("the
repo's configured full_test_cmd") -- outcome 2 here, never reaching the
unclassifiable branch at all. That leg now admits the same provably-inert
class (``_argv_is_inert``) as Tier F, and is unchanged for every opaque
wrapper. See that function's own comment for the measurement.

Negative-spec:
  - This module NEVER calls ``coordinator_core.session.grant.
    write_tier_u_grant`` -- it is READ-only over a grant
    (``check_tier_u_grant``). A caller wiring this gate into a
    routine pre-commit/pre-ceremony surface (``/coordinator:validate``,
    ``/workday-complete``) must never become grant-consuming in the sense
    of writing or mutating a grant; it may only read whether one already
    exists. Making it grant-consuming-by-writing would either burn grants
    constantly or normalise granting on a surface that is supposed to be a
    routine gate.
  - Does NOT implement tie-detection between ``fast_test_cmd`` and
    ``full_test_cmd`` (string identity is not the discriminator -- see the
    ruling memo's "do not detect a tie" section: DoE-claude legitimately
    declares the identical *scoped* string under both keys, and a
    tie-detector would false-positive-refuse that repo). Classification is
    delegated entirely to ``classify_command``'s shape-based classifier,
    which decides U-vs-F on shape alone.
  - Does NOT re-implement scope/shape detection -- reuses
    ``coordinator_core.bash_guards.check_test_suite_invocation.
    classify_command`` verbatim rather than hand-rolling a second opinion.
  - Does NOT teach the classifier about ``fast_tier_unscoped_reason`` (R7,
    hard prohibition). The declaration is read and applied entirely inside
    this module, AFTER ``classify_command`` has already returned its
    shape-only verdict -- ``check_test_suite_invocation.py`` never sees the
    declaration and must never be made to. A classifier that returns Tier F
    because a repo declared an exemption would reinstate the provenance-
    laundering the fast-leg fix (R7) removed, in a new costume.
  - Does NOT teach the classifier about ``fast_tier_shape`` either, for the
    same reason -- read and applied entirely in this module, after
    ``classify_command`` has already returned (in this case, an empty
    match list). The classifier's job stays "what shape is this command,"
    never "what did the repo declare about its own wrapper."
  - Does NOT let the declaration blanket-authorize. It is checked only
    against the LITERAL resolved ``fast_test_cmd`` string for this repo,
    re-resolved here via ``resolve_validation_cmd.cs_resolve_fast_test_cmd``
    and compared for exact equality with the ``cmd`` this call was given --
    any other Tier-U command still refuses, declaration or not.

TIER-F EXTENSION (2026-08-04) -- DR-088 § Amendment (2026-08-04) (this
repo's own record: docs/plans/2026-08-04-tier-f-is-grant-gated.md) removed
the fast tier's blanket exemption from the grant leg. Previously, ANY
non-Tier-U ``classify_command`` match (Tier F included) proceeded
immediately, without ever consulting the declaration or the grant. Now the
gate fires on {"U", "F"}, reading both from the SAME ``classify_command``
call below -- no second tiering path.

The R6 ``fast_tier_unscoped_reason`` declaration exit is UNCHANGED and
stays on the Tier-U leg only, by explicit PM ruling (2026-08-04): "the
grant ask IS the escape hatch" for Tier F, and no companion declaration
exit was authorized. A Tier-F match therefore skips
``_fast_tier_unscoped_declaration_covers`` entirely and goes straight to
``check_tier_u_grant`` -- branched explicitly rather than sharing the
Tier-U leg's fall-through, so a repo carrying a stale
``fast_tier_unscoped_reason`` declaration alongside a now-scoped
``fast_test_cmd`` cannot get its Tier-F command discharged by the
declaration for free (that would rebuild the exact escape hatch the PM
forbade, by omission rather than by a written branch).

Both resolve-and-execute callers of this module
(``validate-fast-and-packageability.py``'s ``run_fast``,
``workday-complete-step1-validate.py``'s ``main()``) hand this function
the DIFF-SCOPED command (the bare configured ``fast_test_cmd`` with
changed test paths appended) at their primary call site, and the bare
UNSCOPED command at their zero-tests-collected ``gate_full`` fallback call
site. The bare form classifies Tier U in this repo (R1/R2 policy: a
generic classification that is itself unscoped stays Tier U) and remains
R6-discharged; the diff-scoped form classifies Tier F
(``_tier_for_cfg_match`` sees a scoped generic shape) and is newly gated by
this extension -- two different objects, pinned separately in this
module's tests.

Residual -- no caller-identity check on the declaration exit, and this is
NOT an oversight. The ruling requires the declaration exit to apply only
to the top-level EM, never widening the subagent rung. This module's
callers (``validate-fast-and-packageability.py``, ``workday-complete-
step1-validate.py``) are naked-Python CLIs invoked in-process via the Bash
tool -- by the time this function runs, there is no ``PreToolUse`` hook
payload in scope, and every identity resolver this repo owns
(``coordinator_core.subagent_sandbox.engine.resolve_effective_types``,
``coordinator_core.write_guards.block_subagent_plan_body_write.
_resolve_subagent_identity``) is payload-keyed -- it reads
``payload["agent_id"]``/``payload["session_id"]`` off that hook JSON, which
simply does not exist at this in-process CLI seam. There is no OS-level
env var this repo sets that carries the same signal into a spawned
subprocess. Faking one (e.g. trusting an unverified env var a caller could
set on itself) would be worse than having none -- it would look like an
identity check while being trivially spoofable by the exact caller it is
meant to exclude. Until an identity signal genuinely reaches this seam
(e.g. the CLI wrapper itself becomes hook-invoked, or a verified
session-scoped identity token is threaded through), the R6 declaration
exit is available to WHATEVER caller invokes these CLIs with the literal
resolved ``fast_test_cmd`` string -- narrower than a full Tier-U grant
(single string, single command shape) but not narrowed to EM-only. Flagged
here rather than silently narrowed with a fake check.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Optional

from coordinator_core.bash_guards.check_test_suite_invocation import (
    RUNNER_FOOTPRINT_NONE,
    RUNNER_FOOTPRINT_SCOPED,
    classify_command,
    classify_runner_footprint,
)
from coordinator_core.resolve_validation_cmd import (
    cs_read_local_md_key,
    cs_resolve_fast_test_cmd,
)
from coordinator_core.session.fast_tier_declaration import (
    fast_tier_unscoped_declaration,
)
from coordinator_core.session.grant import check_tier_u_grant


def _fast_tier_unscoped_declaration_covers(cmd: str, repo_root: Optional[str]) -> bool:
    """Does this repo's ``fast_tier_unscoped_reason`` declaration (R6) cover
    ``cmd``?

    True only when (a) ``coordinator.local.md`` frontmatter carries a
    non-empty (post-strip) ``fast_tier_unscoped_reason:`` value, AND (b)
    ``cmd`` is EXACTLY the repo's resolved ``fast_test_cmd`` string. An
    absent key, an empty string, or a whitespace-only value is not a
    declaration. This is the sole reach of the declaration -- see module
    docstring negative-spec.

    The declaration itself is read by
    ``coordinator_core.session.fast_tier_declaration``, which owns the key
    and the "non-empty after strip" rule for both authority-layer consumers
    (this gate and the ``PreToolUse`` guard's own authority leg). What is
    owned HERE, and only here, is the R6 EXIT -- the (b) leg below and the
    decision that a covered command proceeds without a grant.
    """
    root = repo_root if repo_root is not None else os.getcwd()
    reason = fast_tier_unscoped_declaration(root)
    if not reason:
        return False
    resolved = cs_resolve_fast_test_cmd(root, _quiet=True)
    return resolved.exit_code == 0 and resolved.cmd == cmd


def _fast_tier_shape_declaration(repo_root: Optional[str]) -> Optional[str]:
    """Read this repo's ``fast_tier_shape`` declaration for the
    unclassifiable-command case.

    Returns ``"scoped"`` or ``"unscoped"`` iff ``coordinator.local.md``
    frontmatter carries exactly that (post-strip) value for
    ``fast_tier_shape:``. An absent key, an empty string, a whitespace-only
    value, or any other value returns ``None`` -- treated as "no
    declaration" by the caller, which refuses. See module docstring for the
    fail-closed contract this backs.
    """
    root = repo_root if repo_root is not None else os.getcwd()
    value = cs_read_local_md_key(root, "fast_tier_shape").strip()
    if value in ("scoped", "unscoped"):
        return value
    return None


def _unclassifiable_refusal_message(cmd: str, repo_root: Optional[str]) -> str:
    """Refusal message for a command ``classify_command`` returned zero
    matches for, whose runner footprint is UNPROVEN (an opaque wrapper), and
    for which this repo declares no (or an invalid) ``fast_tier_shape``.

    Names the exact key, both legal values, and this repo's own resolved
    ``fast_test_cmd`` string -- this message is the entire remediation
    surface an EM in a broken repo will see, matching the tone of the
    detected-Tier-U refusal below. Also names the third exit the footprint
    narrowing opened (see module docstring): a scoped invocation of a
    recognized runner needs no declaration at all.
    """
    root = repo_root if repo_root is not None else os.getcwd()
    resolved = cs_resolve_fast_test_cmd(root, _quiet=True)
    resolved_cmd = resolved.cmd if resolved.exit_code == 0 else cmd
    return (
        "Refusing to run: this command could not be classified by the "
        f"Tier-U shape classifier at all (no match -- resolved "
        f"fast_test_cmd: {resolved_cmd!r}), it carries a test-runner "
        "footprint this classifier could not parse (an opaque wrapper, so "
        "its breadth cannot be derived from its shape), and this repo's "
        "coordinator.local.md declares no fast_tier_shape breadth. "
        "Fail-closed default: an unclassifiable wrapper refuses unless the "
        "repo declares its shape.\n\n"
        "Add ONE of the following to this repo's coordinator.local.md "
        "frontmatter:\n"
        "  fast_tier_shape: scoped     -- this wrapper runs a bounded "
        "subset of tests; proceeds.\n"
        "  fast_tier_shape: unscoped   -- this wrapper runs the full "
        "suite; treated exactly like a detected Tier-U command (requires "
        "a fast_tier_unscoped_reason declaration or a live Tier-U grant "
        "to proceed).\n\n"
        "Or drop the wrapper: an invocation of a runner this classifier "
        "recognizes, scoped by a path, node id, or -k expression "
        "(`pytest path/to/test_x.py`), needs no declaration at all -- its "
        "breadth is readable off its own shape.\n"
    )


@dataclasses.dataclass(frozen=True)
class TierUGateResult:
    """Outcome of ``enforce_tier_u_gate`` -- ``proceed`` is the caller's
    only branch; ``refusal_message`` is populated iff ``proceed`` is
    False, ready to print to stderr verbatim."""

    proceed: bool
    refusal_message: Optional[str] = None


def enforce_tier_u_gate(
    cmd: str, *, repo_root: Optional[str] = None, session_id: Optional[str] = None
) -> TierUGateResult:
    """Classify ``cmd``'s shape; refuse when any segment classifies Tier U
    OR Tier F, OR when ``cmd`` is UNCLASSIFIABLE (zero ``classify_command``
    matches) and the repo has not declared its fast-tier breadth -- and, in
    any refusing case, the calling session holds no live Tier-U grant.

    TIER-F EXTENSION (2026-08-04, see module docstring): Tier F is gated
    identically to Tier U as of this dispatch -- the fast tier is no longer
    exempt from the grant leg. The tier set is read from the SAME
    ``classify_command`` call as Tier U (no second tiering path). The R6
    declaration exit is reached ONLY on the Tier-U leg -- a Tier-F match is
    branched explicitly to the grant check, never falling through the
    declaration leg, so a stale ``fast_tier_unscoped_reason`` cannot
    discharge a Tier-F command by omission (PM ruling: the grant ask IS the
    Tier-F escape hatch; no declaration-based exit is to be added).

    Fail-closed default (PM ruling, 2026-07-28; see module docstring) --
    FOUR outcomes, not two:

      1. Classified, neither Tier U nor Tier F (at least one match, none
         U/F) -- proceeds immediately, without touching the grant machinery
         or the declaration helpers at all.
      2. Classified Tier F (and no Tier U match) -- goes straight to
         ``check_tier_u_grant``; the R6 declaration is never consulted for
         this leg. Refuses if no live grant.
      3. Classified Tier U, or UNCLASSIFIABLE with a
         ``fast_tier_shape: unscoped`` declaration -- both funnel into the
         SAME declaration-then-grant check below (no duplicated logic):
         ``_fast_tier_unscoped_declaration_covers`` first, then
         ``check_tier_u_grant``; refuses if neither covers it.
      4. UNCLASSIFIABLE with ``fast_tier_shape: scoped`` -- proceeds.
         UNCLASSIFIABLE with no declaration (absent, empty, or any other
         value) -- the decision falls to the command's RUNNER FOOTPRINT
         (``classify_runner_footprint``; see the module docstring's FOOTPRINT
         NARROWING section for the measured over-refusal that motivated it):
         a command with no test-runner footprint (``true``, ``exit 3``) or
         one whose every runner invocation was positively parsed as scoped
         (``pytest path/test_x.py``, ``pytest -k expr``) PROCEEDS; an opaque
         wrapper (``pnpm run tier:fast``, ``bash scripts/run-tests.sh
         --tier fast``) still REFUSES, naming the exact key, both legal
         values, and this repo's resolved ``fast_test_cmd``
         (``_unclassifiable_refusal_message``).

    The declaration is read BEFORE the footprint, deliberately: a repo that
    declared its own wrapper's breadth is answered by its declaration
    regardless of what this classifier can or cannot parse, so the footprint
    leg only ever narrows the NO-declaration default and can never override
    a declaration in either direction. This ordering is orthogonal to the
    Tier-F leg above (2), which never reaches the footprint/declaration code
    at all -- it has its own, classified match, not an unclassifiable one.

    READ-only consumption of a grant (``check_tier_u_grant``) -- never
    writes, mutates, or expires one (R4; see module negative-spec). Case 1
    never even attempts the grant lookup, so it is trivially never a naive
    fast==full tie-detector (see module negative-spec): whatever verdict
    classify_command reaches is the only thing this function reads for a
    classified command.

    ``repo_root`` is forwarded to ``classify_command`` (as ``cwd``, for
    testpaths/configured-cmd resolution), the declaration helpers (for
    ``coordinator.local.md`` resolution), and ``check_tier_u_grant`` (as
    ``cwd``, for session-directory resolution) -- all default to the
    process cwd when omitted, matching each function's own default.
    ``session_id`` is forwarded to ``check_tier_u_grant`` only -- test-only
    override for a specific (possibly non-current) session id; production
    callers omit it and let ``check_tier_u_grant`` resolve the calling
    session itself.

    Before consuming the grant machinery in case 3, checks whether this
    repo's ``fast_tier_unscoped_reason`` declaration (R6) covers ``cmd``
    exactly (``_fast_tier_unscoped_declaration_covers``) -- when it does,
    this proceeds WITHOUT calling ``check_tier_u_grant`` at all, so a
    declared repo's routine fast-tier invocation never touches, consumes,
    or is recorded against the grant machinery. See module docstring
    negative spec for the declaration's narrow reach. Case 2 (Tier F) never
    reaches this check at all -- see the TIER-F EXTENSION note above.
    """
    matches = classify_command(cmd, cwd=repo_root)
    tier_u_matches = [m for m in matches if m.tier == "U"]
    tier_f_matches = [m for m in matches if m.tier == "F"]

    if matches and not tier_u_matches and not tier_f_matches:
        return TierUGateResult(proceed=True)

    if not tier_u_matches and not tier_f_matches:
        # Unclassifiable: zero matches at all.
        shape = _fast_tier_shape_declaration(repo_root)
        if shape == "scoped":
            return TierUGateResult(proceed=True)
        if shape != "unscoped":
            footprint = classify_runner_footprint(cmd, cwd=repo_root)
            if footprint in (RUNNER_FOOTPRINT_NONE, RUNNER_FOOTPRINT_SCOPED):
                return TierUGateResult(proceed=True)
            return TierUGateResult(
                proceed=False,
                refusal_message=_unclassifiable_refusal_message(cmd, repo_root),
            )
        # shape == "unscoped": fall through to the shared Tier-U-shape
        # declaration/grant check below -- reused, not duplicated.

    if tier_f_matches and not tier_u_matches:
        # Tier-F leg -- branched explicitly (never shares the Tier-U leg's
        # fall-through) so the R6 declaration exit is UNREACHABLE here. Per
        # PM ruling 2026-08-04, the grant ask is the only Tier-F escape
        # hatch; a stale fast_tier_unscoped_reason declaration must not
        # discharge a Tier-F command for free.
        granted, _record = check_tier_u_grant(cwd=repo_root, session_id=session_id)
        if granted:
            return TierUGateResult(proceed=True)

        detected = tier_f_matches[0].detected
        remediation = tier_f_matches[0].remediation
        message = (
            "Refusing to run: this command is the repo's configured fast "
            f"test tier (Tier F -- detected: {detected}) and the calling "
            "session holds no live Tier-U grant. Per PM ruling 2026-08-04 "
            "the grant ask is the only escape hatch for the fast tier -- "
            "there is no declaration-based exemption for Tier F.\n\n"
            f"{remediation}\n\n"
            "Ask the PM for a grant, then run: "
            "tier-u-grant-cli grant pm \"<verbatim PM utterance>\""
        )
        return TierUGateResult(proceed=False, refusal_message=message)

    # Tier-U leg (detected Tier U, or UNCLASSIFIABLE declared unscoped).
    if _fast_tier_unscoped_declaration_covers(cmd, repo_root):
        return TierUGateResult(proceed=True)

    granted, _record = check_tier_u_grant(cwd=repo_root, session_id=session_id)
    if granted:
        return TierUGateResult(proceed=True)

    if tier_u_matches:
        detected = tier_u_matches[0].detected
        remediation = tier_u_matches[0].remediation
    else:
        detected = (
            f"unclassifiable command with repo-declared fast_tier_shape: "
            f"unscoped ({cmd!r})"
        )
        remediation = (
            "This command could not be classified by the Tier-U shape "
            "classifier, and this repo's coordinator.local.md declares "
            "`fast_tier_shape: unscoped` -- an admitted full-suite "
            "invocation, requiring the same authorization as a detected "
            "Tier-U command."
        )

    message = (
        "Refusing to run: this command is an unscoped/full-suite "
        f"invocation (Tier U -- detected: {detected}) and the "
        "calling session holds no live Tier-U grant.\n\n"
        f"{remediation}\n\n"
        "Three honest exits: (a) configure a scoped fast_test_cmd (a test "
        "file, directory, or node-id scope -- see the remediation above), "
        "(b) declare fast_tier_unscoped_reason in coordinator.local.md with "
        "a prose rationale (covers only the literal resolved fast_test_cmd "
        "string -- see DR-088 R6), or (c) run the suite through a granted "
        "ceremony."
    )
    return TierUGateResult(proceed=False, refusal_message=message)
