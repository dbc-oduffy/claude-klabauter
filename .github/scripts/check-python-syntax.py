#!/usr/bin/env python3
"""Every published .py file must parse under this interpreter.

Cheap pre-publish gate: the test job proves behaviour, but it runs after the
publish decision and only over `testpaths`. A file that does not even compile —
a truncated copy, a bad encoding, a syntax feature newer than the declared
`requires-python` floor — is a broken artifact regardless of test coverage, and
this catches it in the gate percolate actually invokes.

MID-BOOTSTRAP DEGRADATION
  A tree with no .py files yet is reported as such and passes. Only a file that
  exists and fails to compile is a finding.

EXIT CONTRACT
  0 — every .py file compiles (or there are none)
  1 — at least one .py file failed to compile
"""

from __future__ import annotations

import pathlib
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import read_text, repo_files, repo_root  # noqa: E402


def main() -> int:
    root = repo_root()
    py_files = [p for p in repo_files(root) if p.endswith(".py")]

    if not py_files:
        print("Python syntax check: no .py files in tree (mid-bootstrap) — nothing to compile.")
        return 0

    errors: list[str] = []
    for rel in py_files:
        text = read_text(root / rel)
        if text is None:
            errors.append(f"{rel}: unreadable or binary content in a .py file")
            continue
        try:
            compile(text, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else 0
            errors.append(f"{rel}:{line}: {exc.msg}")
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")

    if errors:
        print(f"Python syntax check FAILED (interpreter {sys.version.split()[0]}):")
        for err in errors:
            print(f"  {err}")
        return 1

    print(f"Python syntax check passed ({len(py_files)} files compiled).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
