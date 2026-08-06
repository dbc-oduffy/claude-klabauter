"""
coordinator_core.ops.create_github_remote — idempotent GitHub remote
creation + first push (op `repo.create_and_push_remote`).

Purpose: native replacement for the `skills/new-project/SKILL.md:127` fence
(`gh repo create` with `--private|--public --source=. --remote=origin
--push`). The fence has no existence check before remote-create, so a rerun
against an already-created remote fails instead of no-opping — the exact
re-run-correctness hazard the C0a manifest flags. Settlement A3
(docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A3,
RATIFIES) closes it existence-check-first on BOTH layers:

  1. `gh repo view <name>` — exists → `created: false, already_existed:
     true`; absent → `gh repo create` with the fence's flags (minus
     `--push`; the push is this op's own step 3 so it runs on both paths).
  2. local remote — `git remote get-url <remote>`; configure from
     `gh repo view --json url` when missing (the already-existed rerun path
     may have a created GitHub repo but no local remote entry yet).
  3. `git push -u <remote> HEAD` runs on BOTH paths — push is inherently
     idempotent (up-to-date → no-op), which is what makes the whole op
     rerunnable after any partial failure: a created-but-push-failed run
     reruns straight through to the push.

Belt-and-suspenders (settlement A3): gh's "already exists" error string is
also caught on the create call, in case of a TOCTOU race with another
creator between the `gh repo view` probe and the `gh repo create`.

Idempotency (DEC-7 / AC7-AC8 note): achieved by existence-precheck on both
layers plus an intrinsically idempotent final step. A second invocation with
identical inputs finds the GitHub repo present (`already_existed: true`,
`created: false`), finds the local remote configured (no re-add), and
re-runs the push, which git resolves to an up-to-date no-op — the documented
no-op shape `{created: false, already_existed: true, pushed: true}`.

Windows-portability: `gh` and `git` are single cross-platform binaries
invoked as direct list-argv subprocesses (CC-1 — no shell interpreter in the
chain, no `shell=True`); console-window suppression per the
CONSOLE-FLASH-GUARD-PY convention.

Negative-spec: this op never deletes or renames a GitHub repo, never force
pushes, and never mutates an existing remote's URL (`git remote set-url` is
deliberately absent — an existing `<remote>` entry is trusted as-is). A
non-up-to-date push rejection (diverged remote) raises; it is never
resolved by force.

Op contract (C0a manifest row `create-and-push-github-remote`):
    repo.create_and_push_remote
    params: {repo_root: str, name: str, visibility: 'private'|'public',
             remote: str='origin'}
    -> {created: bool, already_existed: bool, pushed: bool}
    scope: common_dir (writes `.git/config`, shared across linked worktrees)

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A3
               docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
Fence source: skills/new-project/SKILL.md:127 (example-doctrine-repo)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Optional, Union

from coordinator_core.ipc import register_op

_PathLike = Union[str, Path, None]

_GIT_TIMEOUT = 60
# `gh` and `git push` are network calls; cap generously but never hang forever.
_NETWORK_TIMEOUT = 180

_VALID_VISIBILITY = ("private", "public")


def _run(
    cmd: list[str], cwd: _PathLike = None, timeout: int = _GIT_TIMEOUT
) -> subprocess.CompletedProcess:
    """Direct list-argv subprocess (CC-1) with hang cap + console suppression."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _git(args: list[str], cwd: _PathLike = None, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _gh(args: list[str], cwd: _PathLike = None) -> subprocess.CompletedProcess:
    """All `gh` CLI traffic funnels through here — the test seam (mock gh)."""
    return _run(["gh", *args], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def _remote_repo_exists(name: str, repo_root: Path) -> bool:
    return _gh(["repo", "view", name], cwd=repo_root).returncode == 0


def _remote_repo_url(name: str, repo_root: Path) -> str:
    res = _gh(["repo", "view", name, "--json", "url"], cwd=repo_root)
    if res.returncode != 0:
        raise RuntimeError(
            "repo.create_and_push_remote: gh repo view --json url failed for "
            f"{name!r}: {res.stderr.strip()}"
        )
    try:
        url = json.loads(res.stdout)["url"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "repo.create_and_push_remote: unparseable `gh repo view --json url` "
            f"output for {name!r}: {res.stdout!r}"
        ) from exc
    if not url:
        raise RuntimeError(
            f"repo.create_and_push_remote: empty repo url for {name!r}"
        )
    return url


def create_and_push_remote(
    repo_root: Union[str, Path],
    name: str,
    visibility: str,
    remote: str = "origin",
) -> dict:
    """Idempotently ensure GitHub repo *name* exists, local *remote* is
    configured in *repo_root*, and the current branch is pushed upstream.

    Raises (CC-7 fail-loud, never a silent half-state):
      ValueError   — invalid params (visibility outside private|public,
                     empty name, repo_root not a git worktree).
      RuntimeError — gh create failed for a reason other than
                     already-exists; url resolution failed; remote-add
                     failed; push rejected.
    """
    if not name:
        raise ValueError("repo.create_and_push_remote: `name` is required")
    if visibility not in _VALID_VISIBILITY:
        raise ValueError(
            "repo.create_and_push_remote: `visibility` must be one of "
            f"{_VALID_VISIBILITY}, got {visibility!r}"
        )
    if not remote:
        raise ValueError("repo.create_and_push_remote: `remote` must be non-empty")

    root = Path(repo_root)
    if _git(["rev-parse", "--git-dir"], cwd=root).returncode != 0:
        raise ValueError(
            f"repo.create_and_push_remote: {root} is not a git worktree"
        )

    # Layer 1 — GitHub-side existence check (settlement A3 step 1).
    already_existed = _remote_repo_exists(name, root)
    created = False
    if not already_existed:
        res = _gh(
            [
                "repo",
                "create",
                name,
                f"--{visibility}",
                "--source",
                str(root),
                "--remote",
                remote,
            ],
            cwd=root,
        )
        if res.returncode == 0:
            created = True
        elif "already exists" in (res.stderr + res.stdout).lower():
            # TOCTOU belt-and-suspenders: another creator won the race
            # between our `gh repo view` probe and this create.
            already_existed = True
        else:
            raise RuntimeError(
                f"repo.create_and_push_remote: gh repo create failed for "
                f"{name!r}: {res.stderr.strip() or res.stdout.strip()}"
            )

    # Layer 2 — local remote entry (settlement A3 step 2). `gh repo create
    # --source --remote` configures it on the create path; the
    # already-existed path (and a mocked/partial create) may not have it.
    if _git(["remote", "get-url", remote], cwd=root).returncode != 0:
        url = _remote_repo_url(name, root)
        add = _git(["remote", "add", remote, url], cwd=root)
        if add.returncode != 0:
            raise RuntimeError(
                f"repo.create_and_push_remote: git remote add {remote!r} "
                f"{url!r} failed: {add.stderr.strip()}"
            )

    # Step 3 — push runs on BOTH paths (settlement A3 step 3). Push is
    # inherently idempotent (up-to-date → no-op), which makes the whole op
    # rerunnable after any partial failure.
    push = _git(["push", "-u", remote, "HEAD"], cwd=root, timeout=_NETWORK_TIMEOUT)
    if push.returncode != 0:
        raise RuntimeError(
            f"repo.create_and_push_remote: git push -u {remote} HEAD failed: "
            f"{push.stderr.strip()}"
        )

    return {"created": created, "already_existed": already_existed, "pushed": True}


@register_op("repo.create_and_push_remote")
async def _create_and_push_remote_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC `repo.create_and_push_remote` handler.

    params: {repo_root: str, name: str, visibility: 'private'|'public',
             remote: str='origin'}
    -> {created: bool, already_existed: bool, pushed: bool}

    `repo_root` in params is this op's contractual target (common_dir scope:
    `.git/config` is shared across linked worktrees); it takes precedence
    over the IPC-injected `repo_root`.
    """
    target = params.get("repo_root") or repo_root
    if target is None:
        raise ValueError(
            "repo.create_and_push_remote: no repo_root (neither params nor "
            "IPC-injected)"
        )
    name = params.get("name") or ""
    visibility = params.get("visibility") or ""
    remote = params.get("remote") or "origin"
    return await asyncio.to_thread(
        create_and_push_remote, target, name, visibility, remote
    )
