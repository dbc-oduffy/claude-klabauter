"""
coordinator_core.ops.resolve_baton_path — op "baton.resolve_path_and_repo": resolve a
caller-supplied baton path to its absolute native form and discover its owning git repo.

Purpose: path primitive for baton pickup flows — given a possibly-relative,
possibly-tilde'd, possibly-MSYS-mount-form baton path, return the absolute native
path, the owning repo root, and the git-relative path of the baton within that repo.

Contract (op-classification manifest row `resolve-baton-path-and-repo`):
    params: {baton_path: str}
        -> {abs_path: str, repo_root: str, git_relative_path: str}
    or {"error": <str>} when baton_path is missing/empty or resolves outside any
    git repository (structured error, cartography.churn precedent).

Design (wave-3 settlement B5, RATIFIES — binding):
    - `coordinator_core._settings_home.normalize_native_path()` on the input
      (MSYS/cygdrive '/c/...' -> native 'C:/...'; no-op on POSIX), then
      `Path.expanduser().resolve()` for absolutization.
    - Owning-repo discovery via `git rev-parse --show-toplevel` /
      `--show-prefix` run against the baton's PARENT directory — git itself
      computes the C:/-vs-/c/-safe repo-relative path; there is deliberately NO
      manual prefix strip here (the exact reason the fence chose rev-parse).
    - Outside any repo -> structured error. The fence's fail-loud gate is
      load-bearing: an empty repo-root silently degrades every downstream
      `git -C` to cwd.

Idempotency (DEC-7 note, AC8 — platform risk was rated high): inherent — this op
performs no writes of any kind (pure read/resolve: path normalization plus two
read-only `git rev-parse` queries), so re-invocation with identical inputs returns
the identical result and mutates nothing.

Scope: `none` (manifest-justified) — takes an explicit `baton_path` param and
self-discovers its owning repo dynamically via git rev-parse on that path; it reads
no state relative to the caller's _origin_worktree/repo_root.

Negative-spec:
    - NEVER strip the repo-root prefix from the resolved path manually
      (string-slicing `abs_path[len(repo_root):]`) — that reintroduces the
      Windows C:/ vs MSYS /c/ divergence the settlement closes; the
      git-relative path comes ONLY from `git rev-parse --show-prefix` plus the
      baton's basename.
    - NEVER shell out via a shell interpreter (CC-1): both git invocations
      route through `coordinator_core.ops.ceremony.git_native._git()`, the
      shared list-argv, Windows-safe (CREATE_NO_WINDOW + stdin=DEVNULL) git
      helper this wave's `commit_exec_bit.py` also uses — never a raw ad hoc
      `subprocess.run` of the `git` binary.
    - NEVER return a partial result on failure — outside-any-repo, missing
      parent directory, and git-unavailable all return the structured
      `{"error": ...}` shape, not a result with an empty repo_root.

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B5
Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
    § Mandated resolvers (normalize_native_path row), DEC-2, DEC-7
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git


def _git_rev_parse(parent: Path, flag: str):
    """Run `git rev-parse <flag>` in *parent* via the shared Windows-safe
    `git_native._git()` helper (list argv, no shell — CC-1).

    Review: code-reviewer (F2, 2026-07-22) — this previously spawned a raw
    ad hoc `subprocess.run(["git", "-C", ...])` with neither
    `creationflags=CREATE_NO_WINDOW` nor `stdin=subprocess.DEVNULL`,
    reintroducing the console-popup / interactive-hang risk `git_native.py`
    exists to close for every other native git call landed this same wave.
    Routing through `_git()` also means OSError (git absent) is already
    converted to a non-ok GitResult, so this wrapper no longer needs its own
    try/except.

    Returns (stdout_stripped, None) on success, (None, error_message) on any
    failure (non-zero exit, git binary absent, parent dir missing).
    """
    result = _git(["rev-parse", flag], cwd=parent)
    if not result.ok:
        stderr = (result.stderr or "").strip()
        return None, stderr or f"git rev-parse {flag} failed (exit {result.returncode})"
    # --show-prefix legitimately yields an empty string at the repo root, so
    # strip only the trailing newline, never treat empty stdout as failure.
    return result.stdout.rstrip("\n"), None


@register_op("baton.resolve_path_and_repo")
def _resolve_baton_path_and_repo(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "baton.resolve_path_and_repo" handler (sync — offloaded to a thread).

    Required params: baton_path.

    Returns {abs_path, repo_root, git_relative_path} (see module docstring), or
    {"error": <str>} when baton_path is missing/empty or resolves outside any
    git repository.
    """
    baton_path = params.get("baton_path")
    if not baton_path or not isinstance(baton_path, str):
        return {"error": "baton.resolve_path_and_repo requires a non-empty baton_path param"}

    resolved = normalize_native_path(baton_path).expanduser().resolve()
    parent = resolved.parent

    toplevel, err = _git_rev_parse(parent, "--show-toplevel")
    if err is not None:
        return {
            "error": (
                f"baton path {resolved} is not inside any git repository "
                f"(git rev-parse --show-toplevel on {parent}: {err})"
            )
        }

    prefix, err = _git_rev_parse(parent, "--show-prefix")
    if err is not None:
        return {
            "error": (
                f"failed to compute git-relative path for {resolved} "
                f"(git rev-parse --show-prefix on {parent}: {err})"
            )
        }

    # git emits the prefix with a trailing "/" (empty at the repo root) and
    # forward slashes on every platform — append the basename, no manual strip.
    git_relative_path = prefix + resolved.name

    return {
        "abs_path": str(resolved),
        "repo_root": toplevel,
        "git_relative_path": git_relative_path,
    }
