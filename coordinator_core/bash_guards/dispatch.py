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
       j. check_probe_spray                (advisory, fail-open)
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
    dialect_from_tool_name as _dialect_from_tool_name,
)
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
from coordinator_core.warm.caller_context import (
    resolve_caller_context as _resolve_caller_context,
)
from coordinator_core.bash_guards._advisory_dedupe import (
    advisory_dedupe_key as _advisory_dedupe_key,
    already_advised as _already_advised,
    degrade_advisory_envelope as _degrade_advisory_envelope,
    mark_advised as _mark_advised,
)
from coordinator_core.bash_guards._write_bump_marker import (
    resolve_gitdir as _resolve_gitdir_for_dedupe,
)
from coordinator_core.bash_guards._command_tokenizer import (
    ResolvedCommand as _ResolvedCommand,
    resolve_command_positions as _resolve_command_positions,
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
from coordinator_core.bash_guards.check_raw_pid_liveness import (
    check as _check_raw_pid_liveness,
    MATCHERS as _matchers_raw_pid_liveness,
)
from coordinator_core.bash_guards.block_worktree_creation import (
    check as _check_worktree_creation,
    MATCHERS as _matchers_worktree_creation,
)
from coordinator_core.bash_guards.block_approval_sentinel_creation import (
    check as _check_approval_sentinel_creation,
    MATCHERS as _matchers_approval_sentinel_creation,
)
from coordinator_core.bash_guards.block_worktree_sentinel_creation import (
    check as _check_worktree_sentinel_creation,
    MATCHERS as _matchers_worktree_sentinel_creation,
)
# block_dev_repo_sentinel_removal.py DOES declare a module-level MATCHERS,
# but on the module whose registered leg here is `check_advisory` -- the
# `check()` leg that pairs with the declaration was RETIRED from guard_chain
# (see that registration's own comment below). Treat this registration as
# having no applicable declaration: no MATCHERS import from this module, and
# its GuardEntry below passes the ("Bash",) default literally, same as any
# other registration whose backing module declares nothing.
from coordinator_core.bash_guards.block_dev_repo_sentinel_removal import (
    check as _check_dev_repo_sentinel_removal,
    check_advisory as _check_dev_repo_sentinel_removal_advisory,
)
from coordinator_core.bash_guards.block_stash_destruction import (
    check as _check_stash_destruction,
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
from coordinator_core.bash_guards.block_subagent_guard_grant import (
    check as _check_subagent_guard_grant,
    MATCHERS as _matchers_subagent_guard_grant,
)
from coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal import (
    check as _check_repo_setup_claude_home_refusal,
    MATCHERS as _matchers_repo_setup_claude_home_refusal,
)
from coordinator_core.bash_guards.block_noncanonical_branch_creation import (
    check as _check_block_noncanonical_branch_creation,
    MATCHERS as _matchers_noncanonical_branch_creation,
)
from coordinator_core.bash_guards.guard_branch_set_precedence import (
    check as _check_branch_set_precedence,
    MATCHERS as _matchers_branch_set_precedence,
)
from coordinator_core.bash_guards.guard_longlived_branch_naming import (
    check as _check_longlived_branch_naming,
    MATCHERS as _matchers_longlived_branch_naming,
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

The membership test runs against the command AFTER ``_crlf_strip`` and
``_join_backslash_newlines``, because those are the only normalizations in
this package that DELETE characters from the middle of the text and can
therefore *manufacture* a keyword the raw string does not contain
(``gi\\<newline>t`` -> ``git``). Every other transform on the guards' own
paths only removes spans wholesale, so it cannot introduce a keyword that was
not already present.
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

    Fails toward denial on every uncertain path: an unmapped guard, an empty
    command, or any exception raised while computing the answer all return
    ``False``, restoring the chain-wide deny.
    """
    triggers = _CRASH_TRIGGER_SUBSTRINGS.get(guard_name)
    if not triggers:
        return False
    try:
        probe = _dc._crlf_strip(cmd or "")
        probe = _dc._join_backslash_newlines(probe)
    except Exception:  # noqa: BLE001 -- normalization must never widen the deny path
        return False
    return not any(token in probe for token in triggers)


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

    Returns ``None`` on a miss (never raises) -- the caller (a future
    consuming guard) is responsible for its OWN fail-open behavior on that
    ``None``, same as it always would be for `resolve_plugin_root()`'s
    existing ``Optional[str]`` contract; this function only makes the miss
    LOUD, it does not change what happens next.

    No consumer yet (2026-08-28): nothing in `guard_chain` calls this today
    -- C3/C4/C5 are this function's first consumers, landing in later chunks
    of the same plan. Defining it here, ungated, mirrors C0's own "no caller
    wiring yet" shape for the identical reason: the miss-handling contract
    is worth pinning and testing on its own, independent of any one
    consumer's timing.
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

    Returns ``None`` on ANY miss -- ``plugin_root`` itself unresolved (see
    ``resolve_plugin_root_loud``, already LOUD about that miss on its own;
    this function does not duplicate that stderr/counter emission), the
    manifest file absent, unreadable, not valid JSON, or valid JSON that is
    not a flat list of strings. Every one of those is fail-open by design:
    this function has no consumer yet (this chunk lands the reader only; a
    future consuming guard -- C4 -- owns deciding what "no manifest" means
    for ITS OWN detection, same no-caller-wiring-yet shape as
    ``resolve_plugin_root_loud``'s own "No consumer yet" note above). Never
    raises.
    """
    if not plugin_root:
        return None
    import os

    manifest_path = os.path.join(plugin_root, _GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 -- any read/parse failure is a fail-open miss, never a crash
        return None
    if not isinstance(data, list):
        return None
    if not all(isinstance(entry, str) for entry in data):
        return None
    return data


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
            _matchers_branch_set_precedence,
            _matchers_longlived_branch_naming,
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
        return _evaluate_payload_json_budgeted(
            raw,
            policy_file,
            host_is_windows,
            resolved,
            resolution_class,
            collect_advisories=collect_advisories,
        )
    finally:
        _dc._disarm_git_probe_deadline()


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
        # Malformed stdin from the harness is unexpected on every real
        # invocation (mirrors bash `[ -z "$INPUT" ]` early-exit, extended to
        # cover unparseable-but-non-empty input) -- surfaced since a silent
        # allow here means the WHOLE guard chain below never ran.
        print("bash_guards.dispatch: unparseable PreToolUse payload: %s" % exc, file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None

    # C1 (docs/plans/2026-08-26-the-http-leg-normalizes-the-tool-name-it-was-
    # handed.md): a LOCAL, gating-only normalized tool-name value, derived
    # once here from the single parse above. `payload["tool_name"]` itself is
    # NEVER written -- every `check_*` below still receives byte-identical
    # what the caller sent. `check_destructive_git_revert` and
    # `check_no_verify` gate on `dialect_from_tool_name(payload["tool_name"])
    # is Dialect.POWERSHELL` to decide whether to run
    # `expand_start_process_invocations` before their regex pipeline;
    # mutating the payload would silently disable that path for every
    # PowerShell-labelled call -- measured live regression, `Start-Process git
    # -ArgumentList 'stash','drop'` under `tool_name="PowerShell"` denies from
    # `check_destructive_git_revert` today and goes SILENT under a rewritten
    # payload (see the plan's "one parse, two values" table). So: one parse,
    # two values -- this LOCAL one for the two gating reads below, the
    # untouched `payload` for everything else.
    #
    # Every value in `_tool_names.COMMAND_TOOL_NAMES` (today: "Bash" and
    # "PowerShell") normalizes to `"Bash"` for gating purposes ONLY -- a
    # guard registered at the `("Bash",)` default now sees a command-shaped
    # call regardless of which shell relayed it, while a guard already
    # widened to `COMMAND_TOOL_NAMES` is unaffected (`"Bash"` is a member of
    # its own declared set either way). A `tool_name` outside that universe
    # (a non-command tool, or `None`) passes through unchanged, so the
    # existing "not a command tool" rejection below is untouched.
    #
    # NEGATIVE SPEC -- no opt-out. This normalization is unconditional: no
    # env var, no settings key, no sentinel file, no payload field disables
    # it. `block_disarm_marker_sentinel_creation` already treats an agent-
    # writable chain-wide self-disarm sentinel as hostile, and DoE's own
    # `_rearm_command_tool_name` docstring records a session-keyed opt-out
    # sentinel that was written and removed again before landing -- harmless
    # under an opt-in default, but a one-line chain-wide self-disarm the
    # moment it inverts to opt-out. Do not add a conditional here to
    # "simplify" this back into that shape; a future guard's own disarm logic
    # belongs in that guard, never in this domain derivation.
    _raw_tool_name = payload.get("tool_name")
    _gating_tool_name = "Bash" if _raw_tool_name in COMMAND_TOOL_NAMES else _raw_tool_name

    # C1 master gate (docs/plans/2026-08-07-command-guards-fire-under-both-
    # tool-names.md): union check against the DECLARED-matchers set, NOT
    # against `_tool_names.COMMAND_TOOL_NAMES` (the observable universe) --
    # gating on the universe would convert this early return from a
    # zero-cost skip into a full chain traverse (the tokenizer walk, chain
    # construction, disarm/host resolution, and the guard loop below) for
    # every payload whose `tool_name` is IN the universe but matched by NO
    # registered guard, on every command issued through this operator's
    # PRIMARY shell, for zero coverage gain until a guard actually widens.
    # `_any_declared_matchers()` is a cached union of exactly the `matchers`
    # each `GuardEntry` below declares -- while every entry sits at its
    # `("Bash",)` default, this restores today's cheap early exit exactly
    # (a `PowerShell` payload is rejected HERE), and it widens automatically
    # the moment any guard's own `MATCHERS` does, with no edit required at
    # this call site. Reads the LOCAL normalized value (C1 above), not
    # `payload["tool_name"]` directly.
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

    # Dispatcher-level CRLF normalize-once (defense-in-depth; each check
    # ALSO strips its own CR at entry -- see module docstring "Parse-once
    # contract").
    cmd = cmd.replace("\r", "")

    # M5 resolve-once (AC-8): computed AT MOST once per dispatch, gated on a
    # cheap substring pre-filter so the walk is skipped entirely for the
    # majority of calls no cohort-A guard would have inspected anyway (see
    # this function's own ``resolved`` docstring above). Wrapped defensively
    # -- a crash HERE must not take down every guard below it, only cost the
    # walk-reduction for this one call; `check_no_verify` falls back to its
    # own pre-existing, self-contained tokenize path whenever `resolved` is
    # `None`, so degrading to that on any exception is always safe.
    if resolved is None and "git" in cmd:
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

    # ------------------------------------------------------------------
    # Combined legacy cross-process order (module docstring 1a..1k, 2..6).
    # Each entry is a `GuardEntry(name, fn, fail_closed, band, advisory_value,
    # matchers)`.
    #   fail_closed=True  -> hard-deny class: an exception is routed through
    #                        _crash_deny (F1 -- fails CLOSED per-guard).
    #   fail_closed=False -> soft/content/advisory class: an exception is
    #                        swallowed and treated as allow/no-context (a
    #                        crash in a convenience/advisory guard must never
    #                        block the user intended command).
    # ------------------------------------------------------------------

    guard_chain = _build_guard_chain(cmd, session_id, cwd, payload, policy_file, host_is_windows, resolved)

    # Blanket-disarm marker (M-disarm-wiring, 2026-07-30): evaluated ONCE per
    # dispatch, never per guard -- `disarm_status` is itself per-process
    # cached by `(session_id, is_em)` (see `_blanket_disarm.py`'s own "HOT-
    # PATH CHEAP" note), so a second call here would cost nothing extra, but
    # computing the verdict once and reusing it for every guard below keeps
    # the loop's own reasoning in one place. `git_root` is deliberately NOT
    # resolved here and threaded in -- `_blanket_disarm._is_em_caller` only
    # needs the payload's top-level `agent_id`/`agent_type` legs to tell an
    # EM apart from a dispatched subagent (see that module's own
    # `blanket_disarm_active` docstring: omitting `git_root` "still resolves
    # the EM/subagent distinction correctly for the two top-level legs...
    # which is what `_is_em_caller` actually depends on"). Adding a
    # dispatcher-level `resolve_git_root()` call here would reintroduce
    # exactly the F0 hazard this module's own docstring warns against
    # ("a single resolve_git_root() call accidentally reused as a shared,
    # dispatcher-level git root passed into EVERY check function").
    #
    # Import deliberately LOCAL, not module-level: `_blanket_disarm` itself
    # imports `GuardBand` FROM this module (it reuses this enum verbatim
    # rather than inventing a second spelling of the three band names -- see
    # that module's own "BAND-SCOPED SUPPRESSION" docstring), so a
    # module-level import here would be a circular import the moment either
    # module is imported first. Deferring to call time is safe and cheap:
    # by the time `evaluate_payload_json` runs, this module has already
    # finished executing its own top level (including the `GuardBand` class
    # statement above), so `_blanket_disarm`'s own module-level `from
    # coordinator_core.bash_guards.dispatch import GuardBand` resolves
    # cleanly against the now-complete module in `sys.modules`.
    from coordinator_core.bash_guards._blanket_disarm import disarm_status as _disarm_status

    _disarm = _disarm_status(payload)

    # H4 (docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md):
    # resolve the EFFECTIVE host ONCE per dispatch, exactly like `_disarm`
    # above, and reuse it for every guard's suppression check below.
    # `host_is_windows=None` is the PRODUCTION shape (every real harness
    # call passes nothing) and means "read the real host", never "not
    # Windows" -- `_resolve_host_is_windows_public` is the one seam that
    # answers that question; nothing here re-reads `os.name` directly.
    _effective_host_is_windows = _resolve_host_is_windows_public(host_is_windows)

    # C10 aggregate path (see ``collect_advisories`` docstring above): unused,
    # and never appended to, when ``collect_advisories`` is False -- the
    # default path below still returns directly on the first non-None
    # envelope exactly as it always has, byte-identical to pre-C10 code.
    _collected: List[Dict[str, Any]] = []

    for entry in guard_chain:
        name, fn, fail_closed, _band = entry.name, entry.fn, entry.fail_closed, entry.band
        if _gating_tool_name not in entry.matchers:
            # C1 edit 4: this entry's own declared `matchers` excludes the
            # observed (LOCAL-normalized, per the C1 derivation above)
            # `tool_name` -- skip WITHOUT calling `fn()` at all,
            # same skip-without-invoking shape as the `_disarm` band-
            # suppression skip immediately below (the in-file precedent this
            # edit follows). Deliberately silent (no stderr line, unlike the
            # disarm skip below): once any guard widens (C3), a single
            # non-matching payload can skip a large fraction of the chain
            # this way, and a print per skipped entry would turn the common
            # case into per-call log noise; the disarm skip stays print-
            # loud because it is the rare, notable case.
            continue
        if (
            _disarm.active
            and _disarm.bands
            and _band is not GuardBand.CONFINEMENT_DENY
            and _band.value in _disarm.bands
        ):
            # Marker names this guard's own band as suppressed -- skip
            # WITHOUT calling `fn()` at all. `_disarm.bands` can never
            # contain `GuardBand.CONFINEMENT_DENY.value` (a marker naming it
            # is rejected in its entirety by `_parse_suppressed_bands` --
            # see that module's own "BAND-SCOPED SUPPRESSION" doctring), but
            # the explicit `_band is not GuardBand.CONFINEMENT_DENY` guard
            # here is deliberate belt-and-suspenders: this loop must never
            # depend SOLELY on the marker-parsing module getting that right,
            # since a suppressed confinement guard would be exactly the
            # bypass this whole package exists to prevent.
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
            # Soft/advisory guard crashed -- per this loop's fail_closed=False
            # contract it must degrade to allow/no-context, never block, but
            # a silently-crashing advisory guard is otherwise undiscoverable.
            print(
                "bash_guards.dispatch: %s guard crashed (%s: %s); "
                "treating as no-context (advisory, fail-open)."
                % (name, type(exc).__name__, exc),
                file=sys.stderr,
            )
            out = None
        if out is not None:
            # In-session operator unlock (docs/plans/2026-08-03-in-session-
            # operator-unlock-for-the-hard-.md § C3): mirrors write_guards/
            # engine.py's own intercept (C2) at the single seam every
            # hard-deny firing passes through here, regardless of which of
            # the 23 CONFINEMENT_DENY/PLATFORM_CONDITIONED_DENY guards fired
            # -- this loop is one flat sequential chain, so this is the ONLY
            # place a hard-deny envelope is ever returned. `fail_closed` is
            # this entry's own hard-deny classification (module docstring
            # F1); it is not sufficient alone -- `block-dev-repo-sentinel-
            # removal` is `fail_closed=True` (CONFINEMENT_DENY) yet can also
            # return an ALLOW+additionalContext advisory envelope for
            # genuinely unexaminable indirection (see its own module
            # docstring "POSTURE"), which must never be treated as a deny to
            # unlock. Gating on the envelope's own `permissionDecision ==
            # "deny"` is what distinguishes an actual hard-deny firing from
            # that allow leg. AC4 is satisfied by construction: this seam is
            # per-guard AT FIRING TIME, not per-guard-name, so the removal-
            # leg guards (block-dev-repo-sentinel-removal, block-worktree-
            # sentinel-creation, block-stash-destruction, and every sibling
            # in the CONFINEMENT_DENY band) route through the identical
            # check as every other hard-deny -- no exemption list, no
            # guard-name carve-out.
            #
            # `session_id` is the SAME variable this function already
            # extracted from the payload above (`payload.get("session_id")
            # or ""`) -- the one existing convention on this seam, not a
            # second one. An unresolvable (empty) session id short-circuits
            # to `False` on the `session_id and ...` guard below without
            # even calling `_consume_unlock`, so a missing session id can
            # never grant: fail closed (AC2/AC3's per-session scoping
            # extends naturally, since a peer session's sentinel is keyed to
            # a DIFFERENT session id and `_consume_unlock` looks up this
            # exact pair).
            #
            # `True` -> skip THIS guard's deny and CONTINUE the loop (a
            # later hard-deny guard, or an advisory further down the chain,
            # must still get its own chance to fire) -- never an early-
            # return ALLOW, which would skip every guard still to come.
            # `False` -> fall through unchanged to the deny return below.
            # Review: coordinator:code-reviewer Finding 1 (P1) -- mirror
            # `_advisory_value.suppress_advisory`'s own isinstance discipline
            # one line above in this exact loop, since this computation sits
            # OUTSIDE the per-guard try/except and must therefore be TOTAL BY
            # CONSTRUCTION: `out.get("hookSpecificOutput", {})` would raise
            # AttributeError (uncaught, killing evaluate_payload_json before
            # any deny envelope reaches stdout) if a guard ever returned
            # `{"hookSpecificOutput": None}` or another non-dict value there.
            _hso = out.get("hookSpecificOutput")
            # Gated on the ENVELOPE ALONE (permissionDecision == "deny"),
            # NOT on `fail_closed` -- `fail_closed` is this entry's own
            # crash-routing policy (module docstring F1), orthogonal to
            # whether the envelope its `fn()` actually returned is a real
            # deny. Before this fix the `fail_closed and` conjunct silently
            # excluded every `fail_closed=False` guard that nonetheless
            # emits a genuine deny verdict on its NORMAL (non-crash) path --
            # `bump-foreign-repo-write`/`bump-outside-repo-write` are
            # deliberately `fail_closed=False` (a crash must swallow to
            # allow, per each one's own registration comment) while still
            # returning `permissionDecision: "deny"` as their real, intended
            # verdict, so their deny was neither `guard_unlock_sentinel`-
            # clearable nor advertised one. CORRECTION (Review:
            # coordinator:code-reviewer P1, re-derived independently by
            # review-integrator): the claim below this comment originally
            # made -- that only these two bump guards are affected -- was
            # incomplete. Other `fail_closed=False` entries also compose a
            # genuine `"deny"` on their own normal path (`validate-commit`,
            # `git-commit-safe-commit-advise`, and more found on later
            # audits); see `_SENTINEL_ELIGIBLE_ADVISORY_GUARDS`'s own
            # docstring immediately above this function for the membership
            # rule (not a fixed count -- repeated independent audits have
            # each found a case the last one missed) and why sentinel
            # eligibility for `fail_closed=False` entries is now an
            # explicit per-guard allowlist rather than following from
            # `_is_hard_deny_envelope` alone.
            _is_hard_deny_envelope = (
                isinstance(out, dict)
                and isinstance(_hso, dict)
                and _hso.get("permissionDecision") == "deny"
            )
            # Review: coordinator:code-reviewer (P1, re-derived independently
            # by review-integrator) -- envelope-only (`_is_hard_deny_envelope`
            # alone) is TOO WIDE for sentinel eligibility: it makes every
            # `fail_closed=False` guard that composes a genuine deny on its
            # normal path clearable/advertisable, and this chain has more
            # than the two (`bump-foreign-repo-write`/`bump-
            # outside-repo-write`) the fix that dropped the old `fail_closed
            # and` conjunct was scoped to -- see
            # `_SENTINEL_ELIGIBLE_ADVISORY_GUARDS`'s own docstring for the
            # membership rule; do not trust a fixed count here, repeated
            # independent audits have each turned up a case the last one
            # missed. Guards like `validate-commit`'s strict-mode scope deny
            # and `git-commit-safe-commit-advise`'s foreign-staged-index deny
            # are commit-safety denies on a worktree nine-plus sessions
            # share; a sentinel drop is not an appropriate remedy for either
            # (neither guard's own registration comment argues it should be
            # suppressible this way), so clearability stays an explicit
            # per-guard opt-in rather than a side effect of composing a deny
            # envelope. Every `fail_closed=True` (CONFINEMENT_DENY /
            # PLATFORM_CONDITIONED_DENY) guard remains unconditionally
            # eligible, unchanged from before this fix -- only the
            # `fail_closed=False` population is now gated by this explicit
            # allowlist.
            _sentinel_eligible = fail_closed or name in _SENTINEL_ELIGIBLE_ADVISORY_GUARDS
            # Review: coordinator:code-reviewer Finding 3 (P2) -- compute
            # host-default suppression BEFORE consuming any unlock grant
            # (within-iteration reorder only; the guard-chain CALL order
            # above is untouched). `_is_hard_deny_envelope` already requires
            # `permissionDecision == "deny"`, and `suppress_advisory`'s own
            # negative-spec (AC-5) guarantees it returns `False` for any
            # envelope that is not a positively-recognised WINDOWS_COST_ONLY
            # allow -- so a genuine deny is never suppressed today, and this
            # reorder is a no-op against the current guard population. It is
            # still made explicit here (rather than relying on that
            # guarantee holding forever) so a one-shot sentinel is only ever
            # spent on the envelope actually about to be returned, per H4's
            # RUN-THEN-DROP discipline: the guard has already run and
            # produced `out`; the host default is consulted here, AFTER
            # `fn()`, and OUTSIDE the try/except above, so a bug in the
            # predicate itself is never mistaken for a crashing guard and
            # routed through `_crash_deny`.
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
                # C4 (docs/plans/2026-08-03-in-session-operator-unlock-for-
                # the-hard-.md): the grant check just above failed (no
                # sentinel, or an unresolvable session_id), so this envelope
                # IS the deny being returned -- append the in-session-unlock
                # line here, at this single seam, via the shared builder both
                # engines call (guard_unlock_sentinel.annotate_deny) so the
                # wording cannot drift between the two legs. Every one of the
                # 47 hard-deny guards inherits the line for free; no per-guard
                # edit.
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
                    # Fully suppressed (pure advisory, no rewrite leg) --
                    # treat exactly as if the guard had returned None and
                    # keep walking the chain. This is D-2's shadowing note
                    # in code: a later-registered guard may now surface
                    # where this suppressed one used to win.
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
                # Item 7 (state/handoffs/2026-07-30-boot-context-bloat-non-
                # orientation-surfaces.md): this exact (guard, shape) advisory
                # already fired once this session -- degrade to the terse
                # alternative, not a full re-explanation (docs/wiki/guard-
                # messaging.md § Register: the first firing already delivered
                # the full guidance; the repeat is strictly new, shorter
                # content -- the alternative alone -- not a second delivery of
                # the same prose). The audit-count line just above still
                # records this as a real firing (DR-277's count-and-log
                # contract is unaffected by whether the agent's rendered text
                # is full or degraded).
                #
                # This RETURNS the degraded envelope rather than `continue`-
                # ing the chain: a `continue` here would let a LOWER-
                # precedence guard win the slot a higher-precedence one had
                # already claimed, making advisory precedence a function of
                # per-session firing history. The chain-order contract (deny
                # wins the hard chain; a non-suppressed advisory returns
                # immediately) is unchanged -- this branch takes the exact
                # same collect/return path as any other advisory below, just
                # with `out` swapped for its degraded form.
                degraded = _degrade_advisory_envelope(out)
                if degraded is not None:
                    out = degraded
                # `degraded is None` (no terse alternative could be isolated)
                # falls open to the FULL envelope already in `out` -- never
                # silence (module docstring, "FAIL OPEN, UNCONDITIONALLY").
            if collect_advisories and not _is_hard_deny_envelope:
                # A genuine (non-suppressed) hard-deny envelope must NEVER be
                # folded into `_collected` -- "deny wins the hard chain" is
                # preserved by returning it immediately below instead,
                # discarding whatever soft/content/advisory envelopes this
                # call already collected earlier in the same chain (see
                # `collect_advisories` docstring above).
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
    # Deferred import, same circular-import reason as `evaluate_payload_
    # json`'s own `_blanket_disarm` import: `block_disarm_marker_sentinel_
    # creation.py` imports `_blanket_disarm.MARKER_BASENAME` at ITS top
    # level, and `_blanket_disarm.py` imports `GuardBand` FROM this module
    # at ITS top level -- so a module-level import of the sentinel guard
    # here (or in this file's top-of-file import block) would resolve
    # `_blanket_disarm` before `GuardBand` exists on a fresh interpreter,
    # the identical cycle `evaluate_payload_json` already works around.
    # `_build_guard_chain` is only ever CALLED (never imported-from-the-
    # top) after this module has finished executing its own top level, so
    # deferring the import to here (function body, not module top) is safe
    # for both callers: `evaluate_payload_json` at runtime, and
    # `test_guard_band_membership.py`'s direct `dispatch._build_guard_
    # chain(...)` structural call.
    from coordinator_core.bash_guards.block_disarm_marker_sentinel_creation import (
        check as _check_disarm_marker_sentinel_creation,
        MATCHERS as _matchers_disarm_marker_sentinel_creation,
    )

    # `check_destructive_git_revert`'s hard-deny leg and its advisory sibling
    # (`check_destructive_git_revert_advisory`) both compute from
    # `_dc._check_destructive_git_revert_full` -- the same `git status`/`git
    # rev-parse` oracle calls. Cache the result for THIS dispatch call only
    # (never module-scope: see `_check_destructive_git_revert_full`'s own
    # docstring for why lru_cache there would leak stale test-mock results
    # across unrelated cases) so a dispatch pass that evaluates both legs
    # -- see the CONFINEMENT_DENY registration and the ADVISORY_REWRITE
    # registration further below, Review: staff-eng Finding 0 -- never
    # re-spawns the oracle.
    #
    # The cache key is `(cmd, session_id, override_identity)`. The third term
    # is not redundant with the first two: the oracle reads a caller override
    # (`COORDINATOR_OVERRIDE_GIT_REVERT`) through `_override(payload=...)`,
    # which C14c re-keyed to prefer `payload["env"]` over ambient `os.environ`.
    # Two calls agreeing on `cmd` and `session_id` but carrying different
    # override env therefore have DIFFERENT correct verdicts.
    #
    # Within one `_build_guard_chain` call the third term is constant --
    # `payload` is a single closed-over value -- so it buys nothing today and
    # costs one env scan per call. It is here so that hoisting this cache onto
    # anything that outlives one call (module scope, a warm-server-scoped memo)
    # cannot serve one session's override verdict to another session's
    # identical `cmd`. That hazard is invisible to every existing test: the
    # narrow key passes them all and only fails in production, on a warm
    # server, as a disarmed guard.
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

    guard_chain: List[GuardEntry] = [
        # 1. preuse-bash-dispatch.sh own internal 11-check order.
        # `payload=payload` / `hook_payload=payload` threaded into every `_override()`-
        # consuming check below (C14b, state/handoffs/2026-08-23-the-warm-guard-op-gets-
        # registered.md): C14c re-keyed `dispatch_checks._override()` to prefer
        # `payload["env"]` over ambient `os.environ`, but that re-key does nothing unless
        # THIS chain actually hands each check its own payload -- before this edit, every
        # entry below called its check with only `(cmd, session_id[, ...])`, so
        # `_override` always fell through to `os.environ` regardless of what the caller's
        # payload carried. Harmless in the cold path (a fresh child process's own environ
        # IS the caller's shell env, so the fallback was always correct there), but on a
        # warm server this was the exact invisible-disarm/dead-override hazard C14c's own
        # docstring warns about, undetected because no existing test drives an override
        # through a NON-`os.environ` payload at this layer. Additive-only: a payload
        # carrying no `"env"` key (every cold caller, every pre-existing test) falls back
        # to `os.environ` unchanged -- see `_override`'s own docstring.
        # C3 (pln-the-destructive-core-learns-the-she, docs/plans/2026-08-26-
        # the-destructive-core-learns-the-shell-it-guards.md): declaration
        # LAST, per entry, only once the check's own detection reads the
        # dialect -- `check_no_verify` already does (C2's dialect-aware
        # segmentation seam), `check_destructive_rm` (this chunk's
        # `_PS_REMOVE_VERBS` table-driven PowerShell leg), and now the
        # three git-shaped checks below (`_ps_git_bypass_segments` --
        # git's own argv is byte-identical across dialects, so the work
        # was the anti-bypass surface, not new vocabulary). `runaway-find`
        # stays `("Bash",)`: POSIX `find` has no PowerShell-equivalent
        # argv shape to widen onto (reclassified Bash-only-by-construction
        # in the matchers ratchet, not merely deferred).
        GuardEntry("no-verify", lambda: _dc.check_no_verify(cmd, session_id, resolved=resolved, hook_payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-git-orphan", lambda: _dc.check_destructive_git_orphan(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-rm", lambda: _dc.check_destructive_rm(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("destructive-git-clean", lambda: _dc.check_destructive_git_clean(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Hard-deny leg ONLY -- never returns the advisory half (Review:
        # staff-eng, Finding 0: an advisory returned from THIS
        # CONFINEMENT_DENY slot would short-circuit `evaluate_payload_json`
        # and shadow every hard-deny guard registered after it, which
        # `GuardBand`'s own docstring forbids for this band). The advisory
        # leg is `destructive-git-revert-advisory`, registered below in
        # ADVISORY_REWRITE, after every CONFINEMENT_DENY guard.
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES 2026-08-21. The
        # unscoped-`git stash` deny this guard carries is the only EM-side
        # coverage for a shape `block_subagent_stash_creation` (subagent-
        # only) and `block_stash_destruction` (drop/clear only) do not
        # cover -- and both of THOSE already declare COMMAND_TOOL_NAMES, so
        # a Bash-only matcher here left `git stash` reachable from the
        # PowerShell tool for exactly the caller the other two exempt.
        # Observed: three unscoped stashes on this shared tree in one day
        # (14:02, 16:18, 18:27), the last capturing 144 files of concurrent
        # sessions' uncommitted work, while every stash shape this guard
        # classifies denied correctly on a Bash payload.
        #
        # PARTIAL COVERAGE, NOT DIALECT PARITY -- scoped honestly here
        # because the first draft of this comment overclaimed it, and a
        # reviewer was right to call it. `git stash`, `git -C <path> stash
        # push -u` and `& "git" stash` were each verified to deny through a
        # `tool_name: "PowerShell"` payload before this line changed, and
        # they are the shapes the incident actually produced. But all three
        # are POSIX-idiom spellings that happen to `shlex`-tokenize; the
        # body behind this entry (`_check_destructive_git_revert_full`) is
        # regex-over-raw-string plus `shlex`, NOT the tree-sitter dialect
        # tokenizer the 2026-08-19 held-cohort conversion built
        # (`archive/specs/2026-08/2026-08-19-the-held-guard-cohort-becomes-
        # dialect-safe.md`). PowerShell-native shapes still evade it:
        # `Start-Process git -ArgumentList 'stash'`, splatting
        # (`git @('stash')`), and aliased invocation.
        #
        # Widening is still right: those shapes evaded on the Bash tool too,
        # so this closes a real hole (the plain `git stash` that took the
        # tree) without opening one. What it does NOT do is make this guard
        # dialect-safe, and nobody should read the matcher as saying it did.
        #
        # NOT RATCHET-COVERED, and that is the governance gap worth naming:
        # `tests/test_guard_matchers_ratchet.py` watches modules carrying a
        # module-level `MATCHERS` constant and skips `dispatch.py` via
        # `_NON_GUARD_MODULES`. This guard is registered inline here with no
        # backing module, so its matchers are policed by nothing -- neither
        # this widening nor a future narrowing would fail a test. Census
        # entry: `docs/reference/guard-tool-name-membership.md` § 3.
        GuardEntry("destructive-git-revert", lambda: _git_revert_full()[0], True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        GuardEntry("blanket-git-add", lambda: _dc.check_blanket_git_add(cmd, session_id, hook_payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Bash-only is correct by construction, not unconverted -- this
        # entry keys on POSIX `find`'s own argv shape and no PowerShell
        # cmdlet or binary shares it, so there is no vocabulary to widen
        # onto. Drafted as Bucket A, reclassified by C3 on measurement:
        # docs/reference/guard-tool-name-membership.md § 8c.
        GuardEntry("runaway-find", lambda: _dc.check_runaway_find(cmd, session_id, payload=payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        # Must precede `offer-git-c`. That check rewrites `cd <dir> && git <sub>`
        # into `git -C <dir> <sub>` and returns allow+updatedInput, which
        # short-circuits the rest of the chain -- so a worktree guard placed
        # after it never sees `cd /tmp && git worktree add ...` and the ban is
        # bypassable by prefixing a `cd`. Hard-denies belong ahead of every
        # rewrite/offer check for exactly this reason.
        GuardEntry("block-worktree-creation", lambda: _check_worktree_creation(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_worktree_creation)),
        # Same ordering requirement as block-worktree-creation immediately
        # above, for the identical reason: `offer-git-c` short-circuits any
        # guard placed after it via allow+updatedInput, so a guard denying
        # `cd /tmp && touch .coordinator-doctrine-edit-approved` must sit
        # ahead of it too. Not identity-gated -- fires for every caller
        # including the main-loop EM, since the EM is exactly who this
        # sentinel exists to constrain (see module docstring).
        GuardEntry(
            "block-approval-sentinel-creation",
            lambda: _check_approval_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_approval_sentinel_creation),
        ),
        # Same ordering requirement as block-worktree-creation and
        # block-approval-sentinel-creation immediately above, for the
        # identical reason: `offer-git-c` short-circuits any guard placed
        # after it via allow+updatedInput, so a guard denying `cd /tmp &&
        # touch .coordinator-override-worktree-guard` must sit ahead of it
        # too. Registered directly adjacent to block-approval-sentinel-
        # creation -- the same class of guard (a second sentinel-creation
        # ban), same non-identity-gated posture (the EM is exactly who this
        # sentinel exists to constrain).
        GuardEntry(
            "block-worktree-sentinel-creation",
            lambda: _check_worktree_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_worktree_sentinel_creation),
        ),
        # `block-dev-repo-sentinel-removal`'s hard-deny leg was RETIRED here
        # (C13, docs/plans/2026-08-06-apply-guard-class-census.md), collapsing
        # its former TWO-LEG SPLIT into the single already-registered
        # `block-dev-repo-sentinel-removal-advisory` entry below in
        # ADVISORY_REWRITE. `check()`'s own detector only ever returned
        # VERDICT_DENY or VERDICT_ADVISORY, mutually exclusively, from the
        # same `_evaluate(cmd)` call the advisory leg also makes -- so no
        # command shape is orphaned by this deletion PROVIDED `check_advisory`
        # is widened to also render an advisory on a VERDICT_DENY result;
        # today `check_advisory` returns `None` (silent allow, no comment) for
        # VERDICT_DENY, since it only matches `verdict != VERDICT_ADVISORY`.
        # That widening is a `check()`-BODY change, out of this chunk's
        # registration-seam-only scope (see this module's own "TWO-LEG SPLIT"
        # docstring section) -- flagged for the peer chunk that owns module
        # bodies; until it lands, the direct high-confidence
        # `rm`/`mv`/`git rm`/`git mv .coordinator-dev-repo` shape that used to
        # hard-deny now silently allows with NO advisory context at all.
        # Same ordering requirement as the three sentinel/worktree guards
        # immediately above, for the identical reason -- and this one is
        # additionally the guard that closes the disarm marker's own
        # bootstrap loop: `GuardBand.CONFINEMENT_DENY` is unconditionally
        # non-suppressible by ANY blanket-disarm marker (see
        # `_blanket_disarm.py`'s own "BAND-SCOPED SUPPRESSION"), so
        # registering this guard in a weaker band would let a forged
        # marker suppress the very guard meant to stop it being forged.
        # Not identity-gated -- fires for every caller including the
        # main-loop EM, since `Scope: machine-total` is itself EM-audience-
        # narrowed precisely because the EM is exactly who this guard also
        # exists to constrain (see that module's own module docstring).
        GuardEntry(
            "block-disarm-marker-sentinel-creation",
            lambda: _check_disarm_marker_sentinel_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_disarm_marker_sentinel_creation),
        ),
        # Same ordering requirement as the sentinel/worktree guards above,
        # for the identical `offer-git-c` short-circuit reason: a guard
        # denying `cd <repo> && git stash drop` must sit ahead of the entry
        # that rewrites `cd <dir> && git <sub>` into allow+updatedInput.
        # Not identity-gated -- fires for every caller including the
        # main-loop EM. `block-subagent-destructive-action` below already
        # classifies `git stash drop`/`clear` as a deny, but its Layer-2
        # identity gate fails OPEN when no subagent resolves, exempting the
        # EM -- who is the caller with the MOST stack drift between its own
        # stash push and its own drop, and the one observed dropping a peer
        # session's entry. Same main-loop-leg posture as
        # block-worktree-creation.
        GuardEntry(
            "block-stash-destruction",
            lambda: _check_stash_destruction(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_stash_destruction),
        ),
        # Same ordering requirement as the sentinel/worktree/stash guards
        # immediately above, for the identical `offer-git-c` short-circuit
        # reason: a guard denying `cd <repo> && git stash push` must sit
        # ahead of the rewrite too. Registered directly adjacent to
        # block-stash-destruction -- the CREATE-side half of the same gap
        # (see module docstring entry 5h). Identity-gated to subagents only
        # (unlike block-stash-destruction, which fires for every caller
        # including the EM); the EM must remain able to `git stash push`.
        GuardEntry(
            "block-subagent-stash-creation",
            lambda: _check_subagent_stash_creation(payload),
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_subagent_stash_creation),
        ),
        # `block-noncanonical-branch-creation` RETIRED from this CONFINEMENT_
        # DENY slot (C13, docs/plans/2026-08-06-apply-guard-class-census.md):
        # moved to ADVISORY_REWRITE, at the tail of that band -- see its new
        # registration, below, for the flip's rationale.
        #
        # The three identity/confinement hard-denies below sit AHEAD of
        # `offer-git-c` for the same reason as the three sentinel/worktree
        # guards above it, and they were the ones that had drifted behind it.
        # `offer-git-c` returns allow+updatedInput for any `cd <dir> && git
        # <sub>`, which short-circuits the rest of the chain -- so while these
        # sat below it, every one of them was bypassable by prefixing a `cd`:
        # a subagent barred from committing could commit by typing
        # `cd <repo> && git commit`, and the reviewer bash confinement and the
        # destructive-git ban were both evadable the same way. The guards
        # were reachable only for commands `offer-git-c` happened not to
        # rewrite, which is the opposite of a confinement. Anything that
        # returns a rewrite must come after every hard-deny. (`block-
        # subagent-plan-body-bash-write`, formerly the fourth guard in this
        # group, RETIRED from here in the same C13 move as `block-
        # noncanonical-branch-creation` above -- see its own new
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
        # block-subagent-commit -- structural teeth for the no-self-commit
        # rule. Own-module hard-deny, pinned alongside the three guards above
        # (all fire on git-history/identity confinement, which outrank a
        # machine-load deny); supersedes nudge-subagent-scoped-commit.
        GuardEntry("block-subagent-commit", lambda: _check_subagent_commit(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_commit)),
        # guard-host-subagent-bash-ban -- registered port of DoE's folded
        # `_run_folded_bash_guards` entry (C6, state/dispatch-briefs/2026-08-
        # 28-the-four-folded-bash-guards-get-registered-not-folded/C6.md).
        # Pinned alongside the three identity/confinement guards above for
        # the identical reason: fires on identity confinement (a resolved
        # subagent + this host's `subagent_bash_policy: deny` opt-in), which
        # outranks a machine-load deny, and must sit ahead of `offer-git-c`
        # so a `cd <dir> && <cmd>` prefix cannot bypass it via that guard's
        # rewrite short-circuit. MATCHERS pinned to ("Bash",) ONLY -- see
        # that module's own docstring "SCOPED TO `Bash`" section; do not
        # widen this one onto COMMAND_TOOL_NAMES alongside its siblings.
        GuardEntry("guard-host-subagent-bash-ban", lambda: _check_host_subagent_bash_ban(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_host_subagent_bash_ban)),
        # This one is a hard-deny (fail_closed) and belongs on this side of every
        # rewriting guard for the same reason as the three above. It previously sat
        # further down the chain, behind `offer-git-c`. No bypass was demonstrated for
        # it — `offer-git-c` only rewrites `cd <dir> && git <sub>`, and a suite
        # invocation wears no such shape — but the safety of its
        # position depended on reasoning about which rewrites could match which command
        # shapes, and that reasoning has to be redone correctly every time a rewriting
        # guard is added. The uniform rule
        # (every hard-deny precedes every rewrite) needs no such reasoning and cannot
        # rot, so it moves here and joins the invariant's set. (`check-raw-pid-
        # liveness`, formerly its sibling in this pair, RETIRED from here in C13
        # -- see its own new ADVISORY_REWRITE registration below.)
        GuardEntry("check-test-suite-invocation", lambda: _check_test_suite_invocation(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_test_suite_invocation)),
        # block-subagent-grant-acquisition -- see module docstring entry 5i.
        # Hard-deny, identity-gated to subagents only (same posture as
        # block-subagent-stash-creation, 5h): denies a resolved subagent
        # acquiring the CLAUDE.md write grant via `coordinator_core.session.
        # claude_md_grant grant`. Registered directly adjacent to
        # check-test-suite-invocation, at the tail of the hard-deny run --
        # same `offer-git-c` short-circuit ordering requirement as every
        # entry in this CONFINEMENT_DENY run.
        GuardEntry("block-subagent-grant-acquisition", lambda: _check_subagent_grant_acquisition(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_grant_acquisition)),
        # block-subagent-guard-grant -- Bash-channel leg of the EM
        # guard-grant route (plan 2026-08-13-em-exercisable-in-band-grant-
        # route.md, C3), load-bearing per PM ruling 1 (see that module's own
        # docstring). Hard-deny, identity-gated to subagents only, same
        # posture as block-subagent-grant-acquisition immediately above.
        # Registered directly adjacent to it -- same `offer-git-c`
        # short-circuit ordering requirement as every entry in this
        # CONFINEMENT_DENY run.
        GuardEntry("block-subagent-guard-grant", lambda: _check_subagent_guard_grant(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_subagent_guard_grant)),
        # guard-repo-setup-claude-home-refusal -- ported from DoE-claude's
        # in-process fold (docs/plans/2026-08-28-the-four-folded-bash-
        # guards-get-registered-not-folded.md, C5). Hard-deny, NOT identity-
        # gated (fires for every caller, EM included): makes repo-setup's
        # "never target ~/.claude" precondition executable rather than
        # prose. Its predicate keys on command text naming the scaffold
        # mechanism plus a resolved target root, structurally disjoint from
        # every guard above and below it, so its position among the
        # CONFINEMENT_DENY entries is a convenience, not a behaviour
        # dependency -- registered at the tail of the hard-deny run, same
        # `offer-git-c` short-circuit ordering requirement as every entry in
        # this CONFINEMENT_DENY run.
        GuardEntry("guard-repo-setup-claude-home-refusal", lambda: _check_repo_setup_claude_home_refusal(payload), True, GuardBand.CONFINEMENT_DENY, AdvisoryValue.NOT_COST_ARGUED, matchers=tuple(_matchers_repo_setup_claude_home_refusal)),
        # Advisory (never a deny) sibling of `destructive-git-revert` above
        # (Review: staff-eng, Finding 0). Registered here -- after EVERY
        # CONFINEMENT_DENY hard-deny guard, and ahead of `offer-git-c`'s
        # allow+updatedInput rewrite further below -- so it can never
        # shadow a hard deny: it is the first non-CONFINEMENT_DENY entry in
        # the chain and `offer-git-c` still gets a chance to fire after it
        # for a command this guard does not advise on. `fail_closed=False`
        # (unlike its hard-deny sibling): a crash in the advisory leg must
        # swallow to allow, never route through the hard-deny crash path --
        # the same reasoning `bump-foreign-repo-write`'s own registration
        # comment states for its identical choice.
        # Matches its hard-deny leg's matchers (above): the two split out of
        # one guard and share `_git_revert_full`'s body, so a payload the
        # deny leg classifies must be able to reach the advisory leg too --
        # otherwise a PowerShell sweep that does not meet the deny bar
        # passes with no nudge at all.
        GuardEntry("destructive-git-revert-advisory", lambda: _git_revert_full()[1], False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Advisory (never a deny) sibling of `block-dev-repo-sentinel-
        # removal` above (same CONFINEMENT_DENY shadowing hazard
        # `destructive-git-revert-advisory` above fixes; see this guard's
        # own module docstring "TWO-LEG SPLIT"). Registered here -- after
        # EVERY CONFINEMENT_DENY hard-deny guard, and ahead of
        # `offer-git-c`'s allow+updatedInput rewrite further below -- so it
        # can never shadow a hard deny: an input tripping both this
        # advisory and a later hard deny (e.g. `block-approval-sentinel-
        # creation` at an earlier chain position) still returns that deny
        # first, unaffected by this entry's position.
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4,
        # docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-
        # guards.md, Bucket B): `check_advisory` itself is ALREADY fully
        # dialect-aware -- it resolves `dialect_from_tool_name(payload.get(
        # "tool_name"))` and declines to rule (`dialect is None`) rather than
        # assuming Bash, then calls the shared `_evaluate(cmd, dialect)`. The
        # registered leg has carried working PowerShell detection since it
        # was authored; only the chain-entry `matchers` gate here kept a
        # PowerShell payload from ever reaching it, per the master-gate note
        # in `_any_declared_matchers`. This is connecting a built dialect
        # leg to the dispatcher, not authoring new detection -- same shape as
        # `bump-outside-repo-write`'s own C4 widening above. The literal
        # tuple (not a `MATCHERS` import) stays for the reason given in the
        # comment above: the declaration on this module's dead `check()` leg
        # does not describe `check_advisory`'s own tool-name coverage.
        GuardEntry("block-dev-repo-sentinel-removal-advisory", lambda: _check_dev_repo_sentinel_removal_advisory(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Same ordering requirement as the sentinel/worktree/stash/branch
        # guards above, for the identical `offer-git-c` short-circuit
        # reason: that check rewrites `cd <dir> && git <sub>` into `git -C
        # <dir> <sub>` and returns allow+updatedInput, which short-circuits
        # the rest of the chain -- so C4's write-confinement speed bump
        # below (`bump-foreign-repo-write`) must sit ahead of it too, or a
        # bare `cd <foreign-repo> && git commit` never reaches it at all.
        # This is the defect this move fixes for `bump-foreign-repo-write`:
        # both that guard and `offer-git-c` were registered in the same
        # `GuardBand.ADVISORY_REWRITE` band, and only the first non-None
        # result in chain order wins, so `offer-git-c` answered first for
        # every un-pathspec'd `cd <dir> && git commit` and the bump C2 built
        # a destination-class message axis for was never seen -- a message
        # split nobody ever sees is not a fix.
        #
        # `bump-outside-repo-write` (C5, immediately below C4) moves here
        # too, but NOT for that same defect: its own module docstring
        # ("WRITE-SINK CLASSIFICATION") deliberately excludes git
        # subcommands from its candidate set, and `_write_bump_sink_shapes.
        # py`'s binary table has no `"git"` entry at all -- a `cd <dir> &&
        # git <sub>` shape was never in this guard's candidate scope, at
        # any chain position, so `offer-git-c` never had anything to shadow
        # for it. It moves purely for registration-shape consistency with
        # its sibling C4, both C4/C5 speed bumps sitting together ahead of
        # the git-rewrite guard they're grouped with in the plan, not
        # because of a `cd <foreign-repo> && git commit` reachability gap.
        #
        # Reordering also moves both bumps ahead of `probe-spray` and
        # `block-illegal-filename` below (previously the bumps sat at the
        # TAIL of the ADVISORY_REWRITE band, after both) -- a repeated or
        # illegally-named cross-repo/outside-repo bash write now hits the
        # bump's stronger, destination-specific signal first instead of
        # those guards' more generic advisories. Judged safe: both shadowed
        # guards are non-blocking advisories and the bump's deny is the more
        # useful signal on overlap.
        #
        # Not identity-gated -- fires for every caller including the
        # main-loop EM. Unlike the CONFINEMENT_DENY guards above, these two
        # stay ADVISORY_REWRITE (see each one's own attribute-explanation
        # comment immediately below) -- band and chain-order are
        # orthogonal; a guard need not be a hard-deny for `offer-git-c`'s
        # rewrite to be capable of shadowing it, it only needs to sit after
        # it in registration order.
        #
        # C4, docs/plans/2026-08-02-write-confinement-guards.md (DoE-claude
        # repo) -- the Bash-surface CROSS-REPO write-confinement speed bump.
        # THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY (see that plan's
        # "Design posture -- passable by construction"); every attribute
        # below is named explicitly rather than copied from a neighbour.
        #
        #   fail_closed=False -- the OPPOSITE of every neighbouring
        #     CONFINEMENT_DENY entry above (all `True`): this guard's whole
        #     job is an advisory nudge, never a confinement, so a crash
        #     inside it must be swallowed as "allow" via `_crash_deny`'s own
        #     `fail_closed=False` contract, never routed through the
        #     hard-deny crash path -- crashing closed here would turn a
        #     deliberately passable bump into an accidental hard wall the
        #     moment this module has a bug.
        #   band=GuardBand.ADVISORY_REWRITE, NOT CONFINEMENT_DENY -- the
        #     blanket-disarm marker (`_blanket_disarm.py`) can suppress
        #     every band EXCEPT CONFINEMENT_DENY, so registering a
        #     DELIBERATELY passable bump in the one band that switch cannot
        #     suppress would make it the single LEAST passable, least
        #     disarmable guard in this whole suite -- exactly backwards for
        #     a speed bump. Matches C7's `CLASS = 'advisory'` choice on the
        #     tool-surface guard for cross-surface consistency.
        #   advisory_value=AdvisoryValue.NOT_COST_ARGUED -- this guard's
        #     message is about which REPO a write lands in, never about
        #     Windows spawn cost, so it is neither WINDOWS_COST_ONLY nor
        #     HOST_INDEPENDENT; left at the UNCLASSIFIED default would fail
        #     `dispatch.py`'s own registry-validation test (AC-1).
        #
        # `cwd` consumption is genuinely required here, not the widened-
        # sharing this module warns against: a `git -C <dir>`/`cd <dir> &&
        # git ...`/plain-bash-write-sink target is always resolved RELATIVE
        # to wherever the command actually runs, i.e. the live payload cwd
        # -- this guard resolves that relative path itself, locally, once
        # per candidate, never caching a resolved root for reuse by another
        # guard. C2 (`_write_bump_applicability.resolve_launch_anchor`)
        # anchors applicability on the session's STABLE launch root instead
        # of this same live cwd precisely because the payload cwd moves
        # under an ordinary `cd` -- the two are answering different
        # questions ("where did this session start" vs "where does THIS
        # command's own relative-path resolution begin") and both are
        # needed.
        GuardEntry(
            name="bump-foreign-repo-write",
            fn=lambda: _check_bump_foreign_repo_write(cmd, session_id, cwd, payload),
            fail_closed=False,
            band=GuardBand.ADVISORY_REWRITE,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            # Widened from ("Bash",) to COMMAND_TOOL_NAMES by
            # docs/plans/2026-08-07-liveness-seam-validates-its-repo-root.md
            # C5 (2026-08-07): C4 (immediately above, same date) widened this
            # guard's sibling `bump-outside-repo-write` because that module
            # already carried a dead PowerShell leg; THIS module carried none
            # at all, so C5 authored `_check_bump_foreign_repo_write_
            # powershell` (candidate extraction via the PowerShell cmdlet
            # write-sink table, judged through the SAME predicate the Bash
            # body uses) before flipping this matcher -- widening the
            # matcher alone, with no leg to reach, would have routed
            # PowerShell payloads into a Bash-only code path for zero
            # coverage gain.
            matchers=COMMAND_TOOL_NAMES,
        ),
        # C5, docs/plans/2026-08-02-write-confinement-guards.md (DoE-claude
        # repo) -- the Bash-surface OUTSIDE-repo write-confinement speed
        # bump, C4's sibling: fires when a plain-bash write-sink TARGET
        # resolves under NO git root at all (C4 fires when it resolves
        # under a DIFFERENT git root -- see that guard's own module
        # docstring for the full split). THIS IS A SPEED BUMP, NOT A
        # SECURITY BOUNDARY -- every attribute below mirrors C4's own
        # registration exactly, for the identical stated reasons:
        #
        #   fail_closed=False -- a crash inside this guard must swallow to
        #     "allow", never route through the hard-deny crash path, for the
        #     same reason C4's own registration comment states.
        #   band=GuardBand.ADVISORY_REWRITE, NOT CONFINEMENT_DENY -- the
        #     blanket-disarm marker can suppress every band except
        #     CONFINEMENT_DENY; registering a deliberately passable bump
        #     there would make it the LEAST passable, least disarmable guard
        #     in the suite. Matches C7's `CLASS = 'advisory'` choice on the
        #     tool-surface guard, and C4's identical band choice, for
        #     cross-surface consistency.
        #   advisory_value=AdvisoryValue.NOT_COST_ARGUED -- this guard's
        #     message is about which git root (if any) a write lands under,
        #     never about Windows spawn cost, so it is neither
        #     WINDOWS_COST_ONLY nor HOST_INDEPENDENT; left at the
        #     UNCLASSIFIED default would fail `dispatch.py`'s own
        #     registry-validation test (AC19).
        #
        # `cwd` consumption is genuinely required here for the identical
        # reason C4's own registration comment states: a plain-bash-write-
        # sink target is always resolved RELATIVE to wherever the command
        # actually runs (the live payload cwd), not the session's stable
        # launch anchor C2 uses for applicability.
        GuardEntry(
            name="bump-outside-repo-write",
            fn=lambda: _check_bump_outside_repo_write(cmd, session_id, cwd, payload),
            fail_closed=False,
            band=GuardBand.ADVISORY_REWRITE,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            # Widened from ("Bash",) to COMMAND_TOOL_NAMES by
            # docs/plans/2026-08-07-liveness-seam-validates-its-repo-root.md
            # C4 (2026-08-07): this module already carried a complete
            # `_check_bump_outside_repo_write_powershell` leg behind a
            # `Dialect.POWERSHELL` gate that could never fire while this
            # entry stayed pinned to ("Bash",) -- the dispatcher's C1 master
            # gate rejected any PowerShell payload before the chain ran, so
            # the leg had been dead since it shipped. C4 does not author
            # detection, only connects the built PowerShell leg to the
            # dispatcher. `bump-foreign-repo-write` immediately above is
            # deliberately NOT widened here -- it has no PowerShell leg yet;
            # that is C5's job.
            matchers=COMMAND_TOOL_NAMES,
        ),
        # Soft: one of two checks that receive `cwd` (F0-adjacent note
        # above; the other is `reap-stale-git-lock` immediately below).
        # Ungated chain member (C1 audit finding): guard_offer_git_c.py
        # declares no MATCHERS and carries no tool_name gate of its own.
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("offer-git-c", lambda: _check_offer_git_c(cmd, session_id, cwd), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        # Self-heal leg of the fleet-wide `.git/index.lock` contention
        # campaign: a stat-gated (zero-subprocess in the common no-lock case)
        # pre-op check that reaps an ORPHANED `.git/index.lock` ahead of a raw
        # lock-taking git invocation (add/commit/status/diff/mv/stash),
        # reusing `ops.reap_stale_locks`' own age-and-stability gate
        # untouched -- see guard_reap_stale_git_lock.py's own module
        # docstring. Always returns None (side-effect-only guard, never a
        # rewrite/deny).
        #
        # INVARIANT: a side-effect-only guard (always returns None) must be
        # registered ahead of any rewriting guard in this first-wins chain
        # (see dispatch.py:~1315's `return out` on first non-None envelope).
        # A rewrite envelope returned by a guard downstream in registration
        # order never runs -- placing a side-effect-only guard after one
        # starves it by construction. `git-no-optional-locks` returns a
        # rewrite envelope for `git status`/bare `git diff`, so
        # `reap-stale-git-lock` must precede it here.
        #
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, docs/plans/
        # 2026-08-26-the-destructive-core-learns-the-shell-it-guards.md,
        # Bucket B): `_find_lock_taking_git_invocation` splits `cmd` into
        # segments and matches each on the literal substring `"git"` plus a
        # resolved subcommand -- a `git`-shaped invocation is spelled
        # identically under PowerShell (`git add`/`git commit`/... take no
        # dialect-specific form), so this is a foreign-binary-argv case per
        # C1's audit rule: correct without a `_dialect` reference. No
        # detection change; only the chain-entry gate moves.
        GuardEntry("reap-stale-git-lock", lambda: _check_reap_stale_git_lock(cmd, cwd, session_id), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Mechanical leg of the same campaign: auto-rewrites `git status`/bare
        # `git diff` to insert `--no-optional-locks` pre-subcommand,
        # prompt-free -- see guard_no_optional_locks.py's own module
        # docstring for the measured evidence this rewrite is
        # behavior-preserving.
        #
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, same plan/Bucket
        # B as above): the insertion point is located via `tokenize_full_
        # command` plus a raw-character-offset scan over `cmd`'s own text --
        # a bare `git status`/`git diff` invocation is identical text under
        # both dialects, the same foreign-binary-argv case `reap-stale-
        # git-lock` immediately above already documents. No detection
        # change.
        GuardEntry("git-no-optional-locks", lambda: _check_git_no_optional_locks(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Content: deliberately NOT crash-deny-routed (see module docstring).
        # Review: code-reviewer (Finding 2) -- check_validate_commit's git
        # calls (staged-file list, scope-check toplevel resolution, CLAUDE.md
        # blob fetch, frontmatter diff) are all cwd-sensitive; thread cwd
        # through so they resolve against the payload's actual working
        # directory rather than this process's own os.getcwd().
        #
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, same plan/Bucket
        # B as above): `check_validate_commit`'s own `contains_git_commit`
        # gate is a raw `re.match(r"^git\s+commit(\s|$)", command)` (and the
        # same pattern re-run per `&&`/`||`/`;`-split segment) -- `git
        # commit` is spelled identically in both dialects, the same
        # foreign-binary-argv case as the two entries above. No detection
        # change.
        GuardEntry("validate-commit", lambda: _dc.check_validate_commit(cmd, session_id, cwd, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # Review: review-integrator -- Finding 2. Registered AHEAD of
        # `probe-spray` (moved up from the dispatcher's own tail below it),
        # satisfying two ordering constraints at once:
        #   1. It still sits after every hard-deny above (identity/
        #      confinement/git-history denies all outrank a search answer),
        #      same invariant every rewrite/advisory entry in this chain
        #      already honors.
        #   2. It must precede `probe-spray` specifically: `check_probe_
        #      spray`'s ring-buffer recurrence check (`in_ring`) fires on
        #      ANY exact-repeated command shape, including a repeated grep,
        #      and its advisory envelope short-circuits this loop before
        #      `inprocess-search` would ever run. An answered search spawns
        #      nothing, so probe-spray's machine-load concern is moot for it
        #      -- answering a repeated grep in-process is strictly better
        #      for the machine than nudging about it and then letting the
        #      grep spawn anyway. `inprocess-search` never denies a command
        #      it cannot answer, so moving it earlier cannot introduce a new
        #      bypass of anything below it (same reasoning already applied
        #      to its position relative to the rewrite guards further down).
        GuardEntry("inprocess-search", lambda: _check_inprocess_search(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_inprocess_search)),
        # Advisory (dispatcher own tail).
        #
        # Widened from ("Bash",) to COMMAND_TOOL_NAMES (C4, docs/plans/
        # 2026-08-26-the-destructive-core-learns-the-shell-it-guards.md,
        # Bucket B): every `is_probe`/`is_strong_probe` test above is a raw
        # `re.match`/`re.search` against `cmd`'s own text (`echo`, `true`,
        # `sleep <n>`, ...) plus the session-keyed ring-buffer recurrence
        # check, none of which reads through the bash-only tokenizer -- a
        # probe-shaped one-liner is spelled identically whether the caller
        # names the tool `Bash` or `PowerShell` (`echo probe` is valid text
        # under either), so this is the same foreign-binary/literal-text
        # case as the other four Bucket B entries. No detection change.
        GuardEntry("probe-spray", lambda: _dc.check_probe_spray(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=COMMAND_TOOL_NAMES),
        # 2. block-illegal-filename.sh (cohort 1, Bash leg, advisory).
        GuardEntry("block-illegal-filename", lambda: _check_illegal_filename(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_illegal_filename)),
        # 5b. check-test-suite-invocation -- no legacy bash predecessor (new
        # 2026-07-23, DoE DR-088 layers 1/2/6). Positioned at the TAIL of the
        # hard-deny cohort-1 run rather than interleaved: it has no parity
        # ordering to preserve, and every guard above it protects git history
        # or an identity confinement, which outrank a machine-load deny when
        # two would fire on the same command.
        # 5c. check-raw-pid-liveness -- no legacy bash predecessor (new
        # 2026-07-27, DoE C14/RAW-PID-LIVENESS-GUARD). Not identity-gated
        # (fires on every caller, EM included -- see its module docstring);
        # has no parity ordering to preserve, so it sits at the very tail.
        # BX-16 (DoE docs/plans/2026-07-29-windows-viability-stop-the-spawn-
        # storms.md) -- generalises offer-git-c's rewrite seam from
        # cd-over-git to bash-over-op. Registered at the very TAIL,
        # deliberately after every hard-deny above (including the two
        # (check-test-suite-invocation, check-raw-pid-liveness) that
        # themselves sit after offer-git-c and so already carry the same
        # "must not be preceded by anything that can short-circuit the
        # chain" requirement -- appending here rather than interleaving
        # near offer-git-c cannot introduce a NEW bypass of any existing
        # hard-deny, regardless of what row later fixes that ordering).
        # None of these five ever deny (see their shared module comment) --
        # they only offer an auto-rewrite or an advisory, so their own
        # position relative to EACH OTHER carries no confinement risk.
        # `inprocess-search` (formerly registered here) moved up ahead of
        # `probe-spray` -- see that entry's own comment above for why.
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("find-exec-rewrite", lambda: _dc.check_find_exec_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.WINDOWS_COST_ONLY, matchers=("Bash",)),
        # Bash-only is correct by construction, not unconverted -- per-leg
        # reason (distinct from its dual-declaring sibling `grep-via-bash-
        # guard`): docs/reference/guard-tool-name-membership.md §8a.
        GuardEntry("grep-via-bash-rewrite", lambda: _dc.check_grep_via_bash_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("sed-range-read-advise", lambda: _dc.check_sed_range_read_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("cat-heredoc-write-advise", lambda: _dc.check_cat_heredoc_write_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        # `git_root` is threaded here (unlike this row's cat-heredoc sibling
        # above, which needs only `cmd`) because this check's own detection
        # -- "does the write target resolve INSIDE the repo" -- is pure path
        # arithmetic against a repo root, and `cwd` is this dispatcher's
        # existing, no-git-spawn stand-in for that root (see validate-commit/
        # offer-git-c/bump-foreign-repo-write above, all `cwd`-threaded for
        # the identical cwd-sensitive-resolution reason). Passing `None`
        # here would make the guard permanently silent in production (its
        # own fail-closed contract on an empty/None `git_root`), never
        # firing outside a direct unit-test call.
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("heredoc-repo-write-advise", lambda: _dc.check_heredoc_repo_write_advise(cmd, session_id, payload, cwd), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        GuardEntry("git-commit-safe-commit-advise", lambda: _dc.check_git_commit_safe_commit_advise(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash", "PowerShell")),
        # BX-7/BX-8's missing rewrite targets, closing the two-shape gap this
        # dispatch was sent to close (DoE docs/plans/2026-07-29-windows-
        # viability-stop-the-spawn-storms.md, row BX-16): MULTI_PROBE_BANNER
        # (40.1% of forks) and HEAD_TAIL_PLUMBING (25%) had no rewrite target
        # in this section until now. Registered at the tail, after every
        # existing BX-16 entry above, for the identical reason those five are
        # tail-registered: neither ever denies (see this section's module
        # comment in dispatch_checks.py), so position relative to the other
        # rewrite/advisory-only checks carries no confinement risk, and both
        # still sit after every hard-deny above per the chain's own ordering
        # invariant (test_hard_denies_precede_rewrites.py).
        # Bash-only is correct by construction, not unconverted -- per-leg
        # reason (distinct from its dual-declaring sibling `multiprobe-
        # banner`): docs/reference/guard-tool-name-membership.md §8a.
        GuardEntry("multiprobe-banner-rewrite", lambda: _dc.check_multiprobe_banner_rewrite(cmd, session_id, payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=("Bash",)),
        # guard_head_tail_rewrite.py declares no module MATCHERS but reads
        # dialect via `check_head_tail_plumbing_rewrite`'s own docstring
        # (AC16 CALLEE-GRAPH AUDIT, C6 pln-the-shape-classifier-reaches-
        # a-e743e5), which named this exact registration's literal
        # `("Bash",)` as out of that chunk's write scope. Built-but-not-
        # wired (Bucket D, state/audits/2026-08-26-guard-detection-
        # language-dependence-recensus.md Finding 4); C9 closes the
        # deferral by widening to the declared universe AND passing
        # `dialect=` explicitly -- unlike `bump-outside-repo-write`'s own
        # callee, this function does not derive dialect from `payload`
        # internally; its `dialect is Dialect.POWERSHELL` gate only fires
        # on an explicit caller-supplied `dialect=`, per this function's
        # own AC16 docstring.
        GuardEntry("head-tail-plumbing-rewrite", lambda: _check_head_tail_plumbing_rewrite(cmd, session_id, dialect=_dialect_from_tool_name(payload.get("tool_name") if isinstance(payload, dict) else None), payload=payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.WINDOWS_COST_ONLY, matchers=COMMAND_TOOL_NAMES),
        # Registered in the rewrite band for the same reason as the entries
        # above it (its rewrite is provably params-identical -- see that
        # module's rung-A argument), with one difference worth naming: unlike
        # the five BX-16 entries, this one CAN deny, on the multi-line command
        # it cannot splice a heredoc into. That deny is not a confinement
        # boundary and so is deliberately absent from
        # test_hard_denies_precede_rewrites.py's CONFINEMENT_HARD_DENIES --
        # it protects a payload's integrity, not an identity gate, and there
        # is no reshape-around-a-rewrite evasion to close (the only guard that
        # rewrites this command shape is this one).
        #
        # Chain-position note: because the guard chain returns on the first
        # non-None result, any command this guard rewrites OR denies shadows
        # the PLATFORM_CONDITIONED_DENY band below (`grep-via-bash-guard`,
        # `multiprobe-banner`, `plumbing-and-loops`) -- those three never see
        # a `coordinator_core.invoke` call this guard has already handled.
        # This is safe only because the target shapes don't overlap (invoke
        # calls don't look like `grep -r`, the multiprobe banner shape, or
        # `find | head`); it is an assumption, not an asserted invariant, and
        # any future guard added ahead of the tail band should re-check it.
        # Ungated chain member (C1 audit finding): guard_offer_invoke_params_
        # stdin.py declares no MATCHERS and carries no tool_name gate of its
        # own.
        # Bash-only is correct by construction, not unconverted -- reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("offer-invoke-params-stdin", lambda: _check_offer_invoke_params_stdin(cmd, session_id), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.NOT_COST_ARGUED, matchers=("Bash",)),
        # `grep-via-bash-guard` moved from PLATFORM_CONDITIONED_DENY to
        # ADVISORY_REWRITE (H11(a), 2026-07-30, docs/plans/2026-07-30-os-
        # aware-guard-advisory-defaults.md) -- its own substitutable/deny
        # branch (`_platform_verdict_for_shape`) was removed the same day
        # because it produced 0 denies on either platform across the full
        # corpus, provably unreachable. What remains (`_composed_advisory`)
        # never denied on any host to begin with, so this guard has no deny
        # vocabulary left -- band and `fail_closed` are deliberately
        # orthogonal (see `GuardBand`'s own docstring above), so BOTH are
        # changed explicitly in this one edit: band to ADVISORY_REWRITE, and
        # `fail_closed` to `False` (was `True`) so a crash inside this
        # now-purely-advisory guard degrades to allow via `_crash_deny`'s
        # own `fail_closed=False` contract, rather than denying a
        # machine-wide 30.8%-of-Bash-traffic shape for a crash in a guard
        # that can no longer deny on its own verdict.
        #
        # PHYSICALLY RELOCATED here (was at the tail, alongside
        # multiprobe-banner/plumbing-and-loops) -- the band model's own
        # contiguity invariant (`test_bands_are_contiguous_and_in_fixed_
        # sequence`) requires every ADVISORY_REWRITE entry to sit together,
        # ahead of the PLATFORM_CONDITIONED_DENY band; a band-label change
        # with no matching move would put this entry's rank BEHIND the two
        # still-PLATFORM_CONDITIONED_DENY guards below, breaking that
        # invariant. Chain position still does not affect this guard's own
        # correctness (`check()` re-derives substitutability internally and
        # self-suppresses for anything `grep-via-bash-rewrite` already
        # claims, regardless of relative registration order), so the move is
        # purely to satisfy band contiguity, not a behavioural requirement.
        #
        # `advisory_value` ALSO reclassified, WINDOWS_COST_ONLY ->
        # HOST_INDEPENDENT (H11 dispatch, 2026-07-30 -- not itself named by
        # the plan row, found while confirming AC-4). Left at
        # WINDOWS_COST_ONLY, H4's own suppression default would SILENCE
        # this guard on non-Windows -- exactly the host its own surviving
        # message is about (`_composed_advisory`'s GNU-only-construct leg
        # names "behavior can diverge on BSD grep (macOS)" -- a BSD-vs-GNU
        # divergence has nothing to do with Windows spawn cost). Verified
        # empirically: at WINDOWS_COST_ONLY the guard fired on Windows and
        # was silently suppressed on macOS for the one command class its
        # message is written for. The guard's OTHER remaining leg (the
        # partial-pipe fork-reduction advisory) is the same fork-reduction
        # argument its sibling `grep-via-bash-rewrite` already carries as
        # HOST_INDEPENDENT (line above, in this same band) -- matching that
        # precedent rather than inventing a third value for a guard that is
        # now, in substance, this guard's own narrower cousin.
        GuardEntry("grep-via-bash-guard", lambda: _check_grep_via_bash(payload, host_is_windows=host_is_windows), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_grep_via_bash)),
        # cross-repo/inbox/ dispatch, "Guard powershell-via-bash mangling"
        # (2026-08-08): registered adjacent to `grep-via-bash-guard` above --
        # same theme (an advisory naming a Bash-spawn hazard, never a deny),
        # same band-contiguity requirement (ADVISORY_REWRITE, ahead of the
        # two PLATFORM_CONDITIONED_DENY guards below). Never denies (module
        # docstring "Never denies"), so `fail_closed=False` -- a crash here
        # degrades to allow/no-context, matching every other pure-advisory
        # entry in this band.
        # Bash-only is correct by construction, not unconverted -- its whole
        # subject is PowerShell invoked FROM bash, so widening to the
        # PowerShell tool matches nothing it exists to catch. Reason:
        # docs/reference/guard-tool-name-membership.md §8.
        GuardEntry("powershell-via-bash-guard", lambda: _check_powershell_via_bash(payload), False, GuardBand.ADVISORY_REWRITE, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_powershell_via_bash)),
        # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C5/C7.
        # Both are advisory-only (never deny -- see each module's own "the
        # one true never-denies template" posture), registered here in the
        # ADVISORY_REWRITE band, after every hard-deny above (including
        # block-noncanonical-branch-creation, C1) and after every rewrite,
        # ahead of the two remaining PLATFORM_CONDITIONED_DENY guards below
        # -- required by the band model's own contiguity invariant
        # (`test_bands_are_contiguous_and_in_fixed_sequence`).
        GuardEntry(
            "branch-set-precedence",
            lambda: _check_branch_set_precedence(payload),
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_branch_set_precedence),
        ),
        GuardEntry(
            "longlived-branch-naming",
            lambda: _check_longlived_branch_naming(payload),
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
            matchers=tuple(_matchers_longlived_branch_naming),
        ),
        # C13 (docs/plans/2026-08-06-apply-guard-class-census.md) -- four
        # guard-class-census band flips, moved from CONFINEMENT_DENY to
        # ADVISORY_REWRITE (`fail_closed=True` -> `False`, `band=
        # CONFINEMENT_DENY` -> `ADVISORY_REWRITE`). Appended here, at the
        # tail of the ADVISORY_REWRITE band (lowest precedence in this band,
        # first non-`None` still wins) ahead of the two PLATFORM_CONDITIONED_
        # DENY guards below, per the band model's own contiguity invariant
        # (`test_bands_are_contiguous_and_in_fixed_sequence`). Band-move-
        # first is chain-SAFE regardless of tail position: an entry whose
        # own `check()` body still returns a deny envelope is merely
        # late-precedence in its new band, never a shadowed hard-deny --
        # `evaluate_payload_json` still returns on the first non-`None`
        # result, and nothing above these four in the chain is itself an
        # ADVISORY_REWRITE/rewrite entry these four could now shadow FROM
        # BEHIND (the four sit at the very end of the band). Whether tail
        # position is the BEST slot for each guard's OWN signal (as opposed
        # to merely chain-safe) is a separate question this move does not
        # resolve -- `offer-git-c`, `validate-commit`, and `git-commit-
        # safe-commit-advise` are earlier ADVISORY_REWRITE entries that
        # co-match plausible compound commands and could shadow one of
        # these four's advisory on overlap; flagged, not resolved, in C13's
        # own run report.
        #
        # SECOND, deliberate change riding inside the same flag on all four:
        # `fail_closed` is CRASH-PATH routing policy (module docstring F1),
        # orthogonal to band -- today a crash in any of these four guards
        # DENIES (`fail_closed=True`); after this flip a crash in any of
        # them silently ALLOWS instead (`fail_closed=False`, swallowed by
        # `_crash_deny`'s own fail-open contract). Not merely a side effect
        # of the band move: an explicit, separate semantics change.
        #
        # `block-worktree-creation` (also named in C13's own guard-class
        # census row) is DELIBERATELY NOT included in this move -- it is
        # coupled to `block-worktree-sentinel-creation`, a KEEP-HARD guard
        # (AC7) that exists solely to protect THIS guard's own override
        # sentinel from Bash-level creation. Flipping `block-worktree-
        # creation` to advisory while its sentinel-creation guard stays a
        # hard deny would leave that hard deny protecting the off-switch of
        # a guard that no longer blocks anything -- an incoherent pairing.
        # Retiring `block-worktree-sentinel-creation`'s keep-hard status (or
        # deciding this pairing is fine as-is) is a real product decision,
        # not a mechanical consequence of this band-flip wave, so C13 holds
        # `block-worktree-creation` back rather than deciding it inline; see
        # C13's own run report for the surfaced question. `block-worktree-
        # creation` therefore remains registered above, unchanged, in
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
        # BX-7/BX-8's own platform-conditioned advisory policy
        # (`guard_multiprobe_banner.py` / `guard_plumbing_and_loops.py`) --
        # deliberately registered at the very TAIL, AFTER every rewrite/
        # advise entry above (including `offer-git-c` and, as of H11,
        # `grep-via-bash-guard` right above this comment -- ADVISORY_REWRITE
        # now, no longer part of this cohort, but still registered adjacent
        # to it for the same reason: after every rewrite, before nothing
        # else matters ordering-wise).
        #
        # Each of these two is CLASS="hard-deny"/`fail_closed=True` (a crash
        # inside either still fails closed), but NEITHER GUARD CAN ITSELF
        # PRODUCE A DENY VERDICT ANY MORE (DR-280, 2026-08-07): each guard's
        # own platform-conditioned deny branch gated on
        # `_seam_confirmed_rewrite` against the SAME seam an
        # earlier-registered `ADVISORY_REWRITE` chain entry (e.g.
        # `multiprobe-banner-rewrite`) already consumes and returns on
        # first, so that gate could never open through the real dispatcher
        # -- the deny branch was retired as dead code, not narrowed. Both
        # guards still fire, and still call
        # `_platform_verdict.platform_verdict_for_shape`, but now always
        # render its advisory template (see each guard's own `check()`
        # docstring). `fail_closed=True` and `PLATFORM_CONDITIONED_DENY`
        # membership are unchanged by this -- `band` is verdict-vocabulary
        # classification, not a promise every member currently uses every
        # verdict in that vocabulary (see `GuardBand`'s own docstring:
        # `fail_closed` and `band` are explicitly orthogonal fields).
        #
        # Tail placement itself is UNCHANGED and still load-bearing: placed
        # BEFORE the rewrites, on macOS the guard's own shape would stop
        # being auto-rewritten into the cheaper equivalent and become an
        # ignorable advisory instead -- the fix this guard exists to
        # encourage would never apply. Placed AFTER (here), every
        # shape/platform combination correctly auto-rewrites first, and
        # each guard's own advisory still fires when the rewrite's own
        # `COORDINATOR_ALLOW_*` override has disabled it (or the seam
        # offers no confirmed outlet for this exact command -- see
        # `guard_plumbing_and_loops._seam_confirmed_rewrite`); that
        # override/no-outlet case is each guard's actual value, and it only
        # works correctly at the tail. An integration review once
        # recommended moving these two ahead of the rewrites (this rationale
        # applied identically to `grep-via-bash-guard` before H11 narrowed
        # its own deny branch away entirely -- see that guard's own
        # registration comment above); that was tested empirically and
        # REVERTED for the reason just given.
        #
        # Deliberately NOT added to `CONFINEMENT_HARD_DENIES` in
        # `test_hard_denies_precede_rewrites.py`: that invariant exists to
        # stop a caller EVADING a security boundary (identity gates,
        # git-history protection, the subagent-commit ban) by reshaping a
        # command around a rewrite. These two are machine-load guards with
        # no adversarial-evasion shape -- none of their target shapes are
        # reachable via the `cd <dir> && git ...` mechanism that invariant
        # closes -- so forcing them into that set would require the exact
        # ordering that causes the regression described above.
        GuardEntry("multiprobe-banner", lambda: _check_multiprobe_banner(payload, host_is_windows=host_is_windows), True, GuardBand.PLATFORM_CONDITIONED_DENY, AdvisoryValue.HOST_INDEPENDENT, matchers=tuple(_matchers_multiprobe_banner)),
        GuardEntry("plumbing-and-loops", lambda: _check_plumbing_and_loops(payload, host_is_windows=host_is_windows), True, GuardBand.PLATFORM_CONDITIONED_DENY, AdvisoryValue.WINDOWS_COST_ONLY, matchers=tuple(_matchers_plumbing_and_loops)),
    ]
    return guard_chain


def main() -> int:
    """Standalone entry point (mirrors the bash dispatcher direct-invocation
    path): read stdin ONCE, evaluate, print the envelope JSON on non-None,
    always exit 0 (ALLOW/DENY is conveyed via stdout, never exit code)."""
    raw = sys.stdin.read()
    out = evaluate_payload_json(raw)
    if out is not None:
        sys.stdout.write(json.dumps(out))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
