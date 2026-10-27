"""Split terminal_commit modules must resolve test-patched names late, through terminal_commit."""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
_SPLIT = (
    "terminal_commit_refusals",
    "terminal_commit_review",
    "terminal_commit_checkpoints",
    "terminal_commit_closeout",
    "terminal_commit_stamp",
    "terminal_commit_ledger",
)
_PATCHERS = {"tc", "terminal_commit", "_tc"}


def _patched_names() -> set[str]:
    names: set[str] = set()
    for test in sorted((_PKG / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(test.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "setattr" or len(node.args) < 2:
                continue
            target, name = node.args[0], node.args[1]
            if (
                isinstance(target, ast.Name)
                and target.id in _PATCHERS
                and isinstance(name, ast.Constant)
                and isinstance(name.value, str)
            ):
                names.add(name.value)
    return names


def _module_globals(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out.update(t.id for t in targets if isinstance(t, ast.Name))
    return out


def _own_global_reads(path: Path, patched: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owned = _module_globals(tree) & patched
    bad: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs]}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in owned
                and node.id not in local
                and node.id != fn.name
            ):
                bad.append(f"{path.name}:{node.lineno} {fn.name} reads {node.id} from its own globals")
    return bad


_KNOWN = {
    ("terminal_commit_closeout", "_launch_close_job", "CLOSE_JOB_SCRIPT"),
    ("terminal_commit_closeout", "_ship_cited_sizing", "reentrant_dispatch"),
    ("terminal_commit_ledger", "_changed", "_changed_paths"),
    ("terminal_commit_ledger", "_changed_paths", "run_git"),
    ("terminal_commit_ledger", "_commit_residue", "_changed"),
    ("terminal_commit_ledger", "_commit_residue", "_own_prefix_files"),
    ("terminal_commit_ledger", "_entangled_chunks", "_changed"),
    ("terminal_commit_ledger", "_entangled_chunks", "_own_prefix_files"),
    ("terminal_commit_ledger", "_ledger_met_regrades", "_own_prefix_files"),
    ("terminal_commit_ledger", "_regradable_chunks", "_own_prefix_files"),
    ("terminal_commit_ledger", "_unchanged_since_halt_failures", "run_git"),
    ("terminal_commit_ledger", "_withheld_product_files", "_changed"),
    ("terminal_commit_ledger", "_withheld_work_pending", "run_git"),
    ("terminal_commit_ledger", "_worktree_differs_from_head", "run_git"),
    ("terminal_commit_ledger", "_xs_closeout_regrades", "_changed"),
    ("terminal_commit_ledger", "_xs_closeout_regrades", "_own_prefix_files"),
    ("terminal_commit_refusals", "_new_file_rows", "run_git"),
    ("terminal_commit_refusals", "_peer_adjusted_rows", "run_git"),
    ("terminal_commit_refusals", "_xs_over_budget_refusal", "run_git"),
    ("terminal_commit_review", "_review_since", "_run_started_at"),
}
"""Ratchet: sites whose tests pass today although they read a patched name from their own globals.
A new site, or any patched-name read in a function not listed, fails; shrink the set, never grow it."""


def test_split_modules_do_not_read_terminal_commit_patch_targets_from_own_globals():
    patched = _patched_names()
    assert "_unchanged_since_halt_failures" in patched
    found: dict[tuple[str, str, str], str] = {}
    for mod in _SPLIT:
        for line in _own_global_reads(_PKG / f"{mod}.py", patched):
            fn, _reads, name, *_ = line.split(" ", 1)[1].split()
            found[(mod, fn, name)] = line
    new = [line for key, line in sorted(found.items()) if key not in _KNOWN]
    assert not new, (
        "resolve these through `from coordinator_core.ops.dispatch_emit import terminal_commit as _tc` "
        "inside the function:\n" + "\n".join(new)
    )
    assert not (_KNOWN - set(found)), "baseline entry no longer offends; remove it from _KNOWN"


def test_guard_flags_an_own_global_read(tmp_path):
    src = tmp_path / "m.py"
    src.write_text("def _x():\n    return 1\n\ndef f():\n    return _x()\n", encoding="utf-8")
    assert _own_global_reads(src, {"_x"})
    src.write_text(
        "def _x():\n    return 1\n\ndef f():\n    from p import terminal_commit as _tc\n    return _tc._x()\n",
        encoding="utf-8",
    )
    assert not _own_global_reads(src, {"_x"})
