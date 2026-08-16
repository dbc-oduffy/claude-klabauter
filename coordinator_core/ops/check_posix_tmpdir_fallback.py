"""coordinator_core.ops.check_posix_tmpdir_fallback — RED-on-existence guard
for the "hardcoded POSIX /tmp fallback" recurrence.

Purpose: this exact shape has already broken Windows twice — once fixed at
`coordinator_core/ops/gen_claude_doe_launcher.py:247-249` (2026-07-17,
code-reviewer Finding 1) and NOT swept to its sibling module
`coordinator_core/ops/gen_doe_root_pointer.py:281`, which carried the
identical bug until the 2026-07-28 dispatch that authored this guard. A
one-site fix with no recurrence check is exactly the failure mode that let
it happen twice; this module is the cheap AST-scanned tripwire so a third
occurrence fails a test instead of shipping silently.

Detected AST shape (`TMPDIR`/`TEMP`/`TMP` env var name, POSIX `/tmp`
fallback literal):

  - `os.environ.get("TMPDIR", "/tmp")` / `os.getenv("TMPDIR", "/tmp")` — a
    two-arg `.get()`/`.getenv()` call whose second (default) argument is a
    POSIX tmp-dir literal.
  - `os.environ.get("TMPDIR") or "/tmp"` / `os.getenv("TMPDIR") or "/tmp"`
    — a `BoolOp(Or, ...)` whose right operand is a POSIX tmp-dir literal
    and whose left operand is a single-arg `.get()`/`.getenv()` call for
    the same env var name.

`tempfile.gettempdir()` (the fix) is structurally invisible to both
patterns — neither shape appears in code that calls it — so there is
nothing to explicitly exclude.

Measured on this repo (2026-07-28): 2 true positives (the two sites this
module's own dispatch fixed), 0 false positives — no baseline/ratchet
machinery is provided because none is needed; this is a strict RED-on-
existence guard, not a ratcheted one. If a future run of this guard needs a
baseline to pass, that is itself a signal something is wrong (either a
false positive needing a narrower pattern, or a real regression that should
be fixed, not grandfathered).

Negative-spec:
    - Does NOT implement the broader "absolute path literal in a
      filesystem-constructing position" rule (measured 1:51 true-to-false-
      positive generically, ~1:4 even AST-narrowed at authoring time) — out
      of scope by deliberate choice, not an oversight. Scope is exactly the
      TMPDIR/TEMP/TMP-fallback-to-/tmp shape above.
    - Does NOT walk the working tree directly — enumerates via
      `git ls-files`, matching `check_posix_exec_assumptions.py`'s
      convention, so untracked scratch never trips the guard.
    - Does NOT flag `tempfile.gettempdir()` call sites — see above.

Spec backlink: 2026-07-28 Windows-tempdir-convergence dispatch, Rule B.
Prior art: coordinator_core/ops/check_posix_exec_assumptions.py
  (git-ls-files enumeration idiom, AST-scanned pattern classes,
  `_NO_WINDOW` subprocess-creationflags convention) — modeled on, not
  edited.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Tuple
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.repo_root import show_toplevel


_NO_WINDOW = no_console_creationflags()


_ENV_VAR_NAMES = {"TMPDIR", "TEMP", "TMP"}
_POSIX_TMP_LITERALS = {"/tmp"}


class Violation(NamedTuple):
    relpath: str
    lineno: int
    snippet: str


def _git(root, args: List[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root)] + args,
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )
    return proc.stdout


def _tracked_py_files(root) -> List[str]:
    out = _git(root, ["ls-files", "*.py"])
    return [line for line in out.splitlines() if line]


def _is_env_get_call(node: ast.AST) -> ast.Call | None:
    """Returns the Call node if `node` is `os.environ.get(...)` (or
    `environ.get(...)` / any `<x>.environ.get(...)` alias shape), OR
    `os.getenv(...)` (or `<x>.getenv(...)` / bare `getenv(...)` alias
    shape), else None.

    Review: code-reviewer Finding 1 (2026-07-28) — `os.getenv(...)` is
    functionally identical to `os.environ.get(...)` for this guard's
    purpose and is at least as common an idiom; the original matcher only
    covered the `.environ.get` spelling, leaving a detection blind spot in
    exactly the shape this guard exists to catch.

    Matched loosely on the trailing `.environ.get` / `.getenv` attribute
    chain (or bare `getenv` name) rather than requiring the `os` name
    specifically, since `import os as _os`, `from os import environ`, and
    `from os import getenv` are all in-repo-plausible aliases."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "getenv":
        return node
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "getenv":
        return node
    if func.attr != "get":
        return None
    inner = func.value
    if isinstance(inner, ast.Attribute) and inner.attr == "environ":
        return node
    if isinstance(inner, ast.Name) and inner.id == "environ":
        return node
    return None


def _env_get_var_name(call: ast.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _const_str(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_source(src: str) -> List[Tuple[int, str]]:
    """Returns [(lineno, pattern_kind), ...] for every match in `src`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    hits: List[Tuple[int, str]] = []

    for node in ast.walk(tree):
        # -- os.environ.get("TMPDIR", "/tmp") ------------------------------
        call = _is_env_get_call(node)
        if call is not None and len(call.args) >= 2:
            var_name = _env_get_var_name(call)
            default_val = _const_str(call.args[1])
            if var_name in _ENV_VAR_NAMES and default_val in _POSIX_TMP_LITERALS:
                hits.append((node.lineno, "two_arg_default"))

        # -- os.environ.get("TMPDIR") or "/tmp" ----------------------------
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for i in range(len(node.values) - 1):
                left = node.values[i]
                right = node.values[i + 1]
                left_call = _is_env_get_call(left)
                right_val = _const_str(right)
                if left_call is None or len(left_call.args) != 1:
                    continue
                var_name = _env_get_var_name(left_call)
                if var_name in _ENV_VAR_NAMES and right_val in _POSIX_TMP_LITERALS:
                    hits.append((node.lineno, "or_fallback"))

    return hits


def scan(root) -> List[Violation]:
    """Scan all tracked `.py` files under `root` (a git repo) for the
    POSIX-only tempdir-fallback pattern. Returns a sorted list of
    Violation(relpath, lineno, snippet)."""
    root = Path(root)
    violations: List[Violation] = []
    for relpath in _tracked_py_files(root):
        abspath = root / relpath
        try:
            src = abspath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, kind in scan_source(src):
            violations.append(Violation(relpath, lineno, kind))
    return sorted(violations)


def _default_root() -> str:
    import os

    root = show_toplevel()
    return root or os.getcwd()


def main(argv: List[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="check-posix-tmpdir-fallback",
        description=(
            "RED-on-existence guard for the hardcoded POSIX /tmp "
            "tempdir-fallback recurrence (os.environ.get('TMPDIR', '/tmp') "
            "and os.environ.get('TMPDIR') or '/tmp' shapes). No baseline: "
            "any match is a fresh regression."
        ),
    )
    ap.add_argument(
        "--root",
        default=None,
        help="Repo root to scan (default: git rev-parse --show-toplevel of cwd).",
    )
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(_default_root())
    violations = scan(root)

    if not violations:
        print("OK: no POSIX-only tempdir-fallback violations found.")
        return 0

    print(f"FOUND {len(violations)} POSIX-only tempdir-fallback violation(s):")
    for v in violations:
        print(f"  {v.relpath}:{v.lineno} ({v.snippet})")
    print(
        "  Fix: replace the TMPDIR/TEMP/TMP-with-'/tmp'-fallback with "
        "tempfile.gettempdir() (honours TMPDIR/TEMP/TMP per-platform, "
        "resolves correctly on Windows)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
