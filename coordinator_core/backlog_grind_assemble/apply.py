"""
coordinator_core.backlog_grind_assemble.apply — the MUTATING half
`backlog_grind_assemble.brief()`'s read-only decision object hands off to.

Purpose: recomputes the brief in-process from `cadence` (never trusts a
caller-supplied decision object), executes its `directives[]` through
`coordinator_core.contract.apply_base`'s shared directive-execution engine
(composed, never re-derived — directive-dependency ordering, per-directive
judgment gating, session-identity propagation, in-repo path safety all live
in `apply_base`; this module supplies only its own closed `_CLI_DISPATCH`
table and the git/session-grant plumbing its handlers invoke), and halts at
the first unresolved judgment point rather than overriding a denial.

`orient_assemble`/`pickup_assemble`/`baton_assemble`/`consolidate_assemble`
are all precedent here; THIS module's shape follows `consolidate_assemble`'s
apply.py most closely (a cadence/mode-selected brief, no single claimed
artifact path, `Path.cwd()`-resolved repo root, its own `_run_git`) rather
than `pickup_assemble`'s artifact-path-resolved shape.

Contract: DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C4. D-3 (the granularity policy this module's commit verbs
implement) and the Hard-constraints review-gate risk constraint (bug-
blitz's commit directives depend_on an EM judgment point) live in that
plan. The `--wave-path` CLI surface below (`main_apply`) is chunk E2 of
the same plan's follow-on.

The `--wave-path` CLI surface — a caller-supplied PATH input, never a
caller-supplied VERB
------------------------------------------------------------------------
`apply <cadence> [--session-id <id>] [--decisions <json>]
                 [--wave-path <repo-relative-path>]...   (repeatable)
                 [--granularity per-item|per-wave] [--message <message>]`
exists because `brief()`'s own readers cannot honestly manufacture a live
commit directive at boot time — the touched-file paths are only known
mid-wave, by the caller (see `apply()`'s own docstring on
`extra_directives`). `main_apply` validates every `--wave-path` (repo-
relative, in-repo — `apply_base.assert_in_repo_root`, a loud refusal of
the WHOLE run on any bad path, never a filtered subset), then builds
`directives.build_stage_and_commit` directive(s) from them and threads the
result through the EXISTING `apply(..., extra_directives=[...])` seam.

**The verb is ALWAYS engine-chosen from `--granularity`, and is NEVER
spellable by the caller.** There is no `--cli` flag, no directive-JSON
flag, and no way to pass a raw `{"cli": ..., "args": ...}` payload on this
command line — `--wave-path` values are bare path strings, never parsed
as anything else, and the resulting directive's `cli` is always one of the
two literal keys `directives.py`'s own `_CLI_BY_GRANULARITY` names. This
extends AC3's closed-dispatch property (the CLOSED `_CLI_DISPATCH`
table's whole point) to this surface: a future edit that adds a
directive-shaped flag to `main_apply` — "for convenience", to unblock one
caller — punches through that property and must not land. If a caller
needs a new verb, it is added to `_CLI_DISPATCH` by hand, in this file,
reviewed as such; it is never accepted as untrusted CLI input.

The directive/args-shape reconciliation this module performs
(`_prepare_directives_for_dispatch`), and why it exists
------------------------------------------------------------------------
`apply_base.execute_directives` calls every dispatch handler as
`handler(directive.get("args", []), repo_root)` — ONLY `args` and
`repo_root` cross the seam; a directive's other top-level keys (`message`,
`branch`, `expected_branch`, `fields`, `evidence_source`, …) are invisible
to a handler unless something packs them into `args` first. `directives.py`
(C2) and `verifier.py` (C6) both carry those fields as SIBLING dict keys,
not inside `args` — by design, since neither module may call `subprocess`/
`git`/an execution primitive itself (both are pure dict-builders). This
module's own `apply()` orchestration therefore repacks every directive
whose `cli` needs more than a bare `args` list into a single JSON string at
`args[0]` immediately before handing the (now self-contained) directive
list to `apply_base.execute_directives` — the ONE place in this file that
bridges the compute-side dict shape to the mutate-side handler signature.
Every dispatch-table handler below is written against the REPACKED shape
(see each handler's own docstring for its exact `args[0]` JSON schema) —
this is also the exact shape the C1 contract test's `_commit_directive_args`
helper constructs when it calls a commit-verb handler directly, bypassing
`apply()`/`_prepare_directives_for_dispatch` entirely to pin the handler's
own contract in isolation.

Known substrate note (not this chunk's file to fix): `directives.py`'s
`build_stage_and_commit` builds ONE directive per call, `args` = a flat
path list for that ONE item/wave, `message`/`branch`/`expected_branch` as
sibling keys — i.e. a per-item `granularity="per-item"` wave is expressed
as N SEPARATE top-level directives (one per item), never one directive
carrying an `items: [...]` list. `_prepare_directives_for_dispatch` reads
that shape correctly (wraps each directive's own single item into a
one-entry `items` list before repacking) — real dispatch therefore always
sees `items` lists of length 1 per directive (N directives for per-item, 1
for per-wave with all paths pre-combined by the caller), while the C1
contract test exercises a handler directly with a 3-entry `items` list in
ONE call to pin the handler's own internal per-item loop. Both shapes are
handled by the same generic handler logic (loop over `items`, however
many).

Security-load-bearing (AC3): the executable universe this module can reach
is a CLOSED CONSTRUCTION, not an asserted property. `directives[].cli`
resolves through `_CLI_DISPATCH` — a literal, hand-written
`dict[str, Callable]` — never resolved via attribute-lookup or dynamic
module import on a brief-derived name, never a subprocess/shell invocation whose
COMMAND (argv[0], or the program name) is derived from `directives[].cli`
or `directives[].args`. An unrecognized `cli` raises before any directive
in the run executes (`apply_base.resolve_cli`'s own pre-validation pass —
"mutates nothing" means the WHOLE run aborts pre-validation, not merely
the one bad directive). Individual `directives[].args` VALUES (a path, a
branch name, a PM note) MAY appear as elements of a literal-list
`subprocess.run([...])`/`_run_git([...])` call — exactly as
`consolidate_assemble.apply`'s own frozen precedent already does for a
branch name (`_dispatch_delete_only`) — because a list-form subprocess
call never invokes a shell and interprets no metacharacters; what AC3
forbids is a directive-derived value selecting WHICH program runs or
WHICH Python callable dispatches, never a plain argv element passed
through a fixed, hardcoded command.

The commit verbs are INTERNAL, named entries in this table
(`commit-per-item`, `commit-per-wave`) — D-3, mirroring
`consolidate_assemble`'s `delete-only`/`cherry-pick-and-delete`/
`merge-and-delete` precedent — never a shell-out to
`coordinator-safe-commit` (forbidden by name, `bug-blitz.md:403`, off the
2026-05-06-22h42 concurrent-commit-absorption incident; its Python port's
>1-live-session fail-closed gate is exactly wrong for this shared
concurrent branch). Each commit verb re-verifies
`git rev-parse --abbrev-ref HEAD == expected_branch` immediately before
its own commit and HALTS (raises `BranchMismatch`, caught by
`apply_base.execute_directives`'s generic exception backstop and reported
as `APPLY_EXIT_PARTIAL_MUTATION`) rather than committing on a branch that
has moved out from under a run that "spans many seconds" (D-3(b)).

Negative-spec:
    - Do NOT add a dispatch entry that resolves `cli` via attribute
      lookup, dynamic module import, or any brief-derived string — every
      entry in `_CLI_DISPATCH` is a literal key written by hand in this
      file.
    - Do NOT build a subprocess argv via string interpolation/shell=True —
      every git call below passes a literal list to `subprocess.run`.
    - Do NOT invoke `coordinator-safe-commit` from anywhere in this
      module, by name or by proxy.
    - Do NOT re-derive `apply_base`'s directive-ordering/judgment-gating/
      path-safety logic locally — compose it.
    - Do NOT trust a caller-supplied decision object as the mutation plan
      — `apply()` recomputes `brief(cadence)` itself.
    - Do NOT collapse `commit-per-item`/`commit-per-wave` into one code
      path that loses D-3(b)'s branch-recheck cadence or D-3(c)'s
      non-PASS failure sub-path — see the two handlers' own docstrings.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.backlog_grind_assemble import CADENCES, brief
from coordinator_core.backlog_grind_assemble import directives as bga_directives
from coordinator_core.backlog_grind_assemble import readers_blitz as readers_bug_blitz
from coordinator_core.backlog_grind_assemble.verifier import HAIKU_VERIFIER_CLI
from coordinator_core.contract import apply_base
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.session.grant import write_tier_u_grant

# ---------------------------------------------------------------------------
# Exit-code contract — composed from apply_base, shared by every apply/
# dispatch half. NOT inherited from `brief`'s 0/2/3 (this module's own
# `EXIT_OK`/`EXIT_USAGE`/`EXIT_TRANSPORT_FAIL`) — the two halves define
# their own contracts per `computed-skills.md` § Exit-code contract.
# ---------------------------------------------------------------------------
APPLY_EXIT_OK = apply_base.APPLY_EXIT_OK
APPLY_EXIT_HALTED_AT_JUDGMENT = apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
APPLY_EXIT_CLAIM_DENIED = apply_base.APPLY_EXIT_CLAIM_DENIED
APPLY_EXIT_TRANSPORT_FAIL = apply_base.APPLY_EXIT_TRANSPORT_FAIL
APPLY_EXIT_PARTIAL_MUTATION = apply_base.APPLY_EXIT_PARTIAL_MUTATION

UnrecognizedDirective = apply_base.UnrecognizedDirective
OutOfRepoPath = apply_base.OutOfRepoPath
NoResolvableSessionId = apply_base.NoResolvableSessionId
DirectiveDependencyCycle = apply_base.DirectiveDependencyCycle
DirectiveResult = apply_base.DirectiveResult

_resolve_explicit_session_id = apply_base.resolve_explicit_session_id
_session_identity = apply_base.session_identity
_assert_in_repo_root = apply_base.assert_in_repo_root

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BranchMismatch(RuntimeError):
    """Raised by a commit-verb handler when `git rev-parse --abbrev-ref
    HEAD` no longer matches `expected_branch` immediately before a commit
    would fire (D-3(b) — "the loop spans many seconds", so a stale branch
    assumption is a correctness bug on a shared concurrent-EM branch, not
    a nitpick). Raised BEFORE the mismatched item's `git add`/`commit`
    runs — a direct handler-level call (AC7's contract) asserts only the
    already-landed commits count, never one more. Propagates through
    `apply_base.execute_directives`'s generic exception backstop as
    `APPLY_EXIT_PARTIAL_MUTATION` when reached via the full `apply()`
    pipeline; a direct `_CLI_DISPATCH[...]` call (as the C1 contract test
    makes) raises it straight to the caller."""


# ---------------------------------------------------------------------------
# `_run_git` — the in-process git read-model every handler below calls by
# its BARE module-level name (never bound to a local alias at import time)
# so that `monkeypatch.setattr(bga_apply, "_run_git", fake_git)` — the C1
# contract test's own patch point — is observed by every handler.
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def _current_branch(repo_root: Path) -> str:
    proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"rev-parse --abbrev-ref HEAD failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout.strip()


def _assert_branch(repo_root: Path, expected_branch: str) -> None:
    current = _current_branch(repo_root)
    if current != expected_branch:
        raise BranchMismatch(
            f"branch check failed before commit: expected {expected_branch!r}, "
            f"found {current!r} — halting rather than committing on the wrong "
            "branch (D-3(b))"
        )


def _stage_paths(repo_root: Path, paths: list[str]) -> None:
    """`git add -- <paths>` for a COMBINED pathspec, wrapped in
    `run_with_lock_retry` exactly like `_commit_one`'s own `add` call below
    — the ONE `.git/index.lock` acquisition `_dispatch_commit_per_item`
    now spends staging every verified item's paths, instead of one
    acquisition PER ITEM (AC-7: the burst-source table's `backlog_grind_
    assemble/apply.py` row, `add`+`commit` per item in a loop == 2 x N
    acquisitions). Callers that already staged their own paths this way
    pass `stage=False` to `_commit_one` below so its own `add` is skipped
    — the combined `add` here is the ONLY staging acquisition for the
    whole per-item batch. A no-op (no acquisition at all) for an empty
    `paths` list — mirrors `_commit_one`'s own empty-paths no-op."""
    if not paths:
        return
    pathspec = ["--", *paths]
    add_proc = run_with_lock_retry(lambda: _run_git(["add", *pathspec], repo_root))
    if add_proc.returncode != 0:
        raise RuntimeError(
            f"git add {paths} failed (rc={add_proc.returncode}): "
            f"{add_proc.stderr.strip() or '<no stderr>'}"
        )


def _commit_one(
    repo_root: Path, paths: list[str], message: str, *, stage: bool = True
) -> Optional[str]:
    """Stage (unless `stage=False`) + commit exactly `paths`, pathspec-
    scoped on both `add` and `commit` (never `git add -A`/a bare `git
    commit`) — `apply` runs against a shared concurrent-EM working tree
    that may already carry a sibling session's own files staged, and a
    whole-tree add/commit would sweep those into this run's commit. The
    `add` (when `stage=True`) and `commit` calls are each wrapped in
    `coordinator_core.git_lock_retry.run_with_lock_retry`, so
    `.git/index.lock` contention from a sibling session's concurrent
    commit retries with bounded backoff instead of crashing this run on
    first contention; any other git failure still raises on the first
    attempt. Returns the new commit's SHA, or `None` when there was
    nothing staged for `paths` (a clean no-op, not a failure).

    `stage=False` (AC-7): `_dispatch_commit_per_item` pre-stages every
    verified item's paths in ONE combined `_stage_paths` call ahead of
    this loop, so each item's own `_commit_one` call here skips its
    redundant per-item `add` — the commit still fires per item
    (D-3(b)/(c)'s own per-item cadence is untouched; only the STAGING
    acquisition count changes, never the commit cardinality). Default
    `True` preserves this function's original single-call behaviour for
    every other caller (`_dispatch_commit_per_wave`, and this module's own
    pre-C5 test corpus that calls `_commit_one` directly)."""
    if not paths:
        return None
    pathspec = ["--", *paths]
    if stage:
        _stage_paths(repo_root, paths)
    unchanged = _run_git(["diff", "--cached", "--quiet", *pathspec], repo_root)
    if unchanged.returncode == 0:
        return None
    commit_proc = run_with_lock_retry(
        lambda: _run_git(["commit", "-m", message, *pathspec], repo_root)
    )
    if commit_proc.returncode != 0:
        raise RuntimeError(
            f"git commit {paths} failed (rc={commit_proc.returncode}): "
            f"{commit_proc.stderr.strip() or '<no stderr>'}"
        )
    sha_proc = _run_git(["rev-parse", "HEAD"], repo_root)
    return sha_proc.stdout.strip() if sha_proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# commit-per-item / commit-per-wave — D-3's two commit verbs. Both read a
# SINGLE JSON string at `args[0]`:
#   {"items": [{"paths": [str, ...], "message": str, "verified"?: bool,
#               "backlog_note"?: str}, ...],
#    "branch": str, "expected_branch": str, "message"?: str}
# `_prepare_directives_for_dispatch` is the ONE place that builds this
# shape from a real `directives.build_stage_and_commit` directive; the C1
# contract test's `_commit_directive_args` builds it directly to pin the
# handler's own contract in isolation (module docstring).
# ---------------------------------------------------------------------------

_COMMIT_PER_ITEM_CLI = "commit-per-item"
_COMMIT_PER_WAVE_CLI = "commit-per-wave"


def _parse_commit_payload(args: list[str]) -> dict[str, Any]:
    if not args:
        raise UnrecognizedDirective("commit verb: expected a JSON payload at args[0]")
    try:
        payload = json.loads(args[0])
    except (json.JSONDecodeError, TypeError) as exc:
        raise UnrecognizedDirective(f"commit verb: malformed JSON payload: {exc}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise UnrecognizedDirective(
            "commit verb: payload must be an object with an 'items' list"
        )
    return payload


def _dispatch_checkout_and_backlog_note(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`checkout-and-backlog-note` — D-3(c)'s per-item non-PASS failure
    path (`git checkout -- <paths>`), the standalone closed-dispatch
    entry `directives.py`'s `on_non_pass` sub-directive names by `cli`.
    `args` == `["checkout", "--", <path>, ...]` — the exact shape
    `directives.build_stage_and_commit` already writes into
    `directive["on_non_pass"]["args"]`, so this handler needs no
    JSON-repacking (contrast the commit verbs above). The backlog-note
    TEXT itself is not carried in `args` (it is a sibling
    `on_non_pass["backlog_note"]` key `apply_base`'s handler signature
    cannot forward) — `_dispatch_commit_per_item` calls
    `_append_backlog_note` separately, with the note text it already has
    in hand from the parsed JSON payload, when it invokes this checkout
    primitive internally for a non-PASS item."""
    if len(args) < 2 or args[0] != "checkout" or args[1] != "--":
        raise UnrecognizedDirective(
            f"checkout-and-backlog-note: expected ['checkout', '--', <paths...>], got {args!r}"
        )
    paths = args[2:]
    if not paths:
        return {"cli": "checkout-and-backlog-note", "checked_out": []}
    proc = _run_git(["checkout", "--", *paths], repo_root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git checkout -- {paths} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return {"cli": "checkout-and-backlog-note", "checked_out": list(paths)}


#: Scratch log a non-PASS item's backlog note is appended to. Deliberately
#: NOT staged/committed as part of the pathspec-scoped commit this module
#: makes — this is a local, append-only record for the operator, not a
#: structured queue entry (this module has no spec for authoring one; see
#: the module docstring's substrate note and this chunk's exit interview).
_NON_PASS_NOTE_LOG = Path("state/scratch/backlog-grind/non-pass-notes.log")


def _append_backlog_note(repo_root: Path, note: str) -> None:
    log_path = _assert_in_repo_root(_NON_PASS_NOTE_LOG, repo_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{note}\n")


def _non_pass_checkout(repo_root: Path, paths: list[str], note: str) -> dict[str, Any]:
    detail = _dispatch_checkout_and_backlog_note(["checkout", "--", *paths], repo_root)
    _append_backlog_note(repo_root, note)
    detail["backlog_note"] = note
    return detail


def _unstage_or_chain(repo_root: Path, paths: list[str], original_exc: BaseException) -> None:
    """Reverses `paths` via `_unstage_paths` before `original_exc`
    propagates. If `_unstage_paths` itself fails with an `Exception` — its
    own `git reset` goes through `run_with_lock_retry` (`_unstage_paths`'s
    own docstring), so exhaustion under exactly the contention this module
    targets is plausible, not hypothetical — that SECOND failure must never
    silently replace `original_exc` in flight (that would mask a
    `BranchMismatch`/the original `_commit_one`/`_stage_paths` failure from
    any caller doing `except BranchMismatch`). Chains the cleanup failure as
    `__cause__` instead of raising it: `raise original_exc from cleanup_exc`
    re-raises the SAME original exception object (unmasked), with the
    cleanup failure attached for diagnosis rather than substituted in its
    place.

    Scope (Review: code-reviewer — P3, deferred, docstring-only follow-up):
    this guarantee is bounded to the `Exception` hierarchy, deliberately —
    the `except Exception` below (mirrored at both this function's call
    sites, `_dispatch_commit_per_item`'s pre-loop guard and its per-item
    loop) does NOT catch `BaseException` subclasses outside `Exception`
    (`KeyboardInterrupt`, `SystemExit`). A `KeyboardInterrupt` raised by
    `_unstage_paths` here propagates on its own rather than being chained —
    deliberately not widened to `except BaseException`, which would instead
    swallow that interrupt and deliver `original_exc` in its place,
    fighting the operator's own Ctrl-C rather than honoring it. Widening is
    declined; this docstring previously implied unconditional "never
    silently replace" coverage, which overstated what the code actually
    guarantees."""
    try:
        _unstage_paths(repo_root, paths)
    except Exception as cleanup_exc:
        raise original_exc from cleanup_exc


def _unstage_paths(repo_root: Path, paths: list[str]) -> None:
    """`git reset -- <paths>` for a COMBINED, EXPLICIT pathspec only —
    never a bare `git reset` (that would sweep a sibling session's own
    staged work off this shared index, the same residue bug class in the
    opposite direction, and doctrine forbids it). Wrapped in
    `run_with_lock_retry` like every other index-touching call in this
    module. Called ONLY from `_dispatch_commit_per_item`'s abnormal-exit
    cleanup, to close the pre-staged-but-not-yet-committed residue window
    a mid-loop `BranchMismatch` would otherwise leave open indefinitely on
    this shared `.git/index` (`docs/reference/shared-index-residue.md`,
    "Arm C"). A no-op for an empty `paths` list."""
    if not paths:
        return
    pathspec = ["--", *paths]
    reset_proc = run_with_lock_retry(lambda: _run_git(["reset", *pathspec], repo_root))
    if reset_proc.returncode != 0:
        raise RuntimeError(
            f"git reset {paths} failed (rc={reset_proc.returncode}): "
            f"{reset_proc.stderr.strip() or '<no stderr>'}"
        )


def _dispatch_commit_per_item(args: list[str], repo_root: Path) -> dict[str, Any]:
    """D-3's `per-item` granularity: one `git add`/`git commit` pair PER
    ITEM in the payload's `items` list, each re-verifying
    `expected_branch` immediately before its own commit (D-3(b),
    per-commit cadence) — a mid-loop branch flip raises `BranchMismatch`
    BEFORE that item's commit, halting the run without committing the
    remaining items (D-3(b)'s own halt requirement). An item carrying
    `"verified": false` never commits — it is routed to D-3(c)'s non-PASS
    failure sub-path (`git checkout -- <paths>` + an appended backlog
    note) instead, via `_non_pass_checkout`.

    AC-7 (acquisition count, not hold time — see this module's own docstring
    section and the plan's burst-source table): every VERIFIED item's paths
    are staged in ONE combined `_stage_paths` call BEFORE this loop runs,
    instead of one `add` acquisition per item — collapsing `add`+`commit`
    per item (2 x N) down to 1 `add` + N `commit`s. The commit granularity
    itself is deliberately NOT collapsed: D-3(b)'s per-item branch-recheck
    cadence and D-3(c)'s per-item non-PASS failure sub-path both depend on
    each item landing (or not) as its OWN commit, independently revertible
    — batching the commits too would lose exactly that audit trail (see the
    plan's anti-scope: "do not batch across a semantic boundary"). Each
    item's own `_commit_one` call below passes `stage=False` since its
    paths are already staged by the pre-pass; `_commit_one`'s own
    `git diff --cached --quiet` check (still per-item pathspec-scoped) is
    unaffected by other items' paths also being staged in the same index.

    Abnormal-exit cleanup: if the pre-pass `_stage_paths` call itself fails
    (a combined `add` can partially stage successfully-resolved paths and
    still return nonzero for a bad one among N — the collapse from N `add`
    calls to 1 widened this failure mode's blast radius from one item's
    paths to every verified item's), OR if a mid-loop item raises
    (`BranchMismatch`, or any other exception from `_non_pass_checkout`'s
    `git checkout`/backlog-note write, or from `_commit_one`), every
    VERIFIED item at or after the failing point is still staged from the
    pre-pass but will now never reach its own commit — that is exactly the
    residue window `docs/reference/shared-index-residue.md` ("Arm C")
    describes. Both branches of the per-item loop body (the non-PASS
    checkout arm and the verified commit arm) run inside the SAME
    try/except — a structural choice, not incidental: the residue window
    is a property of "this item's paths are staged but nothing has yet
    resolved them," which is true the instant the loop reaches an item
    regardless of which arm it takes, so cleanup covering only one arm
    would silently reopen the window for the other on every future edit
    that touches just one branch. `_unstage_or_chain` reverses ONLY the
    remaining verified items' paths (an explicit combined pathspec, never a
    bare `git reset`) before the original exception propagates — chaining
    rather than masking it if the reversal itself fails (see
    `_unstage_or_chain`'s own docstring). The happy path never calls it at
    all, so its 1 `add` + N `commit`s acquisition count is untouched."""
    payload = _parse_commit_payload(args)
    expected_branch = payload.get("expected_branch", "")
    items = payload["items"]

    verified_paths: list[str] = []
    for item in items:
        if item.get("verified", True):
            verified_paths.extend(item.get("paths") or [])
    try:
        _stage_paths(repo_root, verified_paths)
    except Exception as exc:
        _unstage_or_chain(repo_root, verified_paths, exc)
        raise

    commits: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        paths = list(item.get("paths") or [])
        message = item.get("message", "")
        try:
            if not item.get("verified", True):
                note = item.get("backlog_note") or f"non-PASS, reverted: {message}"
                commits.append(_non_pass_checkout(repo_root, paths, note))
                continue
            _assert_branch(repo_root, expected_branch)
            sha = _commit_one(repo_root, paths, message, stage=False)
        except Exception as exc:
            residue: list[str] = []
            for later_item in items[index:]:
                if later_item.get("verified", True):
                    residue.extend(later_item.get("paths") or [])
            _unstage_or_chain(repo_root, residue, exc)
            raise
        commits.append({"paths": paths, "message": message, "commit_sha": sha})
    return {"cli": _COMMIT_PER_ITEM_CLI, "commits": commits}


def _dispatch_commit_per_wave(args: list[str], repo_root: Path) -> dict[str, Any]:
    """D-3's `per-wave` granularity: ONE `git add`/`git commit` pair for
    every item's paths combined, re-verifying `expected_branch` exactly
    ONCE before that single commit (D-3(b), per-wave cadence — never
    once per item, which would silently degrade to per-item cost while
    claiming per-wave cardinality). Never emits D-3(c)'s non-PASS
    sub-path — that distinction is `per-item`-only by construction; a
    per-wave payload's items are assumed pre-filtered to already-verified
    work by whichever caller assembled the wave."""
    payload = _parse_commit_payload(args)
    expected_branch = payload.get("expected_branch", "")
    items = payload["items"]
    all_paths: list[str] = []
    messages: list[str] = []
    for item in items:
        all_paths.extend(item.get("paths") or [])
        msg = item.get("message")
        if msg:
            messages.append(msg)
    message = payload.get("message") or "; ".join(messages) or "backlog-grind wave commit"
    _assert_branch(repo_root, expected_branch)
    sha = _commit_one(repo_root, all_paths, message)
    return {"cli": _COMMIT_PER_WAVE_CLI, "paths": all_paths, "message": message, "commit_sha": sha}


# ---------------------------------------------------------------------------
# tier-u-grant-cli — the ONE cli in this table whose handler reaches
# outside `coordinator_core.backlog_grind_assemble` (into the EXISTING,
# already-live `coordinator_core.session.grant.write_tier_u_grant`,
# verified live on disk at chunk authoring time). Consumed by direct
# in-process import — same "call the existing primitive, never shell out
# to a bin trampoline for something with a real Python entrypoint"
# convention `pickup_assemble.apply`'s `_dispatch_archive_stamp_cli`
# already sets.
# ---------------------------------------------------------------------------

_TIER_U_GRANT_CLI = "tier-u-grant-cli"


def _dispatch_tier_u_grant_cli(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`args` == `["grant", <granted_by>, <note>, "--ceremony", <name>]`
    (the last two optional) — the exact shape
    `directives.build_tier_u_grant_flow` already writes, unpacked
    directly (`resolve_operator_config`; no JSON repacking needed, unlike
    the commit verbs — every value this handler needs is already a bare
    string in `args`)."""
    if not args or args[0] != "grant":
        raise UnrecognizedDirective(f"tier-u-grant-cli: unrecognized verb {args[:1]!r}")
    if len(args) < 3:
        raise UnrecognizedDirective(
            "tier-u-grant-cli grant: expected <granted_by> <note> [--ceremony <name>]"
        )
    granted_by, note = args[1], args[2]
    ceremony: Optional[str] = None
    if len(args) > 3:
        if args[3] == "--ceremony" and len(args) > 4:
            ceremony = args[4]
        else:
            raise UnrecognizedDirective(
                f"tier-u-grant-cli grant: unrecognized trailing args {args[3:]!r}"
            )
    ok = write_tier_u_grant(granted_by, note, ceremony=ceremony, cwd=str(repo_root))
    if not ok:
        raise RuntimeError(f"tier-u-grant-cli grant {granted_by}: write_tier_u_grant failed")
    return {
        "cli": _TIER_U_GRANT_CLI,
        "verb": "grant",
        "granted_by": granted_by,
        "ceremony": ceremony,
    }


# ---------------------------------------------------------------------------
# spinoff-handoff-template / executor-dispatch-prompt-template (AC26) —
# both surfaces' fixed narrated templates, already fully rendered into
# `directive["fields"]` by the compute-side readers via
# `directives.build_spinoff_handoff_template_emission` (`readers_blitz.py`
# only) / `build_executor_dispatch_prompt_template_emission` (both
# `readers_blitz.py` and `readers_mise.py`) at compute time. This
# handler performs NO further assembly (there is nothing left for it to
# compute — rendering lives entirely on the compute side, never here) — it
# simply hands the pre-rendered fields back through the mutating half's own
# report, so the caller (an EM/orchestrator reading `apply()`'s report)
# never hand-assembles the template itself (AC26's actual ask).
# ---------------------------------------------------------------------------

_SPINOFF_HANDOFF_TEMPLATE_CLI = "spinoff-handoff-template"
_EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI = "executor-dispatch-prompt-template"


def _parse_json_object_arg(args: list[str]) -> dict[str, Any]:
    if not args:
        return {}
    try:
        payload = json.loads(args[0])
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dispatch_spinoff_handoff_template(args: list[str], repo_root: Path) -> dict[str, Any]:
    return {"cli": _SPINOFF_HANDOFF_TEMPLATE_CLI, "fields": _parse_json_object_arg(args)}


def _dispatch_executor_dispatch_prompt_template(args: list[str], repo_root: Path) -> dict[str, Any]:
    return {
        "cli": _EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI,
        "fields": _parse_json_object_arg(args),
    }


def _dispatch_haiku_verifier(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`dispatch-haiku-verifier` names an action this closed dispatch
    table structurally cannot perform: spawning a Haiku agent is an
    orchestrator-level Task/Agent-tool action, never a git/file/
    subprocess primitive `apply.py` can shell out to or import. This
    handler does NOT fabricate an execution that never ran — it surfaces
    the verifier spec (`model`/`evidence_source`/`enum_set`/
    `output_path`, repacked into `args[0]` by
    `_prepare_directives_for_dispatch`) in its own report, for the
    calling orchestrator to act on. Never raises: an unactionable-by-
    apply directive is not a run failure."""
    return {
        "cli": HAIKU_VERIFIER_CLI,
        "requires_external_dispatch": True,
        "note": (
            "apply.py has no subprocess/import path to spawn a Haiku agent — "
            "dispatch the verifier described in 'spec' via the calling "
            "orchestrator's own Task/Agent tool."
        ),
        "spec": _parse_json_object_arg(args),
    }


#: THE closed dispatch table (AC3). Every key is a literal string written
#: here by hand — this dict is never mutated at runtime and never
#: consulted via anything but a plain `dict.get`/`in` on a
#: `directives[].cli` value (`apply_base.resolve_cli`). No
#: `coordinator-safe-commit` entry, by design (D-3).
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    _COMMIT_PER_ITEM_CLI: _dispatch_commit_per_item,
    _COMMIT_PER_WAVE_CLI: _dispatch_commit_per_wave,
    "checkout-and-backlog-note": _dispatch_checkout_and_backlog_note,
    _TIER_U_GRANT_CLI: _dispatch_tier_u_grant_cli,
    _SPINOFF_HANDOFF_TEMPLATE_CLI: _dispatch_spinoff_handoff_template,
    _EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI: _dispatch_executor_dispatch_prompt_template,
    HAIKU_VERIFIER_CLI: _dispatch_haiku_verifier,
}


# ---------------------------------------------------------------------------
# Directive/args-shape reconciliation — see the module docstring's own
# section on this. The ONLY function in this file that reads a directive's
# fields OTHER than `id`/`cli`/`args`/`depends_on`/`already_satisfied`
# (the keys `apply_base` itself understands) — everything downstream of
# this function operates on the repacked, self-contained shape.
# ---------------------------------------------------------------------------


def _prepare_directives_for_dispatch(directives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for directive in directives:
        cli = directive.get("cli")
        if cli in (_COMMIT_PER_ITEM_CLI, _COMMIT_PER_WAVE_CLI):
            directive = dict(directive)
            directive["args"] = [
                json.dumps(
                    {
                        "items": [
                            {
                                "paths": list(directive.get("args") or []),
                                "message": directive.get("message", ""),
                            }
                        ],
                        "branch": directive.get("branch", ""),
                        "expected_branch": directive.get("expected_branch", ""),
                    }
                )
            ]
        elif cli in (_SPINOFF_HANDOFF_TEMPLATE_CLI, _EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI):
            directive = dict(directive)
            directive["args"] = [json.dumps(directive.get("fields", {}))]
        elif cli == HAIKU_VERIFIER_CLI:
            directive = dict(directive)
            directive["args"] = [
                json.dumps(
                    {
                        "model": directive.get("model"),
                        "run_in_background": directive.get("run_in_background"),
                        "evidence_source": directive.get("evidence_source"),
                        "enum_set": directive.get("enum_set"),
                        "output_path": directive.get("output_path"),
                    }
                )
            ]
        prepared.append(directive)
    return prepared


# ---------------------------------------------------------------------------
# `apply()` / `drop()` orchestration.
# ---------------------------------------------------------------------------


def apply(
    cadence: str,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
    extra_directives: Optional[list[dict[str, Any]]] = None,
    run_id: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """`apply <cadence> [--session-id <id>] [--decisions <json>]
    [--run-id <run-id>]` — recomputes `brief(cadence)` in-process and
    executes its `directives[]` (plus any `extra_directives`) through the
    closed dispatch table. Returns `(exit_code, report)`.

    `run_id` (review: code-reviewer — F1) is passed straight through to
    the recomputed `brief()` call below, mirroring `main()`'s own
    `--run-id` passthrough (`__init__.py`) — this module's recompute of
    `brief(cadence)` is otherwise the ONE caller of `brief()` in this
    package that could never honestly supply it, since a mise Phase-6
    directive's `depends_on` wire resolves against THIS run's own
    judgment points (this function's own docstring below), and those
    judgment points are exactly what silently regressed to "unresolved"
    on every `apply mise-en-place` call once mise-inventory records
    existed and `apply()` had no `run_id` of its own to give `brief()`.

    `extra_directives` exists because `brief()`'s own readers do NOT
    manufacture live commit directives at boot time (`readers_mise.py`'s
    own negative-spec: "a static `collect(cadence)` call site ... cannot
    honestly construct [a commit directive] without inventing values" —
    the touched-file paths are only known live, mid-wave). A caller that
    HAS just computed a wave's real paths (e.g. the rebuilt
    `bug-blitz.md`/`mise-en-place.md` bodies, via
    `readers_blitz.build_commit_per_item`/`build_commit_per_wave`) passes
    the resulting directive(s) here rather than this module inventing a
    second, parallel way to inject them.

    Recomputes the brief on every call (never trusts a caller-supplied
    decision object) — `judgment_points` always comes from THIS run's own
    `brief(cadence)`, so a `depends_on` wire a live commit directive
    carries (e.g. bug-blitz's standing `j-bug-blitz-commit-readiness`
    gate, always present in `brief("bug-blitz")`'s own judgment_points
    regardless of backlog state) resolves against a judgment point that
    is genuinely in scope for this run, never a stale one.

    Halts at the first unresolved judgment point (`apply_base`'s own
    per-directive gate) and never overrides a denial — this module adds
    no claim-grant concept of its own (`resolve_claim_grant=None`):
    backlog-grind-assemble has no claimed-artifact lifecycle (see
    `drop()`'s own docstring).
    """
    root = repo_root or Path.cwd()

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(apply_base.SESSION_ENV_READ_ORDER)} — refusing the "
                "ambient tier-4 sentinel"
            ),
        }

    effective_decisions = decisions or {}

    with _session_identity(resolved_sid, env_vars=apply_base.SESSION_ENV_VARS):
        try:
            brief_result = brief(cadence, repo_root=root, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - transport-failure backstop, mirrors brief()'s own
            return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}

        decision = brief_result.decision_object
        directives = list(decision.get("directives", [])) + list(extra_directives or [])
        judgment_points = decision.get("judgment_points", [])

        prepared = _prepare_directives_for_dispatch(directives)

        exit_code, report = apply_base.execute_directives(
            prepared,
            judgment_points,
            root,
            _CLI_DISPATCH,
            decisions=effective_decisions,
        )
        return exit_code, report


def _usage(prog: str) -> int:
    cadence_list = "|".join(CADENCES)
    print(
        f"usage: {prog} apply <{cadence_list}> [--session-id <id>] [--decisions <json>]\n"
        "                    [--run-id <run-id>]\n"
        "                    [--wave-path <repo-relative-path>]...   (repeatable)\n"
        "                    [--granularity per-item|per-wave] [--message <commit message>]",
        file=sys.stderr,
    )
    print(f"       {prog} drop <{cadence_list}> [--session-id <id>]", file=sys.stderr)
    return APPLY_EXIT_TRANSPORT_FAIL


def _build_wave_path_directives(
    *,
    cadence: str,
    wave_paths: list[str],
    granularity: str,
    message: str,
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Validates every `--wave-path` (repo-relative, no escape — via the
    same `apply_base.assert_in_repo_root` every other handler in this file
    calls before touching a directive-derived filesystem path) and builds
    `directives.build_stage_and_commit` directive(s) from them. Raises
    `apply_base.OutOfRepoPath` on any path that fails validation — the
    caller refuses the WHOLE run, never a filtered subset.

    Mirrors the module docstring's own substrate note on this shape:
    `per-wave` is ONE `build_stage_and_commit` call over every collected
    path (one commit, D-3(a)); `per-item` is N SEPARATE calls, one per
    `--wave-path`, each its own directive with its own commit — never one
    directive whose single item bundles every path — because that is what
    preserves bug-blitz's per-item audit trail (the whole reason `per-item`
    exists as a distinct cardinality from `per-wave`). Each directive gets
    its own unique `id` (`order_by_depends_on` keys directives by `id` in a
    dict — a repeated id silently drops every directive after the first).

    `depends_on` is wired to bug-blitz's own standing
    `readers_bug_blitz.COMMIT_READINESS_JP_ID` commit-readiness gate when
    `cadence == "bug-blitz"` — the same wire `readers_bug_blitz.
    build_commit_per_item`/`build_commit_per_wave` apply to every commit
    directive that surface's own runtime builds. This CLI path is an
    ADDITIONAL way to reach `build_stage_and_commit`, not a way around the
    review-gate risk constraint that surface carries: a caller cannot
    spell a bug-blitz wave-path commit that skips it.
    """
    for raw in wave_paths:
        _assert_in_repo_root(Path(raw), repo_root)

    # Review: code-reviewer — F4: this was a cross-module reach into a
    # module-private (underscore-prefixed) constant with no __all__/export;
    # readers_blitz.py now exports COMMIT_READINESS_JP_ID publicly.
    depends_on = readers_bug_blitz.COMMIT_READINESS_JP_ID if cadence == "bug-blitz" else None
    branch = _current_branch(repo_root)

    if granularity == bga_directives.GRANULARITY_PER_WAVE:
        return [
            bga_directives.build_stage_and_commit(
                id="wave-path-per-wave",
                paths=wave_paths,
                message=message,
                granularity=granularity,
                branch=branch,
                expected_branch=branch,
                depends_on=depends_on,
            )
        ]

    return [
        bga_directives.build_stage_and_commit(
            id=f"wave-path-per-item-{idx}",
            paths=[path],
            message=message,
            granularity=granularity,
            branch=branch,
            expected_branch=branch,
            depends_on=depends_on,
        )
        for idx, path in enumerate(wave_paths)
    ]


def main_apply(argv: list[str]) -> int:
    if not argv:
        return _usage("backlog-grind-assemble")

    cadence = argv[0]
    tail = argv[1:]
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    wave_paths: list[str] = []
    granularity: Optional[str] = None
    message: Optional[str] = None
    run_id: Optional[str] = None
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--session-id":
            if i + 1 >= len(tail):
                return _usage("backlog-grind-assemble")
            session_id = tail[i + 1]
            i += 2
        elif tok == "--run-id":
            # Review: code-reviewer — F1: apply.py's recomputed brief()
            # never threaded --run-id, so mise Phase-6 deterministically
            # resolved to the "missing --run-id" judgment point on every
            # `apply mise-en-place` call once records existed. Mirrors
            # main()'s own --run-id handling (a missing value or a repeat
            # of the flag is a usage error, never silently ignored).
            if i + 1 >= len(tail) or run_id is not None:
                return _usage("backlog-grind-assemble")
            run_id = tail[i + 1]
            i += 2
        elif tok == "--decisions":
            if i + 1 >= len(tail):
                return _usage("backlog-grind-assemble")
            try:
                decisions = json.loads(tail[i + 1])
            except json.JSONDecodeError as exc:
                print(
                    f"backlog-grind-assemble apply: malformed --decisions JSON: {exc}",
                    file=sys.stderr,
                )
                return _usage("backlog-grind-assemble")
            i += 2
        elif tok == "--wave-path":
            if i + 1 >= len(tail):
                return _usage("backlog-grind-assemble")
            wave_paths.append(tail[i + 1])
            i += 2
        elif tok == "--granularity":
            if i + 1 >= len(tail):
                return _usage("backlog-grind-assemble")
            granularity = tail[i + 1]
            i += 2
        elif tok == "--message":
            if i + 1 >= len(tail):
                return _usage("backlog-grind-assemble")
            message = tail[i + 1]
            i += 2
        else:
            print(f"backlog-grind-assemble apply: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage("backlog-grind-assemble")

    if wave_paths:
        if granularity is None or message is None:
            print(
                "backlog-grind-assemble apply: --wave-path requires both "
                "--granularity and --message (the two commit cardinalities "
                "are semantically different -- there is no safe default)",
                file=sys.stderr,
            )
            return _usage("backlog-grind-assemble")
        if granularity not in (bga_directives.GRANULARITY_PER_ITEM, bga_directives.GRANULARITY_PER_WAVE):
            print(
                f"backlog-grind-assemble apply: unrecognized --granularity {granularity!r} "
                f"(must be one of {bga_directives.GRANULARITY_PER_ITEM!r}, "
                f"{bga_directives.GRANULARITY_PER_WAVE!r})",
                file=sys.stderr,
            )
            return _usage("backlog-grind-assemble")
    elif granularity is not None or message is not None:
        print(
            "backlog-grind-assemble apply: --granularity/--message require --wave-path",
            file=sys.stderr,
        )
        return _usage("backlog-grind-assemble")

    extra_directives: Optional[list[dict[str, Any]]] = None
    if wave_paths:
        root = Path.cwd()
        try:
            extra_directives = _build_wave_path_directives(
                cadence=cadence,
                wave_paths=wave_paths,
                granularity=granularity,
                message=message,
                repo_root=root,
            )
        except OutOfRepoPath as exc:
            print(f"backlog-grind-assemble apply: {exc}", file=sys.stderr)
            return APPLY_EXIT_TRANSPORT_FAIL

    exit_code, report = apply(
        cadence,
        session_id=session_id,
        decisions=decisions,
        extra_directives=extra_directives,
        run_id=run_id,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


# ---------------------------------------------------------------------------
# `drop` — the AC4 inverse subcommand. Composed with `pickup_assemble`/
# `baton_assemble`'s own `drop()` in mind, but backlog-grind-assemble is
# NOT a claimed-artifact lifecycle the way those two are: `cadence` names
# WHICH mirror surface is asking, it is never itself claimed, parked, or
# handed off (there is no `claim_artifact`/`release_artifact` call
# anywhere in this package — `directives.py` never builds a
# `session-claim-cli` directive). `drop()` is therefore a deliberately
# honest, documented no-op — it returns `APPLY_EXIT_OK` and says so,
# rather than either fabricating a claim-release this assembler has no
# claim state to release, or omitting the subcommand and failing AC4's
# "exists and is callable" bar (the C1 contract test's own scope for this
# AC).
# ---------------------------------------------------------------------------


def drop(
    cadence: str,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> tuple[int, dict[str, Any]]:
    """`drop <cadence> [--session-id <id>]` — AC4's inverse subcommand.
    backlog-grind-assemble carries no claim/artifact-lifecycle state to
    revert (see this section's own docstring above) — returns
    `APPLY_EXIT_OK` with a report explaining why there is nothing to
    release, never `APPLY_EXIT_TRANSPORT_FAIL`, for any recognized
    `cadence`."""
    root = repo_root or Path.cwd()

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(apply_base.SESSION_ENV_READ_ORDER)} — refusing the "
                "ambient tier-4 sentinel"
            ),
        }
    if cadence not in CADENCES:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": f"backlog-grind-assemble drop: unrecognized cadence {cadence!r}",
        }

    with _session_identity(resolved_sid, env_vars=apply_base.SESSION_ENV_VARS):
        return APPLY_EXIT_OK, {
            "cadence": cadence,
            "released": False,
            "note": (
                "backlog-grind-assemble carries no claim/artifact-lifecycle "
                "state to release — cadence selects which mirror surface is "
                "asking, it is never itself claimed the way a pickup/baton "
                "artifact is (no session-claim-cli directive exists anywhere "
                "in this package). drop() is a deliberate, documented no-op "
                "provided for interface parity with pickup_assemble/"
                "baton_assemble's own drop()."
            ),
        }


def _usage_drop(prog: str) -> int:
    return _usage(prog)


def main_drop(argv: list[str]) -> int:
    if not argv:
        return _usage_drop("backlog-grind-assemble")

    cadence = argv[0]
    tail = argv[1:]
    session_id: Optional[str] = None
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--session-id":
            if i + 1 >= len(tail):
                return _usage_drop("backlog-grind-assemble")
            session_id = tail[i + 1]
            i += 2
        else:
            print(f"backlog-grind-assemble drop: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage_drop("backlog-grind-assemble")

    exit_code, report = drop(cadence, session_id=session_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code
