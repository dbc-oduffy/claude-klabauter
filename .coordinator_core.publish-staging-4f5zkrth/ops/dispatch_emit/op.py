"""
coordinator_core.ops.dispatch_emit.op — JSON-RPC "dispatch.emit" operation.

Purpose: thin RPC wrapper that registers the dispatch-emit pipeline
(``spine_read`` -> ``wave_map`` -> ``pathspec`` -> ``emit``,
docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C5) as an
op, and is the ONE place in this pipeline that touches disk for a write.
Every upstream module (``spine_read.py``, ``wave_map.py``, ``pathspec.py``,
``emit.py``) is pure — this module composes their output via
``emit.emit_script`` and writes the resulting script TEXT to a caller-named
path, path-guarded through ``coordinator_core.ops._path_guard.contained_path``
before the write (never written unguarded).

Unlike ``workflow.scaffold`` (returns text only, no disk write) and
``workflow.validate`` (reads a caller-supplied path, guarded via
``coordinator_core.cartography._guard.path_guard``), this op is the WRITE
leg — mirrors the containment shape most write-ops in this package family
use (``coordinator_core.ops._path_guard.contained_path`` with an explicit
``allowed_roots`` list), not the read-only ``cartography._guard`` shape.

Wire params:
    plan_path (str, required)     — plan file to read the task spine from
                                     (passed straight to ``emit.emit_script``).
    output_path (str, required)   — path to write the emitted ``.mjs`` script
                                     to. Path-guarded under ``target_root``
                                     BEFORE the file is written.
    target_root (str, optional)   — explicit containment root. If omitted,
                                     the containment root defaults to
                                     ``repo_root`` (the per-request resolved
                                     repo root) when the caller's request
                                     carries one, so the guard meaningfully
                                     constrains a WRITE op (unlike a
                                     parent-of-output default, which is
                                     trivially satisfied by any path). Only
                                     when ``repo_root`` is unavailable does
                                     this fall back to ``output_path``'s own
                                     parent directory — the same
                                     default-derivation shape
                                     ``workflow.validate`` uses for its
                                     READ-only guard.
                                     (Review: code-reviewer 8479038e, Finding
                                     1 — the parent-of-output default made
                                     containment a near no-op for a write op;
                                     ``target_root`` now prefers the wider,
                                     actually-constraining ``repo_root``.)
    name (str, optional)          — forwarded to ``emit.emit_script``.
    description (str, optional)   — forwarded to ``emit.emit_script``.

Reply fields:
    {"path": "<written path>", "ok": bool,
     "findings": [{"severity","code","message","line"?}, ...],
     "error_count": int, "warn_count": int}
    ``ok := error_count == 0`` (WARN findings never fail the verdict) — the
    same run_checks verdict shape ``workflow.validate`` returns. The op
    writes the script to disk and returns this verdict for transparency; it
    does not refuse to write on a non-zero ``error_count`` — ``emit.py``'s
    own construction already targets AC5's zero-ERROR bar, and any refusal
    for an under-declared spine (``NoWavesError``, ``NoWritesDeclaredError``,
    ``NoTestTargetError``) is raised by ``emit_script``/``pathspec.py``
    BEFORE this op ever reaches the write, and propagates uncaught.

Negative-spec:
  - Does NOT derive waves, pathspecs, or script text itself — delegates
    entirely to ``emit.emit_script``. This module's only original code is
    the path guard and the disk write.
  - Does NOT enumerate the tree, glob, or shell out — the sole filesystem
    interaction beyond the guard's own resolve()/relative_to() checks is one
    targeted ``Path.write_text()`` call on the caller-named, already-guarded
    path. Covered by ``tests/test_no_tree_survey.py``'s AST gate (extended
    to ``emit.py``; this module reads/writes no tree-survey surface of its
    own to gate).

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C5
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import emit_script


# Generator-provenance: writes the emitted script to a caller-supplied,
# path-guarded output_path -- no fixed target, purely caller-named.
GENERATES = []


class PathEscapeError(ValueError):
    """Raised when ``output_path`` resolves outside ``target_root``."""


@register_op("dispatch.emit")
def _dispatch_emit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.emit" handler.

    Args (via params):
        plan_path (str): plan file to read the task spine from.
        output_path (str): path to write the emitted ``.mjs`` script to.
        target_root (str, optional): explicit containment root; defaults to
            ``repo_root`` when the request carries one, else to
            ``output_path``'s parent directory (see module docstring).
        name (str, optional): forwarded to ``emit.emit_script``.
        description (str, optional): forwarded to ``emit.emit_script``.

    Returns:
        {"path": str, "ok": bool, "findings": [<finding dict>, ...],
         "error_count": int, "warn_count": int}

    Raises:
        ValueError — if ``plan_path`` or ``output_path`` is missing
        (descriptive message naming the required param), matching the
        cartography.symbols/tree and ``workflow.validate`` error contract.
        Also raised (as ``emit.NoWavesError`` / ``pathspec.
        NoWritesDeclaredError`` / ``pathspec.NoTestTargetError``,
        propagated uncaught) if the spine under-declares — see ``emit.py``
        module docstring.
        PathEscapeError — if ``output_path`` resolves outside
        ``target_root``.
    """
    plan_path = params.get("plan_path")
    if not plan_path:
        raise ValueError("dispatch.emit requires param: plan_path")

    output_path = params.get("output_path")
    if not output_path:
        raise ValueError("dispatch.emit requires param: output_path")

    target_root = (
        params.get("target_root")
        or (str(repo_root) if repo_root is not None else None)
        or str(Path(output_path).resolve().parent)
    )

    guarded_path = contained_path(Path(output_path), [Path(target_root)])
    if guarded_path is None:
        raise PathEscapeError(
            f"output_path escapes target_root: {output_path!r} not under {target_root!r}"
        )

    script = emit_script(
        plan_path,
        name=params.get("name"),
        description=params.get("description"),
        repo_root=repo_root,
    )

    findings = run_checks(script)
    error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
    warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

    # newline="" suppresses the platform line-ending translation Python's text
    # mode applies by default: on Windows that rewrites every "\n" to "\r\n",
    # and a CRLF-carrying .mjs is rejected by the harness Workflow surface that
    # fires it (control characters in the approval payload), making an emitted
    # script unfireable on the platform this repo treats as first-class.
    guarded_path.write_text(script, encoding="utf-8", newline="")

    return {
        "path": str(guarded_path),
        "ok": error_count == 0,
        "findings": [
            {
                "severity": f.severity.value,
                "code": f.code,
                "message": f.message,
                **({"line": f.line} if f.line is not None else {}),
            }
            for f in findings
        ],
        "error_count": error_count,
        "warn_count": warn_count,
    }
