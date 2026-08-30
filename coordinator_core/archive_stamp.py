"""
coordinator_core.archive_stamp — handoff/memo/plan lifecycle frontmatter-write
orchestration, direct-import Python port of coordinator-archive-stamp.sh
(DoE 243b9a7c, 2026-07-19).

Purpose: the single authorized-writer family for claimed/shipped handoff frontmatter,
cross-repo memo lifecycle frontmatter, and plan-implemented stamping. Each public
function here is a thin orchestration wrapper — SHA/session-id resolution, JSON param
building, ownership-liveness gating — over ALREADY-NATIVE claude-klabauter ops
(handoff.stamp, handoff.transition, memo.transition, session.record_pickup). Unlike the
bash oracle, this module calls those op handlers DIRECTLY IN-PROCESS (no cc_invoke
subprocess hop, no strangler-facade legacy fallback) — this module IS the claude-klabauter engine,
so the "route to native or fall back to legacy node CLI" State-1/2/3 distinction the
bash oracle carried is now moot: there is no legacy path to fall back to.

Port of: coordinator-archive-stamp.sh (DoE 243b9a7c, 2026-07-19; 977 lines, 11 functions).
DoE veneer: coordinator/bin/archive-stamp-cli.py (polyglot trampoline calling this module).

Function-to-oracle map:
    stamp_shipped_in         <- stamp_shipped_in()            (handoff.stamp op)
    cs_claim_handoff         <- cs_claim_handoff()             (handoff.transition claim
                                                                 + session.record_pickup;
                                                                 cs_consume_handoff is a
                                                                 retained deprecated alias)
    cs_claim_memo_stamp      <- cs_claim_memo_stamp()          (memo.transition claim)
    cs_action_memo           <- cs_action_memo()               (memo.transition action +
                                                                 session.shape.session_shape_set)
    cs_release_memo_revert   <- cs_release_memo_revert()       (memo.transition release)
    cs_stamp_plan_implemented<- cs_stamp_plan_implemented()    (native in-process call to
                                                                 coordinator_core.ops.plan_status_transition.main,
                                                                 a completed 1:1 port of the
                                                                 node CLI the bash oracle used
                                                                 — no subprocess/node hop)
    cs_gate_recheck_handoff  <- cs_gate_recheck_handoff()      (handoff.transition gate-recheck)
    cs_repark_handoff        <- cs_repark_handoff()            (handoff.transition repark)
    cs_unclaim_handoff       <- cs_unclaim_handoff()           (handoff.transition unclaim;
                                                                 cs_unconsume_handoff is a
                                                                 retained deprecated alias)
    cs_ship_handoff          <- NEW, no bash-oracle predecessor (handoff.archive_transition
                                                                 stamp_only/stamp_shipped modes —
                                                                 closes the shipped_in-without-
                                                                 deployment_state:shipped
                                                                 half-state a standalone
                                                                 stamp_shipped_in() call could
                                                                 leave behind)
    cs_repair_archived_shipped_in <- NEW, no bash-oracle predecessor (calls
                                                                 handoff_stamp._repair_archived_shipped_in_handler
                                                                 directly in-process — a
                                                                 narrow, separate provenance-
                                                                 repair door onto
                                                                 archive/handoffs/ shipped_in,
                                                                 added 2026-07-22 after a corpus
                                                                 audit found 8 archived handoffs
                                                                 with a mis-stamped shipped_in
                                                                 and no lifecycle verb able to
                                                                 reach the archived path)
    cs_repair_archived_deployment_state <- NEW, no bash-oracle predecessor (calls
                                                                 handoff_stamp._repair_archived_deployment_state_handler
                                                                 directly in-process — sibling
                                                                 narrow door onto
                                                                 archive/handoffs/ deployment_state
                                                                 (+ continued_into/closed_reason),
                                                                 added 2026-07-26 after a
                                                                 DoE-claude cross-repo memo
                                                                 reported 13 archived handoffs
                                                                 hand-edited out of stuck
                                                                 deployment_state: in_flight
                                                                 because ship-handoff's
                                                                 state/handoffs/-only containment
                                                                 refuses archive/handoffs/ paths)
    cs_close_handoff         <- NEW, no bash-oracle predecessor (handoff.transition
                                                                 close verb — DR-084
                                                                 human/session-only
                                                                 deployment_state:closed +
                                                                 closed_reason terminal;
                                                                 closes the archive-stamp-cli
                                                                 verb gap that corrupted
                                                                 roadmap-lvv-07, reverted
                                                                 f145480d, 2026-07-25)
    cs_chain_archive_handoff <- NEW, no bash-oracle predecessor (handoff.archive_transition
                                                                 mode='chain' — unconditional
                                                                 live-children-guarded archive
                                                                 move with NO stamp; closes the
                                                                 cockpit §6.2 CLI-reachability
                                                                 gap, 2026-07-23)
    cs_supersede_archive_handoff <- NEW, no bash-oracle predecessor (handoff.archive_transition
                                                                 mode='supersede' — DR-084
                                                                 continued_into stamp + archive
                                                                 move; distinct from the
                                                                 frontmatter-only
                                                                 handoff.transition 'supersede'
                                                                 verb, which never moves the
                                                                 file; closes cockpit §6.2,
                                                                 2026-07-23)

_cc_normalize_dotdot() from the oracle is NOT ported — it existed only to normalize
`lib/../bin/x.js`-shaped paths for bash's non-canonicalizing string ops before a
file-existence probe; Python's pathlib resolves `..` segments natively (Path.resolve()),
so every call site that needed it in bash needs nothing here.

Exit-code contract (mirrors the oracle function-by-function):
    stamp_shipped_in       — 0 on skip-empty-sha or op success; op's own exit_code
                              propagated on failure (oracle's AC7: no silent State-3 mask).
    cs_claim_handoff, cs_claim_memo_stamp, cs_release_memo_revert,
    cs_gate_recheck_handoff, cs_repark_handoff, cs_unclaim_handoff,
    cs_close_handoff        — fail-loud: 2 on usage error, 1 on session-id/op failure
                              (cs_close_handoff: 1 on an invalid/missing closed_reason
                              or a conflicting shipped|continued terminal — see
                              handoff_transition._close docstring), 0 on
                              success/idempotent no-op (matches oracle).
    cs_chain_archive_handoff, cs_supersede_archive_handoff
                            — 2 on usage error (supersede's missing continued_into,
                              caught by this wrapper before the op call), 1 on op
                              failure, 0 on success OR a guard-retained (live-children
                              found / indeterminate) outcome — retention is never an
                              error, mirroring cs_ship_handoff.
    cs_action_memo          — 1 on liveness-gated ownership REFUSE or op failure, 0 on
                              success (matches oracle; ownership-gate warnings are
                              non-fatal, same as the oracle's fail-open rungs).
    cs_stamp_plan_implemented — pure 0/1 contract: the native port's exit code
                              (coordinator_core.ops.plan_status_transition.main) is
                              returned verbatim — 0 on transition-applied or
                              already-at-target/terminal no-op, 1 on error (bad
                              args, missing --plan, plan not found, unparseable
                              frontmatter). No transport-failure path remains —
                              there is no subprocess/node hop left to fail.

Negative-spec:
    - Does NOT reimplement any frontmatter-mutation LOGIC — that lives entirely in the
      already-native ops this module calls. Reimplementing it here would fork two
      sources of truth for the same YAML-write behavior.
    - Does NOT retain the oracle's strangle_route_mutation State-1/2/3 distinction —
      moot once the caller IS the native engine.
    - cs_action_memo's session-shape actioned_memos write has NO native
      `session.record_pickup`-shaped op (that op is scoped ONLY to the pickup write,
      per its own module docstring) — it calls
      `coordinator_core.session.shape.session_shape_set` DIRECTLY IN-PROCESS
      instead (C4, 2026-07-21): the generic session-shape deep-merge writer is
      already natively ported there (field-level merge, mkdir lock, atomic
      write — see that module's docstring), so this module no longer needs a
      `bash -c "source coordinator-session.sh && cs_session_shape_set ..."`
      subprocess bridge. Retired the DoE-root-pointer bash-lib resolution
      (`_bash_lib_path`) and the subprocess wrapper (`_session_shape_set_bridge`)
      that carried it.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.dag import _read_meta
from coordinator_core.git import repo_root as _repo_root_seam
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    split_frontmatter,
)
from coordinator_core.liveness import cs_claim_holder_live
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import plan_status_transition
from coordinator_core.ops.fleet._common import handoff_archive_dest
from coordinator_core.ops.handoff_stamp import _SHIPPED_IN_KIND_ENUM
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.person_resolver import resolve_operating_person
from coordinator_core.reconcile.commit_reality import (
    _DEFAULT_MECHANICAL_DENYLIST,
    _is_mechanical_subject,
)
from coordinator_core.reconcile.policy_loader import load_policy
from coordinator_core.session.shape import session_shape_set
from coordinator_core.shipped_in_tokens import _NO_COMMIT_TOKEN_RE, _SHA_HEX_RE
from coordinator_core.win_portability import no_console_creationflags

# Windows console-flash suppression (DR-054) — routes through the canonical
# primitive.
_NO_CONSOLE = no_console_creationflags()

# shipped_in value grammar (DR-096, 2026-07-26 ruling) — the shape a
# `shipped_in` value may ever take (a resolvable git SHA, or the sanctioned
# substantively-shipped-no-commit:<date> stealth-skip token) is owned by the
# leaf module `coordinator_core.shipped_in_tokens` (import above), not
# defined here — see that module's docstring for why (breaking the
# archive_stamp <-> pickup_assemble <-> session_ledger import cycle,
# state/debt-backlog/DSR-2026-08-13-archive-stamp-import-order-drops-an-op-
# from-the-registry.yaml). `_SHIPPED_IN_KIND_ENUM` is imported from
# `coordinator_core.ops.handoff_stamp` rather than redefined here — a second,
# independently-driftable frozenset copy of the SAME enum is exactly the
# fork-not-share pattern this whole workstream exists to close.

# kind buckets that require an explicit caller-supplied `sha=` override — a
# specific already-known commit (ship-commit/successor) or the no-commit
# sentinel token (no-commit) is, by construction, never something this
# module derives on the caller's behalf. `scope-derived` is the sole kind
# compatible with an OMITTED override (self-derivation via scope-path or
# branch-tip resolution) — see `stamp_shipped_in`'s docstring for the full
# cross-validation this bucketing drives.
_KIND_REQUIRES_OVERRIDE = frozenset({"ship-commit", "successor", "no-commit"})

_SUBPROCESS_TIMEOUT_SEC = 15


# ---------------------------------------------------------------------------
# Shared git/repo-root helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: Optional[Path] = None, timeout: int = _SUBPROCESS_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """subprocess.run wrapper for git — timeout + stdin=DEVNULL on every call (A2)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **_NO_CONSOLE,
    )


def _worktree_root(start_path: Path) -> Optional[Path]:
    """git rev-parse --show-toplevel, resolved from start_path's directory."""
    cwd = start_path if start_path.is_dir() else start_path.parent
    out = _repo_root_seam.show_toplevel(cwd=str(cwd))
    return Path(out) if out else None


def _git_common_dir(worktree_root: Path) -> Optional[Path]:
    """git rev-parse --git-common-dir — the repo_root shape every op in this module
    expects (P9 WORKTREE DERIVATION: <worktree>/.git, worktree-aware for linked
    worktrees)."""
    out = _repo_root_seam.git_common_dir(cwd=str(worktree_root))
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = worktree_root / p
    return p


def _resolve_repo_root_for(target_path: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Returns (worktree_root, common_dir_repo_root) for the repo containing target_path,
    or (None, None) if target_path is not inside a git repo (or git failed/timed out)."""
    worktree = _worktree_root(target_path)
    if worktree is None:
        return None, None
    common_dir = _git_common_dir(worktree)
    return worktree, common_dir


# ---------------------------------------------------------------------------
# stamp_shipped_in — unit1
# ---------------------------------------------------------------------------

def _parse_scope_paths(handoff_path: Path) -> list[str]:
    """Reads scope: paths from the handoff's YAML frontmatter.

    2026-07-22: formerly a hand-rolled line-scanner (bespoke awk-parser port,
    `_SCOPE_ITEM_RE = re.compile(r"^  - (.*)$")`) that recognized ONLY the
    list form (`scope:\\n  - path`) — a bare scalar (`scope: path`) silently
    resolved to an empty list, so `stamp_shipped_in` treated any scalar-scope
    handoff as "no commit found" and left `shipped_in` unstamped, rc 0,
    indistinguishable from a genuine no-match. That gap was invisible only
    because this module's own tests exclusively seeded list-form scope; a
    sibling implementation (ops.session.boot_sweep's now-retired
    `_stamp_shipped_in_besteff`, which read scope via `dag._read_meta` — a
    full stdlib-only frontmatter parse) correctly handled both forms, and
    exposed the gap when the two were merged onto one shared path.

    Delegates to `coordinator_core.dag._read_meta` (a hand-rolled, stdlib-only
    generic YAML-mapping parser already used repo-wide for this exact
    purpose — no PyYAML dependency introduced; `dag.py` itself has none) rather
    than fixing the bespoke scanner in place: this repo already had TWO
    hand-rolled frontmatter readers, and adding scalar-support as a third
    variant of the narrower one would repeat the exact fork-not-share pattern
    that caused the gap. `_read_meta` is cached by (path, content-hash), so
    this also picks up that cache for free on repeat calls.
    """
    meta = _read_meta(str(handoff_path))
    scope_raw = meta.get("scope") or meta.get("scope_paths")
    if not scope_raw:
        return []
    if isinstance(scope_raw, str):
        return [scope_raw]
    if isinstance(scope_raw, list):
        return [str(p) for p in scope_raw if p]
    return []


#: Cap on how many candidate commits the walk-back below is willing to inspect
#: for one scope-path/source-path resolution — a pathspec-scoped `git log`
#: already excludes every commit that never touched the path, so this bounds
#: pathological worst cases (a hot file like a shared tracker touched hundreds
#: of times) without materially affecting the common case of a handful of
#: touches per artifact.
_WALK_BACK_MAX_CANDIDATES = 50


def _mechanical_commit_denylist() -> Sequence[str]:
    """Resolves the mechanical-commit-subject denylist the walk-back below
    consumes to skip housekeeping/archival-machinery commits, sourced from the
    SAME DoE-owned policy YAML `coordinator_core.reconcile.commit_reality`'s own
    three-signal matcher reads (`mechanical_commit_denylist`, via
    `policy_loader.load_policy`) — never a second, independently-driftable copy
    of the token list.

    Falls back to `commit_reality._DEFAULT_MECHANICAL_DENYLIST` whenever the
    loaded policy's list is EMPTY as well as when it is absent: `policy_loader`'s
    own fail-closed `_conservative_policy()` deliberately returns
    `mechanical_commit_denylist: []` on the (expected, steady-state) "policy file
    not yet authored" branch, and an empty denylist here would silently undo the
    whole point of this walk-back on precisely the common case it exists to
    cover — a wrong `shipped_in` is worse than none, so an unauthored policy
    file must still get the code-side default protection, not none at all.
    """
    denylist = load_policy().policy.get("mechanical_commit_denylist")
    if not denylist:
        return _DEFAULT_MECHANICAL_DENYLIST
    return denylist


#: Field/record separators for the single candidate-walk `git log`. ASCII unit
#: (0x1f) and record (0x1e) separators rather than a space split: `%s` is
#: free-form and a Session-Id trailer value is `valueonly`-extracted but still
#: newline-terminated, so neither field is safe to delimit positionally. Same
#: choice `coverage.py` and `session_attribution.py` already make for their own
#: trailer reads.
_SCOPE_LOG_FIELD_SEP = "\x1f"
_SCOPE_LOG_RECORD_SEP = "\x1e"

#: Per-sha commit facts the candidate walk already paid for: `{sha: (committer
#: date `%cI`, raw Session-Id trailer)}`. Populated by `_scope_commit_candidates`
#: and consumed by `_commit_committer_date` / `_commit_session_id`, which fall
#: back to their own `git log -1` ONLY on a miss — a sha resolved outside the
#: scope window (branch-tip fallback, or an explicit `sha=` override) is not in
#: this cache and still costs its own spawn.
#:
#: NEGATIVE SPEC: this is a per-call dict threaded through the call chain, never
#: module-level state. A sha's committer date and trailer are immutable, but a
#: process-lifetime cache would outlive the worktree it was read from — this
#: module is imported by long-lived op handlers serving more than one repo.
ScopeCommitFacts = dict


def _scope_commit_candidates(
    worktree: Path,
    scope_paths: list[str],
    limit: int = _WALK_BACK_MAX_CANDIDATES,
    facts: Optional[ScopeCommitFacts] = None,
) -> list[tuple[str, str]]:
    """Returns up to `limit` (sha, subject) pairs touching `scope_paths`, newest
    first — the raw candidate list `_resolve_scope_sha` walks to find the first
    non-mechanical one.

    When `facts` is supplied it is filled with `{sha: (committer_date, raw
    Session-Id trailer)}` for every candidate in the window, from the SAME `git
    log` that produces the pair list. The committer date and the trailer are
    fields of the record this walk already reads; asking git for them again per
    sha was three process spawns to re-read bytes already in hand (measured
    2026-08-23: `handoff.archive_transition` spent 4 of its 5 spawns resolving
    one `shipped_in`, and on the sampled call resolved nothing).
    """
    if not scope_paths:
        return []
    fmt = _SCOPE_LOG_FIELD_SEP.join(
        ("%H", "%cI", "%(trailers:key=Session-Id,valueonly)", "%s")
    ) + _SCOPE_LOG_RECORD_SEP
    try:
        proc = _run_git(
            ["log", f"--pretty=format:{fmt}", "--no-color", f"-n{limit}", "--", *scope_paths],
            cwd=worktree,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"_scope_commit_candidates: git log failed for {scope_paths} in {worktree}: {exc}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    candidates: list[tuple[str, str]] = []
    for record in proc.stdout.split(_SCOPE_LOG_RECORD_SEP):
        record = record.strip("\r\n")
        if not record:
            continue
        fields = record.split(_SCOPE_LOG_FIELD_SEP)
        if len(fields) != 4:
            continue
        sha, committer_date, session_trailer, subject = fields
        sha = sha.strip()
        if not sha:
            continue
        candidates.append((sha, subject))
        if facts is not None:
            facts[sha] = (committer_date.strip(), session_trailer)
    return candidates


def _resolve_scope_sha(
    worktree: Path, scope_paths: list[str], facts: Optional[ScopeCommitFacts] = None
) -> Optional[str]:
    """The most recent commit touching `scope_paths` whose subject is NOT a
    denylisted mechanical/housekeeping subject (2026-08-05) — walks back through
    up to `_WALK_BACK_MAX_CANDIDATES` touching commits, newest first, and returns
    the first one `_is_mechanical_subject` does not flag; `None` when every
    candidate in the window is mechanical, or when nothing touches `scope_paths`
    at all.

    Origin: a handoff or plan's most recent toucher is very often the fleet-
    archive sweep (`fleet: archive N ... handoff(s)`) or a corpus-wide DR-084
    vocabulary migration, not the artifact's own actual work — confirmed live
    across two real `deliverable.cascade_terminal` drains (both reverted), where
    the overwhelming majority of resolved `shipped_in` values were housekeeping
    commits, including two UNRELATED batons resolving to the SAME bulk-migration
    sha. This is a resolution-algorithm change, not a guard: unlike the
    ownership/co-commit/not_after guards elsewhere in this module (whose
    documented structural tooth is refuse-only, never re-select), walking back
    to a DIFFERENT, real candidate is exactly what this fix calls for — a
    refuse-only response here would leave every mechanically-touched artifact
    permanently unresolvable even when its genuine ship commit is one commit
    further back.
    """
    candidates = _scope_commit_candidates(worktree, scope_paths, facts=facts)
    if not candidates:
        return None
    denylist = _mechanical_commit_denylist()
    for sha, subject in candidates:
        if not _is_mechanical_subject(subject, denylist):
            return sha
    return None


def _scope_paths_have_uncommitted_changes(worktree: Path, scope_paths: list[str]) -> bool:
    """True when `scope_paths` currently carry ANY uncommitted change (staged,
    unstaged, or untracked) in `worktree` — the structural signature of the
    2026-07-26 defect: `_resolve_scope_sha` only ever sees git history that
    ALREADY exists, so when the stamp write is about to be swept into a
    not-yet-made commit alongside the very ship it is meant to record, "most
    recent commit touching scope" is necessarily the commit BEFORE that
    pending one — confirmed live as a handoff stamped 49 seconds before its
    actual ship commit. Fail-closed on a git error/timeout (treated as
    uncommitted): unable to establish a clean state is "I do not know",
    which routes to refuse under this module's ownership-guard invariant,
    never to assumed-clean.

    Scope: called ONLY when `scope_paths` is non-empty (i.e. `_resolve_scope_sha`
    produced a genuine scope-path-derived candidate) — never for the
    `allow_branch_tip_fallback` branch-tip resolution, whose own semantics
    this fix must not touch (see `stamp_shipped_in`'s Negative-spec / HARD
    CONSTRAINTS)."""
    try:
        proc = _run_git(
            ["--no-optional-locks", "status", "--porcelain", "--", *scope_paths], cwd=worktree
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"_scope_paths_have_uncommitted_changes: git status failed for "
            f"{scope_paths} in {worktree}: {exc}",
            file=sys.stderr,
        )
        return True
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO-8601 timestamp (the shape `advanced_at`/cascade `at` params
    already carry) into a timezone-aware `datetime`, or None when `value` is
    empty/unparseable. A bare `Z` suffix is translated to `+00:00` (Python's
    `datetime.fromisoformat` predates PEP 616's `Z` support on the 3.10 floor
    this repo targets); a naive result (no offset in the source string) is
    assumed UTC rather than left ambiguous, matching this module's own
    `_iso_now()`-produced values elsewhere."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _commit_committer_date(
    worktree: Path, sha: str, facts: Optional[ScopeCommitFacts] = None
) -> Optional[datetime]:
    """Committer date (timezone-aware) for `sha`, or None when unresolvable/
    unparseable/timed out. Deliberately committer date (`%cI`), not author
    date (`%aI`) — the committer date is the honest "when did this land"
    signal for a rebased/cherry-picked/amended commit, matching the same
    choice `git log --format=%H` elsewhere in this module already implies by
    ordering on commit recency rather than authorship."""
    if facts is not None and sha in facts:
        return _parse_iso_timestamp(facts[sha][0])
    try:
        proc = _run_git(["log", "-1", "--format=%cI", sha], cwd=worktree)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"_commit_committer_date: git log failed for {sha} in {worktree}: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    return _parse_iso_timestamp(proc.stdout.strip())


def _scope_sha_postdates_trigger(
    worktree: Path,
    sha: str,
    not_after: Optional[str],
    facts: Optional[ScopeCommitFacts] = None,
) -> bool:
    """True when `sha` cannot be plausible evidence because it postdates
    `not_after` — the cascade's own trigger timestamp (`advanced_at`, per
    `deliverable_cascade._advance_one`) — or because either date is
    unparseable/unresolvable while `not_after` WAS supplied (fail-closed:
    "I cannot verify this is plausible" routes to refuse, same discipline as
    `_scope_paths_have_uncommitted_changes`). Returns False (no postdate
    objection) when `not_after` is None/blank — a caller that never supplies
    a temporal anchor gets this guard's prior no-op behaviour; every existing
    caller of `stamp_shipped_in` before this param existed is unaffected.

    At-minimum guard (2026-08-04): closes one concrete shape of the
    directory-granularity false-positive `_resolve_scope_sha` is structurally
    prone to — a scope: entry naming a coarse directory can resolve to the
    most recent EXISTING commit touching that directory regardless of
    whether that commit has anything to do with the candidate's own work,
    and the existing ownership guard does not catch it when the unrelated
    commit happens to be the calling session's own (that guard exists to
    stop stamping a PEER session's commit, not to second-guess the caller's
    own recent-but-unrelated one). This is a floor, not a complete defence —
    see `resolve_source_ship_sha`'s docstring for the complementary,
    stronger fix (prefer the cascade's actual firing source over a
    scope-derived guess whenever one is available)."""
    if not not_after:
        return False
    trigger_dt = _parse_iso_timestamp(not_after)
    if trigger_dt is None:
        return True
    commit_dt = _commit_committer_date(worktree, sha, facts=facts)
    if commit_dt is None:
        return True
    return commit_dt > trigger_dt


def _handoff_created_field(handoff_path: Path) -> Optional[str]:
    """Reads the `created:` frontmatter field off `handoff_path` via `_read_meta`
    (same generic reader `_parse_scope_paths` already uses for `scope:` — no
    third hand-rolled frontmatter reader). `None` when absent/unparseable."""
    meta = _read_meta(str(handoff_path))
    created = meta.get("created")
    return str(created).strip() if created else None


def _scope_sha_predates_creation(
    worktree: Path,
    sha: str,
    created: Optional[str],
    facts: Optional[ScopeCommitFacts] = None,
) -> bool:
    """True when `sha` cannot be plausible SCOPE-DERIVED evidence because it
    predates the handoff's own `created` frontmatter field — or because either
    date is unparseable/unresolvable while `created` WAS supplied (fail-closed,
    same discipline as `_scope_sha_postdates_trigger`). Returns False (no
    predate objection) when `created` is None/blank.

    Origin (2026-08-05): a real cascade drain (reverted) stamped
    `2026-07-29-windows-verify-debash-surface.md` (created 2026-07-29) with a
    commit dated 2026-07-23 — six days before the handoff existed. A commit
    cannot ordinarily be the ship commit for scope-path evidence that predates
    the artifact it is meant to document, so a scope-derived candidate this
    implausible is refused rather than stamped.

    SCOPE-DERIVED path only (never `resolve_source_ship_sha`'s source-artifact
    path, and never the branch-tip fallback, mirroring the co-commit/not_after
    guards' own scope restriction): `resolve_source_ship_sha` resolves the
    commit that shipped a DIFFERENT artifact — the plan or handoff whose own
    terminal transition FIRED the cascade — which legitimately, and often,
    predates the candidate handoff it is being used to stamp (a reconcile-style
    baton is written specifically to document already-shipped work after the
    fact). Applying this same lower bound there would refuse that entire
    legitimate class rather than the false-positive class it targets, trading
    one false-refuse hazard for a different, more common one; per this guard's
    own refuse-only discipline (never re-select, only ever turn a write into a
    non-write), that tradeoff is the wrong one to make silently, so this bound
    is intentionally NOT applied on the source-derived path."""
    if not created:
        return False
    created_dt = _parse_iso_timestamp(created)
    if created_dt is None:
        return True
    commit_dt = _commit_committer_date(worktree, sha, facts=facts)
    if commit_dt is None:
        return True
    return commit_dt < created_dt


def _resolve_branch_tip_sha(worktree: Path) -> Optional[str]:
    try:
        proc = _run_git(["log", "--format=%H", "-n1"], cwd=worktree)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"_resolve_branch_tip_sha: git log failed in {worktree}: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


# ---------------------------------------------------------------------------
# Ownership guard (2026-07-22) — never stamp a DERIVED sha that cannot be
# established as the calling session's. Discriminant mechanism mirrors
# coordinator_core.coverage.py:1017-1040's Session-Id trailer extraction +
# UUID-shape validation (that module is not imported here — it is off-limits
# to touch/couple for this fix, so the same small, well-tested mechanism is
# replicated rather than shared cross-module; the fidelity guard is copied
# verbatim, not reinvented).
# ---------------------------------------------------------------------------

_SESSION_ID_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$")


def _commit_session_id(
    worktree: Path, sha: str, facts: Optional[ScopeCommitFacts] = None
) -> Optional[str]:
    """Extracts and UUID-shape-validates `sha`'s Session-Id trailer, or None if
    absent/malformed/unreadable (mirrors coverage.py's fidelity guard 1 —
    a malformed trailer must never be treated as a match)."""
    if facts is not None and sha in facts:
        sid = facts[sha][1].strip().strip("\r\n")
        if not sid or not _SESSION_ID_UUID_RE.match(sid):
            return None
        return sid
    try:
        proc = _run_git(
            ["log", "-1", "--format=%(trailers:key=Session-Id,valueonly)", sha],
            cwd=worktree,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    sid = proc.stdout.strip().strip("\r\n")
    if not sid or not _SESSION_ID_UUID_RE.match(sid):
        return None
    return sid


def _ownership_block_reason(
    resolve_worktree: Path,
    resolved_sha: str,
    facts: Optional[ScopeCommitFacts] = None,
) -> Optional[str]:
    """Returns None when `resolved_sha` is established as the CALLING session's own
    commit (safe to stamp); otherwise a distinct, human-readable reason the write
    must be skipped instead.

    THE INVARIANT: never stamp a sha that cannot be established as the calling
    session's. Missing/unresolvable/malformed information on EITHER side (the
    caller's own session id, or the candidate commit's trailer) means "I do not
    know" — routes to indeterminate-then-unset, NEVER to assumed-mine. Fail-open
    here is precisely the 2026-07-22 incident (a sibling `ship-handoff` stamped a
    concurrent peer session's sha) reproduced in an automated/CI/hook context,
    where it is least observable.

    Scope: called ONLY for a DERIVED sha (scope-path resolution or
    `allow_branch_tip_fallback`'s branch-tip resolution) — NEVER for a
    caller-supplied `sha` override. An explicit override is the caller's own
    assertion of ownership; that assertion is the entire point of the override
    and this guard does not second-guess it.

    Structural tooth (do not soften): this function may only ever turn a write
    into a non-write. It never selects a different sha, and never walks history
    looking for one — a guard that searches for a better candidate has become
    the correction walk Position A deleted (see handoff_archive_transition.py's
    module docstring, "Position A: no branch-tip fallback, no Session-Id
    trailer-correction walk"). There is no repair path here, only a refusal path.
    """
    caller_sid = resolve_current_session_id(resolve_worktree)
    if not caller_sid:
        return (
            "caller session-id unresolvable (none of COORDINATOR_SESSION_ID/"
            "CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID set in the environment) — "
            "cannot establish ownership of anything, so nothing is safe to stamp"
        )
    candidate_sid = _commit_session_id(resolve_worktree, resolved_sha, facts=facts)
    if candidate_sid is None:
        return (
            f"ownership unestablished for {resolved_sha[:8]} — candidate commit has "
            "no valid Session-Id trailer (missing or malformed)"
        )
    if candidate_sid != caller_sid:
        return (
            f"{resolved_sha[:8]} belongs to a different session ({candidate_sid}), "
            f"not the caller's ({caller_sid}) — refusing to stamp a peer's commit"
        )
    return None


@dataclass(frozen=True)
class StampOutcome:
    """Envelope returned by `stamp_shipped_in` in place of the bare rc int it
    returned before 2026-07-28 (§ S11, `docs/plans/2026-07-28-handoff-close-path-
    fail-loud.md`, chunk C0). A bare rc cannot answer "was the value already
    there the SAME as what the caller supplied" — `shipped_in` is stored
    `resolved[:8]` (`_final_stamp_value`) while callers hold full-length SHAs, so
    a before/after disk string diff declares "different" on every legitimate
    same-commit re-stamp. Only this envelope's `prior_value`, compared
    canonically (prefix-of, case-insensitive) against a caller's full SHA,
    supports that comparison correctly.

    exit_code   (int)       — 0 ok (write, force-replace, or a guard/validation
                               skip); 1 error (bad params, malformed override,
                               unresolvable worktree, or the underlying
                               handoff.stamp op's own non-zero exit).
    applied     (bool)      — True only when shipped_in was actually inserted
                               or force-replaced this call.
    replaced    (bool)      — True only when `applied` AND an existing value
                               was overwritten (force=True path).
    prior_value (str|None)  — the value that was already present, when known
                               (idempotent-skip or force-replace paths); None
                               when nothing was ever present, or when this
                               envelope comes from an early guard/validation
                               exit that never reached the underlying op.
    error       (str|None)  — human-readable reason, set on exit_code 1.
    message     (str|None)  — human-readable outcome description, set on some
                               exit_code 0 paths (mirrors the underlying
                               handoff.stamp op's own `message` field). Review:
                               code-reviewer (nit F5) — plumbed through and
                               currently unread by every caller in this diff
                               (do_stamp/do_stamp_only, boot_sweep's
                               _stamp_shipped_in_besteff); it exists for
                               future/other callers that want to surface it,
                               not a dropped consumer.
    """

    exit_code: int
    applied: bool = False
    replaced: bool = False
    prior_value: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


def _read_current_shipped_in(handoff_path: str) -> Optional[str]:
    """Read the current (unquoted) `shipped_in` value straight off disk, or None.

    Fallback ONLY — `handoff.stamp`'s own idempotent-skip branch
    (`coordinator_core/ops/handoff_stamp.py:192-196`, untouched by this pass;
    see `stamp_shipped_in`'s Negative-spec) never populates its own
    `prior_value` when `existing is not None and not force` — it returns the
    unchanged text with `applied: False`, `prior_value: None`, because that
    branch was written before any caller needed to know WHAT the retained
    value was, only THAT nothing changed. Reading the frontmatter directly is
    safe here specifically because a skip is, by construction, a non-write:
    the value on disk after the op call is identical to the value before it.
    Mirrors `handoff_archive_transition._current_shipped_in`'s same read
    (independent implementation — this module does not import that one, to
    avoid the ops-package import-cycle `stamp_shipped_in`'s own docstring
    documents for `_stamp_handler`)."""
    try:
        # Review: code-reviewer (nit F4) — UnicodeDecodeError (a ValueError
        # subclass, not an OSError) on non-UTF-8 bytes must also degrade to
        # None per this function's "None on unreadable" contract; this sits
        # directly on the AC6/AC7 refusal-vs-noop decision path.
        text = Path(handoff_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    val = read_fm_field_unquoted(split.fm_text, "shipped_in")
    return val if val not in (None, "null", "") else None


def _final_stamp_value(resolved: str) -> str:
    """Storage-form transform applied to a value about to be written as
    `shipped_in` — truncates a hex SHA to the module's own 8-char format
    contract (unchanged from before this pass), but leaves the sanctioned
    no-commit token VERBATIM: `resolved[:8]` on
    `"substantively-shipped-in-no-commit:2026-07-26"` would silently corrupt
    it to `"substant"`, which is exactly the kind of second-format-divergence
    this module exists to foreclose (see `handoff_stamp._repair_archived_shipped_in_handler`'s
    own truncate-after-shape-validation note for the sibling instance of this
    same discipline)."""
    if _NO_COMMIT_TOKEN_RE.fullmatch(resolved):
        return resolved
    return resolved[:8]


def stamp_shipped_in(
    handoff_path: str,
    *,
    kind: str,
    allow_branch_tip_fallback: bool = False,
    sha: Optional[str] = None,
    force: bool = False,
    worktree: Optional[Path] = None,
    not_after: Optional[str] = None,
) -> StampOutcome:
    """Resolve the most recent git SHA touching the handoff's scope: paths, then insert
    `shipped_in: <SHA8>` into the frontmatter via the native handoff.stamp op.

    kind: REQUIRED, keyword-only, no default (DR-096, DoE-claude 2026-07-26 ruling —
    "kind must be REQUIRED at the seam, not defaulted silently. A caller that does not
    say which kind it is writing is exactly how this field acquired five meanings.").
    This function is the SINGLE choke point that owns the `shipped_in` value grammar —
    both the shape a value may take (a 7-64 hex SHA, or the sanctioned
    `substantively-shipped-no-commit:<date>` token) AND which `kind` may pair with which
    shape. One of `ship-commit | successor | scope-derived | no-commit`
    (`coordinator_core.ops.handoff_stamp._SHIPPED_IN_KIND_ENUM`, imported not
    redefined — a second copy of this enum is the exact fork this pass exists to close).

    Cross-validation between `kind` and the `sha` override (BEFORE any resolution runs):
      - `kind in {"ship-commit", "successor"}` REQUIRES a non-empty `sha` override that
        shape-validates as a hex SHA. These two kinds mean "the caller already has a
        specific, known commit in hand" — never something this function derives.
      - `kind == "no-commit"` REQUIRES a non-empty `sha` override that shape-validates
        as the sanctioned no-commit token — the caller is asserting there genuinely is
        no ship commit, not asking this function to go looking for one.
      - `kind == "scope-derived"` REQUIRES the `sha` override be ABSENT (empty/whitespace
        normalizes to absent, matching existing behavior) — this is the self-derivation
        path (`_resolve_scope_sha` / `allow_branch_tip_fallback`'s branch-tip resolution).
        A `sha` override paired with `kind="scope-derived"` is a caller contradicting
        itself ("here is a specific commit I have in hand" + "please derive one for me")
        and is rejected fail-loud (return 1, no write) rather than silently picking one
        meaning over the other.
    Any mismatch above, or a `kind` outside the enum, or a malformed `sha` override
    (neither hex-SHA-shaped nor no-commit-token-shaped) fails loud (return 1, no write) —
    no combination is silently coerced into a different one.

    --allow-branch-tip-fallback: when no scope-path commit is found, fall back to the
    branch-tip SHA. ONLY correct for ceremony-complete call sites — mirrors the oracle's
    documented restriction verbatim (do NOT set from an orphan-sweep caller). Only
    reachable with `kind="scope-derived"` (see cross-validation above) — the branch tip is
    itself a self-derived value, not a caller assertion.

    sha: optional caller-supplied SHA override (or, when `kind="no-commit"`, the
    no-commit token). When supplied (non-empty after `.strip()`), bypasses
    `_resolve_scope_sha` AND the branch-tip fallback entirely — the supplied value
    is ALWAYS stamped, never subject to the "nothing resolved" no-op skip below.
    Intended caller: an orphan-sweep/crash-recovery caller that has already
    independently derived the correct ship SHA — a value NOT derivable from the
    handoff's own scope-paths. When omitted, or an empty/whitespace-only string,
    behavior is unchanged from the self-derive path (normalized to None, falls
    through to `_resolve_scope_sha`/branch-tip fallback) — and REQUIRES
    `kind="scope-derived"`, per the cross-validation above. A supplied override
    MUST be 7-64 hex characters (a valid abbreviated-or-full git SHA shape,
    widened from 7-40 to match the ratified schema pattern — a SHA-256 repo's id
    can run past 40 hex chars) or the sanctioned no-commit token, or the call
    fails loud (return 1, no write) — see Finding 1,
    state/review-trail/findings/2026-07-21-codereview-slicearchive-stamp-sha-override-*.md.

    force: provenance-repair escape (added 2026-07-22, incident: a sibling `ship-handoff`
    call stamped a concurrent peer session's sha into `shipped_in`, and `sha=` alone could
    not repair it — the downstream handoff.stamp op's own idempotency guard silently
    no-ops when `shipped_in` is already present, regardless of this function's own
    no-op branch). When True, an already-stamped `shipped_in` (and, when `kind` is
    supplied, `shipped_in_kind` in lockstep) is REPLACED instead of skipped, and the
    op's response reports the prior value(s) for auditability.

    Negative-spec: force=True REQUIRES a non-empty `sha` override — force never triggers
    `_resolve_scope_sha`/branch-tip resolution of its own. A force-overwrite that then
    resolves a sha is the exact hazard that caused the incident this escape repairs, so
    that combination fails loud (return 1, no write) rather than silently resolving.

    worktree: optional override for WHERE the scope-path/branch-tip git-log resolution
    runs (added 2026-07-22, merging in the formerly-duplicated
    ops.session.boot_sweep._stamp_shipped_in_besteff resolution). Two-repo callers
    (_STATE_REPO != GIT_ROOT) must resolve scope-path commits against GIT_ROOT — the
    repo the scope: paths are relative to — even though `handoff_path` itself lives in
    the STATE repo. When None (default, every existing caller), resolution runs against
    the worktree auto-derived from `handoff_path`'s own location — behavior unchanged.
    This override affects resolution ONLY: the handoff.stamp op's write target and
    containment check are still derived from `handoff_path` itself, never from this
    override — a caller cannot use `worktree` to redirect where the frontmatter write
    lands.

    Ownership guard (2026-07-22, the P1 fix this repair path exists for): a DERIVED
    sha (scope-path OR branch-tip resolution — never an explicit `sha` override) is
    stamped ONLY when it can be established as the CALLING session's own commit via
    its Session-Id trailer (see `_ownership_block_reason`). THE INVARIANT: never
    stamp a sha that cannot be established as the calling session's; when ownership
    cannot be established (unresolvable caller session id, missing/malformed trailer,
    or a trailer belonging to a different session), `shipped_in` is left UNSET, a
    distinct warning is printed, and this returns 0 — NEVER a hard failure. Negative-
    spec, verbatim, the categorical line between this guard and the machinery
    Position A deleted: the guard may only ever turn a write into a non-write. It may
    NEVER select a different SHA, and never walks history looking for one. A guard
    that searches for a better candidate has become the correction walk Position A
    deleted. This guard, the co-commit guard below, and the kind/sha cross-validation
    above are ALL refuse-only, structural-tooth guards — none of them softens under
    this pass; `kind` is validated ADDITIONALLY, never as a replacement for any of them.

    Co-commit guard (2026-07-26): a scope-path-derived sha (never the branch-tip
    fallback, whose own semantics are untouched by this fix) is refused when
    `scope_paths` themselves still carry uncommitted worktree changes — see
    `_scope_paths_have_uncommitted_changes`'s docstring. `_resolve_scope_sha`
    only ever sees git history that ALREADY exists, so when this write is about
    to be swept into a not-yet-made commit alongside the very ship it is meant
    to record, the resolved sha is necessarily the commit BEFORE that pending
    one — confirmed live as a handoff stamped 49 seconds before its actual ship
    commit. Same structural tooth as the ownership guard: refuse-only, never a
    re-resolve or a history walk for a better candidate.

    not_after (2026-08-04): optional ISO-8601 upper bound — a scope-path-derived
    sha (same restriction as the co-commit guard: never the branch-tip fallback)
    whose committer date postdates `not_after` is refused rather than stamped.
    See `_scope_sha_postdates_trigger`'s docstring for the incident this closes
    (directory-granularity `scope:` resolving to the most recent unrelated
    commit in that tree, which the ownership guard alone does not catch when
    that commit happens to be the calling session's own). None (default)
    preserves prior behaviour for every existing caller — this param did not
    exist before this pass. Same structural tooth as every other guard in this
    docstring: refuse-only, never a re-resolve.

    Returns a `StampOutcome` envelope (see that dataclass's docstring for field
    semantics) — NOT a bare rc int (retired 2026-07-28, § S11). `exit_code` is 0
    when sha resolution finds nothing, when the ownership guard refuses a derived
    sha (stamping is skipped either way — matches the oracle's "if sha is empty,
    exit 0" contract), when the co-commit guard refuses a scope-path-derived sha
    whose scope paths are still uncommitted, or on op success (insert, force-
    replace, or idempotent skip); 1 on a malformed override, an invalid force/sha
    combination, an unknown `kind`, a `kind`/`sha` combination that fails the
    cross-validation above, or the op's own non-zero exit_code on failure (AC7 —
    no silent State-3-equivalent mask). `applied`/`replaced`/`prior_value` are
    populated from the underlying op's own envelope on the success path, and
    default False/False/None on every early guard/validation exit — a caller
    that needs to compare "already-present" against its own supplied sha reads
    `prior_value`, never a before/after disk diff.
    """
    if kind not in _SHIPPED_IN_KIND_ENUM:
        print(
            f"stamp_shipped_in: rejected unknown kind {kind!r} — must be one of "
            f"{sorted(_SHIPPED_IN_KIND_ENUM)}",
            file=sys.stderr,
        )
        return StampOutcome(exit_code=1, error=f"unknown kind {kind!r}")

    hpath = Path(handoff_path)
    derived_worktree, repo_root = _resolve_repo_root_for(hpath)
    if derived_worktree is None or repo_root is None:
        print(f"stamp_shipped_in: could not resolve git worktree for {handoff_path}", file=sys.stderr)
        return StampOutcome(exit_code=1, error=f"could not resolve git worktree for {handoff_path}")
    resolve_worktree = worktree if worktree is not None else derived_worktree

    override = sha.strip() if sha else None

    if force and not override:
        print(
            "stamp_shipped_in: rejected force=True without an explicit sha override — "
            "force must never trigger its own resolution (see Negative-spec)",
            file=sys.stderr,
        )
        return StampOutcome(exit_code=1, error="force=True without an explicit sha override")

    if override:
        is_no_commit_token = bool(_NO_COMMIT_TOKEN_RE.fullmatch(override))
        is_hex = bool(_SHA_HEX_RE.fullmatch(override))
        if not (is_no_commit_token or is_hex):
            print(
                f"stamp_shipped_in: rejected malformed sha override {override!r} "
                "(expected 7-64 hex chars, or the sanctioned "
                "substantively-shipped-no-commit:<YYYY-MM-DD> token)",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=1, error=f"malformed sha override {override!r}")
        if kind == "no-commit" and not is_no_commit_token:
            print(
                f"stamp_shipped_in: rejected kind='no-commit' paired with a "
                f"hex-shaped sha override {override!r} — kind='no-commit' requires "
                "the sha param to CARRY the sanctioned no-commit token, not a "
                "commit id",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=1, error=f"kind='no-commit' paired with hex-shaped sha override {override!r}")
        if kind in ("ship-commit", "successor") and not is_hex:
            print(
                f"stamp_shipped_in: rejected kind={kind!r} paired with the "
                f"no-commit token {override!r} — {kind!r} requires a hex sha "
                "override (a specific commit the caller has in hand), not the "
                "no-commit sentinel",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=1, error=f"kind={kind!r} paired with the no-commit token {override!r}")
        if kind == "scope-derived":
            print(
                f"stamp_shipped_in: rejected kind='scope-derived' paired with an "
                f"explicit sha override {override!r} — 'scope-derived' means "
                "self-derivation from scope: paths / branch-tip; a caller "
                "supplying its own sha wants kind='ship-commit' or 'successor' "
                "instead",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=1, error=f"kind='scope-derived' paired with an explicit sha override {override!r}")
        resolved = override
    else:
        if kind != "scope-derived":
            print(
                f"stamp_shipped_in: rejected kind={kind!r} with no sha override — "
                "only kind='scope-derived' may omit sha (self-derivation via "
                "scope: paths / branch-tip); 'ship-commit', 'successor', and "
                "'no-commit' all require an explicit sha override",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=1, error=f"kind={kind!r} with no sha override")
        scope_paths = _parse_scope_paths(hpath)
        scope_facts: ScopeCommitFacts = {}
        resolved = _resolve_scope_sha(resolve_worktree, scope_paths, facts=scope_facts)
        # Co-commit guard (2026-07-26): a scope-path-derived sha can never be
        # the ship commit while the ship itself is still sitting uncommitted
        # in the worktree — see `_scope_paths_have_uncommitted_changes`'s
        # docstring. Refuse-only, mirroring `_ownership_block_reason`'s own
        # structural tooth: this never re-resolves or walks history for a
        # better candidate, it only turns the write into a non-write.
        if resolved is not None and scope_paths and _scope_paths_have_uncommitted_changes(
            resolve_worktree, scope_paths
        ):
            print(
                f"stamp_shipped_in: co-commit guard — leaving shipped_in unset for "
                f"{handoff_path}: scope path(s) {scope_paths} have uncommitted "
                f"changes, so {resolved[:8]} (the most recent EXISTING commit "
                "touching scope) cannot be the ship this write is meant to record",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=0)
        # not_after guard (2026-08-04): same scope restriction as the co-commit
        # guard immediately above (scope-path-derived only, never branch-tip) —
        # see `_scope_sha_postdates_trigger`'s docstring for the structural
        # tooth (refuse-only, never re-select) and the incident it closes.
        if resolved is not None and scope_paths and _scope_sha_postdates_trigger(
            resolve_worktree, resolved, not_after, facts=scope_facts
        ):
            print(
                f"stamp_shipped_in: not-after guard — leaving shipped_in unset for "
                f"{handoff_path}: {resolved[:8]} (the most recent EXISTING commit "
                f"touching scope) postdates the cascade trigger ({not_after}) and "
                "cannot be evidence for a cascade that fired at or before that time",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=0)
        # created-date lower bound (2026-08-05): same scope restriction as the
        # co-commit/not_after guards immediately above (scope-path-derived
        # only, never branch-tip, never resolve_source_ship_sha's own
        # source-artifact path — see `_scope_sha_predates_creation`'s docstring
        # for why the source-derived path deliberately does NOT get this bound)
        # — see that docstring for the structural tooth (refuse-only, never
        # re-select) and the incident it closes.
        if resolved is not None and scope_paths and _scope_sha_predates_creation(
            resolve_worktree, resolved, _handoff_created_field(hpath), facts=scope_facts
        ):
            print(
                f"stamp_shipped_in: created-date guard — leaving shipped_in unset for "
                f"{handoff_path}: {resolved[:8]} (the most recent EXISTING non-mechanical "
                "commit touching scope) predates this handoff's own created date and "
                "cannot be the ship commit for work that did not yet exist",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=0)
        if resolved is None and allow_branch_tip_fallback:
            resolved = _resolve_branch_tip_sha(resolve_worktree)
        if resolved is None:
            # Documented oracle-parity no-op (see docstring: "matches the
            # oracle's 'if sha is empty, exit 0' contract") — but previously
            # silent, so a direct `archive-stamp-cli stamp-shipped-in` caller
            # saw exit 0 with no signal that shipped_in was left unset.
            # handoff.archive_transition callers already surface this case
            # via their own before/after comparison (see that module's
            # "resolved no commit for ... scope: paths" warning); this print
            # covers the standalone-CLI call path that has no such wrapper.
            print(
                f"stamp_shipped_in: no commit found for {handoff_path}'s scope: "
                "paths (allow_branch_tip_fallback="
                f"{allow_branch_tip_fallback}) — shipped_in left unset",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=0)
        # Ownership guard — DERIVED sha only (scope-path AND branch-tip
        # resolution both funnel through here); never applied to `override`
        # above. See _ownership_block_reason's docstring for the invariant and
        # the structural tooth (refuse-only, never re-select).
        block_reason = _ownership_block_reason(resolve_worktree, resolved, facts=scope_facts)
        if block_reason is not None:
            print(
                f"stamp_shipped_in: ownership guard — leaving shipped_in unset for "
                f"{handoff_path}: {block_reason}",
                file=sys.stderr,
            )
            return StampOutcome(exit_code=0)

    from coordinator_core.ops.handoff_stamp import _handler as _stamp_handler

    stamp_params = {
        "handoff_path": handoff_path,
        "sha": _final_stamp_value(resolved),
        "kind": kind,
    }
    if force:
        stamp_params["force"] = True
    result = asyncio.run(_stamp_handler(stamp_params, repo_root=repo_root))
    rc = int(result.get("exit_code", 1))
    prior_value = result.get("prior_value")
    if rc == 0 and not result.get("applied") and prior_value is None:
        # The op's own idempotent-skip branch (handoff.stamp, untouched by
        # this pass) never populates prior_value on the true "already present,
        # not forced" no-op — only its force-replace branch does. Fall back
        # to a direct disk read, safe here because a skip is a non-write (see
        # _read_current_shipped_in's docstring) — this is what makes AC6's
        # refusal predicate evaluable at all.
        prior_value = _read_current_shipped_in(handoff_path)
    outcome = StampOutcome(
        exit_code=rc,
        applied=bool(result.get("applied", False)),
        replaced=bool(result.get("replaced", False)),
        prior_value=prior_value,
        error=result.get("error"),
        message=result.get("message"),
    )
    if rc != 0:
        print(
            f"stamp_shipped_in: WARNING stamp op failed (exit {rc}) for {handoff_path}: "
            f"{result.get('error', '')}",
            file=sys.stderr,
        )
    elif outcome.replaced:
        print(
            f"stamp_shipped_in: force-replaced shipped_in for {handoff_path} "
            f"(was {outcome.prior_value!r}, now {resolved[:8]})",
            file=sys.stderr,
        )
    return outcome


# ---------------------------------------------------------------------------
# resolve_source_ship_sha — source-artifact-derived shipped_in evidence
# ---------------------------------------------------------------------------


def resolve_source_ship_sha(
    source_path: str,
    *,
    not_after: Optional[str] = None,
    worktree: Optional[Path] = None,
) -> Optional[str]:
    """Resolves the commit that landed `source_path`'s own most recent change —
    the honest "what shipped THIS" answer for a cascade's firing source
    artifact (the plan or handoff whose terminal transition fired
    `deliverable.cascade_terminal`), as opposed to Position A's baton-`scope:`
    self-derivation (`stamp_shipped_in`'s `kind="scope-derived"` path).

    Origin (2026-08-04): `deliverable_cascade._advance_one` had ONLY
    scope-derived evidence available — a baton whose own `scope:` paths never
    happened to intersect any commit left `shipped_in` unresolvable even
    though the cascade itself was fired BY a terminal artifact (a plan
    reaching `status: implemented`, or an archived terminal handoff) that
    unambiguously DOES have a ship commit. This function lets a caller use
    that already-known firing source as the PRIMARY evidence, falling back to
    Position A only when this returns None.

    Reuses the SAME refuse-only plausibility machinery `stamp_shipped_in`'s
    own scope-derived path applies (co-commit guard + `not_after` postdate
    guard, see `_scope_paths_have_uncommitted_changes` / `_scope_sha_postdates_trigger`)
    — a source commit that is still sitting uncommitted, or that postdates
    `not_after`, is refused (returns None) exactly like a scope-derived
    candidate would be, never silently accepted. Same structural tooth as
    every guard in this module: refuse-only, never a re-resolve or a history
    walk for a better candidate.

    Deliberately independent of the ownership guard (`_ownership_block_reason`):
    the source artifact's own ship commit legitimately belongs to WHATEVER
    session landed it — almost never the session currently running the
    cascade — so ownership-gating it would refuse the overwhelming common
    case. A caller that stamps this value onto `shipped_in` does so via an
    explicit `sha=` override (`kind="ship-commit"`, per DR-096's taxonomy:
    "the caller already has a specific, known commit in hand"), which
    `stamp_shipped_in`'s own cross-validation already treats as a caller
    assertion the ownership guard never second-guesses.

    Mechanical/housekeeping-commit exclusion (2026-08-05): also inherits
    `_resolve_scope_sha`'s walk-back — `source_path` (a plan or handoff) is
    routinely re-touched by the fleet-archive sweep or a corpus-wide vocabulary
    migration AFTER its own genuine ship commit, so "the single most recent
    commit touching this path" was resolving to that housekeeping commit far
    more often than to the actual ship; the walk-back returns the first
    NON-mechanical toucher instead (`None` if every touch in the window is
    mechanical), rather than a change to THIS function.

    Deliberately does NOT apply `stamp_shipped_in`'s scope-derived
    created-date lower bound (`_scope_sha_predates_creation`) — see that
    function's own docstring for why: this function resolves the ship commit
    for a DIFFERENT artifact than the one ultimately being stamped, and a
    reconcile-style baton legitimately documents already-shipped work whose
    firing source's own ship commit can predate the baton's `created` by
    construction.

    Returns None when `source_path` is empty, its git worktree is
    unresolvable, it has no non-mechanical commit history of its own, it still
    carries uncommitted changes, or its resolved commit postdates `not_after`
    — every case routes the caller back to Position A scope-derived
    resolution, never to a hard failure.
    """
    if not source_path:
        return None
    hpath = Path(source_path)
    derived_worktree, _ = _resolve_repo_root_for(hpath)
    resolve_worktree = worktree if worktree is not None else derived_worktree
    if resolve_worktree is None:
        return None
    source_facts: ScopeCommitFacts = {}
    resolved = _resolve_scope_sha(resolve_worktree, [source_path], facts=source_facts)
    if resolved is None:
        return None
    if _scope_paths_have_uncommitted_changes(resolve_worktree, [source_path]):
        print(
            f"resolve_source_ship_sha: co-commit guard — {source_path} carries "
            "uncommitted changes, so its most recent EXISTING commit cannot be "
            "the ship this write is meant to record",
            file=sys.stderr,
        )
        return None
    if _scope_sha_postdates_trigger(resolve_worktree, resolved, not_after, facts=source_facts):
        print(
            f"resolve_source_ship_sha: {resolved[:8]} postdates the cascade "
            f"trigger ({not_after}) — cannot be evidence for a cascade that "
            "fired at or before that time",
            file=sys.stderr,
        )
        return None
    return resolved


# ---------------------------------------------------------------------------
# handoff.transition verb wrappers — units 3 & 4
# ---------------------------------------------------------------------------

def _call_handoff_transition(handoff_path: str, params: dict) -> dict:
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        return {"exit_code": 1, "error": f"could not resolve git worktree for {handoff_path}"}
    from coordinator_core.ops.handoff_transition import _handler as _transition_handler

    full_params = {"handoff_path": handoff_path, **params}
    return asyncio.run(_transition_handler(full_params, repo_root=repo_root))


def cs_gate_recheck_handoff(handoff_path: str, at: str, cleared: bool = False) -> int:
    """Gate-recheck transition — always stamps last_gate_recheck; --cleared additionally
    flips awaiting_gate -> ready_to_fire and strips gate_dependency."""
    result = _call_handoff_transition(
        handoff_path, {"verb": "gate-recheck", "at": at, "cleared": cleared}
    )
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_gate_recheck_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
    return rc


def cs_repark_handoff(handoff_path: str) -> int:
    """Repark transition: in_flight -> ready_to_fire (status untouched)."""
    result = _call_handoff_transition(handoff_path, {"verb": "repark"})
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_repark_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
    return rc


def cs_close_handoff(handoff_path: str, reason: str) -> int:
    """Close transition: deployment_state -> closed + closed_reason: <reason>
    (DR-084 human/session-only terminal — cancelled | displaced | stale).

    Refuses (exit_code=1, no write) an empty or out-of-enum reason, and
    refuses to overwrite an already-shipped/continued handoff — see
    coordinator_core.ops.handoff_transition._close's docstring for the full
    contract. This is the verb archive-stamp-cli lacked when an executor
    tried to close a genuinely dead baton (roadmap-lvv-07) and instead
    archived it via chain-archive-handoff with a zero-byte frontmatter diff,
    leaving status: open on an archived record (reverted f145480d)."""
    result = _call_handoff_transition(handoff_path, {"verb": "close", "reason": reason})
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_close_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
    return rc


def cs_unclaim_handoff(
    handoff_path: str, note: Optional[str] = None, reaped_from: Optional[str] = None
) -> int:
    """Unclaim: full inverse of claim — status:claimed->open,
    deployment_state->ready_to_fire, strips claimed_at/claimed_by.

    Optional `reaped_from` is the reaper's opt-in provenance signal (plan
    docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § C2) — see
    coordinator_core.ops.handoff_transition._unclaim's docstring for the
    reaped_from_session resolution order it triggers.

    Resolves a `session_id` the same way `cs_claim_handoff` does
    (`resolve_current_session_id(worktree_root=worktree)`) and threads it into
    params for `_unclaim`'s Session Ledger discharge-evidence advisory. ONE
    deliberate divergence from `cs_claim_handoff`'s precedent: an unresolvable
    sid does NOT fail loud here — claim's empty-claimed_by stake does not apply
    to an advisory riding on the release path, so no sid simply means no warn
    and a normal unclaim (docs/plans/2026-08-14-discharge-evidence-at-the-
    unclaim-seam.md AC2c)."""
    params: dict = {"verb": "unclaim"}
    if note:
        params["note"] = note
    if reaped_from:
        params["reaped_from"] = reaped_from
    worktree, _repo_root = _resolve_repo_root_for(Path(handoff_path))
    if worktree is not None:
        sid = resolve_current_session_id(worktree_root=worktree)
        if sid:
            params["session_id"] = sid
    result = _call_handoff_transition(handoff_path, params)
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_unclaim_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
    return rc


# Deprecated alias — retained so external importers of the pre-rename name
# keep working. Not a re-implementation: same function object.
cs_unconsume_handoff = cs_unclaim_handoff


# ---------------------------------------------------------------------------
# handoff.archive_transition verb wrapper — cs_ship_handoff
# ---------------------------------------------------------------------------

#: Move cap for the sweep this module's verbs now carry. Mirrors
#: `coordinator/bin/sweep-terminal-handoffs.py`'s own `_CAP` rather than
#: re-deriving a second recommended value; `handoff.housekeeping` itself refuses
#: an absent or non-positive cap, so this is a choice, never a fallback.
_SWEEP_CAP = 150


def _call_handoff_archive_transition(handoff_path: str, params: dict) -> dict:
    """Composes the archive-transition compute for one named handoff — the
    targeted, zero-corpus-read path for the three modes
    `coordinator_core.ops.handoff_stamp_targeted` implements (`stamp_only`,
    `chain`, `supersede`), falling back to the sweep-coupled
    `housekeeping.cycle` door ONLY for `stamp_shipped`, the one mode that
    module does not implement (plan C1/C2: C2 implements ship/stamp_only
    only; C3 implements chain/supersede; `stamp_shipped` — `cs_ship_handoff
    (archive=True)` — was never in either chunk's scope).

    Repointed 2026-08-30 per
    `docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-that.md` chunk
    C4. Was, until this change, routed THROUGH `housekeeping.cycle` (itself a
    2026-08-30 repoint of the dead `handoff.housekeeping` op key — kill means
    kill forever, PM 2026-08-23) for every mode, paying that cycle's
    corpus-wide `read_live_corpus`/`open_index`/`compute_terminal_set` scan
    on every single-record call — see the governing plan's Problem section
    for the measured cost (373ms p50) and why the fused sweep could not even
    archive anything on this path once `ed95dd5f80`'s worktree-dirty rail
    landed (0 of 400 terminal targets moved across 70 calls).

    THE RETURN SHAPE IS UNCHANGED, and that is load-bearing rather than
    incidental. Every caller here — `cs_ship_handoff`, `cs_chain_archive_handoff`,
    `cs_supersede_archive_handoff`, and DoE's `archive-stamp-cli` behind them —
    keys on the transition op's own `exit_code`/`retained`/`moved`/`message`/
    `retain_reason`/`warnings`. `handoff_stamp_targeted`'s three functions
    reproduce `handoff_archive_transition._handler`'s own envelope
    byte-for-byte per mode (see that module's own docstrings), so not one of
    those predicates moves. `coordinator_core.tests.
    test_stamp_verbs_stay_off_the_sweep` is this repoint's own contract test,
    diffing the returned envelope against the pre-change (`housekeeping.cycle`)
    path for the same inputs.

    WHAT DOES NOT CHANGE for `stamp_only`/`chain`/`supersede`: the archival
    sweep no longer runs on these calls at all (it never usefully could — see
    above) — `stamp_only` already left the file in `state/handoffs/` for the
    cadence step (`handoff-housekeeping`, per the 2026-08-27 PM ruling,
    already runs at `workday_complete`/`workweek_complete`); `chain`/
    `supersede` still move the file themselves, via `handoff_stamp_targeted`'s
    own `archive_and_commit` call, never via the cycle's sweep.

    A `stamp_shipped` call takes the unchanged fallback path: a housekeeping
    refusal that happens BEFORE the transition is composed (an unresolvable
    worktree, a bad cap) comes back with `transition: None`, relayed as
    housekeeping's own error dict rather than an empty one, because every
    caller prints `result["error"]` and a bare `{}` would have them report
    "unknown error" for a cause this function was told.
    """
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        return {"exit_code": 1, "error": f"could not resolve git worktree for {handoff_path}"}

    mode = params.get("mode")

    if mode == "stamp_only":
        from coordinator_core.ops.handoff_stamp_targeted import ship_stamp_only

        return ship_stamp_only(
            handoff_path,
            repo_root,
            sha=params.get("sha"),
            kind=params.get("kind"),
            force=bool(params.get("force", False)),
        )

    if mode == "chain":
        from coordinator_core.ops.handoff_stamp_targeted import chain_archive_handoff

        return asyncio.run(chain_archive_handoff(handoff_path, repo_root))

    if mode == "supersede":
        from coordinator_core.ops.handoff_stamp_targeted import supersede_archive_handoff

        return asyncio.run(
            supersede_archive_handoff(
                handoff_path,
                repo_root,
                continued_into=params.get("continued_into", ""),
                sha=params.get("sha"),
                kind=params.get("kind"),
                force=bool(params.get("force", False)),
            )
        )

    # stamp_shipped — not in C2/C3's scope; unchanged sweep-coupled fallback.
    from coordinator_core.housekeeping.cycle import _handler as _housekeeping_handler

    housekeeping = _housekeeping_handler(
        {
            "close": False,
            "cap": _SWEEP_CAP,
            "transition": {"handoff_path": handoff_path, **params},
        },
        repo_root=repo_root,
    )
    transition = housekeeping.get("transition")
    if transition is None:
        return {
            "exit_code": housekeeping.get("exit_code", 1),
            "error": housekeeping.get("error", "handoff.housekeeping returned no transition"),
        }
    return transition


def cs_ship_handoff(
    handoff_path: str,
    archive: bool = False,
    sha: Optional[str] = None,
    force: bool = False,
) -> int:
    """Terminal ship transition: stamp shipped_in + flip deployment_state -> shipped.

    Composes handoff.archive_transition (NOT the plain handoff.transition op) so
    every caller — standalone (/pickup, /workstream-complete embedded bash blocks
    via DoE's archive-stamp-cli) included — gets the SAME unconditional
    live-children guard and stamp-then-flip ordering as the archive-ceremony
    call sites, closing the incoherent half-state (shipped_in present while
    deployment_state stays in_flight) a standalone stamp_shipped_in() call could
    otherwise leave behind.

    archive=False (default) -> handoff_archive_transition mode 'stamp_only'
        (guard, stamp, flip; file stays in state/handoffs/ for the async sweep)
    archive=True            -> mode 'stamp_shipped' (stamp, guard, git mv + commit)

    sha: optional caller-supplied SHA override, threaded verbatim to
    `stamp_shipped_in`'s own `sha=` override (see that function's docstring for
    validation/precedence). Default None preserves prior behaviour (self-derived
    from scope-paths) for every existing caller. Added 2026-07-22 — see `force`
    below for why `sha` alone does not repair an already-stamped handoff.

    force: provenance-repair escape, threaded verbatim to `stamp_shipped_in`'s
    own `force=` param. Default False leaves the idempotent no-op unchanged for
    every existing caller. When True, REQUIRES `sha` — a force-overwrite that
    resolves its own sha is rejected fail-loud (see `stamp_shipped_in`
    Negative-spec). Origin: 2026-07-22 incident — `sha=` alone could not correct
    an already-stamped `shipped_in`, because the downstream handoff.stamp op's
    idempotency guard no-ops regardless of what sha the caller supplies.

    Retention (the live-children guard finds the handoff still a live
    merge-parent, or indeterminate/fail-closed) is NEVER an error — the op
    itself returns exit_code:0 for that case, and this wrapper propagates that
    verbatim. Idempotent: a second call on an already-shipped handoff is a
    clean deployment_state no-op (handoff_transition._ship's own idempotency)
    that still returns 0 — unless force=True, in which case shipped_in is
    replaced (deployment_state's own idempotency is unaffected by force).

    Corrected contract (2026-07-28, § S11/AC6/AC6b/AC7, chunk C0): a `sha`
    override is never silently discarded. `handoff.archive_transition`'s
    `do_stamp`/`do_stamp_only` now compare the underlying `stamp_shipped_in`
    envelope's `prior_value` (never a before/after disk string diff — see
    `StampOutcome`'s docstring for why that comparison was unevaluable before
    this pass) against the supplied `sha`: a same-commit re-stamp (the full
    SHA canonically matches the stored 8-char `prior_value`) is a legitimate
    no-op and does NOT refuse (AC6b); any OTHER already-present, non-matching
    value now REFUSES loudly (non-zero exit, naming `--force` as the remedy)
    instead of exiting 0 having silently retained the old value (AC6). A
    genuine no-commit-found no-op still distinguishes "left unset" (nothing
    was ever there) from "retained prior value `<X>`" (AC7) — it no longer
    reports both as "left unset".
    """
    if force and not (sha and sha.strip()):
        print(
            "cs_ship_handoff: rejected force=True without an explicit sha — "
            "force must never trigger its own resolution",
            file=sys.stderr,
        )
        return 1
    mode = "stamp_shipped" if archive else "stamp_only"
    params: dict = {"mode": mode}
    if sha:
        params["sha"] = sha
    if force:
        params["force"] = True
    result = _call_handoff_archive_transition(handoff_path, params)
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_ship_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
    else:
        # A legitimate retain (live-children guard) returns exit_code:0 with
        # message/retain_reason/warnings set — previously silent on the rc==0
        # path, which made a retained-not-shipped handoff look like a clean
        # ship with no signal to the operator. Print whenever non-empty,
        # regardless of rc; guard's own retain/indeterminate semantics
        # (§ docstring above) are untouched by this change.
        for key in ("message", "retain_reason", "warnings"):
            value = result.get(key)
            if value:
                print(f"cs_ship_handoff: {key}={value}", file=sys.stderr)
    return rc


def _archive_move_landed(handoff_path: str, worktree: Optional[Path]) -> bool:
    """Independent, on-disk confirmation that a git-mv archival actually landed —
    NEVER trusted from the op's own `moved` bool alone (§ C2, `docs/plans/2026-07-28-
    handoff-close-path-fail-loud.md`, AC2/AC3). A move has landed only when BOTH the
    source no longer exists at `handoff_path` AND the derived
    archive/handoffs/YYYY-MM/ destination exists — the exact pair of facts S6's ref-
    lock race left inconsistent (op returned exit_code:0, `moved` unset, source still
    present) while only stderr carried the truth.

    worktree=None (repo-root resolution itself failed) is treated as verification
    FAILURE, not success — a wrapper that cannot even locate the repo cannot assert
    anything about where the file ended up."""
    if worktree is None:
        return False
    src = Path(handoff_path)
    if src.exists():
        return False
    dest = handoff_archive_dest(worktree, src)
    return dest.exists()


def cs_chain_archive_handoff(
    handoff_path: str,
    exclude: Optional[list[str]] = None,
) -> int:
    """Unconditional archive-only transition: handoff.archive_transition mode='chain'
    (the op's own default mode) — UNCONDITIONAL live-children guard; if safe, git mv to
    archive/handoffs/YYYY-MM/ + commit. NO stamp of any kind (shipped_in untouched).

    This is the scout-verified CLI gap cockpit §6.2 flagged: 'chain' was reachable
    in-process (the op's own default) but had no archive-stamp-cli verb — every
    existing verb in this module (cs_ship_handoff, cs_supersede_archive_handoff, ...) routes
    through a stamping mode. Distinct from cs_ship_handoff(archive=True) (mode
    'stamp_shipped', which stamps shipped_in + deployment_state:shipped BEFORE the
    move) — this verb moves a handoff that is ALREADY correctly stamped (or never
    needed stamping) without touching its frontmatter beyond the guard's own move.

    exclude: optional list of paths dropped from the live-children guard's scan set
    before checking (mirrors handoff.archive_transition's own `exclude` param —
    forward-slash POSIX needles, per the op's own contract).

    Retention (guard finds live children, or indeterminate/fail-closed) is NEVER an
    error — returns the op's own exit_code:0 for that case, propagated verbatim.

    Negative-spec (§ C2, AC2/AC3, `docs/plans/2026-07-28-handoff-close-path-fail-
    loud.md`): the op's own exit_code:0 does NOT, by itself, mean the move landed —
    S6 reproduced live a `git mv`/commit that lost a `HEAD` ref-lock race and still
    returned exit_code:0 with `moved` unset, because the op's own git-mv-failure
    branch is deliberately non-fatal (a concurrent session may have already moved
    it). This wrapper does NOT relay that rc verbatim the way it used to: when the
    guard did NOT retain (a move was ATTEMPTED, not a deliberate retain), it re-stats
    the filesystem via `_archive_move_landed` before returning 0 — a retain-vs-
    failed-move distinction the op's own dict already carries in `retained`/`moved`
    but that this wrapper must NOT take on faith alone. A retained outcome is NEVER
    downgraded to non-zero by this check; only an attempted-and-unlanded move is.
    """
    params: dict = {"mode": "chain"}
    if exclude:
        params["exclude"] = list(exclude)
    result = _call_handoff_archive_transition(handoff_path, params)
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_chain_archive_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
        return rc
    # A guard-retained outcome (live children found, or indeterminate/
    # fail-closed) is a legitimate exit_code:0 non-move — previously
    # silent on this path, which made a retained-not-archived handoff
    # indistinguishable from a successful archive to the caller. Print
    # whenever non-empty, mirroring cs_ship_handoff's own retain-signal
    # block; the op's own retain/indeterminate semantics are untouched.
    for key in ("message", "retain_reason", "warnings"):
        value = result.get(key)
        if value:
            print(f"cs_chain_archive_handoff: {key}={value}", file=sys.stderr)
    if result.get("retained"):
        return 0
    worktree, _ = _resolve_repo_root_for(Path(handoff_path))
    if not _archive_move_landed(handoff_path, worktree):
        print(
            f"cs_chain_archive_handoff: {handoff_path}: archive move was ATTEMPTED "
            "(guard did not retain) but did not land — source still present or "
            "archive/handoffs/ destination missing; see message/warnings above for "
            "the underlying reason",
            file=sys.stderr,
        )
        return 1
    return 0


def _reread_supersede_frontmatter(current_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Re-reads `deployment_state` and `continued_into` straight off disk at
    `current_path` — the independent-of-the-op read `cs_supersede_archive_handoff`
    uses to confirm its own write (§ C2, AC3) rather than trusting the op's own
    `superseded` bool. Returns (None, None) when the file is unreadable or carries
    no frontmatter; that shape reads as a verification failure to the caller, never
    as a silent pass."""
    try:
        # Review: code-reviewer (nit F4) — UnicodeDecodeError (a ValueError
        # subclass, not an OSError) on non-UTF-8 bytes must also fail closed
        # to (None, None) per this function's own contract.
        text = current_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    split = split_frontmatter(text)
    if split is None:
        return None, None
    deployment_state = read_fm_field_unquoted(split.fm_text, "deployment_state")
    continued = read_fm_field_unquoted(split.fm_text, "continued_into")
    return deployment_state, continued


def _locate_after_supersede(handoff_path: str, worktree: Optional[Path]) -> Path:
    """Where to re-read frontmatter from after a supersede call: `handoff_path`
    itself when the archival move did not happen (guard-retained, or the git mv
    genuinely failed), or the derived archive/handoffs/YYYY-MM/ destination when it
    did. Falls back to `handoff_path` verbatim when `worktree` is unresolvable —
    the re-read then fails closed via `_reread_supersede_frontmatter`'s own OSError
    handling, not via a path this function guesses at."""
    src = Path(handoff_path)
    if src.exists() or worktree is None:
        return src
    dest = handoff_archive_dest(worktree, src)
    return dest if dest.exists() else src


def cs_supersede_archive_handoff(
    handoff_path: str,
    continued_into: str,
    exclude: Optional[list[str]] = None,
) -> int:
    """Supersede-and-archive transition: handoff.archive_transition mode='supersede'
    (NOT handoff.transition — see the distinction below).

    Composes stamp_shipped_in BEFORE the live-children guard, then — once the guard
    clears — status:claimed + deployment_state:continued + continued_into:<successor>
    (the DR-084 supersede verb), THEN git mv + commit to archive/handoffs/YYYY-MM/.
    `continued_into` (the successor handoff's id-or-path) is REQUIRED — the op itself
    rejects mode='supersede' with no successor as a usage error (exit_code:2); an
    automated writer that cannot name the successor cannot stamp `continued` by
    construction.

    Distinction from the frontmatter-only `handoff.transition` 'supersede' verb
    (composes plain handoff.transition 'supersede', never moves the file, and takes
    no continued_into parameter): this function is the DR-084-vocabulary,
    archive-and-move form.

    exclude: optional list of paths dropped from the live-children guard's scan set
    before checking (mirrors handoff.archive_transition's own `exclude` param).

    Retention (guard finds live children, or indeterminate/fail-closed) is NEVER an
    error — returns the op's own exit_code:0 for that case, propagated verbatim,
    PROVIDED the status flip below has independently verified as landed. A missing/
    blank continued_into is a usage error handled by this wrapper itself (exit_code
    2) so the CLI can fail loud before even reaching the op.

    DR-242 gate (§ C5a, `docs/plans/2026-07-28-handoff-close-path-fail-loud.md`):
    before calling the op at all, this wrapper checks
    `coordinator_core.archival.claimed_or_shipped_at_path` on
    `handoff_path` itself and refuses (exit_code 1) if the parent was never
    claimed or shipped — a successor-named child pointing at `handoff_path` is not,
    by itself, evidence there is anything here to supersede.

    Negative-spec (§ C2, AC3, `docs/plans/2026-07-28-handoff-close-path-fail-
    loud.md`): before this wrapper existed, `cs_supersede_archive_handoff` on the
    path the consumed-handoff tripwire explicitly redirects EMs to (§ S7,
    `state/improvement-queue/2026-07-25-archive-stamp-cli-supersede-archive-hand-
    6b71b547b70a.yaml`) could exit 0 while the status flip never landed — the root
    cause (the mutation ran AFTER the live-children guard's early return) is already
    fixed at the op layer (`2b4aafb7`), but relaying the op's rc verbatim still
    means this wrapper is trusting a claim it never independently checked. This
    wrapper does NOT trust `result["superseded"]` alone: on the exit_code:0 path it
    re-reads the predecessor's on-disk frontmatter (`_reread_supersede_frontmatter`,
    at whichever of `handoff_path`/its archive destination currently holds the
    file — `_locate_after_supersede`) and asserts `deployment_state: continued`
    AND `continued_into: <the supplied successor>` actually landed BEFORE
    returning 0. This assertion runs REGARDLESS of `retained` — the status flip is
    not gated on the archival-move guard (see `handoff_archive_transition`'s own
    docstring), so a guard-retained call must still show the flip on disk. If it did
    not land, this returns non-zero naming the handoff and what was found instead —
    never a bare relay of the op's own exit_code.
    """
    if not (continued_into and continued_into.strip()):
        print(
            "cs_supersede_archive_handoff: mode 'supersede' requires a non-empty "
            "continued_into (successor handoff id-or-path)",
            file=sys.stderr,
        )
        return 2
    # Review: code-reviewer (P2) — this previously imported
    # `claimed_or_shipped_at_path` out of a package literally named `tests`
    # (a future `exclude = ["coordinator_core.tests*"]` on `pyproject.toml`'s
    # `include = ["coordinator_core*"]` would have silently broken this at
    # import time). Relocated to `coordinator_core.archival` — a production
    # home — 2026-08-06; the future-packaging hazard this comment used to
    # flag no longer applies to this site.
    from coordinator_core.archival import claimed_or_shipped_at_path

    if not claimed_or_shipped_at_path(handoff_path):
        print(
            f"cs_supersede_archive_handoff: {handoff_path}: refusing — this baton "
            "was never claimed or shipped (DR-242: a successor-named child is not "
            "evidence of succession; nothing to supersede)",
            file=sys.stderr,
        )
        return 1
    params: dict = {"mode": "supersede", "continued_into": continued_into}
    if exclude:
        params["exclude"] = list(exclude)
    result = _call_handoff_archive_transition(handoff_path, params)
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(
            f"cs_supersede_archive_handoff: {result.get('error', 'unknown error')}",
            file=sys.stderr,
        )
        return rc
    # A guard-retained outcome (live children found, or indeterminate/
    # fail-closed) is a legitimate exit_code:0 non-write/non-move —
    # previously silent on this path, which made a retained-not-
    # superseded handoff indistinguishable from a successful supersede
    # to the caller. Print whenever non-empty, mirroring cs_ship_handoff's
    # own retain-signal block; the op's own retain/indeterminate
    # semantics are untouched.
    for key in ("message", "retain_reason", "warnings"):
        value = result.get(key)
        if value:
            print(f"cs_supersede_archive_handoff: {key}={value}", file=sys.stderr)
    worktree, _ = _resolve_repo_root_for(Path(handoff_path))
    current = _locate_after_supersede(handoff_path, worktree)
    deployment_state, landed_continued_into = _reread_supersede_frontmatter(current)
    if deployment_state != "continued" or landed_continued_into != continued_into:
        print(
            f"cs_supersede_archive_handoff: {handoff_path}: expected "
            f"deployment_state='continued' with continued_into={continued_into!r} "
            f"after supersede, re-read {current} and found "
            f"deployment_state={deployment_state!r} "
            f"continued_into={landed_continued_into!r} — the status flip did not "
            "land",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# cs_claim_handoff — unit2
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_human_claimant_best_effort(target_path: str, worktree: Path) -> None:
    """C8: additive claim-time stamp of the OPERATING HUMAN, beside
    `picked_up_by`/`claimed_by` (which keep carrying the SESSION id,
    unchanged — liveness and the claim ledger still key on those).

    PM ruling, 2026-08-19 (one-box-one-human): the claiming session resolves
    the operating human at the moment it claims, exactly as C4 does at the
    creation door (`resolve_operating_person().get("github")`, the same
    resolution `minted_by` uses) — authored, never derived, and never
    resolved inside a sweep. Do NOT use `machine_resolver.compute_contributor`
    here — that is a differently-derived, differently-shaped "contributor
    slug" (env var / machine-registry / email-derived, with an "unknown"
    fallback) that is NOT this axis's value space; see this chunk's brief.

    Best-effort and non-fatal, mirroring `_record_pickup_best_effort`'s own
    contract: an unresolvable operating human, or any write failure, leaves
    `human_claimant` unset rather than aborting the caller's claim. Inserted
    ONLY when absent — a claim record already carrying `human_claimant` (e.g.
    idempotent re-claim by the same session) is left untouched, never
    re-stamped or overwritten.
    """
    slug = resolve_operating_person().get("github")
    if not slug:
        return
    try:
        repo_root = _git_common_dir(worktree)
        if repo_root is None:
            return

        def _mutate(old_text: str) -> str:
            split = split_frontmatter(old_text)
            if split is None:
                raise MutateAbort(f"no parseable YAML frontmatter in {target_path}")
            fm_text = split.fm_text
            if read_fm_field(fm_text, "human_claimant") is not None:
                return old_text
            fm_text = insert_fm_field(fm_text, "human_claimant", slug, "picked_up_by", numeric_quoting=True)
            return rebuild(split, fm_text)

        locked_rmw(Path(target_path), _mutate, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — best-effort, must never abort the caller
        print(
            f"_record_human_claimant_best_effort: WARNING — human_claimant not "
            f"recorded for {target_path} ({exc}); non-fatal",
            file=sys.stderr,
        )


def _record_pickup_best_effort(handoff_path: str, worktree: Path, sid: str) -> None:
    """C2 write-moment: best-effort, non-fatal session.record_pickup — mirrors the
    oracle's foreign-repo-bleed fix (repo-relative handoff path, never absolute)."""
    try:
        proc = _run_git(["rev-parse", "--show-prefix"], cwd=worktree)
        prefix = proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Best-effort per this function's docstring — a prefix-resolution
        # failure degrades to a bare-basename handoff path, never aborts.
        print(f"_record_pickup_best_effort: git rev-parse --show-prefix failed: {exc}", file=sys.stderr)
        prefix = ""
    hp_rec = f"{prefix}{Path(handoff_path).name}" if prefix else Path(handoff_path).name

    # Cheap single-field frontmatter read (mirrors the deployment_state read
    # in reconcile_dead_handoff_claim_frontmatter) — the claimed handoff's
    # deliverable-spine id, if it carries one. Absent/null/unreadable all
    # normalize to "" (read_frontmatter_field's own contract); the op call
    # below omits deliverable_id from the write when it is "".
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

    deliverable_id = read_frontmatter_field(handoff_path, "deliverable_id")

    try:
        from coordinator_core.ops.session.record_pickup import _handler as _pickup_handler

        common_dir = _git_common_dir(worktree)
        params = {"sid": sid, "handoff_relpath": hp_rec, "repo_root": str(worktree)}
        if deliverable_id:
            params["deliverable_id"] = deliverable_id
        result = asyncio.run(
            _pickup_handler(
                params,
                repo_root=common_dir,
            )
        )
        if int(result.get("exit_code", 1)) != 0:
            print(
                "cs_claim_handoff: WARNING — session.record_pickup op did not complete; "
                "pickup not recorded in session-shape (non-fatal)",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, must never abort the caller
        print(
            f"cs_claim_handoff: WARNING — session.record_pickup op did not complete "
            f"({exc}); pickup not recorded in session-shape (non-fatal)",
            file=sys.stderr,
        )


#: Cap on the `goal` value written by `_record_session_goal_best_effort` —
#: mirrors the field's own diagnostic (not authoritative-record) purpose;
#: no caller needs an unbounded-length title/summary in a one-line field.
_SESSION_GOAL_MAX_CHARS = 200


def _record_session_goal_best_effort(handoff_path: str, worktree: Path, sid: str) -> None:
    """C2 write-moment sibling of `_record_pickup_best_effort`: best-effort,
    non-fatal write of the claiming session's `goal` (meta.json), sourced
    from the just-claimed handoff's own `title` (falling back to `summary`).

    Writer-seam rationale (state/handoffs/2026-08-13-session-goal-field-has-
    no-writer.md): `holder_evidence.holder_evidence` — the sole consumer of
    meta.json's `goal` — is only ever reached via `compute_claim_grant` /
    `compute_competing_claim`, whose holder is by construction a session
    that claimed a handoff, i.e. one that went through pickup. A
    session-init or plan-adoption writer would add seams with no
    contention case behind them, so pickup (here, alongside
    `_record_pickup_best_effort`, both called from `cs_claim_handoff`'s
    tail) is the sole writer.

    Mirrors `_record_pickup_best_effort`'s contract exactly: wrapped in a
    bare `try/except Exception`, prints a WARNING to stderr, NEVER raises
    into the caller — `goal` is a diagnostic, never a gate (Anti-scope).
    Reads `title`/`summary` via the already-native
    `read_frontmatter_field` (no subprocess, no corpus read) and writes via
    `coordinator_core.session.core.update_meta_field` — no new meta.json
    field, no structured goal object.
    """
    try:
        from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
        from coordinator_core.session import core as _session_core

        title = read_frontmatter_field(handoff_path, "title")
        value_source = title if title else read_frontmatter_field(handoff_path, "summary")
        if not value_source:
            # Neither title nor summary is available — write nothing rather
            # than a placeholder (Anti-scope: empty stays empty until real).
            return

        value = f"pickup: {value_source}"[:_SESSION_GOAL_MAX_CHARS]

        # `ensure_session`, not `session_dir`: a session directory used to be
        # routinely created by a bookkeeping writer that never wrote the
        # meta.json record, and `update_meta_field` no-ops on an absent file by
        # contract -- so this write reported "did not complete" on every claim
        # in such a session and `goal` stayed permanently unset, which is what
        # a peer's claim-contention check reads. The constructor now closes
        # that at the source; this call remains the one that GUARANTEES the
        # record before the write, which is what this site actually needs.
        # Second consumer of this call: `stable_pid` liveness stamping —
        # `ensure_session` is also the writer that (re-)stamps `stable_pid` on
        # a claim (see its own docstring's re-stamp arm). Narrowing this
        # call to `session_dir`/`update_meta_field` would silently drop
        # that stamp too, not just `goal`.
        sdir = _session_core.ensure_session(sid, str(worktree))
        if not sdir:
            return
        if not _session_core.update_meta_field(sdir, "goal", value):
            print(
                "cs_claim_handoff: WARNING — session goal write did not complete "
                "(update_meta_field returned False); goal not recorded (non-fatal)",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, must never abort the caller
        print(
            f"cs_claim_handoff: WARNING — session goal write did not complete "
            f"({exc}); goal not recorded (non-fatal)",
            file=sys.stderr,
        )


def cs_claim_handoff(handoff_path: str, *, return_result: bool = False) -> "int | dict":
    """Pickup-time claim transition: status:open->claimed, deployment_state->
    in_flight, +claimed_at/claimed_by. Fails loud on unresolvable session id (an
    empty claimed_by would corrupt the claim gate).

    ``return_result`` (additive kwarg, C2/2026-08-13): when True, returns the
    full ``handoff.transition`` claim op response dict — landed
    (``exit_code: 0, applied: True``), no-op (``exit_code: 0, applied: False``,
    with a ``message``), or rejection (``exit_code: 1`` plus an ``error``
    string, e.g. a `_HANDOFF_CROSS_FIELD_RULES` validation failure) — instead
    of the bare exit code. Mirrors ``cs_claim_memo_stamp``'s own
    ``return_result`` shape verbatim (same additive kwarg, same unaffected
    default — every existing positional caller keeps its exact int-return
    contract). Default False."""
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        print(f"cs_claim_handoff: could not resolve git worktree for {handoff_path}", file=sys.stderr)
        result = {"exit_code": 1, "applied": False, "error": f"could not resolve git worktree for {handoff_path}"}
        return result if return_result else result["exit_code"]

    sid = resolve_current_session_id(worktree_root=worktree)
    if not sid:
        print(
            "cs_claim_handoff: could not resolve a session id (empty claimed_by would "
            "corrupt the claim gate) — set COORDINATOR_SESSION_ID, CLAUDE_SESSION_ID, "
            "or CLAUDE_CODE_SESSION_ID in the environment",
            file=sys.stderr,
        )
        result = {"exit_code": 1, "applied": False, "error": "could not resolve a session id"}
        return result if return_result else result["exit_code"]

    ts = _now_iso()
    result = _call_handoff_transition(
        handoff_path, {"verb": "claim", "session_id": sid, "at": ts}
    )
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_claim_handoff: {result.get('error', 'unknown error')}", file=sys.stderr)
        return result if return_result else rc

    _record_pickup_best_effort(handoff_path, worktree, sid)
    _record_session_goal_best_effort(handoff_path, worktree, sid)
    _record_human_claimant_best_effort(handoff_path, worktree)
    return result if return_result else 0


# Deprecated alias — retained so external importers of the pre-rename name
# keep working. Not a re-implementation: same function object.
cs_consume_handoff = cs_claim_handoff


# ---------------------------------------------------------------------------
# memo.transition verb wrappers — unit3
# ---------------------------------------------------------------------------

def _call_memo_transition(memo_path: str, params: dict) -> dict:
    from coordinator_core.ops.memo_transition import _handler as _memo_handler

    full_params = {"memo": memo_path, **params}
    # memo.transition is scope "show_top" — repo_root is received but unused by the
    # handler (memo location comes from params["memo"], consumer-agnostic design).
    return asyncio.run(_memo_handler(full_params, repo_root=None))


def cs_claim_memo_stamp(memo_path: str, *, return_result: bool = False) -> "int | dict":
    """Pickup-time claim stamp: open->in_progress, +picked_up_at/picked_up_by.

    ``return_result`` (additive kwarg, C13/DR-273): when True, returns the
    full memo.transition op response dict (carrying its additive
    ``commit_sha`` key on a landed write) instead of the bare exit code.
    Default False — every existing positional caller keeps the exact int
    return contract it already has."""
    mpath = Path(memo_path)
    worktree = _worktree_root(mpath)
    sid = resolve_current_session_id(worktree_root=worktree) if worktree else None
    if not sid:
        print(
            "cs_claim_memo_stamp: could not resolve a session id (empty picked_up_by "
            "would corrupt the claim gate) — set COORDINATOR_SESSION_ID, "
            "CLAUDE_SESSION_ID, or CLAUDE_CODE_SESSION_ID in the environment",
            file=sys.stderr,
        )
        result = {"exit_code": 1, "applied": False, "error": "could not resolve a session id"}
        return result if return_result else result["exit_code"]
    ts = _now_iso()
    result = _call_memo_transition(memo_path, {"verb": "claim", "session_id": sid, "at": ts})
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_claim_memo_stamp: {result.get('error', 'unknown error')}", file=sys.stderr)
    elif worktree is not None:
        _record_human_claimant_best_effort(memo_path, worktree)
    return result if return_result else rc


def cs_release_memo_revert(memo_path: str, *, return_result: bool = False) -> "int | dict":
    """Release transition: reverts in_progress->open, strips picked_up_by/at.

    ``return_result`` (additive kwarg, C13/DR-273): see ``cs_claim_memo_stamp``'s
    docstring — same additive-return shape, same unaffected default."""
    result = _call_memo_transition(memo_path, {"verb": "release"})
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_release_memo_revert: {result.get('error', 'unknown error')}", file=sys.stderr)
    return result if return_result else rc


_DISPOSITION_FLAGS = {
    "--decision": "decision",
    "--decision-note": "decision_note",
    "--realized-by": "realized_by",
    "--actioned-note": "actioned_note",
    "--distill-fate": "distill_fate",
    "--in-repo-capture": "in_repo_capture",
    "--superseded-by": "superseded_by",
    # Append-only supersede-disposition (both required together) — records a
    # reversed verdict on an already-actioned/superseded memo WITHOUT
    # overwriting the original decision/actioned_note/realized_by. See
    # coordinator_core.ops.memo_transition._handle_supersede.
    "--supersede-note": "supersede_note",
    "--supersede-realized-by": "supersede_realized_by",
    "--supersede-at": "supersede_at",
}

# Boolean (no-value) disposition flags — distinct from _DISPOSITION_FLAGS above,
# which all consume the following argv token as a value.
#
# --correct-realization: narrow, opt-in re-action of an already-actioned memo
# whose decision: is UNCHANGED — permits realized_by/decision_note to move
# (e.g. a cited commit was later reverted). NOT a --force/--override: a
# decision: CHANGE still fails loud whether or not this flag is set. See
# coordinator_core/ops/memo_transition.py _handle_already_actioned.
_DISPOSITION_BOOL_FLAGS = {
    "--correct-realization": "correct_realization",
}


def _parse_disposition_args(args: tuple[str, ...]) -> dict:
    """Parses cs_action_memo's disposition flags into memo.transition action params.
    Mirrors the flag surface bin/memo-transition.js action documents (see that CLI's
    usage block for the authoritative flag list), plus the native-only boolean
    --correct-realization flag (_DISPOSITION_BOOL_FLAGS) which has no JS mirror."""
    params: dict = {}
    i = 0
    while i < len(args):
        flag = args[i]
        bool_key = _DISPOSITION_BOOL_FLAGS.get(flag)
        if bool_key:
            params[bool_key] = True
            i += 1
            continue
        key = _DISPOSITION_FLAGS.get(flag)
        if key and i + 1 < len(args):
            params[key] = args[i + 1]
            i += 2
        else:
            i += 1
    return params


def cs_action_memo(memo_path: str, *disposition_args: str, return_result: bool = False) -> "int | dict":
    """Action transition: in_progress->actioned, writes disposition. Applies a
    liveness-gated ownership guard BEFORE the write — refuses to close a memo a
    DIFFERENT live session holds (fail-open at every rung except the live-holder
    conflict, mirroring the oracle's Guards 1-6 verbatim).

    ``return_result`` (additive kwarg, C13/DR-273): see ``cs_claim_memo_stamp``'s
    docstring — same additive-return shape, same unaffected default. On the
    liveness-guard REFUSE path (no memo.transition call made) this still
    returns a dict shape when requested, with no ``commit_sha`` key."""
    mpath = Path(memo_path)
    memo_git_root = _worktree_root(mpath)

    # Claim dirs live under the COMMON git dir (_git_common_dir), never a
    # literal <memo_git_root>/.git join: in a linked worktree that join
    # resolves through <worktree>/.git, a gitdir-pointer FILE, and would
    # scope claims to that one worktree instead of contending across every
    # worktree of the repo — exactly the collision the claim lock exists to
    # prevent.
    memo_common_dir = _git_common_dir(memo_git_root) if memo_git_root is not None else None

    if memo_common_dir is not None:
        claim_dir = memo_common_dir / "coordinator-sessions" / "memo-claims" / mpath.name
        # Guard 1: claim dir absent -> PROCEED.
        if claim_dir.is_dir():
            caller_sid = resolve_current_session_id(worktree_root=memo_git_root)
            # Guard 2: caller sid unresolvable -> PROCEED.
            if caller_sid:
                sid_file = claim_dir / "session_id"
                have_sid_file = sid_file.is_file()
                holder_sid = sid_file.read_text(encoding="utf-8").strip() if have_sid_file else ""
                # Guard 3: holder == caller -> PROCEED.
                if holder_sid != caller_sid:
                    cwd_git_root = _worktree_root(Path.cwd())
                    # Guard 4: cwd/memo-root asymmetry -> PROCEED (cross-repo, untrustworthy).
                    if cwd_git_root == memo_git_root:
                        if cs_claim_holder_live(str(claim_dir)):
                            # Guard 5: holder is LIVE.
                            if os.environ.get("COORDINATOR_OVERRIDE_MEMO_ACTION_CLAIM"):
                                print(
                                    f"cs_action_memo: WARNING — memo held by live session "
                                    f"{holder_sid} (override COORDINATOR_OVERRIDE_MEMO_ACTION_CLAIM "
                                    f"set) — proceeding",
                                    file=sys.stderr,
                                )
                            else:
                                print(
                                    f"cs_action_memo: REFUSING to action — memo '{memo_path}' is "
                                    f"held by a DIFFERENT live session ({holder_sid}); you are "
                                    f"({caller_sid}). Release it, coordinate, or set "
                                    f"COORDINATOR_OVERRIDE_MEMO_ACTION_CLAIM=1 to override.",
                                    file=sys.stderr,
                                )
                                refuse_result = {
                                    "exit_code": 1,
                                    "applied": False,
                                    "error": f"REFUSING to action — held by live session {holder_sid}",
                                }
                                return refuse_result if return_result else refuse_result["exit_code"]
                        else:
                            # Guard 6: holder is DEAD — stale claim, warn and PROCEED.
                            if not have_sid_file:
                                print(
                                    f"cs_action_memo: WARNING — stale claim on '{memo_path}' "
                                    f"(legacy pid-only claim dir, no session_id file); proceeding "
                                    f"(claim dir will be reaped by next reaper sweep)",
                                    file=sys.stderr,
                                )
                            else:
                                print(
                                    f"cs_action_memo: WARNING — stale claim on '{memo_path}' held "
                                    f"by dead session {holder_sid}; proceeding (claim dir will be "
                                    f"reaped by next reaper sweep)",
                                    file=sys.stderr,
                                )
    # (non-git memo dir -> no claim infrastructure possible -> PROCEED)

    disposition_params = _parse_disposition_args(disposition_args)

    # --superseded-by and --decision/--actioned-note are alternative terminal
    # shapes (status: superseded vs status: actioned) — accepting two
    # reintroduces the ambiguity the dedicated pair removes. Refused here,
    # BEFORE any op call, the same discipline memo_transition.py's own
    # "--decision and --actioned-note are mutually exclusive" check applies
    # to its own pair (_validate_action_disposition) — no write occurs.
    if disposition_params.get("superseded_by") and (
        disposition_params.get("decision") or disposition_params.get("actioned_note")
    ):
        print(
            "cs_action_memo: --superseded-by and --decision/--actioned-note are "
            "mutually exclusive — alternative terminal shapes, not combinable",
            file=sys.stderr,
        )
        refuse_result = {
            "exit_code": 1,
            "applied": False,
            "error": "--superseded-by and --decision/--actioned-note are mutually exclusive",
        }
        return refuse_result if return_result else refuse_result["exit_code"]

    # --supersede-note/--supersede-realized-by are a fourth, append-only shape —
    # corrects an EXISTING disposition, never combinable with a shape that sets
    # a fresh one. Same discipline as the --superseded-by check above, applied
    # here before any op call (no write occurs).
    if disposition_params.get("supersede_note") and (
        disposition_params.get("decision")
        or disposition_params.get("actioned_note")
        or disposition_params.get("superseded_by")
    ):
        print(
            "cs_action_memo: --supersede-note/--supersede-realized-by are mutually "
            "exclusive with --decision/--actioned-note/--superseded-by — supersede "
            "corrects an EXISTING disposition, it does not set a new one",
            file=sys.stderr,
        )
        refuse_result = {
            "exit_code": 1,
            "applied": False,
            "error": (
                "--supersede-note/--supersede-realized-by are mutually exclusive "
                "with --decision/--actioned-note/--superseded-by"
            ),
        }
        return refuse_result if return_result else refuse_result["exit_code"]

    result = _call_memo_transition(memo_path, {"verb": "action", **disposition_params})
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_action_memo: {result.get('error', 'unknown error')}", file=sys.stderr)
        return result if return_result else rc

    # C4 write-moment: best-effort, non-fatal actioned_memos record.
    am_sid = resolve_current_session_id(worktree_root=memo_git_root) if memo_git_root else None
    if am_sid:
        ok = session_shape_set(
            am_sid,
            {
                "actioned_memos": [
                    {"basename": mpath.name, "decision": disposition_params.get("decision", "")}
                ]
            },
            cwd=str(memo_git_root) if memo_git_root else None,
        )
        if not ok:
            print(
                "cs_action_memo: WARNING — session-shape actioned_memos record failed (non-fatal)",
                file=sys.stderr,
            )
    return result if return_result else 0


def cs_resolve_memo(memo_path: str, *disposition_args: str, return_result: bool = False) -> "int | dict":
    """Resolve transition: open->actioned in ONE locked_rmw closure (memo.transition
    verb ``resolve``, native-only — see coordinator_core/ops/memo_transition.py's
    ``_resolve`` for the collapsed claim-then-action semantics).

    Deliberately does NOT run cs_action_memo's pre-write memo-claims-dir ownership
    guard block: ``_resolve``'s own docstring negative-spec states it does not
    acquire or write ``.git/coordinator-sessions/memo-claims/`` at all, and runs its
    OWN live-claim refusal (comparing the caller's session_id against an existing
    ``picked_up_by`` inside the same locked_rmw closure) rather than consulting that
    directory — duplicating cs_action_memo's directory-based guard here would check
    a mechanism resolve intentionally bypasses, not add safety.

    ``return_result`` (additive kwarg, C13/DR-273, added for symmetry with the
    other three memo verb wrappers): see ``cs_claim_memo_stamp``'s docstring —
    same additive-return shape, same unaffected default.
    """
    mpath = Path(memo_path)
    worktree = _worktree_root(mpath)
    sid = resolve_current_session_id(worktree_root=worktree) if worktree else None
    if not sid:
        print(
            "cs_resolve_memo: could not resolve a session id (empty picked_up_by "
            "would corrupt the claim gate) — set COORDINATOR_SESSION_ID, "
            "CLAUDE_SESSION_ID, or CLAUDE_CODE_SESSION_ID in the environment",
            file=sys.stderr,
        )
        result = {"exit_code": 1, "applied": False, "error": "could not resolve a session id"}
        return result if return_result else result["exit_code"]
    ts = _now_iso()
    disposition_params = _parse_disposition_args(disposition_args)
    result = _call_memo_transition(
        memo_path, {"verb": "resolve", "session_id": sid, "at": ts, **disposition_params}
    )
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_resolve_memo: {result.get('error', 'unknown error')}", file=sys.stderr)
    return result if return_result else rc


# ---------------------------------------------------------------------------
# cs_stamp_plan_implemented — native in-process call to the completed
# coordinator_core.ops.plan_status_transition port (no subprocess/node hop).
# ---------------------------------------------------------------------------

_AC_ROW_RE = re.compile(
    r"^\s*\|\s*(AC\d+)\s*\|.*\|\s*([A-Za-z][\w-]*)\s*\|\s*$"
)


def _count_open_acs(plan_path: str) -> Optional[int]:
    """Conservatively counts `open` rows in a plan's Acceptance-Criteria
    table. Spec: state/audits/2026-08-13-implemented-plans-keep-ac-tables-at-
    open.md — the census that found 17/40 `implemented` plans still carrying
    an all- or partly-`open` AC table with nothing in the close ceremony
    ever touching those cells.

    Returns None (never a count) when the table is absent or its shape
    can't be confidently read — a false count trains readers to ignore the
    message, which costs more than staying silent. Only lines matching
    `| ACn | ... | <status> |` on one physical line are counted; the
    criterion-prose column is matched non-greedily-agnostic (`.*` before
    the final `|`) so embedded pipe characters inside backticks in that
    column cannot manufacture a phantom row or misread the status field.

    Negative-spec: does NOT normalise the observed status vocabulary
    (`open`, `met`, `shipped`, `closed`, `deferred`, ...) — only `open`
    counts as un-dispositioned, everything else is treated as dispositioned
    verbatim, per the audit's own instruction not to invent a closed
    vocabulary.
    """
    try:
        text = Path(plan_path).read_text(encoding="utf-8")
    except OSError:
        return None

    open_count = 0
    saw_row = False
    for line in text.splitlines():
        match = _AC_ROW_RE.match(line)
        if not match:
            continue
        saw_row = True
        if match.group(2) == "open":
            open_count += 1

    return open_count if saw_row else None


def cs_stamp_plan_implemented(plan_path: str) -> int:
    """Flips a plan's frontmatter status: to implemented via the native
    coordinator_core.ops.plan_status_transition port (a completed 1:1,
    byte-parity port of the node oracle's stamp-implemented verb — see that
    module's docstring for the full status-transition matrix). Calls it
    directly in-process; no subprocess, no node dependency, no DoE-root
    resolution. Returns the port's own exit code verbatim.

    Offer, not a block (spec: state/audits/2026-08-13-implemented-plans-
    keep-ac-tables-at-open.md): after a successful stamp, if the plan
    carries an Acceptance-Criteria table with any row still `open`, prints
    a one-line notice to stderr naming the count and the plan path. The
    stamp itself, and this function's return value, are unaffected either
    way — silence on no table or an unparseable one, never a false count.
    """
    rc = plan_status_transition.main(["stamp-implemented", "--plan", plan_path])
    if rc == 0:
        open_count = _count_open_acs(plan_path)
        if open_count:
            plural = "s" if open_count != 1 else ""
            print(
                f"stamp-plan-implemented: {open_count} AC row{plural} still "
                f"open in {plan_path}",
                file=sys.stderr,
            )
    return rc


# ---------------------------------------------------------------------------
# cs_repair_archived_shipped_in — provenance-repair verb for an ALREADY-
# ARCHIVED handoff's shipped_in field. NEW, no bash-oracle predecessor.
# ---------------------------------------------------------------------------

def cs_repair_archived_shipped_in(
    handoff_path: str,
    reason: str,
    sha: Optional[str] = None,
    unset: bool = False,
) -> int:
    """Repairs ``shipped_in`` on a handoff that has ALREADY been archived
    (``archive/handoffs/``) — a narrow, separate door onto a path every other
    lifecycle verb (ship/claim/supersede/repark/stamp) deliberately cannot
    reach, added 2026-07-22 after a corpus audit found 8 archived handoffs
    with a mis-stamped ``shipped_in`` and no way to correct them.

    Calls ``coordinator_core.ops.handoff_stamp._repair_archived_shipped_in_handler``
    directly in-process — that handler is deliberately NOT ``@register_op``-
    registered (see its own docstring): this is a single-purpose repair tool,
    not a JSON-RPC-dispatchable op, so it carries none of the DR-208
    classification / DR-... key-scope plumbing a registered op would require.

    NEVER resolves a sha of its own — no scope-path git-log lookup, no
    branch-tip fallback, no ownership guard (contrast ``stamp_shipped_in``
    above, which has all three). The caller must name the EXACT sha, or pass
    ``unset=True`` to clear the field entirely — the honest outcome for rows
    with no recoverable correct sha (four of the eight audited rows have
    none). ``sha`` and ``unset=True`` are mutually exclusive.

    ``reason`` is REQUIRED on every call and is echoed back in the result for
    the caller's own audit trail — this function does not persist a ledger of
    its own. Convention (not enforced): prefix ``reason`` with the root cause
    — e.g. ``"peer-race: ..."`` for a concurrent session's commit having
    touched a shared scope: path more recently, or ``"incomplete-scope: ..."``
    for a scope: block that structurally could not see the true semantic-
    witness commit — so a reader can tell the two apart; fixing either
    underlying cause is out of scope for this repair verb.

    Returns 0 on a successful repair/clear (or a byte-identical no-op — the
    requested state already holds), 1 on any rejection (missing reason,
    sha/unset both-or-neither, malformed sha shape, path outside
    archive/handoffs/, file not found, malformed frontmatter, lock timeout).
    """
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        print(
            f"cs_repair_archived_shipped_in: could not resolve git worktree for {handoff_path}",
            file=sys.stderr,
        )
        return 1

    from coordinator_core.ops.handoff_stamp import (
        _repair_archived_shipped_in_handler as _repair_handler,
    )

    params: dict = {"handoff_path": handoff_path, "reason": reason, "unset": unset}
    if sha:
        params["sha"] = sha

    result = asyncio.run(_repair_handler(params, repo_root=repo_root))
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_repair_archived_shipped_in: {result.get('error', 'unknown error')}", file=sys.stderr)
    else:
        print(f"cs_repair_archived_shipped_in: {result.get('message', '')}", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------
# cs_repair_archived_deployment_state — provenance-repair verb for an
# ALREADY-ARCHIVED handoff's deployment_state field. Sibling of
# cs_repair_archived_shipped_in above.
# ---------------------------------------------------------------------------

def cs_repair_archived_deployment_state(
    handoff_path: str,
    reason: str,
    deployment_state: str,
    continued_into: Optional[str] = None,
    continued_into_override: bool = False,
    closed_reason: Optional[str] = None,
) -> int:
    """Repairs ``deployment_state`` (and its state-conditional companion field,
    ``continued_into`` or ``closed_reason``) on a handoff that has ALREADY been
    archived (``archive/handoffs/``) — a narrow, separate door onto a path
    every other lifecycle verb (ship/claim/supersede/repark/stamp)
    deliberately cannot reach, sibling of ``cs_repair_archived_shipped_in``
    above. Added 2026-07-26 after a DoE-claude cross-repo memo reported 13
    archived handoffs hand-edited out of stuck ``deployment_state: in_flight``
    because ``ship-handoff``'s ``state/handoffs/``-only containment refuses
    ``archive/handoffs/`` paths.

    Calls ``coordinator_core.ops.handoff_stamp._repair_archived_deployment_state_handler``
    directly in-process — that handler is deliberately NOT ``@register_op``-
    registered (see its own docstring), for the same reason as the
    shipped_in repair verb.

    ``continued_into`` is REQUIRED when ``deployment_state="continued"`` and
    REJECTED (usage error) for any other target state; ``closed_reason`` is
    REQUIRED when ``deployment_state="closed"`` and REJECTED for any other
    target state — the handler enforces both cross-field rules (mirroring
    handoff-archived.schema.json's own ``allOf`` rules) BEFORE any write, so
    a repair through this verb can never reproduce the exact
    continued-without-continued_into defect a hand-edit produced (10 of the
    13 DoE-claude hand-edits did exactly this).

    ``continued_into`` is ALSO resolution-and-existence checked (2026-07-26):
    it must resolve to a real file under the worktree (searched by path and
    by handoff_id — see ``coordinator_core.ops.handoff_stamp._resolve_continued_into``)
    unless ``continued_into_override=True`` is also passed. This closes the
    "wrong lineage" gap the same 10-file incident's own repair left open — a
    caller could stamp a fabricated, well-formed-looking slug and the verb
    accepted it silently (rc=0); only an independent cross-check against the
    successor's real ``handoff_id`` caught it. ``continued_into_override`` is
    for the genuinely-cannot-verify-locally cases (a successor deleted by a
    distill sweep and recovered from git history; a cross-repo continuation
    this single-repo op cannot resolve) — the mandatory ``reason`` is the
    audit trail for why the override was needed.

    ``reason`` is REQUIRED on every call and is echoed back in the result for
    the caller's own audit trail — this function does not persist a ledger of
    its own.

    Returns 0 on a successful repair (or a byte-identical no-op — the
    requested state already holds), 1 on any rejection (missing reason,
    unknown deployment_state, missing/mismatched continued_into or
    closed_reason, unresolved continued_into without override, path outside
    archive/handoffs/, file not found, malformed frontmatter, lock timeout).
    """
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        print(
            f"cs_repair_archived_deployment_state: could not resolve git worktree for {handoff_path}",
            file=sys.stderr,
        )
        return 1

    from coordinator_core.ops.handoff_stamp import (
        _repair_archived_deployment_state_handler as _repair_handler,
    )

    params: dict = {
        "handoff_path": handoff_path,
        "reason": reason,
        "deployment_state": deployment_state,
    }
    if continued_into:
        params["continued_into"] = continued_into
    if continued_into_override:
        params["continued_into_override"] = True
    if closed_reason:
        params["closed_reason"] = closed_reason

    result = asyncio.run(_repair_handler(params, repo_root=repo_root))
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(
            f"cs_repair_archived_deployment_state: {result.get('error', 'unknown error')}",
            file=sys.stderr,
        )
    else:
        print(f"cs_repair_archived_deployment_state: {result.get('message', '')}", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------
# handoff.correct_body verb wrapper — cs_correct_handoff_body
# ---------------------------------------------------------------------------

def cs_correct_handoff_body(handoff_path: str, old_string: str, new_string: str) -> int:
    """CLI veneer over the `handoff.correct_body` op — a bounded, authorship-gated
    body correction for a `status: claimed` (or legacy `status: consumed`)
    `state/handoffs/*.md` file. Mirrors `_call_handoff_archive_transition`'s
    repo-root resolution + direct in-process handler call shape.

    Spec: archive/specs/2026-07/2026-07-31-claimed-baton-body-correction-route.md,
    chunk C8. Op: coordinator_core/ops/handoff_correct_body.py.

    THE AUTHORSHIP GATE IS ANTI-ACCIDENT, NOT ANTI-ADVERSARY (DR-247 § 3). The
    op's `authoring_session` check is a pure caller-controlled environment-
    variable lookup (COORDINATOR_SESSION_ID > CLAUDE_SESSION_ID >
    CLAUDE_CODE_SESSION_ID) performed inside a subprocess the caller itself
    spawns — a caller that deliberately sets any of these three variables
    before invoking this CLI passes the gate unconditionally. There is no
    socket-authoritative or otherwise caller-independent source of session
    identity on this invoke surface, so this gate is categorically WEAKER
    than a genuine access-control boundary. The actual control this relies on
    is not the gate — it is the stamped, auditable correction note the op
    writes on every applied correction, naming the resolved session id and
    which env var resolved it: a spoofed invocation is not prevented, it is
    made VISIBLE ON DISK rather than closed off. This CLI must never be read
    as enforcing "only the author can correct this body."

    Returns the op's own `exit_code` verbatim: 0 applied / 1 refused (one of
    19 distinct, verbatim refusal reasons — see the op's module docstring).
    Nothing is written on refusal. Prints `error` to stderr on refusal,
    `message` to stderr on success (mirrors this module's other wrappers).
    """
    hpath = Path(handoff_path)
    worktree, repo_root = _resolve_repo_root_for(hpath)
    if worktree is None or repo_root is None:
        print(
            f"cs_correct_handoff_body: could not resolve git worktree for {handoff_path}",
            file=sys.stderr,
        )
        return 1

    from coordinator_core.ops.handoff_correct_body import _handler as _correct_body_handler

    params = {
        "handoff_path": handoff_path,
        "old_string": old_string,
        "new_string": new_string,
    }
    result = asyncio.run(_correct_body_handler(params, repo_root=repo_root))
    rc = int(result.get("exit_code", 1))
    if rc != 0:
        print(f"cs_correct_handoff_body: {result.get('error', 'unknown error')}", file=sys.stderr)
    else:
        print(f"cs_correct_handoff_body: {result.get('message', '')}", file=sys.stderr)
    return rc
