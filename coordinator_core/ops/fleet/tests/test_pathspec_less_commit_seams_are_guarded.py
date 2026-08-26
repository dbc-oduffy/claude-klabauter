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

#: Every name an empty-tree refusal answers to. The class this test polices is
#: "pathspec-less commit from a private index", NOT "commit seams in one
#: module" — and the engine already fixed that class twice, independently:
#: fleet's `_empty_private_index_breach` and ceremony/git_native.py's
#: `_empty_private_index_refusal` (its own EMPTY_TREE_SHA, its own docstring
#: citing the fleet fix by name). Pinning a single name meant ceremony's seams
#: could regress with this test still green — the same call-site-shaped
#: blindness the 2026-08-10 point-fix had. Add a name here when a third
#: implementation appears; better still, converge them.
GUARD_NAMES = frozenset({
    "_empty_private_index_breach",
    "_empty_private_index_refusal",
})

#: Porcelain `git commit` is not the only way to write a tree from a private
#: index — `git commit-tree` plumbing does the same thing, and ceremony's two
#: seams use exactly that. Matching only the literal "commit" left them
#: invisible to this walk: not reported unguarded, simply never examined.
COMMIT_ARGV_HEADS = ("commit", "commit-tree")

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
                (getattr(c.func, "id", "") in GUARD_NAMES
                 or getattr(c.func, "attr", "") in GUARD_NAMES)
                for c in body_calls
            )
            for call in body_calls:
                if not _is_subprocess_exec(call):
                    continue
                lits = _arg_literals(call)
                if not any(head in lits for head in COMMIT_ARGV_HEADS):
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


#: The in-process, spawn-free commit-landing helper `archive_and_commit`
#: (2026-08-26, C2, `dccf2fc01`) and — pending C3 — `rm_and_commit` call
#: instead of a spawned `git commit`/`commit-tree`. It carries its OWN
#: CAS-guarded landing (a locked `cas_ref` compare-and-swap keyed on
#: `old_head`) rather than `_empty_private_index_breach`'s write-tree-based
#: refusal, so a fleet seam that calls it in place of a spawned commit is a
#: DELIBERATE, still-guarded replacement — not the seam quietly vanishing.
COMMIT_LANDING_HELPERS = frozenset({"_commit_via_head_spine"})


def _calls_any(fn: ast.AST, names: frozenset[str]) -> bool:
    return any(
        (getattr(c.func, "id", "") in names or getattr(c.func, "attr", "") in names)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    )


def test_the_known_fleet_seams_are_still_pathspec_less_and_guarded():
    """Pins the FORWARD-B design: these two must stay pathspec-less AND guarded.

    Guards against the wrong fix — someone silencing the test above by adding a
    pathspec, which trades a loud recoverable failure for a silent recurring leak.

    A fleet seam satisfies this test ONE of two ways, and the two must not be
    conflated: (1) a spawned `git commit`/`commit-tree` seam, pathspec-less and
    calling `_empty_private_index_breach`; or (2) no spawned commit seam at all,
    but a call to `_commit_via_head_spine` — the CAS-guarded in-process landing
    path C2 moved `archive_and_commit` onto (`dccf2fc01`). A function with
    NEITHER is a silent loss of commit capability (or the seam moved somewhere
    this AST walk cannot see) and must fail loudly rather than pass vacuously
    on "found no seam, nothing to check."
    """
    fleet = ENGINE_ROOT / "coordinator_core" / "ops" / "fleet" / "_common.py"
    source = fleet.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    fn_by_name = {
        fn.name: fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    seams = {
        fn: (has_pathspec, guarded)
        for p, fn, _lineno, has_pathspec, guarded in _commit_seams()
        if p == fleet
    }
    for name in ("archive_and_commit", "rm_and_commit"):
        assert name in fn_by_name, f"{name} no longer exists in {fleet.name}"
        if name in seams:
            has_pathspec, guarded = seams[name]
            assert not has_pathspec, (
                f"{name} gained a trailing pathspec — that is FORWARD-B, the "
                f"mechanism that laundered 34 hand-edited changes on 2026-07-26."
            )
            assert guarded, f"{name} no longer calls {GUARD_NAME}()"
        else:
            assert _calls_any(fn_by_name[name], COMMIT_LANDING_HELPERS), (
                f"{name} contains neither a spawned `git commit`/`commit-tree` "
                f"seam nor a call to {sorted(COMMIT_LANDING_HELPERS)} — its "
                f"commit landing has silently disappeared rather than moved to "
                f"a known, still-guarded path."
            )
