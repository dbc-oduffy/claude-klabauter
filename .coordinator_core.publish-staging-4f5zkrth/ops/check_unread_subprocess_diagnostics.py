"""
coordinator_core.ops.check_unread_subprocess_diagnostics — second-lens static
tripwire for the silent-exception-swallow campaign (2026-07-22).

Purpose: the campaign's first-lens detector (bare `except: pass`/`continue`)
cannot see a captured-but-never-read value -- the proven blind-spot instance
was `coordinator_core/goals/reassess_krs.py` spawning `query-records.js` under
`subprocess.run(..., capture_output=True)` and never reading `proc.stderr`,
so the child process's own diagnostics died in the pipe while the detector
scored the file CLEAN (fixed 2026-07-22, `1b152f64`). This module is a second
AST-based lens over the same swallow class, in two parts:

  Lens A -- unread stderr: a `subprocess.run(...)` (or `.check_output`)
    call with `capture_output=True` or `stderr=subprocess.PIPE` (or the bare
    `PIPE` name, if imported directly), assigned to a name, whose `.stderr`
    attribute is never referenced anywhere later in the same function body.

  Lens B -- silent returncode branch: an `if` test that compares or checks
  `<name>.returncode` (equality/inequality against a literal, or truthy/
    falsy of the attribute itself) whose branch body returns/yields an
    empty-or-default value (`None`, `""`, `[]`, `{}`, `False`, `0`) with no
    diagnostic call (`print`, `logging.*`, `self._log*`, `warnings.warn`, or
    any call whose name contains "log"/"warn"/"error") anywhere in that
    branch.

Both lenses are heuristic, not exhaustive -- they narrow a human/agent triage
pass, the same role the first-lens bare-except detector plays. A hit is a
CANDIDATE swallow site, not an automatic verdict; each hit still needs the
same break-class-vs-benign triage the campaign already applies to bare-except
sites (docs/plans reference: state/bug-backlog/2026-07-22-silent-swallow-
campaign-residual.yaml § detector_blind_spot).

Negative-spec:
    - Lens A excludes `check=True` calls by construction -- a CalledProcessError
      already carries `.stderr` on the exception object, so an unread `.stderr`
      on the success path is not a swallow there. Only `check=False`/omitted
      (the default) calls are in scope.
    - Lens A only inspects the enclosing function's remaining statements
      textually AFTER the assignment (same-scope, forward-only) -- a read of
      `.stderr` through an alias, an unpacked/renamed variable, or in a
      *different* function (e.g. passed to a helper) is NOT seen and will
      false-positive. This mirrors the first-lens detector's own
      textual-window limitation; it is a triage narrower, not a prover.
    - Lens B only recognizes literal empty/default return values, not named
      constants or computed empties (e.g. `return DEFAULT_EMPTY`) -- narrowing
      to literals keeps the false-positive rate low at the cost of missing
      some real sites; widening this is a deliberate future improvement, not
      a bug in the shipped narrowing.
    - Files that fail to parse (`SyntaxError`) are skipped with a WARNING on
      stderr, not silently -- this module does not repeat the swallow class
      it exists to catch.

Port shape: mirrors `check_no_monolith_completion_append.py` -- plain module,
NOT @register_op'd, static-scan-then-report CLI shape, naked Python.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

_SCAN_EXTENSIONS = (".py",)
_DIAGNOSTIC_NAME_HINTS = ("log", "warn", "error")
_EMPTY_DEFAULT_LITERALS = (ast.List, ast.Dict, ast.Set, ast.Tuple)


@dataclass
class Hit:
    lens: str  # "A" (unread stderr) or "B" (silent returncode branch)
    path: str
    lineno: int
    detail: str


def _is_diagnostic_call(node: ast.AST) -> bool:
    """True if `node` (an ast.Call) looks like a logging/print/warn call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = ""
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    name = name.lower()
    if name == "print":
        return True
    return any(hint in name for hint in _DIAGNOSTIC_NAME_HINTS)


def _contains_diagnostic_call(nodes: List[ast.stmt]) -> bool:
    for stmt in nodes:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and _is_diagnostic_call(sub):
                return True
    return False


def _is_capture_call(call: ast.Call) -> bool:
    """True if `call` is subprocess.run/check_output(...) with capture_output=True
    or stderr=PIPE (module-qualified or bare-imported PIPE)."""
    func = call.func
    func_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if func_name not in ("run", "check_output"):
        return False
    for kw in call.keywords:
        # check=True raises CalledProcessError on failure, and that exception
        # carries .stderr itself -- not a swallow, the diagnostic still surfaces.
        if kw.arg == "check" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return False
    for kw in call.keywords:
        if kw.arg == "capture_output" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
        if kw.arg == "stderr":
            v = kw.value
            if isinstance(v, ast.Attribute) and v.attr == "PIPE":
                return True
            if isinstance(v, ast.Name) and v.id == "PIPE":
                return True
    return False


def _name_referenced(nodes: List[ast.stmt], var: str, attr: str) -> bool:
    """True if any `<var>.<attr>` attribute access appears in `nodes`."""
    for stmt in nodes:
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr == attr
                and isinstance(sub.value, ast.Name)
                and sub.value.id == var
            ):
                return True
    return False


def _lens_a_unread_stderr(tree: ast.AST, path: str) -> List[Hit]:
    hits: List[Hit] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = func.body
        for i, stmt in enumerate(body):
            call = None
            target_name = None
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name = stmt.targets[0].id
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id
            if call is None or target_name is None:
                continue
            if not _is_capture_call(call):
                continue
            remainder = body[i + 1 :]
            if not _name_referenced(remainder, target_name, "stderr"):
                hits.append(
                    Hit(
                        lens="A",
                        path=path,
                        lineno=stmt.lineno,
                        detail=f"'{target_name}.stderr' never read after this capture_output/PIPE call in {func.name}()",
                    )
                )
    return hits


def _is_returncode_test(test: ast.expr) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Attribute) and node.attr == "returncode":
            return True
    return False


def _is_empty_default_return(stmt: ast.stmt) -> bool:
    value = None
    if isinstance(stmt, ast.Return):
        value = stmt.value
    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
        value = stmt.value.value
    if value is None:
        return isinstance(stmt, ast.Return)  # bare `return` (implicit None)
    if isinstance(value, ast.Constant):
        return value.value in (None, "", False, 0)
    return isinstance(value, _EMPTY_DEFAULT_LITERALS) and not getattr(value, "elts", None) and not getattr(
        value, "keys", None
    )


def _lens_b_silent_returncode_branch(tree: ast.AST, path: str) -> List[Hit]:
    hits: List[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _is_returncode_test(node.test):
            continue
        for branch in (node.body, node.orelse):
            if not branch:
                continue
            last = branch[-1]
            if not _is_empty_default_return(last):
                continue
            if _contains_diagnostic_call(branch):
                continue
            hits.append(
                Hit(
                    lens="B",
                    path=path,
                    lineno=node.lineno,
                    detail="returncode branch yields an empty/default value with no diagnostic call",
                )
            )
    return hits


def _iter_py_files(roots: List[str]):
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(_SCAN_EXTENSIONS):
                    yield os.path.join(dirpath, name)


def scan(roots: List[str]) -> List[Hit]:
    hits: List[Hit] = []
    for path in sorted(_iter_py_files(roots)):
        try:
            source = open(path, "r", encoding="utf-8").read()
        except OSError as exc:
            print(f"WARNING: check_unread_subprocess_diagnostics: unreadable file skipped: {path}: {exc}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            print(f"WARNING: check_unread_subprocess_diagnostics: unparseable file skipped: {path}: {exc}", file=sys.stderr)
            continue
        hits.extend(_lens_a_unread_stderr(tree, path))
        hits.extend(_lens_b_silent_returncode_branch(tree, path))
    return hits


def main(argv: List[str]) -> int:
    roots = argv if argv else ["coordinator_core", "bin"]
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        print("Error: no scannable roots found (pass explicit paths as argv)", file=sys.stderr)
        return 2

    hits = scan(roots)
    if not hits:
        print("check-unread-subprocess-diagnostics: CLEAN — no candidate sites found.")
        return 0

    for hit in hits:
        print(f"[Lens {hit.lens}] {hit.path}:{hit.lineno}: {hit.detail}")
    print(f"\nTOTAL: {len(hits)} candidate site(s) — triage each (break-class vs benign) per campaign discipline.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
