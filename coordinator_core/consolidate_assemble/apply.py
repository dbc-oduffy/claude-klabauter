"""coordinator_core.consolidate_assemble.apply — the MUTATING half
`consolidate_assemble.brief()`'s read-only decision object hands off to.

Recomputes the brief in-process, then dispatches its `directives[]` through
`coordinator_core.contract.apply_base`'s shared directive-execution engine
(composed, never re-derived — chunk C2/C8 AC): dependency ordering,
per-directive judgment gating, session-identity propagation, and in-repo
path safety all live in `apply_base`; this module supplies only its own
closed `_CLI_DISPATCH` table (one handler per `cli` name
`consolidate_assemble.brief` names: `delete-only`, `merge-and-delete`,
`merge-only`, `merge-ours`, `worktree-remove`, `worktree-prune`, `fetch-prune`) and
the git plumbing those handlers invoke.

Every handler shells out to `git` via an explicit argv list through the
module-local `_run_git` — never a shell string built from `directives[].
args`. `COORDINATOR_OVERRIDE_BRANCH` (when it names the branch a
`delete-only`/`*-and-delete` directive is about to remove) skips the
tip-reachability and unknown-holder refusals; a definite live holder and
`refs/preserve/*` still refuse. It is never a judgment bypass.

Contract (frozen, reviewed): coordinator-content-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: coordinator-content-repo:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C8

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
    - Do NOT absorb by cherry-pick: absorb is a true merge, so no commit is
      orphaned. A conflicted merge runs `git merge --abort`, keeps the
      branch, and raises naming the ref.
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
from coordinator_core.git.machine_local_paths import MACHINE_LOCAL_PATHSPEC, machine_local_refusal
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


def _branch_exists_locally(name: str, repo_root: Path) -> bool:
    return _run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo_root).returncode == 0


def _refuse_unmerged_tips(name: str, remote: bool, repo_root: Path) -> Optional[str]:
    """None when every tip about to be deleted is an ancestor of HEAD; else a
    refusal naming each tip that is not. Spawns: one ref lookup + one
    `merge-base --is-ancestor` per distinct tip (local and remote usually
    share one)."""
    wanted = [f"refs/heads/{name}"] + ([f"refs/remotes/origin/{name}"] if remote else [])
    probe = _run_git(["for-each-ref", "--format=%(refname) %(objectname)", *wanted], repo_root)
    _fail("for-each-ref", probe)
    refs = dict(line.split(" ", 1) for line in probe.stdout.splitlines() if " " in line)
    tips = {r: refs[r] for r in wanted if r in refs}
    reachable: dict[str, bool] = {}
    for sha in sorted(set(tips.values())):
        proc = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], repo_root)
        if proc.returncode not in (0, 1):
            _fail("merge-base", proc)
        reachable[sha] = proc.returncode == 0
    parts = [
        f"{ref.removeprefix('refs/')} {sha} is not an ancestor of HEAD"
        for ref, sha in tips.items()
        if not reachable[sha]
    ]
    return f"{name}: refusing delete: " + "; ".join(parts) if parts else None


def _refuse_held_or_protected(name: str, repo_root: Path, force: bool = False) -> Optional[str]:
    """A refusal when `name` is under refs/preserve/ or a live session may hold
    it (a holder, or None = unknown; `force` waives only unknown); else None."""
    if name.removeprefix("refs/").startswith("preserve/"):
        return f"{name}: refusing delete: refs/preserve/* is never touched"
    from coordinator_core.consolidate_assemble.live_holders import live_holders

    holders = live_holders(_run_git, repo_root, [name]).get(name)
    if holders is None:
        if force:
            return None
        return f"{name}: refusing delete: refused-unknown, no live-holder answer for this branch"
    if holders:
        return f"{name}: refusing delete: held by {'; '.join(holders)}"
    return None


def _delete_branch(name: str, remote: bool, repo_root: Path) -> dict[str, Any]:
    import os

    force = os.environ.get("COORDINATOR_OVERRIDE_BRANCH", "") == name
    refusal = _refuse_held_or_protected(name, repo_root, force)
    if refusal is None and not force:
        refusal = _refuse_unmerged_tips(name, remote, repo_root)
    if refusal is not None:
        raise RuntimeError(refusal)
    # `-D`: `-d` tests ancestry against the branch's upstream, not HEAD.
    flag = "-D"
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


def _refuse_machine_local_additions(ref: str, repo_root: Path) -> None:
    """Raise before any merge or pick when `ref` adds a machine-local path HEAD lacks.
    One three-dot diff per directive."""
    proc = _run_git(
        ["diff", "--no-renames", "--diff-filter=AM", "--name-only", "-z", f"HEAD...{ref}",
         "--", MACHINE_LOCAL_PATHSPEC],
        repo_root,
    )
    _fail("diff-machine-local", proc)
    refusal = machine_local_refusal(p for p in proc.stdout.split("\0") if p)
    if refusal is not None:
        raise RuntimeError(f"{ref}: {refusal}")


def _merge_ref(ref: str, repo_root: Path) -> None:
    # `--no-edit`: with no terminal the message editor fails AFTER the merge
    # result is staged, stranding MERGE_HEAD in a shared tree for the next
    # committer to consume. Any failure aborts, so no in-progress merge is left.
    _refuse_machine_local_additions(ref, repo_root)
    _run_merge(["merge", "--no-ff", "--no-edit", ref], ref, repo_root)


def _run_merge(argv: list[str], ref: str, repo_root: Path) -> None:
    merge_proc = _run_git(argv, repo_root)
    if merge_proc.returncode != 0:
        _run_git(["merge", "--abort"], repo_root)
        raise RuntimeError(
            f"merge conflict or failure absorbing {ref}, merge aborted, branch kept: "
            f"exited {merge_proc.returncode}: "
            f"{merge_proc.stderr.strip() or merge_proc.stdout.strip() or '<no output>'}"
        )


def _dispatch_merge_ours(args: list[str], repo_root: Path) -> dict[str, Any]:
    """Record `ref` as merged without taking its content; `args` = name, ref,
    superseding sha. Never deletes."""
    ref, sha = args[1], args[2]
    _run_merge(
        ["merge", "-s", "ours", "--no-edit", "-m", f"absorb {ref}: superseded by {sha}", ref],
        ref,
        repo_root,
    )
    return {"cli": "merge-ours", "superseded_by": sha}


def _dispatch_merge_and_delete(args: list[str], repo_root: Path) -> dict[str, Any]:
    name, ref = args[0], args[1]
    remote = len(args) > 2 and args[2] == "origin"
    _merge_ref(ref, repo_root)
    delete_detail = _delete_branch(name, remote, repo_root)
    return {"cli": "merge-and-delete", **delete_detail}


def _dispatch_merge_only(args: list[str], repo_root: Path) -> dict[str, Any]:
    """Absorb without deleting: the verb for a branch the recency floor keeps."""
    _merge_ref(args[1], repo_root)
    return {"cli": "merge-only"}


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
#: this chunk: NONE of consolidate's verbs (`delete-only`,
#: `merge-and-delete`, `merge-only`, `merge-ours`, `worktree-remove`,
#: `worktree-prune`, `fetch-prune`) resolve to a registered op, so ALL
#: stay `cli`-named — none migrate to `op`. No new op is minted to force a
#: migration (out of scope by name). Every one of them is a `git`
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
    "merge-and-delete": _dispatch_merge_and_delete,
    "merge-only": _dispatch_merge_only,
    "merge-ours": _dispatch_merge_ours,
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
    superseded: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
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
            decision = brief(repo_root=root, my_email=my_email, superseded=superseded)
        except Exception as exc:  # noqa: BLE001 - transport-failure backstop
            return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}

        directives = decision.get("directives", [])
        judgment_points = decision.get("judgment_points", [])

        outcome = "directive_failed"
        exit_label = None
        try:
            exit_code, report = apply_base.execute_directives(
                directives,
                judgment_points,
                root,
                _CLI_DISPATCH,
                decisions=effective_decisions,
                composition_budget=composition_budget,
            )
            exit_label = apply_base.exit_code_label(exit_code, report)
            if exit_code == apply_base.APPLY_EXIT_OK:
                outcome = "success"
            elif exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION:
                outcome = "partial_mutation"
        finally:
            flush_composition_record(composition_budget, outcome, exit_code_label=exit_label)
        return exit_code, report


_APPLY_HELP = """usage: {prog} apply [--session-id <id>] [--superseded <branch>=<sha>]... [--decisions <json> | --decisions-file <path>]

Recomputes the brief and executes its directives.

  --session-id <id>         session identity; falls back to the session env vars
  --superseded <branch>=<sha>  repeat the brief's flag so its merge-ours directive is recomputed
  --decisions <json>        judgment-point answers, inline JSON
  --decisions-file <path>   judgment-point answers read from a JSON file
                            (exclusive with --decisions)
  -h, --help                print this help and exit 0

A directive behind an unanswered judgment point does not run. A branch whose tip is
younger than 24h is never deleted: absorbing it keeps it."""


def _usage(prog: str) -> int:
    print(_APPLY_HELP.format(prog=prog), file=sys.stderr)
    return APPLY_EXIT_TRANSPORT_FAIL


def main_apply(argv: list[str]) -> int:
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    superseded: dict[str, str] = {}
    if any(tok in ("--help", "-h") for tok in argv):
        print(_APPLY_HELP.format(prog="consolidate-assemble"))
        return APPLY_EXIT_OK
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
        elif tok == "--superseded":
            branch, sep, sha = (argv[i + 1] if i + 1 < len(argv) else "").partition("=")
            if not sep or not branch or not sha:
                return _usage("consolidate-assemble")
            superseded[branch] = sha
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

    exit_code, report = apply(session_id=session_id, decisions=decisions, superseded=superseded)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code
