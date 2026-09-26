"""coordinator_core.bash_guards.dispatch -- naked-Python PreToolUse(Bash)
dispatcher. Python port of DoE's ``coordinator/hooks/scripts/
preuse-bash-dispatch.sh``, per the W3a/W3b naked-Python hook migration recipe
(scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md
Sec(c)).

Runs BOTH guard cohorts in one process (collapsing what was ~14 separate
`bash.exe` spawns on the legacy dispatch chain into a single `python3`
invocation -- the whole point of the Windows cold-start-tax fix this
migration exists for):

  1. The subagent-identity cohort ported/added as discovery-style modules in
     this package (``block_subagent_plan_body_bash_write``,
     ``block_reviewer_bash_outside_allowlist``, ``block_subagent_destructive_
     action``, ``block_subagent_commit`` -- CLASS="hard-deny";
     ``block_illegal_filename`` -- CLASS="advisory"). Each exposes
     ``check(payload) -> dict | None`` per write_guards/INTERFACE.md.
     ``nudge_subagent_scoped_commit`` (formerly cohort-1 advisory position 6)
     was RETIRED 2026-07-24 in the same change that added
     ``block_subagent_commit`` -- that module's identity-gate + git-commit
     detection is what ``block_subagent_commit`` builds on, with the verdict
     flipped from advisory-allow to hard-deny and the scoped-pathspec
     exemption removed; leaving both registered would have shipped a
     two-gate stack for the same rule.
  2. The 11 checks in ``dispatch_checks.py`` -- ``check_<name>(cmd,
     session_id[, cwd]) -> dict | None``, mirroring their bash predecessors
     own sourceable-function contract.

Combined cross-cohort order (recipe Sec(c) "Phase structure to preserve
exactly", extended to fold in cohort 1): this dispatcher does NOT batch
"all hard-deny checks first, then soft/content/advisory" across cohorts --
that shape was this module first draft and it is a PARITY BREAK against
legacy. The legacy runtime is 6 SEPARATE PreToolUse:Bash hook processes,
first-non-empty-stdout-wins, in hooks.json REGISTRATION order:

  1. preuse-bash-dispatch.sh (the 11-check fold), which INTERNALLY runs its
     own hard -> soft -> content -> advisory phases in this order:
       a. check_no_verify                  (hard, fail-closed)
       b. check_destructive_git_orphan     (hard, fail-closed)
       c. check_destructive_rm             (hard, fail-closed)
       d. check_destructive_git_clean      (hard, fail-closed)
       e. check_destructive_git_revert     (hard, fail-closed)
       f. check_blanket_git_add            (hard, fail-closed)
       g. check_runaway_find               (hard, fail-closed)
       h. check_offer_git_c                (soft, fail-open; cwd-forwarded)
       i. check_validate_commit            (content, fail-open, NOT
                                             crash-deny-routed)
  2. block_illegal_filename       (cohort 1, Bash leg -- advisory, fail-open)
  3. block_subagent_plan_body_bash_write   (cohort 1 -- hard, fail-closed)
  4. block_reviewer_bash_outside_allowlist (cohort 1 -- hard, fail-closed)
  5. block_subagent_destructive_action     (cohort 1 -- hard, fail-closed)
 5a. block_subagent_commit                 (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-24 (M4) as the structural teeth for
     the no-self-commit rule (AC6/AC13). Pinned alongside the three guards
     above -- all four fire on git-history protection or identity
     confinement, which outrank a machine-load deny. Supersedes
     ``nudge_subagent_scoped_commit`` (formerly position 6, RETIRED here).
 5b. check_test_suite_invocation           (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-23 for DoE's DR-088 test-breadth ladder.
     It has no parity ordering to preserve, so it sits at the tail of the
     hard-deny run: every guard above it protects git history or an identity
     confinement, both of which outrank a machine-load deny when two would
     fire on the same command.
 5c. check_raw_pid_liveness                (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-27 (DoE C14/RAW-PID-LIVENESS-GUARD) to
     close the RAW-PID-LIVENESS tripwire's long-standing "forthcoming"
     mechanical-enforcement tier. Not identity-gated (unlike 5/5a/5b above,
     it fires on EVERY caller including the EM -- the raw-pid liveness
     anti-pattern is wrong regardless of who types it), and has no parity
     ordering to preserve, so it sits at the very tail: nothing above it
     shares its detection surface.
 5d. block_worktree_creation                (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-28 (DoE-claude, fleet-wide structural
     git-worktree ban, main-loop leg). Not identity-gated, same posture as
     5c: `block_subagent_destructive_action` (5) already denies `git
     worktree add` but ONLY for a resolved subagent, leaving the main-loop
     EM exempt -- this guard has no such exemption. Positioned after 5c for
     the same reason: no parity ordering to preserve, and it shares no
     detection surface with anything above it (it classifies exclusively on
     `git worktree <subcommand>`, never on the destructive-action cohort's
     broader git/rm/chmod surfaces).
 5e. block_approval_sentinel_creation       (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-28 (DoE-claude, doctrine-approval
     sentinel un-creatable-by-agent guard). Not identity-gated, same
     posture as 5c/5d -- the EM is exactly who this sentinel exists to
     constrain, so an EM exemption would defeat its own purpose. Registered
     directly adjacent to 5d ahead of `offer-git-c` for the identical
     short-circuit reason (see its own registration comment above and its
     module docstring "REGISTRATION ORDERING").
 5f. block_worktree_sentinel_creation       (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-28 (DoE-claude, closing a confirmed
     security hole: the git-worktree ban's own override sentinel,
     `.coordinator-override-worktree-guard`, was creatable via Bash `touch`/
     redirection/etc, reintroducing the exact agent-self-grant that guard's
     deliberately-omitted env-var override leg exists to prevent). Same
     posture and registration-ordering rationale as 5e -- not identity-
     gated, registered directly adjacent to it, ahead of `offer-git-c`.
     Shares its detection engine with 5e via
     `_sentinel_creation_guard.SentinelCreationDetector`, parameterized on a
     different target basename.
 5g. block_stash_destruction                (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-07-30 (DoE-claude) as the main-loop leg of
     the `git stash drop`/`clear` ban. Entry 5 already classifies both verbs
     as a deny, but its identity gate fails OPEN when no subagent resolves,
     so an EM-typed `git stash drop` was allowed -- and the EM is the caller
     with the most stack drift between its own stash push and its own drop
     (confirmed live: an EM dropped `stash@{0}` believing it its own, and it
     described a peer session's work). Not identity-gated, same posture as
     5c-5f. Deliberately narrower than entry 5: `pop`/`apply` stay allowed
     for the EM, since `pop` is its own restore path -- see that module's
     "WHY DROP/CLEAR AND NOT POP/APPLY".
 5h. block_subagent_stash_creation          (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-08-01 closing the CREATE-side half of the
     gap 5/5g only close on the UNDO side: entry 5 denies a resolved
     subagent `git stash pop`/`apply`, and 5g denies `drop`/`clear` for
     everyone, but nothing in the chain ever denied `git stash push` (or
     bare `git stash`, which is push) for a subagent -- so a dispatched
     subagent could always CREATE a stash it was structurally incapable of
     undoing. Confirmed live 2026-08-01: a `coordinator:review-integrator`
     subagent's `git stash push`, reported as "scoped push on 3 files",
     instead created a bare 60-file stash sweeping a concurrent peer
     session's uncommitted work; its own `pop` was then correctly denied by
     entry 5, and only 3 of the 60 files were recovered by hand. UNLIKE 5g
     (not identity-gated at all) and entry 5 (identity-gated but fails OPEN
     on an unresolvable `agent_id`), this entry is identity-gated AND fails
     CLOSED on a present-but-unresolvable `agent_id` -- the EM is the sole,
     harness-supplied allow signal (raw `agent_id` ABSENT), because failing
     open here would silently re-admit the exact hole this entry exists to
     close. See that module's own "IDENTITY-GATE POSTURE" for the three-way
     comparison against entries 5/5g. Registered directly adjacent to 5g --
     same `offer-git-c` short-circuit ordering requirement as every entry in
     this CONFINEMENT_DENY run.
 5i. block_subagent_grant_acquisition       (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-08-08 (C2, docs/plans/2026-08-08-
     discriminate-the-caller-on-the-write-grant.md) to close a caller-
     discrimination gap on the CLAUDE.md write grant: a dispatched subagent
     (resolved `agent_id` present) invoking `coordinator_core.session.
     claude_md_grant grant` was structurally indistinguishable from the
     EM acquiring its own grant, letting a subagent self-grant write access
     to doctrine it has no dispatch authority over. Identity-gated to
     subagents only, same posture as `block-subagent-stash-creation` (5h) --
     the EM remains the sole allow signal (raw `agent_id` ABSENT). Registered
     directly adjacent to `check-test-suite-invocation`, at the tail of the
     hard-deny run -- same `offer-git-c` short-circuit ordering requirement
     as every entry in this CONFINEMENT_DENY run.
 5j. block_subagent_findings_reject         (hard, fail-closed) -- NO legacy
     bash predecessor; added 2026-09-26 (DoE-claude docs/plans/2026-09-26-
     retire-review-integrator.md, row M3) to make EM-only the `reject`/
     `targets` subcommands of the reviewer-applies-own-findings ledger op
     (`coordinator_core.ops.review_findings_ledger`, M2). Same identity-gate
     posture as `block-subagent-grant-acquisition` (5i) -- raw `agent_id`
     presence alone is the discriminator, fail CLOSED on unresolvable.
     `verify` is deliberately not gated: a reviewer's own self-check before
     returning is the encouraged path. Registered directly adjacent to 5i --
     same `offer-git-c` short-circuit ordering requirement as every entry
     in this CONFINEMENT_DENY run.

Because each of the (now 5, post-2026-07-24 retirement of the former #6
advisory nudge) legacy-shaped processes only ever emits EITHER nothing
(silent allow) OR a single deny/advisory envelope, "first-deny-wins" and
"first-non-empty-stdout-wins" coincide: the combined dispatcher below is
therefore ONE FLAT SEQUENTIAL LIST in exactly this 1a..1k, 2, 3, 4, 5, 5a,
5b, 5c order, evaluated strictly top-to-bottom, returning the first
non-``None`` result. Each entry error-handling (crash-deny vs fail-open) is decided by
its OWN class (hard vs soft/content/advisory), not by its position in a
batched phase -- see the per-entry ``fail_closed`` flag in ``guard_chain``
below. F1 (one guard crash must not swallow later guards) still holds
per-entry, uniformly across both cohorts.

check_validate_commit (1i, "content") is deliberately NOT crash-deny-routed
even though it sits between the hard chain and the advisory phase: its bash
predecessor is fail-open on every standalone error path already (non-git-
repo, absent/erroring bin/ delegate, unparseable commit subject); porting it
to fail-CLOSED-on-exception would be a genuine behavior change this
migration must not introduce.

Parse-once contract (recipe Sec(c) "Parse-once contract to preserve"):
stdin is read and ``json.loads``-parsed exactly ONCE by the caller (this
module ``evaluate_payload_json``); ``tool_name``/``command``/``session_id``/
``cwd`` are extracted once at the top. Non-``Bash`` ``tool_name`` -> ``None``
(the caller stub then emits nothing / fail-open ALLOW). CRLF is stripped
ONCE here at dispatcher level AND, per-check, redundantly-but-safely again
inside each ``check_*``/``check(payload)`` call (this double-strip discipline
is deliberate -- see ``dispatch_checks`` module docstring -- so a check
function copy-pasted or reused standalone later does not regain the CRLF bug
the original bash comment explicitly guards against).

F0 (per-target git resolution) is NOT this module concern -- it lives
entirely inside ``check_destructive_rm``/``check_destructive_git_orphan``/
``check_destructive_git_revert`` (each does its own per-target/per-segment
``git -C <dir>`` resolution). This dispatcher passes ``cwd`` to exactly THREE
checks (``check_offer_git_c``; since 2026-07-16 per code-reviewer Finding
2, ``check_validate_commit`` -- its Check 5/7/8 git calls are cwd-sensitive
and were previously silently resolving against this process's own
``os.getcwd()`` instead; and, since 2026-08-19,
``check_heredoc_repo_write_advise``, which takes it as its containment root
and RESOLVES NOTHING -- pure path arithmetic, no git call of any kind, so it
adds no per-check git resolution to this hot path) and must never widen that further to a shared
dispatcher-level ``resolve_git_root()`` reused across checks (recipe Sec(e)
F0 hazard -- "a single resolve_git_root() call accidentally reused as a
shared, dispatcher-level git root passed into EVERY check function" is the
named regression shape).

F1 (one guard crash must not swallow later guards) is realized by wrapping
EACH check call in its OWN ``try/except`` inside the loop -- never one outer
``try`` around a whole phase (recipe Sec(e) F1 "concrete hazard to name for
a reviewer": an outer-try shape would abort the WHOLE chain on the first
exception, silently skipping checks after the one that raised, which is
only accidentally indistinguishable from correct behavior when the raising
check happens to be positioned where a real deny would occur -- NOT a
general guarantee).

F2 (module-level namespace collisions) is closed structurally by Python
per-module/per-function scoping (no ``source``-equivalent leaking locals into
a shared namespace) AND, within ``dispatch_checks.py``, by every
``COORDINATOR_OVERRIDE_*``/``COORDINATOR_ALLOW_*`` env read being an INLINE
``os.environ.get(...)`` call inside each check function body -- see
``dispatch_checks._override()``, called fresh at every site, never hoisted to
module scope.

Port of: preuse-bash-dispatch.sh (DoE 2f8b8450, 2026-07-16)
Spec backlink: scratch/subagent-sandbox/bash-to-python-migration/
W3a-preuse-bash-recipe.md Sec(c)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple, Union

from coordinator_core.bash_guards._advisory_value import (
    AdvisoryValue,
    resolve_suppressed_envelope as _resolve_suppressed_envelope,
    suppress_advisory as _suppress_advisory,
)
from coordinator_core.bash_guards._platform_verdict import (
    resolve_host_is_windows as _resolve_host_is_windows_public,
)
from coordinator_core.bash_guards._tool_names import (
    COMMAND_TOOL_NAMES,
)
from coordinator_core.bash_guards._dialect import (
    Dialect as _Dialect,
    dialect_from_tool_name as _dialect_from_tool_name,
)
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.session.guard_unlock_sentinel import (
    annotate_deny as _annotate_unlock,
    consume as _consume_unlock,
)
from coordinator_core.bash_guards import dispatch_checks as _dc
from coordinator_core.bash_guards._helpers import (
    _resolve_override_keys_doc_display as _override_keys_doc_display,
)
from coordinator_core.guard_advisory_counter import (
    record_advisory_fire as _record_advisory_fire,
    record_deny_fire as _record_deny_fire,
)
from coordinator_core.bash_guards.chain_arrival_ledger import (
    record_chain_arrival as _record_chain_arrival,
)
from coordinator_core.warm.caller_context import (
    resolve_caller_context as _resolve_caller_context,
)
from coordinator_core.bash_guards._advisory_dedupe import (
    advisory_dedupe_key as _advisory_dedupe_key,
    already_advised as _already_advised,
    mark_advised as _mark_advised,
    silence_repeat_advisory as _silence_repeat_advisory,
)
from coordinator_core.bash_guards._write_bump_marker import (
    resolve_gitdir as _resolve_gitdir_for_dedupe,
)
from coordinator_core.bash_guards._command_tokenizer import (
    ResolvedCommand as _ResolvedCommand,
    resolve_command_positions as _resolve_command_positions,
)
# The subset of MATCHERS symbols `_any_declared_matchers` also reads is


class GuardBand(Enum):
    """The three sequenced bands `guard_chain` below is split into --
    CONFINEMENT_DENY, then ADVISORY_REWRITE, then PLATFORM_CONDITIONED_DENY
    last (CBS-C1, 2026-07-29). Explicit per-entry field, deliberately NOT
    derived from a guard's own `fail_closed` flag: `fail_closed` is
    exception-routing policy (crash-deny vs swallow-as-allow) and is
    orthogonal to which band a guard's OWN verdict vocabulary belongs to --
    the two PLATFORM_CONDITIONED_DENY guards are `fail_closed=True` (a
    crash in them still fails closed) yet must run in the LAST band, after
    every rewrite, for the empirically-tested reason their own registration
    comment below records.

    Band semantics (binding on every entry, not just the named guards):
      - CONFINEMENT_DENY runs to completion in the sense that none of its
        guards ever return a rewrite verdict or an early "allow" with
        content -- every entry in this band emits either a deny or silent
        None, so "first non-None wins" and "first deny wins" coincide here
        by construction.
      - Within ANY band, first deny wins -- registration order is
        precedence order, and a later deny in the same band is discarded
        (never evaluated-and-compared; the loop below still short-circuits
        on the first non-None result, exactly as it always has).
      - ADVISORY_REWRITE is NOT a first-deny-wins band in spirit -- its
        currency is rewrites/advisories, not denials -- but it keeps
        today's first-non-None-wins short-circuit mechanically, same as
        every other band.
      - A crashing guard's `_crash_deny` return keeps its immediate-return
        property regardless of band.
    """

    CONFINEMENT_DENY = "confinement-deny"
    ADVISORY_REWRITE = "advisory-rewrite"
    PLATFORM_CONDITIONED_DENY = "platform-conditioned-deny"


_OVERRIDE_KEY_PREFIXES = ("COORDINATOR_OVERRIDE_", "COORDINATOR_ALLOW_")

_OverrideIdentity = FrozenSet[Tuple[str, str]]


def _override_env_identity(payload: Optional[Dict[str, Any]]) -> _OverrideIdentity:
    """Hashable identity of the override env ONE call would be evaluated
    against -- the cache-key term that makes a memo of a guard verdict safe to
    share between callers.

    Resolves the same way ``dispatch_checks._override`` does (C14c): prefer a
    per-call ``payload["env"]`` mapping, fall back to ambient ``os.environ``.
    Only ``COORDINATOR_OVERRIDE_*``/``COORDINATOR_ALLOW_*`` keys are captured,
    because those are the only names ``_override``/``operator_override_note``
    consult; a full env snapshot would key on PATH churn and defeat the cache.

    NEGATIVE SPEC. This is not a permission check and never decides anything --
    it only distinguishes two calls that must not share a cached verdict. A
    caller reading a truthiness out of the returned set is misusing it; ask
    ``_override`` instead. And it is computed per call, never memoised at
    module scope: the whole point is that ambient env differs between the
    process that fills the cache and the process that reads it.
    """
    import os

    env: Any = None
    if isinstance(payload, dict):
        candidate = payload.get("env")
        if isinstance(candidate, dict):
            env = candidate
    if env is None:
        env = os.environ
    return frozenset(
        (str(k), str(v))
        for k, v in env.items()
        if isinstance(k, str) and k.startswith(_OVERRIDE_KEY_PREFIXES)
    )


@dataclass(frozen=True)
class GuardEntry:
    """One registered entry in ``guard_chain`` (H1, 2026-07-30 --
    os-aware-guard-advisory-defaults row H1). Replaces the former bare
    ``(name, fn, fail_closed, band)`` 4-tuple with a frozen dataclass so a
    fifth field (``advisory_value``) can carry a DEFAULT -- a tuple has no
    defaults, so a 4-tuple entry written by the author of a guard added
    after this change would raise ``ValueError`` on unpack (a crash) the
    moment any code unpacks 5 elements, which is worse than the required
    "an unclassified guard defaults to SHOWN" (AC-4). A dataclass field
    defaulting to ``AdvisoryValue.UNCLASSIFIED`` renders SHOWN while a
    separate registry-validation test (H5) fails loud on any REGISTERED
    guard still carrying that default (AC-1) -- those two requirements are
    only jointly satisfiable with a defaulted field plus a separate test;
    do not collapse them into one check, and do not swap this for a
    ``NamedTuple`` (no per-field defaults on positional construction there
    either).

    Fields:
      name           -- the guard's registration name (unique per entry).
      fn             -- zero-arg closure invoking the guard's own check.
      fail_closed    -- crash-routing policy (see module docstring F1).
      band           -- the `GuardBand` this entry's own verdict vocabulary
                        belongs to (module docstring above).
      advisory_value -- (H1; consumed starting H4) the guard's OS-aware
                        advisory classification -- see `_advisory_value.
                        AdvisoryValue`. Defaults to `AdvisoryValue.
                        UNCLASSIFIED`; every entry classified at H3 sets
                        this explicitly, INCLUDING every `CONFINEMENT_DENY`
                        entry (an exemption for the band that matters most
                        would be a hole in AC-1).
      matchers        -- (C1, docs/plans/2026-08-07-command-guards-fire-
                        under-both-tool-names.md) the command tool names
                        this entry's own detection can read, as a subset of
                        `_tool_names.COMMAND_TOOL_NAMES`. Same defaulted-
                        field shape as `advisory_value` above, for the same
                        reason: the default (`("Bash",)`) is load-bearing --
                        any entry not explicitly widened behaves EXACTLY as
                        it did before this field existed, so introducing it
                        is inert until a guard opts in. Governs CHAIN ENTRY
                        ONLY (whether `evaluate_payload_json`'s loop calls
                        this entry's `fn` at all for the observed
                        `tool_name` -- see that loop's own skip) and confers
                        nothing on a module a registered guard calls
                        internally; widening a delegating guard requires
                        auditing its own callee graph separately.
    """

    name: str
    fn: Callable[[], Optional[Dict[str, Any]]]
    fail_closed: bool
    band: GuardBand
    advisory_value: AdvisoryValue = AdvisoryValue.UNCLASSIFIED
    matchers: Tuple[str, ...] = ("Bash",)


_CRASH_TRIGGER_SUBSTRINGS: Dict[str, Tuple[str, ...]] = {
    "no-verify": ("git",),
    "destructive-git-orphan": ("git",),
    "destructive-git-clean": ("git",),
    "destructive-git-revert": ("git",),
    "blanket-git-add": ("git",),
    "destructive-rm": ("rm",),
    "runaway-find": ("find",),
}
"""Per-guard necessary preconditions, used ONLY on the crash path to scope a
fail-closed deny to the class of command the crashed guard actually polices.

Each entry must be **provably wider** than its guard's own matching: every
command the guard could deny has to contain at least one of these substrings.
These are not guesses about what the guard "looks like it governs" -- each is
read off the guard's own first-branch early return, which is why the mapping
is keyed by guard name rather than derived from anything heuristic:

  - ``check_no_verify``            -- ``re.search(r"\\bgit\\b", flat)``
  - ``check_destructive_git_orphan`` -- ``re.search(r"\\bgit\\b", cmd)``
  - ``check_destructive_git_clean``  -- ``\\bgit\\b`` AND ``\\bclean\\b``
  - ``check_destructive_git_revert`` -- ``\\bgit\\b`` (then a verb test)
  - ``check_blanket_git_add``        -- ``re.search(r"\\bgit\\s+add\\b", cmd)``
  - ``check_destructive_rm``         -- ``re.search(r"\\brm\\b", cmd)``
  - ``check_runaway_find``           -- ``re.search(r"\\bfind\\b", cmd)``

A word-boundary regex is strictly narrower than the bare substring, and the
two conjunction cases are weakened to one conjunct, so each entry over-matches
on purpose. **A guard absent from this mapping keeps today's chain-wide deny
on crash** -- omission is the safe direction, and no guard should be added
here without reading its early returns and confirming the widening.

The membership test runs against a UNION of normalized variants, not a single
pipeline -- see ``_crash_probe_variants``. Claiming that only ``_crlf_strip``
and ``_join_backslash_newlines`` can manufacture a keyword is FALSE and was
the premise two live crash-path bypasses rested on: ``_strip_ws_quoted_spans``
merges the text flanking a span it deletes, and
``_normalize_git_exe_head_to_bare`` rewrites a ``GIT.EXE`` head to bare
``git``. Each guard normalizes differently, and one of those transforms can
remove a keyword as well as introduce one, so no single normalized string is
wider than all of them.

A derivation is only as good as the normalizations it accounts for, and prose
asserting the derivation was done is not the derivation. What proves each
entry is the per-guard property test in
``tests/test_crash_trigger_is_wider_than_its_guard.py``: for every command the
guard denies, its trigger matches. Do not add an entry here on a reading of
the guard's early returns alone.
"""


_SENTINEL_ELIGIBLE_ADVISORY_GUARDS: "frozenset[str]" = frozenset(
    {"bump-foreign-repo-write", "bump-outside-repo-write"}
)
"""Explicit per-guard opt-in for in-session-operator-unlock sentinel
eligibility among `fail_closed=False` `guard_chain` entries -- Review:
coordinator:code-reviewer P1 (re-derived independently by
review-integrator). Membership rule: a `fail_closed=False` guard belongs
here only if (a) it composes a genuine `permissionDecision: "deny"` on a
normal, non-crash path, AND (b) a sentinel drop is an appropriate remedy
for that deny -- a product judgment made per guard, not a mechanical
consequence of (a) alone. `validate-commit` and
`git-commit-safe-commit-advise` satisfy (a) but fail (b), so they stay
out; see the rationale below. Do not trust any historical count of how
many `fail_closed=False` entries satisfy (a) -- three independent audits
of this chain (two, then four, then five) each found one the last had
missed, so a stated number invites a false sense that the census is
closed rather than a reflection of the actual test. To determine (a) for
a given guard, read its backing module and confirm it can return a
`{"permissionDecision": "deny", ...}` envelope (directly, or via
`_hook_envelope.deny()`) on a path that isn't a bare exception-swallow
`return None`/allow default -- `guard_chain`'s own entries name each
guard's backing check. Audit history, not a live count: a first walk
found two (`bump-foreign-repo-write`, `bump-outside-repo-write`); a
second found two more (`validate-commit`,
`git-commit-safe-commit-advise`); a third found a fifth
(`inprocess-search`, correctly excluded here -- its deny means "already
answered", not "refused", and satisfies (a) but not (b) for the same
reason as the commit-safety pair below).

Every `fail_closed=True` (CONFINEMENT_DENY / PLATFORM_CONDITIONED_DENY)
guard stays unconditionally sentinel-eligible (see `_sentinel_eligible`'s
own computation in `evaluate_payload_json`'s loop) -- this allowlist scopes
ONLY the `fail_closed=False` population, restoring the two `bump-*` speed
bumps to clearable/advertised (the original defect this dispatch's parent
fix addressed) while leaving `validate-commit`'s strict-mode scope deny and
`git-commit-safe-commit-advise`'s foreign-staged-index deny NON-clearable
by a sentinel drop -- both are commit-safety denies on a worktree nine-plus
concurrent sessions share, and a single sentinel drop suppressing either
would reintroduce the exact "one session's commit absorbs another's staged
work" hazard `block-subagent-stash-creation`'s own module docstring records
a live incident of, for a different guard in the same worktree-sharing
class. Widening this set to include either commit-safety guard is a
product decision (does a sentinel drop become an appropriate remedy for a
commit-safety deny), not a mechanical consequence of anything here -- if
that is ever wanted, it belongs in a plan of its own, not a silent
allowlist edit.
"""


_RESOLUTION_CLASS_PHRASES: Dict[str, str] = {
    "resolved-engine": "resolved engine",
    "live-working-tree": "live working tree",
    "unresolved": "unresolved",
}
"""Human-readable phrases for DoE's opaque ``resolution_class`` strings, used
ONLY to render the crash-deny envelope. The three keys are owned by DoE's
``coordinator/hooks/scripts/preuse-bash-dispatch.py``/``_engine_root`` and
are treated as opaque here -- nothing is imported from that repo, and any
value absent from this mapping (``None`` included) renders no engine note at
all, restoring the envelope to its pre-``resolution_class`` text exactly.
"""


def _crash_deny_is_out_of_class(guard_name: str, cmd: str) -> bool:
    """True when a crashed fail-closed guard can be skipped instead of denying.

    Answers one question: could this guard, had it not crashed, possibly have
    denied THIS command? If its own necessary precondition is absent, the
    answer is provably no -- the guard would have returned ``None`` -- so
    skipping it and continuing the chain denies nothing the guard would have
    allowed, while leaving the rest of the chain (and the Bash tool) working.

    Fails toward denial on every uncertain path: an unmapped guard, or any
    exception raised while computing the answer, returns ``False`` and restores
    the chain-wide deny.

    An EMPTY command is not an uncertain path and is skippable: no token can be
    present in it, and there is nothing for a guard to have denied. The prose
    here previously listed it alongside the fail-toward-denial cases, which never
    matched the code.
    """
    triggers = _CRASH_TRIGGER_SUBSTRINGS.get(guard_name)
    if not triggers:
        return False
    try:
        variants = _crash_probe_variants(cmd or "")
    except Exception:  # noqa: BLE001 -- normalization must never widen the deny path
        return False
    return not any(token in variant for variant in variants for token in triggers)


def _crash_probe_variants(cmd: str) -> Tuple[str, ...]:
    r"""Every text the mapped guards could have run their precondition against.

    The trigger test must be wider than EVERY guard it is keyed for, and each
    guard normalizes differently before its own first-branch test. Two of those
    normalizations MANUFACTURE a keyword the raw text lacks, and a third DELETES
    one that the raw text has, so no single normalized string is a superset of
    all of them:

      - ``_crlf_strip`` + ``_join_backslash_newlines`` join across a deleted
        newline (``gi\<newline>t`` -> ``git``).
      - ``_strip_ws_quoted_spans`` deletes a whitespace-containing quoted span
        and MERGES the text flanking it, so ``gi"a b"t reset --hard HEAD~3``
        becomes ``git reset --hard HEAD~3``. ``check_destructive_git_orphan``
        runs it before its own ``\bgit\b`` test; the raw text carries no
        ``git``.
      - ``_normalize_git_exe_head_to_bare`` maps a ``GIT.EXE`` head to bare
        ``git``. ``check_destructive_git_revert`` runs it before its own
        ``\bgit\b`` test; a substring test on the un-normalized text is
        case-sensitive and misses it.
      - That same span deletion can also REMOVE a keyword, when the keyword
        lives inside the quoted span it deletes. A guard that does not strip
        (``check_no_verify`` tests its own flattened text) would still have seen
        it, so the stripped text alone is narrower than that guard.

    So the answer is the union rather than a pipeline: a token found in ANY
    variant counts as present, which keeps the probe wider than each guard
    individually without having to decide which normalization dominates. Adding
    a variant can only widen the deny, never narrow it.
    """
    base = _dc._crlf_strip(cmd)
    base = _dc._join_backslash_newlines(base)
    variants = [cmd, base]
    for transform in (_dc._strip_ws_quoted_spans, _dc._normalize_git_exe_head_to_bare):
        try:
            variants.append(transform(base))
        except Exception:  # noqa: BLE001 -- a transform that raises drops its variant, never the rest
            continue
    try:
        variants.append(_dc._normalize_git_exe_head_to_bare(_dc._strip_ws_quoted_spans(base)))
    except Exception:  # noqa: BLE001
        pass
    return tuple(variants)


def _crash_deny(guard_name: str, exc: BaseException, resolution_class: Optional[str] = None) -> Dict[str, Any]:
    """Dispatcher-authored generic deny -- mirrors bash
    ``_dispatch_crash_deny``. Fails closed so a buggy hard-deny guard never
    silently permits what it was supposed to block. The message is
    dispatcher-authored, NOT a pass-through of the guard own text.

    The remediation text is written to be actionable WITHOUT the Bash tool:
    once a hard-deny guard crashes, this dispatcher denies every subsequent
    Bash command in the session (including `echo probe`, `git status`, and
    committing already-verified work), so "invoke the standalone check
    function" or "re-run" is advice that requires the exact tool the crash
    just disabled. Confirmed 2026-07-28: a session lost entirely to this,
    unable to even hand-deliver its own bug report. The message must instead
    point at what a human/PM can do from OUTSIDE this session (open the
    guard file, run the repair in their own shell) -- see
    `cross-repo/inbox/2026-07-28-example-game-repo-em-sentinel-guard-fails-closed-and-
    bricked-bash.md`.

    BLAST-RADIUS DECISION (2026-07-29) -- **PARTIALLY REVERSED 2026-07-30 by
    PM ruling; read both halves before touching this path.**

    The original decision: return `_crash_deny(...)` IMMEDIATELY on the first
    crashing guard, without scoping the deny to "only the surface that guard
    governs". Considered and rejected then: narrowing the deny via a keyword
    pre-filter (does this command even mention the sentinel basename / a
    destructive verb / `git worktree`). The stated reason for rejecting it was
    that any such pre-filter is as bypassable as the guard logic it stands in
    for -- an adversarial caller could craft input that both trips the crash
    AND avoids resembling the guarded surface, making the narrowed path a
    genuine new bypass rather than merely a smaller blast radius. Exception-
    type narrowing was rejected on the identical ground.

    What reversed it, and what did NOT. Two things changed:

      1. The mitigation the original decision relied on -- "close the crash's
         root cause before it ships", i.e. land a shared helper's signature
         change and all its consumers as one atomic edit -- was tried and has
         now failed TWICE in three days on the same arity-mismatch shape
         (2026-07-28, and again 2026-07-30). A mitigation that depends on
         every future edit being atomic is a rule, not an artifact, and this
         package's own doctrine says a rule nothing discharges is unfinished
         work. The observed cost is total: every Bash call in every session on
         the machine denied, `echo probe` included, by a git-only guard.
      2. The rejection rests on an ADVERSARIAL caller. This workstream's
         ratified threat model says the opposite -- the caller is an eager
         agent, not an adversary -- and that same premise is what justifies
         declining `setsid`/`strace`/`doas` wrapper coverage elsewhere in this
         package. Holding an adversarial premise here while declining it there
         is incoherent; one of the two had to move.

    What survives the reversal, and is the whole reason it is safe: the
    original objection is CORRECT about hand-guessed pre-filters, and this is
    not one. `_CRASH_TRIGGER_SUBSTRINGS` is derived from each guard's own
    first-branch early return and is provably wider than the guard's matching,
    so a command that fails the trigger is one the guard itself would have
    returned `None` on. There is no input that the guard would deny and the
    trigger would skip -- that is a property of the derivation, not a hope
    about attacker behaviour. A guard with no entry in that mapping keeps this
    function's original chain-wide deny unchanged.

    So: deny-everything-on-crash is retained as the DEFAULT and as the
    fallback on every uncertain path, and is narrowed only where the widening
    is demonstrable per guard.

    ``resolution_class`` (cross-plane signal, 2026-08-05): the opaque engine-
    resolution class DoE's ``preuse-bash-dispatch.py`` computes via its own
    ``_engine_root.resolve_claude_klabauter_root_with_class()`` and feature-detects onto
    ``evaluate_payload_json`` -- see that function's own parameter. Threaded
    here so the deny envelope can name WHICH ENGINE crashed, which matters
    because the single most common crash cause is a sibling repo executing
    this repo's live working tree mid-edit (a multi-file rename leaves a
    module that imports fine and raises at call time) -- from the sibling's
    seat that is indistinguishable from the guard working correctly.
    ``live-working-tree`` is a legitimate, expected state on a co-development
    machine, not an error condition; naming it turns an invisible default
    into a visible, deliberate one. Unknown or absent values (``None``, or any
    string this function does not recognize -- DoE's three values are treated
    as opaque and forward-compatible, never imported) degrade silently: the
    envelope reads EXACTLY as it did before this parameter existed, never a
    partial phrase like "engine: None".
    """
    _engine_phrase = _RESOLUTION_CLASS_PHRASES.get(resolution_class)
    _engine_note = (" (engine: %s)" % _engine_phrase) if _engine_phrase else ""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "coordinator hook dispatcher: %s guard crashed (%s: %s)%s.\n\n"
                "This is a BUG IN THE GUARD, not a policy verdict -- your "
                "command was never inspected and was not found to violate "
                "anything. Failing closed regardless, so a crashed guard "
                "cannot silently permit what it would otherwise block: THIS "
                "command is denied, and so is every other command whose "
                "shape reaches the same crashing code path. If the crash is "
                "shape-conditioned (e.g. only trips on a specific pipe or "
                "flag combination), commands of a different shape -- "
                "`echo probe`, `git status`, committing work you have "
                "already finished and verified -- may still go through "
                "normally; try a trivial command to find out which case "
                "you are in.\n\n"
                "The most likely cause of a crash like this is NOT a bug "
                "in the guard's own logic: it is a peer session on this "
                "shared tree (standard practice here, no worktrees) "
                "mid-editing a module the guard's import chain pulls in -- "
                "an import line moved while a usage of it was still "
                "present, or similar -- which is not something opening "
                "the guard file will show you, since the file on disk may "
                "already be back to consistent by the time you read it. A "
                "real bug in the guard is still possible and worth "
                "checking, but do not assume one just because this "
                "message fired.\n\n"
                "This does NOT require the Bash tool to diagnose: file-"
                "reading tools reach the guard source directly, without "
                "Bash. Report this crash (guard name and exception above) "
                "to whoever dispatched you and open claude-klabauter's "
                "`coordinator_core/bash_guards/` (the guard named above, or "
                "its dispatcher wiring in `dispatch.py`) to find and fix "
                "the crash. Escalating to a human or PM with their own "
                "shell remains available as a fallback, but is not the "
                "only option -- there is no rule that this can only be "
                "fixed from outside the session."
                % (guard_name, type(exc).__name__, exc, _engine_note)
            ),
        }
    }


_PLUGIN_ROOT_UNRESOLVED_GUARD_NAME = "plugin-root-unresolved"
"""Synthetic ``guard_name`` label ``resolve_plugin_root_loud`` records its
counted event under (``guard_advisory_counter.record_advisory_fire``'s first
positional argument is a bare string, not a registered ``GuardEntry.name`` --
see that module's own docstring, "Guard name and UTC timestamp only"). Not a
`guard_chain` entry itself: a plugin-root miss is a resolution-failure signal
one or more OTHER guards (C3's manifest read, C4/C5's ported guards) may hit
mid-evaluation, not a guard with its own detection surface."""


def resolve_plugin_root_loud(
    payload: Optional[Dict[str, Any]], session_id: str, cwd: str
) -> Optional[str]:
    """Resolve this call's ``plugin_root`` via the shared per-call accessor
    (``warm.caller_context.resolve_caller_context``), LOUD-fail-open on a
    miss rather than the silent ``None`` ``resolve_plugin_root()``'s existing
    cosmetic callers already tolerate (C2, state/dispatch-briefs/2026-08-28-
    the-four-folded-bash-guards-get-registered-not-folded/C2.md).

    Every consumer this dispatcher will grow that needs ``plugin_root`` (C3's
    manifest read, C4/C5's ported guards) is itself a CONFINEMENT_DENY guard
    that already fails open on a resolution miss -- a manifest-less guard has
    nothing to key its detection on, so it correctly declines rather than
    denies. A resolution failure passing silently through that fail-open path
    would therefore look identical to "nothing to deny here", which is wrong:
    an install with a broken/missing plugin root is a DEGRADED guard chain,
    not a clean one, and that degradation must be visible.

    Two DELIBERATELY REJECTED alternatives, both named so a future reader
    does not re-litigate them:

      - A hard fail-closed deny on a miss. Rejected -- bricks Bash entirely
        on an OSS-mirror install with no ``coordinator-claude`` plugin
        installed at all, which is a legitimate (if degraded) install shape,
        not a violation.
      - A silent fail-open (the status quo `resolve_plugin_root()` callers
        already have). Rejected -- a resolution failure of the confinement
        guards' own manifest source is not something today's chain can
        currently even notice happened.

    So: LOUD fail-open -- a `stderr` line (this dispatcher's existing
    diagnostic channel; see every other ``print(..., file=sys.stderr)`` in
    this module) plus a COUNTED event, via the already-landed
    ``guard_advisory_counter.record_advisory_fire`` (this module already
    imports it for the advisory-fire seam in the guard loop below -- this is
    a call to a landed mechanism, not a mechanism to build, per C2's own
    body). The counted event follows that recorder's own no-op-on-
    unresolvable-``session_id``/no-write-failure-propagation contract:
    wrapped in ``try/except Exception: pass`` here, exactly like every other
    call site of this recorder in this module, so a counter write failure can
    never turn this resolution miss into anything worse than the miss itself.

    NEITHER HALF OF "LOUD" REACHES THE CALLING AGENT TODAY (state/bug-
    backlog/2026-08-29-loud-and-counted-is-neither-*). The `stderr` line goes
    to the RESIDENT WARM SERVER's own stderr, a different process from the
    agent whose Bash call this dispatcher just allowed -- it never reaches
    that agent. The counted event lands in
    `state/subagent-share/<session_id>/advisory-fire-counts.jsonl`, but
    nothing in this tree reads that file back: no session-start step, no
    ceremony, no doctor probe, no report. So "LOUD" is accurate only about
    the mechanism's INTENT and its resident-process-local diagnostic; to the
    caller, a miss here is currently indistinguishable from a call this
    dispatcher had nothing to say about. Fixing that is tracked separately
    and is not this function's job -- do not infer observability from this
    docstring alone.

    Returns ``None`` on a miss (never raises) -- the caller (a future
    consuming guard) is responsible for its OWN fail-open behavior on that
    ``None``, same as it always would be for `resolve_plugin_root()`'s
    existing ``Optional[str]`` contract; this function only makes the miss
    LOUD, it does not change what happens next.

    Consumer: `_build_guard_chain`'s `guard-doctrine-surface-bash-write`
    entry calls this directly (C4, 2026-08-28), feeding its result into
    `resolve_governed_authoring_surfaces` below.
    """
    plugin_root = _resolve_caller_context(payload).plugin_root
    if plugin_root is None:
        print(
            "bash_guards.dispatch: plugin_root could not be resolved for this "
            "call (the per-call payload carried none, and the ambient "
            "fallback rungs -- CLAUDE_PLUGIN_ROOT, the coordinator-claude "
            "plugin directory, the .doe-root pointer -- all missed); any "
            "guard whose detection depends on a plugin-root-rooted manifest "
            "degrades to its own no-manifest fail-open for this call.",
            file=sys.stderr,
        )
        try:
            _record_advisory_fire(_PLUGIN_ROOT_UNRESOLVED_GUARD_NAME, session_id, cwd)
        except Exception:  # noqa: BLE001 -- counter write failure must never widen this miss
            pass
    return plugin_root


_GOVERNED_MANIFEST_UNREADABLE_GUARD_NAME = "governed-surfaces-manifest-unreadable"
"""Synthetic label for the counted event above -- a resolution failure, not a
guard firing. Named separately from `_PLUGIN_ROOT_UNRESOLVED_GUARD_NAME` so the
two misses stay distinguishable in the counter: an install with no plugin at
all and an install whose manifest is corrupt need different remedies."""

_GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME = "governed-authoring-surfaces.json"
"""Filename (never a path) of the flat-list-of-strings manifest DoE-side pins
to their own ``GOVERNED_AUTHORING_SURFACES`` tuple (`coordinator/hooks/scripts/
_claude_md_ledger.py`), with a drift test on their side so staleness fails in
their CI, never inside this deny path (spec: state/dispatch-briefs/2026-08-28-
the-four-folded-bash-guards-get-registered-not-folded/C3.md). Joined onto a
per-call ``plugin_root`` by ``resolve_governed_authoring_surfaces`` below --
never hardcoded as a full path here, since ``plugin_root`` itself is only
known per call (``resolve_plugin_root_loud``)."""


def resolve_governed_authoring_surfaces(
    plugin_root: Optional[str],
    session_id: str = "",
    cwd: str = "",
) -> Optional[List[str]]:
    """Read the flat list of governed-authoring-surface path strings from
    ``<plugin_root>/governed-authoring-surfaces.json``, FRESH ON EVERY CALL.

    NEGATIVE SPEC -- this function does not import DoE's ``_claude_md_ledger``
    module (a non-package module reachable only by ``sys.path.insert``) and
    does not hardcode a copy of ``GOVERNED_AUTHORING_SURFACES`` the way
    ``_RESOLUTION_CLASS_PHRASES`` hardcodes its own copy of DoE's opaque
    strings -- see this function's own spec backlink above for why those two
    axes (import-vs-data, our-copy-vs-read-their-file) are different and why
    this one lands on the read-their-file side of the second axis. It reads
    a JSON manifest DoE pins to their own tuple with a drift test on their
    side, so staleness fails in their CI, never inside this deny path.

    NEVER MEMOIZED -- no module-level cache, unlike ``_ANY_DECLARED_MATCHERS_
    CACHE`` below (which is process-lifetime-stable because it is a union of
    each guard's own hardcoded ``MATCHERS`` tuple, not something read off
    disk per install). A cached manifest here would freeze to whichever
    plugin install happened to be resolved on the call that filled the
    cache -- the exact per-call-vs-resident-process hazard this plan's own
    Anti-scope names ("Do not memoize the governed-authoring-surfaces
    manifest in the resident server. A cached manifest freezes to whichever
    session booted the engine."). Read fresh, every call, no exceptions.

    TWO OUTCOMES THAT MUST NOT LOOK ALIKE, and conflating them was a real
    defect (state/bug-backlog/2026-08-29-the-guard-rehome-is-not-yet-safe-to-
    dele-9f7396118b81.yaml, gap 2). An explicit empty list means "this install
    governs no surfaces" -- a real answer, correctly silent, correctly allow.
    A read failure means "I could not find out what I protect", which is a
    DEGRADED guard, and a confinement guard that silently declines because its
    config is unreadable is indistinguishable from one that had nothing to
    refuse. DoE's cold twin cannot reach this state at all: it does
    ``from _claude_md_ledger import GOVERNED_AUTHORING_SURFACES``, so a broken
    ledger is an ImportError that takes the hook down, never an empty surface
    set. Ours reads a file, so it can -- and did, measured cold DENY / warm
    ALLOW against a plugin_root holding no manifest.

    Still returns ``None`` rather than denying, on the same reasoning C2
    applied to the ROOT miss: a hard deny bricks Bash on an install that
    legitimately has no plugin. The miss is stderr'd and counted, mirroring
    ``resolve_plugin_root_loud`` exactly -- but mirroring that shape also
    means inheriting its limits (see that function's own docstring, "NEITHER
    HALF OF 'LOUD' REACHES THE CALLING AGENT TODAY"): the stderr line lands
    on the resident warm server's own stderr, not the calling agent's, and
    the counted event's `advisory-fire-counts.jsonl` record has no reader
    anywhere in this tree. To the caller, this miss is not currently
    distinguishable from a call this dispatcher had nothing to say about.

    NOT loud when ``plugin_root`` is itself None: that is the OSS-mirror shape,
    ``resolve_plugin_root_loud`` has already spoken for it, and repeating the
    complaint here would double-count one miss and train readers to skip both.
    Never raises.
    """
    if not plugin_root:
        return None
    import os

    manifest_path = os.path.join(plugin_root, _GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME)

    def _unreadable(detail: str) -> None:
        """One stderr line plus one counted event, best-effort, never fatal."""
        print(
            "bash_guards.dispatch: governed-authoring-surfaces manifest at "
            f"{manifest_path} is {detail}; guard-doctrine-surface-bash-write "
            "has no identifier list for this call and will DECLINE rather than "
            "refuse -- this is a degraded guard chain, not a clean one.",
            file=sys.stderr,
        )
        try:
            _record_advisory_fire(_GOVERNED_MANIFEST_UNREADABLE_GUARD_NAME, session_id, cwd)
        except Exception:  # noqa: BLE001 -- counter write failure must never widen this miss
            pass

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        _unreadable("absent")
        return None
    except Exception as exc:  # noqa: BLE001 -- read/parse failure is a miss, never a crash
        _unreadable(f"unreadable or not valid JSON ({type(exc).__name__})")
        return None
    if not isinstance(data, list):
        _unreadable("valid JSON but not a list")
        return None
    if not all(isinstance(entry, str) for entry in data):
        _unreadable("a list containing non-string entries")
        return None
    return data


#: ``_PER_REPO_SURFACES``) -- never a member of DoE's own
_LOCAL_CONFIG_GOVERNED_SURFACE = "coordinator.local.md"


def _with_local_config_surface(
    governed_surfaces: Optional[List[str]],
) -> List[str]:
    """``governed_surfaces`` (the DoE-manifest read, unchanged) plus this
    repo's own CLASS-2 surface, ``coordinator.local.md``.

    THE GAP THIS CLOSES. ``guard_doctrine_surface_edits.py`` (the
    Write/Edit/MultiEdit admission gate) protects ``coordinator.local.md``
    unconditionally -- its frontmatter is the repo's privileged-execution
    surface (the ceremony-executed test-command strings and the Tier-U
    authority declarations that discharge them) -- but the Bash/PowerShell
    mirror here only ever denied on the DoE manifest's own CLASS-1 identifier
    set, which never lists it. A ``python3 -c "open('coordinator.local.md',
    'a').write(...)"`` therefore reached no guard at all: BLOCKED through
    Edit, silent through Bash.

    Composed HERE, in the caller, rather than inside
    ``resolve_governed_authoring_surfaces`` itself -- that function's own
    contract is "read the DoE manifest verbatim, fresh, every call" (see its
    docstring and ``tests/test_governed_surfaces_manifest_miss.py``, which
    pins its return value unchanged on every miss/hit shape); this repo's own
    CLASS-2 addition is not a manifest concern and must not perturb that
    contract.

    UNCONDITIONAL, deliberately: ``coordinator.local.md`` is appended even
    when ``governed_surfaces`` is ``None`` (manifest miss) or ``[]``
    (explicit "no CLASS-1 surfaces governed here") -- mirroring
    ``guard_doctrine_surface_edits.py``'s own CLASS-2 protection, which needs
    no manifest to apply. Idempotent: a manifest that already lists the bare
    basename is not duplicated."""
    surfaces = list(governed_surfaces) if governed_surfaces else []
    if _LOCAL_CONFIG_GOVERNED_SURFACE not in surfaces:
        surfaces.append(_LOCAL_CONFIG_GOVERNED_SURFACE)
    return surfaces


#: _message_envelope.py``'s ``_WIKI_CITATION_RE`` -- and is not something to
_WIKI_CITATION_RE = re.compile(
    r"(?:coordinator/)?docs/wiki/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md)"
)


def resolve_wiki_citation(text: str, plugin_root: Optional[str]) -> str:
    """Rewrite a ``coordinator/docs/wiki/<page>.md`` citation (flat or
    nested) embedded in ``text`` into an absolute path anchored at
    ``plugin_root`` -- the warm engine's mirror of DoE's
    ``_message_envelope.resolve_wiki_citation()``.

    Renamed from ``resolve_doctrine_surface_wiki_citation`` (2026-08-29): the
    name was doctrine-surface-specific from when this had exactly one
    caller (``guard-doctrine-surface-bash-write``); it is now also threaded
    into ``guard-host-subagent-bash-ban`` and
    ``guard-host-subagent-bash-spawn-shapes``, so a name naming one caller
    was wrong for the other two.

    ``plugin_root`` here plays the same role as DoE's own ``_coordinator_dir()``:
    both name the ``coordinator/`` directory a page path is joined onto (see
    ``resolve_governed_authoring_surfaces`` above, which joins the same
    ``plugin_root`` onto ``governed-authoring-surfaces.json`` and finds it at
    ``<DoE-claude>/coordinator/governed-authoring-surfaces.json`` -- the
    identical root DoE's ``_coordinator_dir()`` resolves to).

    Called ONLY from each caller's own deny path: the overwhelming majority
    of Bash/PowerShell calls never deny, so this substitution never runs for
    them -- placing it here rather than on every call keeps the DR-344
    hot-path budget unaffected by a guard that almost never fires.

    Fails open to ``text`` UNCHANGED (never raises, never emits a broken
    absolute path) when: ``plugin_root`` is ``None`` (unresolvable install,
    same shape ``resolve_plugin_root_loud`` already made LOUD/COUNTED
    upstream -- this function does not re-report it), or the citation does
    not match ``_WIKI_CITATION_RE`` at all, or the match does not start at
    the beginning of ``text`` or immediately follow whitespace/an opening
    delimiter (mirrors cold's own double-mangling guard).
    """
    if not plugin_root:
        return text
    from pathlib import Path as _Path

    def _render_resolved(path: "_Path") -> str:
        """Home-collapse a resolved citation so it carries no operator
        identity, mirroring DoE's ``_render_resolved`` (DoE ``367671b59``)
        byte-for-byte -- taken verbatim rather than reimplemented, because
        deny-text parity is measured on the rendered string.

        The ``~/`` prefix is a LITERAL and ``as_posix()`` runs on the
        RELATIVE part only. That is not style: ``str(_Path("~") / rel)``
        renders ``~\\...`` on Windows, and backslash-tilde does not expand
        -- measured against the tool a dispatched agent reads with
        (``~/.claude/CLAUDE.md`` opens, ``~\\.claude\\CLAUDE.md`` does not),
        so the naive collapse would trade an identity leak for an
        UNOPENABLE citation, reintroducing the very defect the resolver
        exists to fix on the one platform where the leak is worst.

        ``relative.parts`` guards the exact-home case, where
        ``relative_to`` yields ``.`` and naive formatting emits ``~/.``.

        Fails open to the absolute path on ``RuntimeError``/``OSError`` as
        well as ``ValueError``: ``Path.home()`` raises when home cannot be
        determined, and leaking a path is recoverable where dropping the
        reader's only pointer is not.
        """
        try:
            relative = path.relative_to(_Path.home())
        except (ValueError, RuntimeError, OSError):
            return str(path)
        return f"~/{relative.as_posix()}" if relative.parts else "~"

    def _sub(match: "re.Match[str]") -> str:
        start = match.start()
        if start > 0 and text[start - 1] not in " \t\n(['\"`":
            return match.group(0)
        # INCLUDING its normalization of a nested `match.group(1)`'s
        return _render_resolved(_Path(plugin_root) / "docs" / "wiki" / match.group(1))

    return _WIKI_CITATION_RE.sub(_sub, text)


_ANY_DECLARED_MATCHERS_CACHE: Optional["frozenset[str]"] = None


def _any_declared_matchers() -> "frozenset[str]":
    """The union of every registered guard's own declared ``matchers`` --
    computed ONCE per process and cached, never per dispatch call (C1,
    docs/plans/2026-08-07-command-guards-fire-under-both-tool-names.md).

    This is the master gate's cheap early-exit set in ``evaluate_payload_
    json`` below: while every ``GuardEntry`` sits at its ``("Bash",)``
    default, this union is just ``{"Bash"}``, so a ``PowerShell`` payload is
    rejected at the master gate -- before ``resolve_command_positions``,
    ``_build_guard_chain``, or the guard loop ever run -- and it widens
    automatically the moment any guard's own ``MATCHERS`` does.

    Deliberately NOT derived by calling ``_build_guard_chain`` itself:
    constructing that list (every lambda, every ``GuardEntry``) is exactly
    the chain-construction cost this gate exists to let a non-matching
    payload skip (see ``evaluate_payload_json``'s own master-gate comment
    below, and AC4 in the plan above). Instead this unions the SAME
    per-module ``MATCHERS`` values (or the ``("Bash",)`` literal, for a
    registration whose backing module declares none) that each
    ``GuardEntry.matchers=`` in ``_build_guard_chain`` is built from -- kept
    in step with that registration list by construction, since both read the
    identical imported names; ``test_tool_name_membership.py`` asserts the
    two agree.

    ``block_disarm_marker_sentinel_creation`` is imported LOCALLY here, not
    at module top, for the identical circular-import reason
    ``_build_guard_chain`` already documents for its own deferred import of
    the same module: that module imports ``_blanket_disarm``, which imports
    ``GuardBand`` FROM this module at ITS top level, so a module-level
    import here would resolve before ``GuardBand`` exists on a fresh
    interpreter. Safe here because this function is only ever CALLED (never
    imported-from-the-top), after this module has finished executing its
    own top level -- the same safety argument ``_build_guard_chain`` already
    makes for its own deferred import.
    """
    global _ANY_DECLARED_MATCHERS_CACHE
    if _ANY_DECLARED_MATCHERS_CACHE is None:
        from coordinator_core.bash_guards.block_disarm_marker_sentinel_creation import (
            MATCHERS as _matchers_disarm_marker_sentinel_creation,
        )
        # P070-C3: the same MATCHERS-only deferral, for the same reason, as
        from coordinator_core.bash_guards.block_subagent_plan_body_bash_write import (
            MATCHERS as _matchers_plan_body_bash_write,
        )
        from coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist import (
            MATCHERS as _matchers_reviewer_bash_outside_allowlist,
        )
        from coordinator_core.bash_guards.block_subagent_destructive_action import (
            MATCHERS as _matchers_subagent_destructive_action,
        )
        from coordinator_core.bash_guards.block_illegal_filename import (
            MATCHERS as _matchers_illegal_filename,
        )
        from coordinator_core.bash_guards.check_test_suite_invocation import (
            MATCHERS as _matchers_test_suite_invocation,
        )
        from coordinator_core.bash_guards.block_subagent_commit import (
            MATCHERS as _matchers_subagent_commit,
        )
        from coordinator_core.bash_guards.check_raw_pid_liveness import (
            MATCHERS as _matchers_raw_pid_liveness,
        )
        from coordinator_core.bash_guards.block_worktree_creation import (
            MATCHERS as _matchers_worktree_creation,
        )
        from coordinator_core.bash_guards.block_approval_sentinel_creation import (
            MATCHERS as _matchers_approval_sentinel_creation,
        )
        from coordinator_core.bash_guards.block_worktree_sentinel_creation import (
            MATCHERS as _matchers_worktree_sentinel_creation,
        )
        from coordinator_core.bash_guards.block_stash_destruction import (
            MATCHERS as _matchers_stash_destruction,
        )
        from coordinator_core.bash_guards.block_subagent_stash_creation import (
            MATCHERS as _matchers_subagent_stash_creation,
        )
        from coordinator_core.bash_guards.block_noncanonical_branch_creation import (
            MATCHERS as _matchers_noncanonical_branch_creation,
        )
        from coordinator_core.bash_guards.guard_inprocess_search import (
            MATCHERS as _matchers_inprocess_search,
        )
        from coordinator_core.bash_guards.guard_grep_via_bash import (
            MATCHERS as _matchers_grep_via_bash,
        )
        from coordinator_core.bash_guards.guard_multiprobe_banner import (
            MATCHERS as _matchers_multiprobe_banner,
        )
        from coordinator_core.bash_guards.guard_plumbing_and_loops import (
            MATCHERS as _matchers_plumbing_and_loops,
        )
        from coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal import (
            MATCHERS as _matchers_repo_setup_claude_home_refusal,
        )

        _ANY_DECLARED_MATCHERS_CACHE = frozenset(("Bash",)).union(
            _matchers_plan_body_bash_write,
            _matchers_reviewer_bash_outside_allowlist,
            _matchers_subagent_destructive_action,
            _matchers_illegal_filename,
            _matchers_test_suite_invocation,
            _matchers_subagent_commit,
            _matchers_raw_pid_liveness,
            _matchers_worktree_creation,
            _matchers_approval_sentinel_creation,
            _matchers_worktree_sentinel_creation,
            _matchers_stash_destruction,
            _matchers_subagent_stash_creation,
            _matchers_noncanonical_branch_creation,
            _matchers_inprocess_search,
            _matchers_grep_via_bash,
            _matchers_multiprobe_banner,
            _matchers_plumbing_and_loops,
            _matchers_disarm_marker_sentinel_creation,
            _matchers_repo_setup_claude_home_refusal,
        )
    return _ANY_DECLARED_MATCHERS_CACHE


def _session_advisory_already_fired(
    name: str, out: Dict[str, Any], session_id: str, cwd: str
) -> bool:
    """Per-session, per-(guard,shape) advisory dedupe consult (item 7,
    state/handoffs/2026-07-30-boot-context-bloat-non-orientation-surfaces.md;
    baseline: state/audits/2026-08-14-boot-payload-baseline.md § "Item 7").

    ``True`` only when this EXACT ``(guard name, advisory text)`` pair has
    already fired once this session -- see
    ``_advisory_dedupe.advisory_dedupe_key`` for the shape fingerprint.
    ``False`` (never suppress) on every other path, by construction: this
    function is TOTAL, called from inside `evaluate_payload_json`'s loop
    OUTSIDE the per-guard try/except (mirroring `_suppress_advisory`'s own
    placement one call above it in that loop), so a bug here must degrade to
    "show the advisory" rather than crash the whole dispatch or silently
    swallow one. On the FIRST firing of a given shape this call also records
    the marker (best-effort, itself fail-open inside
    `_advisory_dedupe.mark_advised`) so the SECOND firing sees it.

    Callers are responsible for only invoking this on an envelope already
    confirmed non-hard-deny (`not _is_hard_deny_envelope`) -- this function
    has no notion of "deny" itself; see `_advisory_dedupe`'s own module
    docstring, "NEVER CALLED FOR A BLOCK".
    """
    if not session_id:
        return False
    try:
        shape_key = _advisory_dedupe_key(name, out)
        if shape_key is None:
            return False
        gitdir = _resolve_gitdir_for_dedupe(cwd)
        if _already_advised(gitdir, session_id, shape_key):
            return True
        _mark_advised(gitdir, session_id, shape_key)
        return False
    except Exception:  # noqa: BLE001 -- fail open: never suppress on a bug here
        return False


def _is_hard_deny_result(result: Union[Dict[str, Any], List[Dict[str, Any]], None]) -> bool:
    """Same predicate `_evaluate_payload_json_budgeted`'s own loop computes
    per-envelope as `_is_hard_deny_envelope` (that function, ``out``/``_hso``
    local vars) -- re-expressed here against the CALL's final result rather
    than an in-loop candidate, since a genuine hard-deny is always the
    single ``Dict`` returned immediately (never folded into the
    ``collect_advisories`` list -- see that function's own docstring)."""
    if not isinstance(result, dict):
        return False
    _hso = result.get("hookSpecificOutput")
    return isinstance(_hso, dict) and _hso.get("permissionDecision") == "deny"


def _record_bash_write_claims(
    raw: str, result: Union[Dict[str, Any], List[Dict[str, Any]], None]
) -> None:
    """C1 (docs/plans/2026-08-30-a-bash-write-reaches-the-ledger-that-
    decides-what-gets-committed.md): fire ``write_claim_record.
    record_write_claims`` for THIS same PreToolUse call, from the ONE seam
    that sees every exit ``_evaluate_payload_json_budgeted`` can take --
    unlike that function's own post-chain ``return (_collected or
    None)...``, reached only when no guard produced an envelope at all,
    which would miss the advisory-allow case (``cat-heredoc-write-advise``)
    this recorder exists for.

    Re-parses ``raw`` independently rather than threading cmd/session_id/cwd
    out of the budgeted call: that function's positional signature is a
    cross-plane contract (see ``evaluate_payload_json``'s own docstring,
    feature-detected by DoE's caller) and must not grow an internal-only
    return channel. The parse reads the same three fields that function
    reads (``tool_input.command``/``session_id``/``cwd``) and is defensive
    where it cannot be sure: a non-dict ``tool_input`` is treated as empty
    rather than rejecting the payload, and a missing ``cmd``/``session_id``
    returns without recording. It is NOT asserted to be byte-identical to
    that function's own parse -- if the two are ever suspected of diverging
    on an exotic payload shape, this is the place to check, and the failure
    direction here is recording nothing. ``root`` is resolved via ``git.repo_root.show_toplevel`` -- a
    walk, never a spawn (see that function's own docstring) -- rather than
    re-deriving a repo-root convention of this module's own.

    Fails toward doing nothing: any parse failure here, and
    ``record_write_claims`` itself, are swallowed -- this function must
    never raise into its caller and must never influence the verdict
    already decided.
    """
    try:
        from coordinator_core.bash_guards.write_claim_record import (
            record_write_claims as _record_write_claims,
        )

        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        cmd = tool_input.get("command") or ""
        if not isinstance(cmd, str):
            cmd = ""
        session_id = payload.get("session_id") or ""
        if not isinstance(session_id, str):
            session_id = ""
        cwd = payload.get("cwd") or ""
        if not isinstance(cwd, str):
            cwd = ""
        if not cmd or not session_id:
            return
        cmd = cmd.replace("\r", "")
        root = _show_toplevel(cwd or None)
        if not root:
            return
        _denied = _is_hard_deny_result(result)
        _record_write_claims(cmd, session_id, root, denied=_denied)
        _record_bash_read_claims(cmd, session_id, root, denied=_denied)
    except Exception:  # noqa: BLE001 -- recording must never affect the verdict
        return


def _record_bash_read_claims(cmd: str, session_id: str, root: str, denied: bool) -> None:
    """C2 RECORD leg (docs/plans/2026-09-02-a-write-that-discards-what-
    you-never-saw.md): best-effort recording of a content-hash fingerprint
    for every in-repo path ``write_claim_record.resolve_read_targets``
    finds this Bash call reading (``cat P``/``head P``/``tail P``/``sed -n
    ... P``/``less P``), appended to THIS session's own touch-record sink
    as a plain TOUCH -- the baseline ``touch_record.last_seen_hash``
    later reads for C2's DENY leg (``dispatch_checks.check_stale_write``).

    Fires from the SAME post-verdict seam ``_record_write_claims`` already
    fires from, immediately alongside it -- never a second post-verdict
    recording seam of its own (C2's own body: "this plan adds no second
    recording seam"). Shares that call's own ``denied`` gate: a read this
    session's own command never actually performed (the call was denied)
    is not a fact this session observed, and recording a baseline for it
    would be exactly the lie ``record_write_claims``'s own docstring warns
    against for the write side.

    Never raises, never influences the verdict: any failure here is
    swallowed, matching ``_record_bash_write_claims``'s own posture. No
    git spawn -- ``compute_content_hash`` is a plain file read/hash, never
    a subprocess.
    """
    if denied or not cmd or not cmd.strip() or not session_id or not root:
        return
    try:
        import os

        from coordinator_core.bash_guards.write_claim_record import (
            resolve_read_targets,
        )
        from coordinator_core.session.touch_record import (
            KIND_READ,
            append_touch_claims,
            compute_content_hash,
        )

        rels: List[str] = []
        hashes: Dict[str, str] = {}
        for raw_target in resolve_read_targets(cmd):
            resolved_target = (
                raw_target
                if os.path.isabs(raw_target)
                else os.path.join(root, raw_target)
            )
            resolved_target = os.path.normpath(resolved_target)
            if not _dc._is_within(resolved_target, root):
                continue
            rel = os.path.relpath(resolved_target, root).replace(os.sep, "/")
            rels.append(rel)
            try:
                h = compute_content_hash(resolved_target)
            except Exception:
                h = None
            if h is not None:
                hashes[rel] = h

        if rels:
            # KIND_READ: this channel is `resolve_read_targets` by construction --
            append_touch_claims(
                rels, session_id, root, content_hashes=hashes or None, kind=KIND_READ
            )
    except Exception:
        return


def evaluate_payload_json(
    raw: str,
    policy_file: Optional[str] = None,
    host_is_windows: Optional[bool] = None,
    resolved: Optional[List[_ResolvedCommand]] = None,
    resolution_class: Optional[str] = None,
    *,
    collect_advisories: bool = False,
) -> Union[Dict[str, Any], List[Dict[str, Any]], None]:
    """Arm this dispatch's git-probe budget, then run the chain (see
    ``_evaluate_payload_json_budgeted`` for the whole contract -- this
    wrapper adds nothing else and returns its result unchanged).

    A WRAPPER, not a ``try``/``finally`` around the chain in place, because
    this function's signature is itself a cross-plane contract: DoE's
    ``coordinator/hooks/scripts/preuse-bash-dispatch.py`` feature-detects
    every parameter below via ``inspect.signature(evaluate_payload_json).
    parameters`` before deciding what to pass. The parameters are therefore
    spelled out here explicitly and forwarded positionally-by-name; a
    ``*args, **kwargs`` passthrough would erase them from that signature and
    silently drop ``policy_file``/``resolution_class`` on the floor at the
    only caller that sends them.

    THE SEAM IS DELIBERATELY THIS ONE AND ONLY THIS ONE. The budget exists
    because the harness cancels a PreToolUse hook that overruns its window
    (15 000 ms), and a cancelled hook delivers NO verdict at all -- the
    2026-08-15 bare-commit sweep landed with neither its deny nor its
    advisory printed, ``durationMs=16336``. This function is the entry point
    of the process that runs inside that window; a check invoked directly
    (every test in this package, ``_alternative_liveness``'s liveness
    harness) is not, keeps no window, and must stay unbudgeted -- see
    ``_dc._git_probe_deadline``'s inert-by-default note.
    """
    _dc._arm_git_probe_deadline()
    try:
        result = _evaluate_payload_json_budgeted(
            raw,
            policy_file,
            host_is_windows,
            resolved,
            resolution_class,
            collect_advisories=collect_advisories,
        )
    finally:
        _dc._disarm_git_probe_deadline()
    _record_bash_write_claims(raw, result)
    return result


def _evaluate_payload_json_budgeted(
    raw: str,
    policy_file: Optional[str] = None,
    host_is_windows: Optional[bool] = None,
    resolved: Optional[List[_ResolvedCommand]] = None,
    resolution_class: Optional[str] = None,
    *,
    collect_advisories: bool = False,
) -> Union[Dict[str, Any], List[Dict[str, Any]], None]:
    """Parse ``raw`` (the full PreToolUse JSON payload) exactly ONCE and run
    every folded guard in the COMBINED legacy cross-process order (see module
    docstring). Returns the FIRST non-``None`` envelope dict encountered
    (deny wins the hard chain; soft/content/advisory each contribute at most
    one), or ``None`` if every guard allows silently.

    ``collect_advisories`` (C10, 2026-08-06; mirrors C6's opt-in contract on
    the write leg, ``write_guards.engine.evaluate``): keyword-only,
    defaulting to ``False`` -- every existing caller (this dispatcher's own
    standalone ``main()``, and every caller written before this parameter
    existed) keeps its current single-envelope return BYTE-IDENTICAL, taking
    the exact same code path as before this parameter existed.

    ``True`` opts into aggregation, closing the silent-drop this dispatcher
    inherited from the same shape C6 fixes: today, whichever soft/content/
    advisory envelope fires FIRST wins and every later one in the same call
    is dropped without a trace. Chosen aggregate shape, DELIBERATELY NOT the
    write leg's flat ``List[envelope]``-always shape, because this
    dispatcher's own docstring (see module docstring, `GuardBand`) already
    distinguishes CONFINEMENT_DENY/PLATFORM_CONDITIONED_DENY (hard-deny,
    ``fail_closed=True``) from ADVISORY_REWRITE (soft/content/advisory,
    mostly ``fail_closed=False``) INTERLEAVED in one flat chain -- unlike the
    write leg, where every hard-deny guard runs in its own phase strictly
    before any advisory guard ever executes. A genuine hard-deny envelope can
    therefore appear AFTER one or more advisory/content/soft envelopes have
    already fired earlier in this same chain (e.g. a PLATFORM_CONDITIONED_DENY
    guard, registered at the tail, denying after an ADVISORY_REWRITE guard
    upstream already produced an allow+context envelope). The hard-deny
    short-circuit (this function's own long-standing "deny wins the hard
    chain" contract) is preserved by construction: the return type is
    ``Union[Dict[str, Any], List[Dict[str, Any]], None]`` --

      * a genuine (non-suppressed) hard-deny envelope, whenever hit, is
        returned IMMEDIATELY as a single ``Dict`` -- identical shape and
        identical envelope to what the ``collect_advisories=False`` path
        would have returned for the same input, discarding any
        soft/content/advisory envelopes already collected earlier in this
        same call (deny wins the chain outranks aggregation, exactly as it
        outranks the single-envelope contract today);
      * every soft/content/advisory envelope that fires along the way
        (INCLUDING a suppressed advisory's surviving rewrite leg, per
        ``_suppress_advisory``/``_resolve_suppressed_envelope`` -- see their
        own docstrings for why that leg is never a deny) is instead appended,
        in registration/priority order, to a list, and the chain keeps
        walking instead of stopping at the first one;
      * if the chain completes with the collected list non-empty, that
        ``List[Dict]`` is returned;
      * if the chain completes with nothing collected (every guard allowed
        silently), ``None`` is returned -- symmetric with the
        ``collect_advisories=False`` "every guard allows silently" case.

    Per-guard bookkeeping (``_record_advisory_fire``) fires for EVERY
    returned advisory/content/soft envelope, not just the first (mirrors
    C6's AC8) -- unchanged from today for the single envelope a hard-deny
    short-circuit returns (no bookkeeping call on that path, exactly as
    today). The in-session operator unlock (``_consume_unlock``) is UNCHANGED
    by this parameter -- it already runs, today, ONLY on the hard-deny path
    (see the loop below), which this parameter does not touch; no unlock
    path is added for soft/content/advisory envelopes, since none exists
    today and this chunk is not the place to invent one.

    Mirrors the bash dispatcher stdin-read-once + `[ -z "$INPUT" ] && exit 0`
    + non-Bash-tool-name early exit + CRLF-normalize-once discipline.

    ``policy_file`` (C5a/C6, 2026-07-27): the explicit path to DoE's
    ``subagent-sandbox-policy.yaml``, computed by the calling
    ``preuse-bash-dispatch.py`` the same way ``enforce-agent-dispatch-
    mode.py`` already does for its own subprocess call
    (``Path(__file__).resolve().parents[2] / "subagent-sandbox-policy.yaml"``)
    and threaded through here as the in-process equivalent of that script's
    ``--policy`` subprocess flag. Forwarded ONLY to
    ``block_reviewer_bash_outside_allowlist.check`` (the sole ``bash_policy``
    consumer as of this change) as its ``policy_path`` keyword; every other
    guard in the chain is unaffected. ``None`` (the default -- what every
    caller that predates this change, and this dispatcher's own standalone
    ``main()``, still pass) makes that guard fall through to its own
    hardcoded-fallback AC11 path, exactly as it did before this parameter
    existed.

    ``host_is_windows`` (BX-9, 2026-07-29): the platform-override kwarg
    ``_platform_verdict.platform_verdict``/``platform_verdict_for_shape``
    define and document under "Platform-override contract (AC-8 / AC-11)" in
    ``_platform_verdict.py``. That module's own docstring pins this exact
    signature as "the contract the dispatch-chain-owning chunk authors
    against" -- placing the override only in ``_platform_verdict.py``
    satisfies macOS-exercisability in isolation but fails the requirement
    that every guard be OBSERVED FIRING THROUGH THE REAL DISPATCHER, not in
    isolation. Threaded here as a per-call override (identical shape to
    ``policy_file`` above), defaulting to ``None`` at every hop so an
    ordinary production call -- the harness's own invocation, and this
    dispatcher's own standalone ``main()`` -- falls through unchanged to
    each guard's own ``os.name == "nt"`` read. Three guards in
    ``guard_chain`` below now DO call ``platform_verdict``/``platform_
    verdict_for_shape`` (BX-6/BX-7/BX-8, ``guard_grep_via_
    bash.py`` / ``guard_multiprobe_banner.py`` / ``guard_plumbing_and_
    loops.py``, registered 2026-07-29 at the TAIL of ``guard_chain`` -- see
    that registration's own inline comment for why tail-not-hard-deny-cohort
    is deliberate). Each one's lambda forwards this same keyword (e.g.
    ``lambda: _check_grep_via_bash(payload, host_is_windows=host_is_
    windows)``), exactly as anticipated here.

    ``resolved`` (M5, 2026-07-30): the pre-computed
    ``_command_tokenizer.resolve_command_positions(cmd)`` result, threaded
    through as a per-call override identical in shape to ``policy_file``/
    ``host_is_windows`` above -- ``None`` (every caller that predates this
    parameter, and this dispatcher's own standalone ``main()``) makes the
    dispatcher compute it itself, below. Forwarded ONLY to
    ``_dc.check_no_verify`` as of this change (the sole cohort-A guard
    migrated onto the shared resolver so far -- see that function's own
    docstring for why the other six are NOT yet safe consumers: each
    resolves its own segments through a DIFFERENT, hand-rolled mechanism
    -- raw regex splitting, a bespoke quote-preserving text splitter -- whose
    output shape is not a drop-in replacement for `resolve_command_positions`'s
    shlex-tokenized one without its own guard-specific verdict-parity proof,
    the same class of work M5P did for `check_no_verify`'s own which/type
    fix). A single dispatch computes this AT MOST once, gated on a cheap
    `"git" in cmd` pre-filter so a non-git command (the overwhelming
    majority of real Bash calls) pays no extra walk -- computing it
    unconditionally would be a net ADDITION against AC-8 for exactly the
    commands no cohort-A guard even inspects today.

    ``resolution_class`` (cross-plane signal, 2026-08-05): opaque engine-
    resolution class computed by DoE's ``coordinator/hooks/scripts/preuse-
    bash-dispatch.py`` (its own ``_engine_root.resolve_claude_klabauter_root_with_
    class()``) and feature-detected onto this function via ``inspect.
    signature(evaluate_payload_json).parameters`` -- that caller passes it
    ONLY if this parameter is present in the signature, so its mere
    existence here is the contract, independent of what any caller actually
    sends. One of three DoE-owned opaque strings (``"resolved-engine"``,
    ``"live-working-tree"``, ``"unresolved"``); never imported from DoE's
    repo, only compared against ``_RESOLUTION_CLASS_PHRASES``' keys.
    Forwarded ONLY to ``_crash_deny`` (via the fail-closed branch below), so
    it changes what a crash's deny envelope SAYS, never any allow/deny
    verdict. ``None`` -- every caller that predates this parameter, and this
    dispatcher's own standalone ``main()`` -- degrades the envelope back to
    its pre-existing text exactly; so does any string this dispatch does not
    recognize.
    """
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        print("bash_guards.dispatch: unparseable PreToolUse payload: %s" % exc, file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None

    # is Dialect.POWERSHELL` to decide whether to run
    # Every value in `_tool_names.COMMAND_TOOL_NAMES` (today: "Bash" and
    # widened to `COMMAND_TOOL_NAMES` is unaffected (`"Bash"` is a member of
    # NEGATIVE SPEC -- no opt-out. This normalization is unconditional: no
    _raw_tool_name = payload.get("tool_name")
    _gating_tool_name = "Bash" if _raw_tool_name in COMMAND_TOOL_NAMES else _raw_tool_name

    # tool-names.md): union check against the DECLARED-matchers set, NOT
    # against `_tool_names.COMMAND_TOOL_NAMES` (the observable universe) --
    # the moment any guard's own `MATCHERS` does, with no edit required at
    if _gating_tool_name not in _any_declared_matchers():
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cmd = tool_input.get("command") or ""
    if not isinstance(cmd, str):
        cmd = ""
    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""
    agent_id = payload.get("agent_id") or ""
    if not isinstance(agent_id, str):
        agent_id = ""
    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""

    cmd = cmd.replace("\r", "")

    # FAIL-SILENT contract); the outer try/except here is belt-and-braces
    try:
        _record_chain_arrival(session_id, bool(agent_id), cwd)
    except Exception:  # noqa: BLE001 -- belt-and-braces; the callee already never raises
        pass

    if (
        resolved is None
        and "git" in cmd
        and _dialect_from_tool_name(_raw_tool_name) is not _Dialect.POWERSHELL
    ):
        try:
            resolved = _resolve_command_positions(cmd)
        except Exception as exc:  # noqa: BLE001 -- degrade to per-guard fallback, never propagate
            print(
                "bash_guards.dispatch: resolve_command_positions crashed (%s: %s); "
                "cohort-A guards fall back to their own tokenize path."
                % (type(exc).__name__, exc),
                file=sys.stderr,
            )
            resolved = None


    guard_chain = _build_guard_chain(cmd, session_id, cwd, payload, policy_file, host_is_windows, resolved)

    # that module's own "BAND-SCOPED SUPPRESSION" docstring), so a
    from coordinator_core.bash_guards._blanket_disarm import disarm_status as _disarm_status

    _disarm = _disarm_status(payload)

    # resolve the EFFECTIVE host ONCE per dispatch, exactly like `_disarm`
    # `host_is_windows=None` is the PRODUCTION shape (every real harness
    _effective_host_is_windows = _resolve_host_is_windows_public(host_is_windows)

    _collected: List[Dict[str, Any]] = []

    for entry in guard_chain:
        name, fn, fail_closed, _band = entry.name, entry.fn, entry.fail_closed, entry.band
        if _gating_tool_name not in entry.matchers:
            continue
        if (
            _disarm.active
            and _disarm.bands
            and _band is not GuardBand.CONFINEMENT_DENY
            and _band.value in _disarm.bands
        ):
            # contain `GuardBand.CONFINEMENT_DENY.value` (a marker naming it
            # see that module's own "BAND-SCOPED SUPPRESSION" doctring), but
            # the explicit `_band is not GuardBand.CONFINEMENT_DENY` guard
            print(
                "bash_guards.dispatch: %s guard skipped -- blanket-disarm "
                "marker suppresses band %s (%s)."
                % (name, _band.value, _disarm.detail),
                file=sys.stderr,
            )
            continue
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001 -- F1: isolate, do not propagate
            if fail_closed:
                if _crash_deny_is_out_of_class(name, cmd):
                    print(
                        "bash_guards.dispatch: %s guard crashed (%s: %s); command is "
                        "outside that guard's own target class, so the deny is scoped "
                        "to the class rather than to the Bash tool. Guard skipped, "
                        "chain continues."
                        % (name, type(exc).__name__, exc),
                        file=sys.stderr,
                    )
                    continue
                return _crash_deny(name, exc, resolution_class=resolution_class)
            print(
                "bash_guards.dispatch: %s guard crashed (%s: %s); "
                "treating as no-context (advisory, fail-open)."
                % (name, type(exc).__name__, exc),
                file=sys.stderr,
            )
            out = None
        if out is not None:
            # the 23 CONFINEMENT_DENY/PLATFORM_CONDITIONED_DENY guards fired
            # removal` is `fail_closed=True` (CONFINEMENT_DENY) yet can also
            # in the CONFINEMENT_DENY band) route through the identical
            # a DIFFERENT session id and `_consume_unlock` looks up this
            # `True` -> skip THIS guard's deny and CONTINUE the loop (a
            # CONSTRUCTION: `out.get("hookSpecificOutput", {})` would raise
            _hso = out.get("hookSpecificOutput")
            # Gated on the ENVELOPE ALONE (permissionDecision == "deny"),
            # clearable nor advertised one. CORRECTION (Review:
            # audits); see `_SENTINEL_ELIGIBLE_ADVISORY_GUARDS`'s own
            _is_hard_deny_envelope = (
                isinstance(out, dict)
                and isinstance(_hso, dict)
                and _hso.get("permissionDecision") == "deny"
            )
            # `_SENTINEL_ELIGIBLE_ADVISORY_GUARDS`'s own docstring for the
            # envelope. Every `fail_closed=True` (CONFINEMENT_DENY /
            # PLATFORM_CONDITIONED_DENY) guard remains unconditionally
            _sentinel_eligible = fail_closed or name in _SENTINEL_ELIGIBLE_ADVISORY_GUARDS
            # envelope that is not a positively-recognised WINDOWS_COST_ONLY
            # RUN-THEN-DROP discipline: the guard has already run and
            _suppressed = _suppress_advisory(
                out,
                advisory_value=entry.advisory_value,
                band=entry.band,
                host_is_windows=_effective_host_is_windows,
            )
            if (
                _is_hard_deny_envelope
                and _sentinel_eligible
                and not _suppressed
                and session_id
                and _consume_unlock(session_id, name)
            ):
                print(
                    "bash_guards.dispatch: %s guard's hard-deny cleared by "
                    "in-session operator unlock (session=%s); chain "
                    "continues." % (name, session_id),
                    file=sys.stderr,
                )
                try:
                    _record_deny_fire(name, session_id, True, cwd)
                except Exception:
                    pass
                continue
            if _is_hard_deny_envelope and not _suppressed:
                try:
                    _record_deny_fire(name, session_id, False, cwd)
                except Exception:
                    pass
            if _is_hard_deny_envelope and _sentinel_eligible and not _suppressed:
                out = _annotate_unlock(
                    out,
                    session_id,
                    name,
                    _override_keys_doc_display(),
                    agent_id=agent_id,
                )
            if _suppressed:
                emitted = _resolve_suppressed_envelope(out)
                print(
                    "bash_guards.dispatch: %s guard suppressed on non-Windows "
                    "host (advisory_value=%s)%s."
                    % (
                        name,
                        entry.advisory_value.value,
                        "" if emitted is None else " -- rewrite leg preserved",
                    ),
                    file=sys.stderr,
                )
                if emitted is None:
                    continue
                if not fail_closed:
                    try:
                        _record_advisory_fire(name, session_id, cwd)
                    except Exception:
                        pass
                if collect_advisories:
                    _collected.append(emitted)
                    continue
                return emitted
            if not fail_closed:
                try:
                    _record_advisory_fire(name, session_id, cwd)
                except Exception:
                    pass
            if (
                not _is_hard_deny_envelope
                and _session_advisory_already_fired(name, out, session_id, cwd)
            ):
                # R6 (2026-09-26): a repeat firing of the same (guard, shape)
                # this session puts NO TEXT in context on an allowed call --
                # `silence_repeat_advisory` strips `additionalContext` for an
                # absent/"allow" `permissionDecision` (keeping any
                # `updatedInput` rewrite), leaves "ask" untouched, and
                # collapses to `{}` (no-advisory-equivalent) when nothing but
                # `hookEventName` remains. Still RETURNED below, never
                # `continue`d -- see `degrade_advisory_envelope`'s retired
                # docstring for why a `continue` here would let a
                # lower-precedence guard win the slot a higher-precedence one
                # already claimed. Fail-open, unconditionally (module
                # docstring, "FAIL OPEN, UNCONDITIONALLY"): any error here
                # falls back to the full envelope, never to silence.
                out = _silence_repeat_advisory(out)
            if collect_advisories and not _is_hard_deny_envelope:
                if out:
                    _collected.append(out)
                continue
            return out

    return (_collected or None) if collect_advisories else None


def _build_guard_chain(
    cmd: str,
    session_id: str,
    cwd: str,
    payload: Dict[str, Any],
    policy_file: Optional[str],
    host_is_windows: Optional[bool],
    resolved: Optional[List[_ResolvedCommand]] = None,
) -> List[GuardEntry]:
    """Build the registered guard chain for one call -- pulled out of
    ``evaluate_payload_json`` so the registration ITSELF (name, fail_closed,
    band, advisory_value -- never the per-call closures) is introspectable
    without forcing a caller to fabricate a full PreToolUse payload and run
    every guard's real logic. ``tests/test_guard_band_membership.py`` calls
    this directly with a harmless dummy command/payload and inspects the
    returned ``GuardEntry`` attributes -- it never calls the ``fn``
    closures, so this stays a STRUCTURAL registration check, the same
    posture the retired ``test_hard_denies_precede_rewrites.py`` held via
    source-text regex parsing (fragile against reformatting); introspecting
    the live registration here is the more robust replacement.

    ``resolved`` (M5, 2026-07-30): the shared-resolver segmentation computed
    at most once in ``evaluate_payload_json`` (``None`` for every caller that
    predates this parameter, including the structural test above, which
    passes a dummy command/payload and never needs it). Forwarded ONLY to
    ``no-verify``'s closure -- the sole cohort-A guard migrated onto it so
    far; see ``check_no_verify``'s own docstring for why the other six stay
    on their own hand-rolled segmentation.
    """
    # creation.py` imports `_blanket_disarm.MARKER_BASENAME` at ITS top
    from coordinator_core.bash_guards.block_disarm_marker_sentinel_creation import (
        check as _check_disarm_marker_sentinel_creation,
        MATCHERS as _matchers_disarm_marker_sentinel_creation,
    )

    from coordinator_core.bash_guards.block_subagent_plan_body_bash_write import (
        check as _check_plan_body_bash_write,
        MATCHERS as _matchers_plan_body_bash_write,
    )
    from coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist import (
        check as _check_reviewer_bash_outside_allowlist,
        MATCHERS as _matchers_reviewer_bash_outside_allowlist,
    )
    from coordinator_core.bash_guards.block_subagent_destructive_action import (
        check as _check_subagent_destructive_action,
        MATCHERS as _matchers_subagent_destructive_action,
    )
    from coordinator_core.bash_guards.block_illegal_filename import (
        check as _check_illegal_filename,
        MATCHERS as _matchers_illegal_filename,
    )
    from coordinator_core.bash_guards.check_test_suite_invocation import (
        check as _check_test_suite_invocation,
        MATCHERS as _matchers_test_suite_invocation,
    )
    from coordinator_core.bash_guards.block_subagent_commit import (
        check as _check_subagent_commit,
        MATCHERS as _matchers_subagent_commit,
    )
    from coordinator_core.bash_guards.guard_host_subagent_bash_ban import (
        check as _check_host_subagent_bash_ban,
        MATCHERS as _matchers_host_subagent_bash_ban,
    )
    from coordinator_core.bash_guards.guard_host_subagent_bash_spawn_shapes import (
        check as _check_host_subagent_bash_spawn_shapes,
        MATCHERS as _matchers_host_subagent_bash_spawn_shapes,
    )
    from coordinator_core.bash_guards.check_raw_pid_liveness import (
        check as _check_raw_pid_liveness,
        MATCHERS as _matchers_raw_pid_liveness,
    )
    from coordinator_core.bash_guards.block_worktree_creation import (
        check as _check_worktree_creation,
        MATCHERS as _matchers_worktree_creation,
    )
    from coordinator_core.bash_guards.p4_verb_fence import (
        check as _check_p4_verb_fence,
        MATCHERS as _matchers_p4_verb_fence,
    )
    from coordinator_core.bash_guards.block_approval_sentinel_creation import (
        check as _check_approval_sentinel_creation,
        MATCHERS as _matchers_approval_sentinel_creation,
    )
    from coordinator_core.bash_guards.block_worktree_sentinel_creation import (
        check as _check_worktree_sentinel_creation,
        MATCHERS as _matchers_worktree_sentinel_creation,
    )
    from coordinator_core.bash_guards.block_fleet_delegation_creation import (
        check as _check_fleet_delegation_creation,
        MATCHERS as _matchers_fleet_delegation_creation,
    )
    # block_dev_repo_sentinel_removal.py DOES declare a module-level MATCHERS,
    # having no applicable declaration: no MATCHERS import from this module, and
    from coordinator_core.bash_guards.block_dev_repo_sentinel_removal import (
        check as _check_dev_repo_sentinel_removal,
        check_advisory as _check_dev_repo_sentinel_removal_advisory,
    )
    from coordinator_core.bash_guards.block_stash_destruction import (
        check as _check_stash_destruction,
        check_apply_advisory as _check_stash_apply_advisory,
        MATCHERS as _matchers_stash_destruction,
    )
    from coordinator_core.bash_guards.block_subagent_stash_creation import (
        check as _check_subagent_stash_creation,
        MATCHERS as _matchers_subagent_stash_creation,
    )
    from coordinator_core.bash_guards.block_subagent_grant_acquisition import (
        check as _check_subagent_grant_acquisition,
        MATCHERS as _matchers_subagent_grant_acquisition,
    )
    from coordinator_core.bash_guards.block_subagent_findings_reject import (
        check as _check_subagent_findings_reject,
        MATCHERS as _matchers_subagent_findings_reject,
    )
    from coordinator_core.bash_guards.block_subagent_guard_grant import (
        check as _check_subagent_guard_grant,
        MATCHERS as _matchers_subagent_guard_grant,
    )
    from coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal import (
        check as _check_repo_setup_claude_home_refusal,
        MATCHERS as _matchers_repo_setup_claude_home_refusal,
    )
    from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
        check as _check_doctrine_surface_bash_write,
        MATCHERS as _matchers_doctrine_surface_bash_write,
    )
    from coordinator_core.bash_guards.block_noncanonical_branch_creation import (
        check as _check_block_noncanonical_branch_creation,
        MATCHERS as _matchers_noncanonical_branch_creation,
    )
    from coordinator_core.bash_guards.bump_foreign_repo_write import (
        check_bump_foreign_repo_write as _check_bump_foreign_repo_write,
    )
    from coordinator_core.bash_guards.bump_outside_repo_write import (
        check_bump_outside_repo_write as _check_bump_outside_repo_write,
    )
    from coordinator_core.bash_guards.guard_inprocess_search import (
        check as _check_inprocess_search,
        MATCHERS as _matchers_inprocess_search,
    )
    from coordinator_core.bash_guards.guard_offer_git_c import (
        check_offer_git_c as _check_offer_git_c,
    )
    from coordinator_core.bash_guards.guard_no_optional_locks import (
        check_git_no_optional_locks as _check_git_no_optional_locks,
    )
    from coordinator_core.bash_guards.guard_reap_stale_git_lock import (
        check_reap_stale_git_lock as _check_reap_stale_git_lock,
    )
    from coordinator_core.bash_guards.guard_head_tail_rewrite import (
        check_head_tail_plumbing_rewrite as _check_head_tail_plumbing_rewrite,
    )
    from coordinator_core.bash_guards.guard_grep_via_bash import (
        check as _check_grep_via_bash,
        MATCHERS as _matchers_grep_via_bash,
    )
    from coordinator_core.bash_guards.guard_powershell_via_bash import (
        check as _check_powershell_via_bash,
        MATCHERS as _matchers_powershell_via_bash,
    )
    from coordinator_core.bash_guards.guard_multiprobe_banner import (
        check as _check_multiprobe_banner,
        MATCHERS as _matchers_multiprobe_banner,
    )
    from coordinator_core.bash_guards.guard_offer_invoke_params_stdin import (
        check_offer_invoke_params_stdin as _check_offer_invoke_params_stdin,
    )
    from coordinator_core.bash_guards.guard_plumbing_and_loops import (
        check as _check_plumbing_and_loops,
        MATCHERS as _matchers_plumbing_and_loops,
    )

    # -- see the CONFINEMENT_DENY registration and the ADVISORY_REWRITE
    # (`COORDINATOR_OVERRIDE_GIT_REVERT`) through `_override(payload=...)`,
    # override env therefore have DIFFERENT correct verdicts.
    _override_identity = _override_env_identity(payload)

    _git_revert_cache: Dict[
        Tuple[str, str, _OverrideIdentity],
        Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]],
    ] = {}

    def _git_revert_full() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        key = (cmd, session_id, _override_identity)
        if key not in _git_revert_cache:
            _git_revert_cache[key] = _dc._check_destructive_git_revert_full(cmd, session_id, hook_payload=payload)
        return _git_revert_cache[key]

    def _doctrine_surface_bash_write_entry() -> Optional[Dict[str, Any]]:
        """``guard-doctrine-surface-bash-write``'s per-call closure --
        resolves ``plugin_root`` ONCE (`resolve_plugin_root_loud`) and reuses
        it for both ``governed_surfaces`` (as before) and the deny-message
        wiki citation, rather than resolving `plugin_root` twice (which would
        double-count `resolve_plugin_root_loud`'s own LOUD/COUNTED miss
        signal). The citation resolver is passed as a THUNK
        (`resolve_wiki_citation` bound to this call's
        `plugin_root`) -- `check()` only invokes it from its own deny path
        (`_compose_deny_message`), so the regex substitution never runs for
        the overwhelming majority of calls that allow (DR-344 hot-path
        budget; see `resolve_wiki_citation`'s own
        docstring)."""
        plugin_root = resolve_plugin_root_loud(payload, session_id, cwd)
        governed_surfaces = resolve_governed_authoring_surfaces(plugin_root, session_id, cwd)
        return _check_doctrine_surface_bash_write(
            payload,
            _with_local_config_surface(governed_surfaces),
            resolve_wiki_citation=lambda citation: resolve_wiki_citation(citation, plugin_root),
        )

    guard_chain: List[GuardEntry] = [
        # `_PS_REMOVE_VERBS` table-driven PowerShell leg), and now the
        GuardEntry("no-verify", lambda: _dc.check_no_verify(cmd, session_id, resolved=resolved, hook_payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-git-orphan", lambda: _dc.check_destructive_git_orphan(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-rm", lambda: _dc.check_destructive_rm(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("stale-write", lambda: _dc.check_stale_write(cmd, session_id, cwd, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-git-clean", lambda: _dc.check_destructive_git_clean(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # CONFINEMENT_DENY slot would short-circuit `evaluate_payload_json`
        # ADVISORY_REWRITE, after every CONFINEMENT_DENY guard.
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES 2026-08-21. The
        # cover -- and both of THOSE already declare COMMAND_TOOL_NAMES, so
        # PARTIAL COVERAGE, NOT DIALECT PARITY -- scoped honestly here
        # NOT RATCHET-COVERED, and that is the governance gap worth naming:
        # module-level `MATCHERS` constant and skips `dispatch.py` via
        # `_NON_GUARD_MODULES`. This guard is registered inline here with no
        GuardEntry("destructive-git-revert", lambda: _git_revert_full()[0], True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("blanket-git-add", lambda: _dc.check_blanket_git_add(cmd, session_id, hook_payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("runaway-find", lambda: _dc.check_runaway_find(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        GuardEntry("block-worktree-creation", lambda: _check_worktree_creation(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_worktree_creation)),
        GuardEntry("p4-verb-fence", lambda: _check_p4_verb_fence(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_p4_verb_fence)),
        GuardEntry(
            "block-approval-sentinel-creation",
            lambda: _check_approval_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_approval_sentinel_creation),
        ),
        GuardEntry(
            "block-worktree-sentinel-creation",
            lambda: _check_worktree_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_worktree_sentinel_creation),
        ),
        GuardEntry(
            "block-fleet-delegation-creation",
            lambda: _check_fleet_delegation_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_fleet_delegation_creation),
        ),
        # ADVISORY_REWRITE. `check()`'s own detector only ever returned
        # VERDICT_DENY or VERDICT_ADVISORY, mutually exclusively, from the
        # command shape is orphaned by this deletion PROVIDED `check_advisory`
        # is widened to also render an advisory on a VERDICT_DENY result;
        # VERDICT_DENY, since it only matches `verdict != VERDICT_ADVISORY`.
        # bootstrap loop: `GuardBand.CONFINEMENT_DENY` is unconditionally
        # `_blanket_disarm.py`'s own "BAND-SCOPED SUPPRESSION"), so
        GuardEntry(
            "block-disarm-marker-sentinel-creation",
            lambda: _check_disarm_marker_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_disarm_marker_sentinel_creation),
        ),
        GuardEntry(
            "block-stash-destruction",
            lambda: _check_stash_destruction(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_stash_destruction),
        ),
        GuardEntry(
            "block-subagent-stash-creation",
            lambda: _check_subagent_stash_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_subagent_stash_creation),
        ),
        # `block-noncanonical-branch-creation` RETIRED from this CONFINEMENT_
        # moved to ADVISORY_REWRITE, at the tail of that band -- see its new
        # ADVISORY_REWRITE registration below.)
        GuardEntry(
            "block-reviewer-bash-outside-allowlist",
            lambda: _check_reviewer_bash_outside_allowlist(payload, policy_path=policy_file),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_reviewer_bash_outside_allowlist),
        ),
        GuardEntry("block-subagent-destructive-action", lambda: _check_subagent_destructive_action(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_destructive_action)),
        GuardEntry("block-subagent-commit", lambda: _check_subagent_commit(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_commit)),
        # rewrite short-circuit. MATCHERS pinned to ("Bash",) ONLY -- see
        # widen this one onto COMMAND_TOOL_NAMES alongside its siblings.
        GuardEntry(
            "guard-host-subagent-bash-ban",
            lambda: _check_host_subagent_bash_ban(
                payload,
                resolve_wiki_citation=lambda citation: resolve_wiki_citation(
                    citation, resolve_plugin_root_loud(payload, session_id, cwd)
                ),
            ),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_host_subagent_bash_ban),
        ),
        # C7.md). Same cohort/CONFINEMENT_DENY posture as the sibling
        # that module's own docstring "BOTH DIALECTS" section.
        # COLLISION 1 (staff-eng review, findings 1/7): this guard's own
        # `check()` DECLINES (returns None) on a command whose primary
        # shape is GREP_VIA_BASH -- `inprocess-search` (registered below,
        # ADVISORY_REWRITE) already answers that family in-process at zero
        # spawn cost, so a CONFINEMENT_DENY entry here would deny toward an
        # dependency -- see that module's own "DECLINE PREDICATE" section.
        # COLLISION 2: this guard SHADOWS `plumbing-and-loops`'s advisory
        # (registered far below, PLATFORM_CONDITIONED_DENY) for the opt-in
        # subagent cohort on HEAD_TAIL_PLUMBING/FOR_LOOP/WHILE_READ_LOOP --
        # own "SECOND COLLISION" section for the full reasoning.
        GuardEntry(
            "guard-host-subagent-bash-spawn-shapes",
            lambda: _check_host_subagent_bash_spawn_shapes(
                payload,
                resolve_wiki_citation=lambda citation: resolve_wiki_citation(
                    citation, resolve_plugin_root_loud(payload, session_id, cwd)
                ),
            ),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_host_subagent_bash_spawn_shapes),
        ),
        # -- see its own new ADVISORY_REWRITE registration below.)
        GuardEntry("check-test-suite-invocation", lambda: _check_test_suite_invocation(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_test_suite_invocation)),
        # entry in this CONFINEMENT_DENY run.
        GuardEntry("block-subagent-grant-acquisition", lambda: _check_subagent_grant_acquisition(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_grant_acquisition)),
        # sibling of block-subagent-grant-acquisition: EM-only surface of the
        # M3, retire-review-integrator: reviewer-applies-own-findings ledger.
        GuardEntry("block-subagent-findings-reject", lambda: _check_subagent_findings_reject(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_findings_reject)),
        # CONFINEMENT_DENY run.
        GuardEntry("block-subagent-guard-grant", lambda: _check_subagent_guard_grant(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_guard_grant)),
        # CONFINEMENT_DENY entries is a convenience, not a behaviour
        # this CONFINEMENT_DENY run.
        GuardEntry("guard-repo-setup-claude-home-refusal", lambda: _check_repo_setup_claude_home_refusal(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_repo_setup_claude_home_refusal)),
        # in-plan (CONFINEMENT_DENY / NOT_COST_ARGUED, per DR-277): a
        # among the CONFINEMENT_DENY entries is a convenience, not a
        # every entry in this CONFINEMENT_DENY run. Closure factored out to
        GuardEntry(
            "guard-doctrine-surface-bash-write",
            _doctrine_surface_bash_write_entry,
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_doctrine_surface_bash_write),
        ),
        # CONFINEMENT_DENY hard-deny guard, and ahead of `offer-git-c`'s
        # shadow a hard deny: it is the first non-CONFINEMENT_DENY entry in
        GuardEntry("destructive-git-revert-advisory", lambda: _git_revert_full()[1], False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # removal` above (same CONFINEMENT_DENY shadowing hazard
        # EVERY CONFINEMENT_DENY hard-deny guard, and ahead of
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4,
        # tuple (not a `MATCHERS` import) stays for the reason given in the
        GuardEntry("block-dev-repo-sentinel-removal-advisory", lambda: _check_dev_repo_sentinel_removal_advisory(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # `stash-apply-verification-advisory` -- the ADVISORY_REWRITE sibling
        # of the two CONFINEMENT_DENY stash guards above, and deliberately
        # OVERLAPPING files match, and says nothing about content unique to
        # ADVISORY per DR-277's default, argued rather than assumed: the
        # harm is a wrong CONCLUSION, not lost work (the entry survives), and
        GuardEntry("stash-apply-verification-advisory", lambda: _check_stash_apply_advisory(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_stash_destruction)),
        # `GuardBand.ADVISORY_REWRITE` band, and only the first non-None
        # ("WRITE-SINK CLASSIFICATION") deliberately excludes git
        # TAIL of the ADVISORY_REWRITE band, after both) -- a repeated or
        # main-loop EM. Unlike the CONFINEMENT_DENY guards above, these two
        # stay ADVISORY_REWRITE (see each one's own attribute-explanation
        # repo) -- the Bash-surface CROSS-REPO write-confinement speed bump.
        # THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY (see that plan's
        #   fail_closed=False -- the OPPOSITE of every neighbouring
        #     CONFINEMENT_DENY entry above (all `True`): this guard's whole
        #   band=GuardBand.ADVISORY_REWRITE, NOT CONFINEMENT_DENY -- the
        #     every band EXCEPT CONFINEMENT_DENY, so registering a
        #     DELIBERATELY passable bump in the one band that switch cannot
        #   advisory_value=AdvisoryValue.NOT_COST_ARGUED -- this guard's
        #     Windows spawn cost, so it is neither WINDOWS_COST_ONLY nor
        #     HOST_INDEPENDENT; left at the UNCLASSIFIED default would fail
        # git ...`/plain-bash-write-sink target is always resolved RELATIVE
        GuardEntry(
            name="bump-foreign-repo-write",
            fn=lambda: _check_bump_foreign_repo_write(cmd, session_id, cwd, payload),
            fail_closed=False,
            band=GuardBand.ADVISORY_REWRITE,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            # Widened from ("Bash",) to COMMAND_TOOL_NAMES by
            matchers=COMMAND_TOOL_NAMES,
        ),
        # repo) -- the Bash-surface OUTSIDE-repo write-confinement speed
        # under a DIFFERENT git root -- see that guard's own module
        # SECURITY BOUNDARY -- every attribute below mirrors C4's own
        #   band=GuardBand.ADVISORY_REWRITE, NOT CONFINEMENT_DENY -- the
        #     CONFINEMENT_DENY; registering a deliberately passable bump
        #   advisory_value=AdvisoryValue.NOT_COST_ARGUED -- this guard's
        #     WINDOWS_COST_ONLY nor HOST_INDEPENDENT; left at the
        #     UNCLASSIFIED default would fail `dispatch.py`'s own
        # sink target is always resolved RELATIVE to wherever the command
        GuardEntry(
            name="bump-outside-repo-write",
            fn=lambda: _check_bump_outside_repo_write(cmd, session_id, cwd, payload),
            fail_closed=False,
            band=GuardBand.ADVISORY_REWRITE,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            # Widened from ("Bash",) to COMMAND_TOOL_NAMES by
            # `Dialect.POWERSHELL` gate that could never fire while this
            matchers=COMMAND_TOOL_NAMES,
        ),
        # declares no MATCHERS and carries no tool_name gate of its own.
        GuardEntry("offer-git-c", lambda: _check_offer_git_c(cmd, session_id, cwd), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        # pre-op check that reaps an ORPHANED `.git/index.lock` ahead of a raw
        # INVARIANT: a side-effect-only guard (always returns None) must be
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, docs/plans/
        GuardEntry("reap-stale-git-lock", lambda: _check_reap_stale_git_lock(cmd, cwd, session_id), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, same plan/Bucket
        GuardEntry("git-no-optional-locks", lambda: _check_git_no_optional_locks(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, same plan/Bucket
        GuardEntry("validate-commit", lambda: _dc.check_validate_commit(cmd, session_id, cwd, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("inprocess-search", lambda: _check_inprocess_search(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_inprocess_search)),
        GuardEntry("block-illegal-filename", lambda: _check_illegal_filename(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_illegal_filename)),
        # 2026-07-27, DoE C14/RAW-PID-LIVENESS-GUARD). Not identity-gated
        GuardEntry("find-exec-rewrite", lambda: _dc.check_find_exec_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.WINDOWS_COST_ONLY, matchers=("Bash",)),
        GuardEntry("grep-via-bash-rewrite", lambda: _dc.check_grep_via_bash_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        GuardEntry("sed-range-read-advise", lambda: _dc.check_sed_range_read_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        GuardEntry("cat-heredoc-write-advise", lambda: _dc.check_cat_heredoc_write_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        GuardEntry("heredoc-repo-write-advise", lambda: _dc.check_heredoc_repo_write_advise(cmd, session_id, payload, cwd), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        GuardEntry("git-commit-safe-commit-advise", lambda: _dc.check_git_commit_safe_commit_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash", "PowerShell")),
        # viability-stop-the-spawn-storms.md, row BX-16): MULTI_PROBE_BANNER
        # (40.1% of forks) and HEAD_TAIL_PLUMBING (25%) had no rewrite target
        GuardEntry("multiprobe-banner-rewrite", lambda: _dc.check_multiprobe_banner_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        # guard_head_tail_rewrite.py declares no module MATCHERS but reads
        # (AC16 CALLEE-GRAPH AUDIT, C6 pln-the-shape-classifier-reaches-
        # internally; its `dialect is Dialect.POWERSHELL` gate only fires
        GuardEntry("head-tail-plumbing-rewrite", lambda: _check_head_tail_plumbing_rewrite(cmd, session_id, dialect=_dialect_from_tool_name(payload.get("tool_name") if isinstance(payload, dict) else None), payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.WINDOWS_COST_ONLY, matchers=COMMAND_TOOL_NAMES),
        # test_hard_denies_precede_rewrites.py's CONFINEMENT_HARD_DENIES --
        # the PLATFORM_CONDITIONED_DENY band below (`grep-via-bash-guard`,
        # stdin.py declares no MATCHERS and carries no tool_name gate of its
        GuardEntry("offer-invoke-params-stdin", lambda: _check_offer_invoke_params_stdin(cmd, session_id), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        # `grep-via-bash-guard` moved from PLATFORM_CONDITIONED_DENY to
        # ADVISORY_REWRITE (H11(a), 2026-07-30, docs/plans/2026-07-30-os-
        # changed explicitly in this one edit: band to ADVISORY_REWRITE, and
        # PHYSICALLY RELOCATED here (was at the tail, alongside
        # sequence`) requires every ADVISORY_REWRITE entry to sit together,
        # ahead of the PLATFORM_CONDITIONED_DENY band; a band-label change
        # still-PLATFORM_CONDITIONED_DENY guards below, breaking that
        # `advisory_value` ALSO reclassified, WINDOWS_COST_ONLY ->
        # HOST_INDEPENDENT (H11 dispatch, 2026-07-30 -- not itself named by
        # WINDOWS_COST_ONLY, H4's own suppression default would SILENCE
        # empirically: at WINDOWS_COST_ONLY the guard fired on Windows and
        # HOST_INDEPENDENT (line above, in this same band) -- matching that
        GuardEntry("grep-via-bash-guard", lambda: _check_grep_via_bash(payload, host_is_windows=host_is_windows), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_grep_via_bash)),
        # same band-contiguity requirement (ADVISORY_REWRITE, ahead of the
        # two PLATFORM_CONDITIONED_DENY guards below). Never denies (module
        GuardEntry("powershell-via-bash-guard", lambda: _check_powershell_via_bash(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_powershell_via_bash)),
        # guard-class-census band flips, moved from CONFINEMENT_DENY to
        # ADVISORY_REWRITE (`fail_closed=True` -> `False`, `band=
        # CONFINEMENT_DENY` -> `ADVISORY_REWRITE`). Appended here, at the
        # tail of the ADVISORY_REWRITE band (lowest precedence in this band,
        # first non-`None` still wins) ahead of the two PLATFORM_CONDITIONED_
        # ADVISORY_REWRITE/rewrite entry these four could now shadow FROM
        # safe-commit-advise` are earlier ADVISORY_REWRITE entries that
        # `fail_closed` is CRASH-PATH routing policy (module docstring F1),
        # census row) is DELIBERATELY NOT included in this move -- it is
        # coupled to `block-worktree-sentinel-creation`, a KEEP-HARD guard
        # CONFINEMENT_DENY.
        GuardEntry(
            "block-noncanonical-branch-creation",
            lambda: _check_block_noncanonical_branch_creation(payload),
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_noncanonical_branch_creation),
        ),
        GuardEntry(
            "block-subagent-plan-body-bash-write",
            lambda: _check_plan_body_bash_write(payload),
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_plan_body_bash_write),
        ),
        GuardEntry(
            "check-raw-pid-liveness",
            lambda: _check_raw_pid_liveness(payload),
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_raw_pid_liveness),
        ),
        # `grep-via-bash-guard` right above this comment -- ADVISORY_REWRITE
        # earlier-registered `ADVISORY_REWRITE` chain entry (e.g.
        # docstring). `fail_closed=True` and `PLATFORM_CONDITIONED_DENY`
        # Tail placement itself is UNCHANGED and still load-bearing: placed
        # `COORDINATOR_ALLOW_*` override has disabled it (or the seam
        # REVERTED for the reason just given.
        # Deliberately NOT added to `CONFINEMENT_HARD_DENIES` in
        GuardEntry("multiprobe-banner", lambda: _check_multiprobe_banner(payload, host_is_windows=host_is_windows), True, GuardBand.PLATFORM_CONDITIONED_DENY, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_multiprobe_banner)),
        GuardEntry("plumbing-and-loops", lambda: _check_plumbing_and_loops(payload, host_is_windows=host_is_windows), True, GuardBand.PLATFORM_CONDITIONED_DENY, AdvisoryValue.WINDOWS_COST_ONLY, matchers=tuple(_matchers_plumbing_and_loops)),
    ]
    return guard_chain


def main() -> int:
    """Standalone entry point (mirrors the bash dispatcher direct-invocation
    path): read stdin ONCE, evaluate, print the envelope JSON on non-None,
    always exit 0 (ALLOW/DENY is conveyed via stdout, never exit code).

    Wraps the evaluate call in `cli_entry.recording_declared_writes` (D4,
    docs/plans/2026-09-11-state-writers-claim-through-one-seam.md § C6) so a
    to-fix guard's seam-routed write (e.g. `block_subagent_destructive_
    action._log_fail_open`) lands a claim instead of running with no
    collection open -- wrapped ONCE at this entry, never per guard site.
    The recorder import is deferred inside this function so a Bash tool
    call that hits zero to-fix guards still pays no import cost beyond
    this module's own; `cli_entry`'s own imports were confirmed at
    authoring time to add no measurable `-X importtime` delta over this
    dispatcher's existing baseline."""
    from coordinator_core.cli_entry import recording_declared_writes  # noqa: PLC0415 -- deferred, see docstring

    raw = sys.stdin.read()
    with recording_declared_writes():
        out = evaluate_payload_json(raw)
    if out is not None:
        sys.stdout.write(json.dumps(out))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
