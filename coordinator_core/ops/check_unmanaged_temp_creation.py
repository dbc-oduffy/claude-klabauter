"""coordinator_core.ops.check_unmanaged_temp_creation — AST scanner for temp
creation outside a managed root.

Findings (tracked, non-test `.py` under coordinator_core/, coordinator/,
scripts/, setup/, plus repo-root `*.py`):

  - `bare-temp-create`: a `mkdtemp` / `mkstemp` / `NamedTemporaryFile` /
    `TemporaryDirectory` call with no `dir=` (or `dir=None`) and no
    positional `dir` argument, unless it is the context expression of a
    `with` and is not `delete=False` (the context manager cleans it up).
  - `gettempdir`: any `gettempdir()` call outside `READ_ONLY_GETTEMPDIR`.

`TemporaryFile` and `SpooledTemporaryFile` create no name and are never
findings. Exit 1 on any finding, else 0.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.git.run import run_git

_SCOPE_DIRS = ("coordinator_core/", "coordinator/", "scripts/", "setup/")

# Positional index of the `dir` parameter per creator.
_CREATOR_DIR_POS = {
    "mkdtemp": 2,
    "mkstemp": 2,
    "TemporaryDirectory": 2,
    "NamedTemporaryFile": 6,
}

# (repo-relative module path, enclosing function) pairs that read Temp
# without writing an accumulating entry into it. "<module>" is a module-level call.
# Entries are closed: a new gettempdir site is a finding until its reason is named here.
READ_ONLY_GETTEMPDIR = frozenset(
    {
        # temp_layout resolvers: define the managed roots from gettempdir.
        ("coordinator_core/temp_layout.py", "_coordinator_root"),
        ("coordinator_core/temp_layout.py", "legacy_top_level_path"),
        # prune_claude_temp: names the harness root it scans; creates nothing.
        ("coordinator_core/ops/prune_claude_temp.py", "claude_temp_root"),
        # scratchpad_sweep / pytest_basetemp_sweep / cloud_setup: locate trees to reap.
        ("coordinator_core/ops/scratchpad_sweep.py", "sweep_scratchpads"),
        ("coordinator_core/ops/fleet/pytest_basetemp_sweep.py", "_candidates"),
        ("scripts/cloud_setup.py", "reap_stale_pytest_trees"),
        # fs_containment: resolves the managed root a test may write under.
        ("coordinator_core/testing/fs_containment.py", "_coordinator_temp"),
        # Path classifiers: compare a path against the temp root, write nothing.
        ("coordinator_core/install/substrate.py", "_refuse_machine_mutation"),
        ("coordinator_core/ops/queue_append.py", "_is_swept_tmp_root"),
        ("coordinator/bin/coordinator-lesson-add.py", "_is_swept_tmp_root"),
        ("coordinator/bin/lib/cli_shared.py", "_is_swept_tmp_root"),
        ("coordinator/bin/coordinator-doc-new.py", "_safe_output_roots"),
        ("coordinator_core/bash_guards/_write_bump_applicability.py", "_all_temp_roots"),
        ("coordinator_core/write_guards/block_subagent_guard_grant_write.py", "_is_sentinel_path"),
        # Legacy-location read for a dual-read window.
        ("coordinator_core/session/autonomous_sentinel.py", "legacy_sentinel_path"),
        # Passes the temp dir as a cwd for a probe; writes nothing.
        ("coordinator_core/bash_guards/roster.py", "guard_roster"),
        # Records the temp root at import to audit later creates against it.
        ("drive_root_write_guard.py", "<module>"),
        # lifecycle: one fixed rendezvous dir, not an accumulating leak.
        ("coordinator_core/lifecycle.py", "global_sentinel_dir"),
        # cli_shared mirror of temp_layout for scripts that cannot import coordinator_core.
        ("coordinator/bin/lib/cli_shared.py", "managed_temp_root"),
        # Default scratch root for its clone; _sweep_orphans reaps its own prefix there.
        # Stdlib-only by contract, so it cannot reach the managed-root mirror.
        # A dry run discards its clone outright; a --confirm clone that fails to push
        # is kept on purpose (it holds the only copy of the merge).
        ("coordinator/bin/klabauter-reconcile.py", "_cmd_reconcile"),
        # Scans Temp top level to delete stale entries; creates nothing.
        ("coordinator_core/ops/prune_claude_temp.py", "_prune_top_level"),
        # Reads the Temp root to detect a nested redirect and set the watch roots; creates nothing.
        ("coordinator_core/testing/fs_containment.py", "pytest_configure"),
        # Not read-only: a byte-sized --check-only probe file, os.remove'd in a finally.
        ("coordinator_core/ops/gen_claude_author_launcher.py", "main"),
        ("coordinator_core/ops/gen_claude_author_shim.py", "main"),
    }
)

# (repo-relative module path, enclosing function) pairs whose bare creator is
# removed on every exit path and cannot import a managed root (standalone OSS
# dist scripts). Closed, like READ_ONLY_GETTEMPDIR.
SELF_CLEANING_CREATORS = frozenset(
    {
        # _cleanup() rmtree's own_temp on every return of main().
        ("setup/dist/oss-only-skills/coordinator-update/lib/compute-update-delta.py", "main"),
    }
)


class Finding(NamedTuple):
    path: str
    line: int
    kind: str
    function: str


def _call_name(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _has_dir(node: ast.Call, name: str) -> bool:
    for kw in node.keywords:
        if kw.arg is None:
            return True
        if kw.arg == "dir":
            v = kw.value
            return not (isinstance(v, ast.Constant) and v.value is None)
    return len(node.args) > _CREATOR_DIR_POS[name]


def _delete_false(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "delete":
            v = kw.value
            return isinstance(v, ast.Constant) and v.value is False
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.findings: List[Finding] = []
        self._funcs: List[str] = []
        self._with_ctx: set[int] = set()

    def _fn(self) -> str:
        return self._funcs[-1] if self._funcs else "<module>"

    def _visit_func(self, node) -> None:
        self._funcs.append(node.name)
        self.generic_visit(node)
        self._funcs.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def _visit_with(self, node) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                self._with_ctx.add(id(item.context_expr))
        self.generic_visit(node)

    visit_With = _visit_with
    visit_AsyncWith = _visit_with

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name == "gettempdir":
            if (self.relpath, self._fn()) not in READ_ONLY_GETTEMPDIR:
                self.findings.append(
                    Finding(self.relpath, node.lineno, "gettempdir", self._fn())
                )
        elif name in _CREATOR_DIR_POS and not _has_dir(node, name):
            managed_with = (
                id(node) in self._with_ctx
                and name in ("NamedTemporaryFile", "TemporaryDirectory")
                and not _delete_false(node)
            )
            if not managed_with and (self.relpath, self._fn()) not in SELF_CLEANING_CREATORS:
                self.findings.append(
                    Finding(self.relpath, node.lineno, "bare-temp-create", self._fn())
                )
        self.generic_visit(node)


def _in_scope(rel: str) -> bool:
    parts = rel.split("/")
    if not rel.endswith(".py"):
        return False
    name = parts[-1]
    if "tests" in parts[:-1] or name.startswith("test_") or name == "conftest.py":
        return False
    return len(parts) == 1 or rel.startswith(_SCOPE_DIRS)


def _tracked_py_files(root: Path) -> List[str]:
    proc = run_git(["-C", str(root), "ls-files", "*.py"])
    if not proc.ok:
        raise RuntimeError(f"git ls-files exited {proc.returncode}: {proc.stderr.strip()}")
    return [ln for ln in proc.stdout.splitlines() if ln and _in_scope(ln)]


def _scan_source(source: str, relpath: str) -> List[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    v = _Visitor(relpath)
    v.visit(tree)
    return v.findings


def scan(paths: Optional[Iterable[str]] = None, root: Optional[Path] = None) -> List[Finding]:
    """Findings for `paths` (repo-relative, forward slashes), or every scoped
    tracked file when `paths` is None. Explicit paths bypass the scope filter."""
    if root is None:
        top = show_toplevel()
        if top is None:
            raise RuntimeError("check_unmanaged_temp_creation: not inside a git repo; pass root")
        root = Path(top)
    rels = list(paths) if paths is not None else _tracked_py_files(root)
    out: List[Finding] = []
    for rel in rels:
        p = root / rel
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.extend(_scan_source(src, rel.replace("\\", "/")))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    findings = scan(args or None)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind} in {f.function}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
