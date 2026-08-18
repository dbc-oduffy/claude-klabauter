"""Every pathspec-less `git commit` in the engine must refuse git's empty tree.

WHY THIS EXISTS — this bomb went off twice, eight days apart, because the first
fix was applied at the call site instead of at the class of call site:

- 2026-08-10, `0a3462b72` (`session.boot_sweep`) committed git's empty tree and
  emptied the branch. It was recovered in `2b9e319aa` after ~130 commits had
  accreted onto the void. boot_sweep was then hardened by giving it the real
  index and a trailing pathspec.
- 2026-08-18, `fbfbd061d` (`fleet.archive_actioned_memos`) did exactly the same
  thing through a different door, deleting 26,264 files on a pushed shared
  branch. Recovered in `6c183fee4`, guarded in `b63e81223`.

The 2026-08-10 fix could not have saved the fleet seams: they commit with NO
trailing pathspec deliberately, because a pathspec makes git read the WORKTREE
instead of the index (the FORWARD-B hazard that laundered 34 hand-edited
frontmatter changes into fleet commits on 2026-07-26). So "give it a pathspec"
is unavailable there, and the point-fix left the class armed.

This test is the part that generalises. It fails when a NEW pathspec-less commit
seam appears without the empty-tree refusal — the seam that does not exist yet
and would otherwise re-arm the same bomb.

SCOPE — this covers PRIVATE-index seams (modules that set `GIT_INDEX_FILE`).
That is the failure class: an absent private index file is what `write-tree`
turns into the empty tree silently. Two pathspec-less seams commit from the REAL
index instead (`ops/bootstrap_orchestrate.py::main`,
`ops/bootstrap_repo.py::main`); they are deliberately NOT covered, because the
guard needs a private index to check and `.git/index` vanishing mid-op is a
different, much rarer fault. They are bootstrap-only and run on fresh repos, not
on a schedule. If either ever gains a private index, this test starts failing on
it — which is the intended behaviour, not a bug.

NEGATIVE-SPEC: do NOT fix a failure here by adding a trailing pathspec to a
fleet seam. That reintroduces FORWARD-B. Call the breach check instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[4]
GUARD_NAME = "_empty_private_index_breach"

def _iter_engine_sources():
    for path in sorted((ENGINE_ROOT / "coordinator_core").rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        yield path


def _arg_literals(call: ast.Call) -> list[str]:
    """Every string literal passed positionally to a subprocess exec call."""
    out = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            for v in arg.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value)
        elif isinstance(arg, ast.List):
            for e in arg.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    out.append(e.value)
    return out


def _is_subprocess_exec(call: ast.Call) -> bool:
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    return name in {
        "create_subprocess_exec", "run", "Popen", "check_call",
        "check_output", "call",
    }


def _commit_seams():
    """Yield (path, funcname, lineno, has_pathspec, guarded) per `git commit` call.

    Only modules that set `GIT_INDEX_FILE` are walked — see SCOPE above.
    """
    for path in _iter_engine_sources():
        source = path.read_text(encoding="utf-8", errors="replace")
        if "GIT_INDEX_FILE" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - engine must parse
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            guarded = any(
                (getattr(c.func, "id", "") == GUARD_NAME
                 or getattr(c.func, "attr", "") == GUARD_NAME)
                for c in body_calls
            )
            for call in body_calls:
                if not _is_subprocess_exec(call):
                    continue
                lits = _arg_literals(call)
                if "commit" not in lits:
                    continue
                has_pathspec = any(
                    lit == "--" or lit.startswith("--pathspec-from-file")
                    for lit in lits
                )
                yield path, fn.name, call.lineno, has_pathspec, guarded


def test_every_pathspec_less_commit_seam_calls_the_empty_tree_guard():
    seams = list(_commit_seams())
    assert seams, "found no `git commit` seams at all — the AST walk is broken"

    unguarded = [
        f"{p.relative_to(ENGINE_ROOT).as_posix()}::{fn} (line {ln})"
        for p, fn, ln, has_pathspec, guarded in seams
        if not has_pathspec and not guarded
    ]
    assert not unguarded, (
        "pathspec-less `git commit` seam(s) with no empty-tree refusal:\n  "
        + "\n  ".join(unguarded)
        + f"\n\nSuch a commit deletes every tracked file when the private index "
        f"goes missing (`git write-tree` returns git's empty tree with rc=0 and "
        f"empty stderr). Call {GUARD_NAME}() before the commit. Do NOT add a "
        f"trailing pathspec to a fleet seam — that reintroduces FORWARD-B."
    )


def test_the_known_fleet_seams_are_still_pathspec_less_and_guarded():
    """Pins the FORWARD-B design: these two must stay pathspec-less AND guarded.

    Guards against the wrong fix — someone silencing the test above by adding a
    pathspec, which trades a loud recoverable failure for a silent recurring leak.
    """
    fleet = ENGINE_ROOT / "coordinator_core" / "ops" / "fleet" / "_common.py"
    seams = {
        fn: (has_pathspec, guarded)
        for p, fn, _lineno, has_pathspec, guarded in _commit_seams()
        if p == fleet
    }
    for name in ("archive_and_commit", "rm_and_commit"):
        assert name in seams, f"{name} no longer contains a `git commit` seam"
        has_pathspec, guarded = seams[name]
        assert not has_pathspec, (
            f"{name} gained a trailing pathspec — that is FORWARD-B, the "
            f"mechanism that laundered 34 hand-edited changes on 2026-07-26."
        )
        assert guarded, f"{name} no longer calls {GUARD_NAME}()"
