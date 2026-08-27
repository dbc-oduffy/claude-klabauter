"""coordinator_core.consolidate_assemble.apply — the MUTATING half
`consolidate_assemble.brief()`'s read-only decision object hands off to.

Recomputes the brief in-process, then dispatches its `directives[]` through
`coordinator_core.contract.apply_base`'s shared directive-execution engine
(composed, never re-derived — chunk C2/C8 AC): dependency ordering,
per-directive judgment gating, session-identity propagation, and in-repo
path safety all live in `apply_base`; this module supplies only its own
closed `_CLI_DISPATCH` table (one handler per `cli` name
`consolidate_assemble.brief` names: `delete-only`, `cherry-pick-and-delete`,
`merge-and-delete`, `worktree-remove`, `worktree-prune`, `fetch-prune`) and
the git plumbing those handlers invoke.

Every handler shells out to `git` via an explicit argv list through the
module-local `_run_git` — never a shell string built from `directives[].
args`. `COORDINATOR_OVERRIDE_BRANCH` (when it names the branch a
`delete-only`/`*-and-delete` directive is about to remove) selects a force
branch-delete (`git branch -D`) over the default safe delete (`git branch
-d`, which git itself refuses on an unmerged branch) — the one place this
module's own mechanics differ from a bare default, and it is expressed as
an env-var-selected argv flag, never a judgment bypass.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C8

Negative-spec:
    - Do NOT add a dispatch entry that resolves `cli` via `getattr`,
      `importlib`, or any brief-derived string — every entry in
      `_CLI_DISPATCH` is a literal key written by hand in this file.
    - Do NOT build a subprocess argv via string interpolation/shell=True —
      every handler below passes a literal list to `subprocess.run`.
    - Do NOT special-case consolidate's real divergence from the
      `apply_base` contract at this call site (the DR-092 anti-pattern) —
      per `consolidate_assemble/__init__.py`'s own module docstring, feed a
      real divergence back into `apply_base`'s parameters instead.
    - Do NOT trust a caller-supplied decision object as the mutation plan —
      `apply()` recomputes `brief()` itself.
    - Do NOT clean up a failed cherry-pick with `git cherry-pick --abort`,
      or `--quit` followed by a tree-wide `git reset --hard HEAD` / `git
      checkout -- .` — `repo_root` is a SHARED working tree serving
      50-70 concurrent sessions on this machine; any of those destroys
      every peer session's uncommitted work across the whole repo, not
      just this cherry-pick's damage. See `_clean_cherry_pick_conflict`,
      which scopes the restore to exactly the paths git reports unmerged.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.contract import apply_base
from coordinator_core.consolidate_assemble import brief
from coordinator_core.telemetry.composition_record import (
    flush_composition_record,
    make_fleet_budget,
)

APPLY_EXIT_OK = apply_base.APPLY_EXIT_OK
APPLY_EXIT_HALTED_AT_JUDGMENT = apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
APPLY_EXIT_CLAIM_DENIED = apply_base.APPLY_EXIT_CLAIM_DENIED
APPLY_EXIT_TRANSPORT_FAIL = apply_base.APPLY_EXIT_TRANSPORT_FAIL
APPLY_EXIT_PARTIAL_MUTATION = apply_base.APPLY_EXIT_PARTIAL_MUTATION

_resolve_explicit_session_id = apply_base.resolve_explicit_session_id
_session_identity = apply_base.session_identity

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def _fail(cli: str, proc: "subprocess.CompletedProcess[str]") -> None:
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cli}: exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip() or '<no output>'}"
        )


def _current_branch(repo_root: Path) -> str:
    proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    _fail("rev-parse", proc)
    return proc.stdout.strip()


def _unique_commit_shas(repo_root: Path, current: str, ref: str) -> list[str]:
    """Oldest-first SHA list for `current..ref` — the order a cherry-pick
    sequence must apply them in to preserve the original commit order."""
    proc = _run_git(["log", "--format=%H", "--reverse", f"{current}..{ref}"], repo_root)
    _fail("log", proc)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _branch_exists_locally(name: str, repo_root: Path) -> bool:
    """A remote-only branch has no `refs/heads/` entry; deleting it locally is
    not a no-op but a hard git failure, so the local leg is skipped for it."""
    return _run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo_root).returncode == 0


def _delete_branch(name: str, remote: bool, repo_root: Path) -> dict[str, Any]:
    import os

    force = os.environ.get("COORDINATOR_OVERRIDE_BRANCH", "") == name
    flag = "-D" if force else "-d"
    detail: dict[str, Any] = {"local_deleted": None, "forced": force}
    if _branch_exists_locally(name, repo_root):
        local_proc = _run_git(["branch", flag, name], repo_root)
        _fail("branch-delete", local_proc)
        detail["local_deleted"] = name
    if remote:
        remote_proc = _run_git(["push", "origin", "--delete", name], repo_root)
        _fail("push-delete", remote_proc)
        detail["remote_deleted"] = name
    return detail


def _dispatch_delete_only(args: list[str], repo_root: Path) -> dict[str, Any]:
    name = args[0]
    remote = len(args) > 1 and args[1] == "origin"
    detail = _delete_branch(name, remote, repo_root)
    return {"cli": "delete-only", **detail}


def _clean_cherry_pick_conflict(repo_root: Path) -> None:
    """On a failed multi-commit cherry-pick, restore ONLY the conflicted
    paths to `HEAD` before clearing the sequencer with `--quit`.

    `--quit` (per `git-cherry-pick(1)`) forgets the sequencer bookkeeping
    ONLY — it does not touch the index or the working tree. Left alone, the
    conflicted paths stay as unmerged index entries with live `<<<<<<<`
    conflict markers written into the files on disk.

    NEGATIVE-SPEC — do NOT replace this with `git cherry-pick --abort`, or
    `--quit` followed by a tree-wide `git reset --hard HEAD` / `git checkout
    -- .`. `repo_root` is a SHARED working tree serving 50-70 concurrent
    sessions on this machine; any of those would discard every peer
    session's uncommitted work across the ENTIRE repo, not just this
    cherry-pick's damage. Scoping the restore to exactly the paths git
    reports as unmerged is safe precisely because git already destroyed
    those files' content with conflict markers — nothing further is lost by
    resetting them to HEAD — while every other file, where peer sessions'
    real uncommitted work lives, is never touched.

    Order: enumerate + restore the conflicted paths FIRST, `--quit` second.
    The sequencer state (`.git/sequencer/`) is independent bookkeeping from
    the index/working-tree state these paths carry, so reading the unmerged
    set before clearing it removes any risk of the enumeration racing its
    own cleanup.
    """
    unmerged_proc = _run_git(["diff", "--name-only", "--diff-filter=U", "-z"], repo_root)
    _fail("diff --diff-filter=U", unmerged_proc)
    unmerged_paths = [p for p in unmerged_proc.stdout.split("\0") if p]
    if unmerged_paths:
        restore_proc = _run_git(["checkout", "HEAD", "--", *unmerged_paths], repo_root)
        _fail("checkout-conflict-cleanup", restore_proc)
    quit_proc = _run_git(["cherry-pick", "--quit"], repo_root)
    if quit_proc.returncode != 0:
        raise RuntimeError(
            "cherry-pick --quit: exited "
            f"{quit_proc.returncode}: "
            f"{quit_proc.stderr.strip() or quit_proc.stdout.strip() or '<no output>'} "
            "— sequencer left in place; clear it by hand with `git cherry-pick --quit`"
        )


def _dispatch_cherry_pick_and_delete(args: list[str], repo_root: Path) -> dict[str, Any]:
    name, ref = args[0], args[1]
    remote = len(args) > 2 and args[2] == "origin"
    current = _current_branch(repo_root)
    shas = _unique_commit_shas(repo_root, current, ref)
    # One `git cherry-pick` invocation carrying every sha (in the same
    # oldest-first order `_unique_commit_shas` already returns) instead of
    # one spawn per commit — git applies a multi-commit cherry-pick
    # sequentially and stops at the first conflict/failure exactly like the
    # former per-sha loop did, so `_fail` still reports the same failure at
    # the same point. Guarded empty: `git cherry-pick` with no arguments is
    # a usage error, not a no-op.
    if shas:
        proc = _run_git(["cherry-pick", *shas], repo_root)
        if proc.returncode != 0:
            # A multi-commit cherry-pick that stops leaves a `.git/sequencer`
            # directory the former one-sha-per-spawn form never created, and a
            # repo left mid-sequence refuses the next cherry-pick in ANY session
            # sharing this tree. See `_clean_cherry_pick_conflict` for the
            # scoped cleanup this requires and why a tree-wide reset/abort is
            # forbidden here.
            _clean_cherry_pick_conflict(repo_root)
        _fail("cherry-pick", proc)
    delete_detail = _delete_branch(name, remote, repo_root)
    return {"cli": "cherry-pick-and-delete", "commits": shas, **delete_detail}


def _dispatch_merge_and_delete(args: list[str], repo_root: Path) -> dict[str, Any]:
    name, ref = args[0], args[1]
    remote = len(args) > 2 and args[2] == "origin"
    merge_proc = _run_git(["merge", "--no-ff", ref], repo_root)
    _fail("merge", merge_proc)
    delete_detail = _delete_branch(name, remote, repo_root)
    return {"cli": "merge-and-delete", **delete_detail}


def _dispatch_worktree_remove(args: list[str], repo_root: Path) -> dict[str, Any]:
    wt_path = args[0]
    proc = _run_git(["worktree", "remove", "--force", wt_path], repo_root)
    _fail("worktree-remove", proc)
    return {"cli": "worktree-remove", "path": wt_path}


def _dispatch_worktree_prune(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_git(["worktree", "prune"], repo_root)
    _fail("worktree-prune", proc)
    return {"cli": "worktree-prune"}


def _dispatch_fetch_prune(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_git(["fetch", "--prune"], repo_root)
    _fail("fetch-prune", proc)
    return {"cli": "fetch-prune"}


#: C6 discriminator decision (docs/plans/2026-08-19-directives-name-an-op-not-
#: a-cli.md § C6 / § The discriminator for the mixed end state) — measured
#: live against `coordinator_core.authz.registration_quad._live_registry()`
#: this chunk: NONE of consolidate's six verbs (`delete-only`,
#: `cherry-pick-and-delete`, `merge-and-delete`, `worktree-remove`,
#: `worktree-prune`, `fetch-prune`) resolve to a registered op, so ALL SIX
#: stay `cli`-named — none migrate to `op`. No new op is minted to force a
#: migration (out of scope by name). Every one of the six is a `git`
#: plumbing call the module's own `_run_git` makes directly off a
#: hardcoded `["git", ...]` argv — never `bash`/`sh`, so
#: `docs/reference/shell-out-carve-outs.md` (scoped to interpreter/shell
#: spawns) does not apply — and none is a `CONSUMES_MANIFEST`-driven script
#: module in the completion-family sense, so no `CONSUMES_MANIFEST` entry
#: applies either. Consequently `ASSEMBLER_DISPATCHABLE`
#: (coordinator_core/authz/dispatchable.py) gains NO `"consolidate_assemble"`
#: entry from this chunk (C1's "ship it EMPTY except for entries actually
#: migrated" — zero migrated here).
#:
#: THE closed dispatch table — every key is a literal string written here
#: by hand, matching `consolidate_assemble.brief`'s `directives[].cli` values.
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "delete-only": _dispatch_delete_only,
    "cherry-pick-and-delete": _dispatch_cherry_pick_and_delete,
    "merge-and-delete": _dispatch_merge_and_delete,
    "worktree-remove": _dispatch_worktree_remove,
    "worktree-prune": _dispatch_worktree_prune,
    "fetch-prune": _dispatch_fetch_prune,
}


def apply(
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    my_email: Optional[str] = None,
    decisions: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """`apply [--session-id <id>] [--decisions <json>]` — recomputes the
    brief in-process and executes its `directives[]` through
    `apply_base.execute_directives` against this module's closed dispatch
    table. Returns `(exit_code, report)`."""
    root = repo_root or Path.cwd()
    composition_budget = make_fleet_budget("consolidate_assemble")

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
            decision = brief(repo_root=root, my_email=my_email)
        except Exception as exc:  # noqa: BLE001 - transport-failure backstop
            return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}

        directives = decision.get("directives", [])
        judgment_points = decision.get("judgment_points", [])

        outcome = "directive_failed"
        try:
            exit_code, report = apply_base.execute_directives(
                directives,
                judgment_points,
                root,
                _CLI_DISPATCH,
                decisions=effective_decisions,
                composition_budget=composition_budget,
            )
            if exit_code == apply_base.APPLY_EXIT_OK:
                outcome = "success"
            elif exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION:
                outcome = "partial_mutation"
        finally:
            flush_composition_record(composition_budget, outcome)
        return exit_code, report


def _usage(prog: str) -> int:
    print(
        f"usage: {prog} apply [--session-id <id>] "
        "[--decisions <json> | --decisions-file <path>]",
        file=sys.stderr,
    )
    return APPLY_EXIT_TRANSPORT_FAIL


def main_apply(argv: list[str]) -> int:
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    conflict = detect_conflicting_payload_channels(argv)
    if conflict is not None:
        print(f"consolidate-assemble apply: {conflict}", file=sys.stderr)
        return _usage("consolidate-assemble")
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--session-id":
            if i + 1 >= len(argv):
                return _usage("consolidate-assemble")
            session_id = argv[i + 1]
            i += 2
        elif (payload := resolve_json_payload_flag(argv, i)).consumed:
            if payload.error is not None:
                print(f"consolidate-assemble apply: {payload.error}", file=sys.stderr)
                return _usage("consolidate-assemble")
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"consolidate-assemble apply: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage("consolidate-assemble")

    exit_code, report = apply(session_id=session_id, decisions=decisions)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code
