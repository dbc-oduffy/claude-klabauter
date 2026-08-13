"""
coordinator_core.install.clone_sibling_repo — JSON-RPC "install.clone_idempotent"
operation.

Purpose: idempotent native git-clone-a-sibling-repo primitive. Port of the
fence at coordinator-claude `commands/install.md:1142`
(``[[ ! -d "$DOE_CLONE/.git" ]] && git clone ...``): checks for an existing
`.git` directory at the target before invoking `git clone`, so a second
invocation against an already-cloned target is a true no-op rather than a
`git clone`-into-nonempty-dir failure. Pairs with
``coordinator_core.ops.repo_bootstrap`` (clone-and-register-sibling-repo,
built separately) — that module composes check -> clone -> register ->
confirm; this module owns only the check -> clone step, referenced by name,
not imported (disjoint write target, Wave 2 chunk w2-clone-sibling).

Op-key / contract (state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv, row `git-clone-idempotent`):
    params:   {repo_url: str, target_dir: str}
    response: {cloned: bool, already_present: bool, path: str}
scope-verdict: none — `repo_url`/`target_dir` are explicit caller-supplied
install-time params (the sibling-repo clone location on the operator's own
machine), not the caller's own dispatching worktree; no `_origin_worktree`
scoping applies (same class as `ping` / `percolate.validate_store` in
`coordinator_core/op_scopes.py`).

DEC-7 idempotency note (oracle-rated idempotency-hazard: low, but recorded
per DEC-7 discipline since the oracle itself flags "tightened here" —
platform-hazard also low, but the caller-supplied `repo_url` is a wrong-repo
hazard, not a platform one): idempotency is achieved by checking
``(target_dir / ".git").is_dir()`` BEFORE invoking `git clone` — mirrors the
fence's own bash guard literally rather than re-deriving a new check. A
second call with identical inputs sees the `.git` directory already present
and returns ``{"cloned": False, "already_present": True, "path": ...}``
without touching git at all; it never re-clones, never fails on a
non-empty-directory `git clone` error, and never mutates an existing
checkout (no fetch/pull/reset is issued on the already-present branch — this
op is a clone primitive, not a sync primitive). The caller (this plan's
`repo_bootstrap` sibling, or any future consumer) is responsible for
resolving `repo_url` through a real registry key rather than a literal
string — this module does not resolve `repo_url` itself, since the oracle
notes DOE_REPO_URL is presently an unresolved placeholder in the fence and
wiring that resolution is the composing caller's concern, not this
primitive's (a hardcoded fallback here would be exactly the "silent
wrong-repo hazard" DEC-4's spirit warns against).

Self-registration: importing this module calls
`register_op("install.clone_idempotent", ...)` as a side-effect (same
pattern as `ops/ping.py`). Already wired into
`coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES` and
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP` as of this
commit. <!-- Review: code-reviewer — Finding 3, docstring previously
claimed wiring was a separate pending pass; it shipped in this same
commit. -->

Spec backlink: pln-coordinator-ops-buildout-from--903224 § Wave 2
Oracle backlink: state/audits/2026-07-22-command-payload-inventory/{op-classification,distinct-ops-new}.tsv
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.ipc import register_op

_CLONE_TIMEOUT_SECS = 300

_TARGET_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the caller-supplied clone
target) — the only clause `clone_idempotent` journals against."""


def _record_resolution(clause_index: int, entries) -> None:
    """Deferred-import wrapper over `resolution_journal.record_resolution`.

    Deferred (not module-level) because this module is transitively
    imported during `coordinator_core.ops`'s eager op-registration walk
    (via `ops.repo_bootstrap`), and `resolution_journal` back-imports
    `uninstall_legs`, which imports `substrate`, whose own import graph can
    reach back into `coordinator_core.ops` before this module has finished
    executing — a module-level import here risks exactly that kind of
    load-order-dependent cycle. Same reasoning as `substrate_migrate.py`'s
    and `shell_rc_guard.py`'s deferred back-imports of `substrate.py`."""
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("clone-sibling-repo", clause_index, entries)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="clone-sibling-repo",
    source_module="coordinator_core.install.clone_sibling_repo",
    clauses=(
        # Clause 1 — `clone_idempotent`'s sole write: `target.parent.mkdir
        # (parents=True, exist_ok=True)` followed by `git clone repo_url
        # target` land a fresh sibling-repo tree at `target_dir`, an
        # explicit caller-supplied param (op contract: {repo_url, target_dir}
        # — see module docstring's op-key/contract block), not a literal
        # path this module owns. SHAPED, not STATIC: the destination is
        # whatever the caller passes, and lands outside this repo's own
        # checkout (a sibling-repo clone location on the operator's
        # machine) — the same "shaped, caller-supplied destination" pattern
        # as install-substrate's own agent-helper forwarder clause. The
        # already-present short-circuit (`(target / ".git").is_dir()`)
        # performs no write at all, so this clause covers only the
        # fresh-clone path.
        ShapedClause(
            discovered_by="clone_idempotent (target_dir param)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<caller-supplied-target_dir>/",
                reason=(
                    "target.parent.mkdir(parents=True) + `git clone "
                    "repo_url target`, only when target_dir/.git is not "
                    "already present (idempotent no-op clause, see module "
                    "docstring's DEC-7 note)"
                ),
            ),
        ),
    ),
)


class CloneSiblingRepoError(RuntimeError):
    """Raised when `git clone` itself fails (non-zero exit, or the `git`
    executable is not resolvable on PATH)."""


def clone_idempotent(repo_url: str, target_dir: str) -> dict:
    """Clone `repo_url` into `target_dir` unless a `.git` directory already
    exists there.

    Returns ``{"cloned": bool, "already_present": bool, "path": str}``:
      - already-present: {"cloned": False, "already_present": True, ...} —
        no subprocess is spawned at all.
      - fresh clone succeeds: {"cloned": True, "already_present": False, ...}
      - `git clone` fails (non-zero exit / `git` missing): raises
        CloneSiblingRepoError — this op does not silently swallow a real
        clone failure, only the already-present short-circuit is silent.

    `target_dir` is normalized via `normalize_native_path()` before either
    the existence check or the clone invocation, so an MSYS/cygdrive-form
    path handed in on Windows resolves the same directory a native
    `git.exe` subprocess would resolve, rather than doubling the drive
    letter.
    """
    target = normalize_native_path(target_dir)

    if (target / ".git").is_dir():
        # Already present is a genuine, confirmed on-disk fact — the target
        # repo exists at this path, whether this run cloned it or a prior
        # one did.
        _record_resolution(
            _TARGET_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(target)),)
        )
        return {"cloned": False, "already_present": True, "path": str(target)}

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["git", "clone", repo_url, str(target)],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECS,
            check=False,
            **no_console_creationflags(),
        )
    except OSError as exc:
        # No `_record_resolution` call on any of these three failure paths —
        # deliberately, not an oversight. Review: coordinator:code-reviewer
        # (2026-08-06, rcpt-R3-writer-wiring) flagged this against
        # `ensure_venv.py`'s opposite convention (journals an empty tuple on
        # its own failed-rebuild path) as two nearest-analogous sites
        # choosing opposite defaults. They are not actually analogous:
        # `ensure_venv.py` `shutil.rmtree`s the partial tree BEFORE
        # journaling `()`, so "resolved to nothing" is a confirmed fact at
        # journal time. This module makes no attempt to remove or even
        # inspect whatever `git clone` may have left at `target` on failure
        # — `git clone` itself can leave a partial working tree on a failed
        # clone (network drop mid-clone, disk full, etc.), and without a
        # cleanup step of our own, on-disk state here is genuinely unknown,
        # not confirmed-empty. Journaling `()` would assert "nothing here"
        # when a partial `.git` directory may well exist; leaving the clause
        # unreported instead maps it to the receipt's cannot-safely-
        # determine class, which is the honest answer for this failure mode.
        raise CloneSiblingRepoError(
            f"install.clone_idempotent: could not invoke git (is it on PATH?): {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CloneSiblingRepoError(
            f"install.clone_idempotent: git clone of {repo_url!r} into "
            f"{target!s} timed out after {_CLONE_TIMEOUT_SECS}s"
        ) from exc

    if result.returncode != 0:
        raise CloneSiblingRepoError(
            f"install.clone_idempotent: git clone of {repo_url!r} into {target!s} "
            f"failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    # `git clone` exited 0 — a genuine fresh clone landed at `target`.
    _record_resolution(
        _TARGET_CLAUSE_INDEX, (WriteSurfaceEntry(kind="file-path", path=str(target)),)
    )
    return {"cloned": True, "already_present": False, "path": str(target)}


@register_op("install.clone_idempotent")
def _clone_idempotent(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.clone_idempotent" handler.

    Params:
        repo_url (str, required) — the source to clone.
        target_dir (str, required) — the destination directory.

    `repo_root` (injected by ipc.dispatch_message) is unused — this op's
    scope-verdict is "none" (see module docstring); the signature is kept
    for dispatch-shape consistency with scoped ops (same as `ping`).

    Plain sync `def`, not `async def`: the body calls a blocking
    `subprocess.run(..., timeout=300)` (git clone). ipc.py's own
    register_op contract (AC-3 Gap-3) requires blocking I/O in a handler
    to either be sync (auto-offloaded to asyncio.to_thread by the engine)
    or explicitly wrapped in asyncio.to_thread if kept async — sync is the
    simpler correct form here since the whole body is blocking.
    (review: code-reviewer — Finding 1, async-handler-blocking-subprocess)
    """
    repo_url = params.get("repo_url")
    target_dir = params.get("target_dir")
    if not repo_url or not target_dir:
        raise ValueError(
            "install.clone_idempotent requires both 'repo_url' and 'target_dir' params"
        )
    return clone_idempotent(repo_url, target_dir)
