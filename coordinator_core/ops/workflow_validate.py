"""
coordinator_core.ops.workflow_validate — JSON-RPC "workflow.validate" operation.

Purpose: thin RPC wrapper that reads a fleet Workflow `.mjs` script from disk
and runs the shared correctness contract (coordinator_core.ops._workflow_contract,
the C1 SSOT) against it, returning a machine-readable findings list. Scope
"none" / COMPUTE_ONLY, mirrors ops/cartography_symbols.py's registration
pattern.

Wire params:
    script_path (str, required) — path to the .mjs Workflow script to
                                   validate. Path-guarded under a derived
                                   containment root BEFORE the file is read.
    target_root (str, optional) — explicit containment root. If omitted, the
                                   containment root is derived as
                                   script_path's parent directory.

Note (the Staff Engineer Finding 6): unlike the cartography family's `(target_root,
files)` shape — where `target_root` is always a CONTAINMENT DIR and `files`
are the paths guarded under it — this op's primary wire param is a single
FILE (`script_path`), not a directory. `target_root` here, when supplied, is
still a containment DIR (never the script file itself); when omitted, the
containment root is derived from `script_path`'s own parent directory. This
divergence from the cartography convention is deliberate, not an oversight.

Reply fields:
    {"ok": bool, "findings": [{"severity","code","message","line"?}, ...],
     "error_count": int, "warn_count": int}
    ok := error_count == 0 (WARN findings never fail validation).

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler only reads the script text at `script_path` (Path.read_text,
     mode 'r') and returns a computed dict; no file is opened for write, no
     git object is touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     The only file touched is opened read-only; no tempfile, no sentinel, no
     os.replace.
  4. Mutates shared mutable state outside its own module?                No.
     _workflow_contract.run_checks operates on an in-memory string and
     returns a fresh list of Finding dataclasses; no shared/global state is
     written by this handler or by the contract module it imports.
  5. Persistent state changes observable across process boundaries?     No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller.
  Git-shelling-is-read-only precedent: this handler shells out to nothing —
  it is pure-Python regex/line checks over already-read source text, a
  strictly narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git
  subprocess reads (DR-208's own table: "all subprocess calls are read-only
  git queries"). No subprocess call is made here at all.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: pln-workflow-skeleton-stamper-maki-adab0d § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.cartography._guard import path_guard
from coordinator_core.ops._workflow_contract import run_checks, Severity


@register_op("workflow.validate")
def _workflow_validate(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.validate" handler.

    Args (via params):
        script_path (str): path to the .mjs Workflow script to validate.
        target_root (str, optional): explicit containment root; defaults to
            script_path's parent directory.

    Returns:
        {"ok": bool, "findings": [<finding dict>, ...],
         "error_count": int, "warn_count": int}

    Raises:
        ValueError — if `script_path` is missing (descriptive message naming
        the required param), matching the cartography.symbols/tree error
        contract.
        coordinator_core.cartography._guard.PathEscapeError — propagated,
        uncaught, if `script_path` resolves outside the containment root.
    """
    script_path = params.get("script_path")
    if not script_path:
        raise ValueError("workflow.validate requires param: script_path")

    target_root = params.get("target_root") or str(Path(script_path).resolve().parent)
    guarded_path = path_guard(target_root, script_path)

    script_text = guarded_path.read_text(encoding="utf-8")
    findings = run_checks(script_text)

    error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
    warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

    return {
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
