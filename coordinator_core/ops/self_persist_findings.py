"""
coordinator_core.ops.self_persist_findings — JSON-RPC "findings.self_persist_fallback" operation.

Purpose: native port of the ad hoc ``python3 -c 'pathlib.Path(...).write_text(...)'``
/ bash-heredoc fallback that agent-facing doctrine fences (agents/coverage-auditor.md,
snippets/findings-self-persist-bash.md) hand-roll whenever a findings report's content
contains characters (embedded quotes, backticks) that make a single-quoted shell
one-liner or heredoc fragile. This op is the one named entrypoint every such fence
relinks to instead of re-deriving its own escaping.

Write path: ``locked_write.locked_rmw`` (mkstemp + os.replace) — the same primitive
used by every other coordinator_core content-write op. A bash heredoc has no atomic
replace step (a crash mid-heredoc-write leaves a truncated/partial file); locked_rmw
closes that race entirely, and its own byte-identical-skip (step 6 of its algorithm)
makes an identical rerun a true no-op with no mtime churn.

DEC-7 idempotency note (manifest-rated medium idempotency-hazard): idempotency is
achieved by making the write itself content-idempotent, not by any op-level dedup
ledger. The ``mutate`` callback compares the requested ``content`` against the file's
current text and returns the text UNCHANGED when they already match; locked_rmw then
sees a byte-identical result and skips the physical write. A second call with the same
(target_path, content) pair is therefore a safe, side-effect-free no-op — ``written``
comes back False and no mtime/inode churn occurs. A second call with DIFFERENT content
for the same target_path is an intentional overwrite (last-writer-wins under the lock),
matching the original fallback fence's own semantics (it always clobbered its target).

Scope: registered with no ``_OP_KEY_SCOPE`` entry (scope-verdict "none" per
state/audits/2026-07-22-command-payload-inventory/op-classification.tsv) —
``target_path`` is entirely caller-supplied and need not live under the invoking
repo at all, so the dispatcher never injects a ``repo_root`` for this op. The cross-
process lock sidecar that ``locked_rmw`` needs still requires SOME path inside a git
repository to resolve ``git rev-parse --git-common-dir`` against; this handler derives
that path itself by walking up from ``target_path``'s parent looking for a ``.git``
entry, rather than trusting an injected value. Callers MUST supply an absolute
``target_path`` — this op does not resolve relative paths against any repo root (a
deliberate design constraint per the manifest row, not a scope gap).

Params:
    target_path (str) — absolute path to write. Required. Rejected if relative.
    content     (str) — exact text to write. Required (empty string is valid content).

Returns a dict:
    exit_code     (int)  — 0 ok / 1 error.
    written       (bool) — True if the file's bytes changed as a result of this call
                            (fresh write or overwrite); False on an idempotent rerun
                            with byte-identical content, or on error.
    bytes_written (int)  — len(content.encode("utf-8")); the byte size of the content
                            now on disk, reported on both the write and no-op paths
                            (0 on error).
    message       (str)  — human-readable outcome (exit_code 0 only).
    error         (str)  — human-readable reason (exit_code 1 only).

Self-registration: importing this module fires
``@register_op("findings.self_persist_fallback", _handler)`` as a side-effect. Add
this module to coordinator_core/ops/__init__.py (_EAGER_OP_MODULES) to trigger
registration at start_server() time — owned by the shared-surface tail pass, not
this module.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 1 C1a; state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
row "self-persist-findings-fallback"; distinct-ops-new.tsv same row (fence sites:
agents/coverage-auditor.md:29, snippets/findings-self-persist-bash.md:26/32/40 —
coordinator-claude-owned, out of this repo's write scope).

Negative-spec:
  - Does NOT itself mkdir target_path's parent — but locked_rmw's write step
    (``target_dir.mkdir(parents=True, exist_ok=True)`` immediately before its
    atomic mkstemp+os.replace) does, so a target_path whose immediate parent
    is missing but whose ancestor chain still resolves to a git-repo root (the
    _find_repo_root walk below) is handled correctly with no explicit mkdir
    call in this module. Review: code-reviewer (F6) — confirmed against
    coordinator_core/locked_write.py::locked_rmw rather than added
    redundantly here; see test_self_persist_findings.py's
    missing-intermediate-directory case.
  - Does NOT create anything at all if no ``.git`` ancestor is found — if no
    git-repo ancestor is found at all, the op fails loud (exit_code 1) rather
    than silently writing outside any lockable, provenance-traceable tree.
  - Does NOT validate or interpret content in any way (no encoding sniffing, no
    trailing-newline normalization) — bytes in, bytes out, exactly as the original
    ``write_text`` fallback did.
  - Does NOT shell out to python3/bash/a heredoc — pure Path.write_text() via
    locked_rmw; the entire hazard class this op exists to retire is the shell
    dependency itself.
  - Does NOT accept a relative target_path — the caller (a relinked doctrine fence)
    is expected to have an absolute path in hand already; silently resolving a
    relative path against an arbitrary cwd would reintroduce the exact
    which-directory-am-I-in ambiguity a heredoc fence already suffered from.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo-root discovery for the locked_rmw lock sidecar
# ---------------------------------------------------------------------------


def _find_repo_root(target: Path) -> Optional[Path]:
    """Walk upward from target's parent looking for a ``.git`` entry.

    Returns the first ancestor directory containing a ``.git`` file-or-directory
    (a linked worktree's ``.git`` is a file; a main checkout's is a directory —
    either satisfies ``locked_rmw``'s ``repo_root`` contract, since it only needs
    a path inside the repo to resolve ``git rev-parse --git-common-dir`` against).
    Returns None if no such ancestor exists (target_path is not inside any git repo).
    """
    node = target.parent
    while True:
        if (node / ".git").exists():
            return node
        parent = node.parent
        if parent == node:
            return None
        node = parent


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("findings.self_persist_fallback")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "findings.self_persist_fallback" handler.

    See module docstring for the full contract, DEC-7 idempotency note, and
    negative-spec. ``repo_root`` (the dispatcher-injected value) is unused —
    this "none"-scoped op derives its own lock-sidecar anchor from target_path
    via ``_find_repo_root`` (see module docstring § Scope).
    """
    target_path_raw = params.get("target_path")
    content = params.get("content")

    if not target_path_raw or not isinstance(target_path_raw, str):
        return _err("missing required param: target_path")
    if content is None or not isinstance(content, str):
        return _err("missing required param: content (must be a string)")

    target = normalize_native_path(target_path_raw)
    if not target.is_absolute():
        return _err(
            f"target_path must be an absolute path: {target_path_raw!r} — "
            "this op does not resolve relative paths against any repo root"
        )

    lock_root = _find_repo_root(target)
    if lock_root is None:
        return _err(
            f"target_path is not inside a git repository (no .git ancestor found): "
            f"{target_path_raw!r}"
        )

    _written = [False]

    def _mutate(old_text: str) -> str:
        if old_text == content:
            # Idempotent rerun: return unchanged text so locked_rmw's
            # byte-identical-skip (step 6) makes this a true no-op write.
            return old_text
        _written[0] = True
        return content

    try:
        await asyncio.to_thread(
            locked_rmw, target, _mutate, repo_root=lock_root, missing_ok=True
        )
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except OSError as exc:
        return _err(f"cannot read/write target_path: {exc}")

    bytes_written = len(content.encode("utf-8"))
    if _written[0]:
        _LOG.debug(
            "findings.self_persist_fallback: wrote %d bytes to %s",
            bytes_written, target,
        )
        return {
            "exit_code": 0,
            "written": True,
            "bytes_written": bytes_written,
            "message": f"wrote {bytes_written} bytes to {target_path_raw}",
        }
    _LOG.debug(
        "findings.self_persist_fallback: %s already holds byte-identical content — "
        "skipping (idempotent)",
        target,
    )
    return {
        "exit_code": 0,
        "written": False,
        "bytes_written": bytes_written,
        "message": (
            f"{target_path_raw} already holds byte-identical content — no-op (idempotent)"
        ),
    }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str) -> dict:
    """Return an exit_code=1 error reply dict."""
    _LOG.warning("findings.self_persist_fallback: %s", msg)
    return {"exit_code": 1, "written": False, "bytes_written": 0, "error": msg}
