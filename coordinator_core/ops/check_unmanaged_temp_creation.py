"""coordinator_core.ops.check_unmanaged_temp_creation — AST scanner for temp
creation outside a managed root.

Findings (tracked, non-test `.py` under coordinator_core/, coordinator/,
scripts/, setup/, plus repo-root `*.py`):

  - `unmanaged-temp-create`: any `mkdtemp` / `TemporaryDirectory` call under
    coordinator_core/ outside coordinator_core/temp_layout.py, with or without
    `dir=` and inside or outside a `with`; use `temp_layout.managed_temp_dir`.
    Other trees keep `bare-temp-create` below.

  - `bare-temp-create`: a `mkdtemp` / `mkstemp` / `NamedTemporaryFile` /
    `TemporaryDirectory` call with no `dir=` (or `dir=None`) and no
    positional `dir` argument, unless it is the context expression of a
    `with` and is not `delete=False` (the context manager cleans it up).
  - `gettempdir`: any `gettempdir()` call outside `READ_ONLY_GETTEMPDIR`.

  - `writer-outside-scratch-root`: an engine write (`open` for write, `mkdir`,
    `makedirs`, `write_text`/`write_bytes`, `shutil` copy/move targets) under
    coordinator_core/ whose path derives, through same-function or module-level
    assignments, from a literal `scratch`/`tmp` path segment or `gettempdir()`
    and from none of `SANCTIONED_ROOT_CALLS`; resolve through
    `temp_layout.scratch_root` / `managed_temp_dir`.

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
        # Path classifiers: compare a path against the temp root, write nothing.
        ("coordinator_core/bash_guards/guard_post_executor_verification.py", "_is_probe_script"),
        ("coordinator_core/hooks/_em_write_budget.py", "_is_scratch_or_tmp"),
        ("coordinator_core/ops/dispatch_emit/ask_brief.py", "_in_session_scratchpad"),
        ("coordinator_core/ops/dispatch_emit/cross_repo_write_refusal.py", "in_session_scratchpad"),
        # Not read-only: a byte-sized --check-only probe file, os.remove'd in a finally.
        ("coordinator_core/ops/gen_claude_author_launcher.py", "main"),
        ("coordinator_core/ops/gen_claude_author_shim.py", "main"),
        # Passes the temp dir as a probe cwd; writes nothing.
        ("coordinator_core/bash_guards/_alternative_liveness.py", "_trigger_guard_disk_fill"),
        ("coordinator_core/guard_routes/assemble.py", "_collect"),
        # Names the harness `claude` dir under Temp to scan; creates nothing.
        ("coordinator_core/ops/claude_home_census.py", "claude_temp_root"),
        # Reads: free-space probe, throwaway-path classifier, scratchpad glob, scratch-target classifier.
        ("coordinator_core/group_em/watch.py", "_low_disk_tick"),
        ("coordinator_core/ops/check_registry_codename_leak.py", "_is_throwaway_path"),
        ("coordinator_core/ops/dispatch_emit/ask_brief.py", "_context_path"),
        ("coordinator_core/ops/new_project_scaffold.py", "_is_scratch_target"),
    }
)

# (repo-relative module path, enclosing function) pairs whose bare creator is
# removed on every exit path (standalone OSS dist scripts that cannot import a
# managed root, and operator measurement scripts). Closed, like READ_ONLY_GETTEMPDIR.
SELF_CLEANING_CREATORS = frozenset(
    {
        # _cleanup() rmtree's own_temp on every return of main().
        ("setup/dist/oss-only-skills/coordinator-update/lib/compute-update-delta.py", "main"),
        # Torn down in its finally (reap + rmtree_or_raise); the short Temp path keeps the repo under Windows MAX_PATH.
        ("coordinator_core/benchmarks/isolated_clone.py", "record_fixture_repo"),
        # Operator measurement scripts: each mkdtemp is rmtree'd in the finally of its own function.
        ("scripts/c6b39c_union_verify/isolated_retest.py", "main"),
        ("scripts/measure_preuse_bash_legs.py", "run_arm"),
        ("scripts/measure_preuse_bash_legs.py", "main"),
    }
)


# Resolvers whose presence in a path expression makes the root managed.
SANCTIONED_ROOT_CALLS = frozenset(
    {
        "scratch_root",
        "managed_temp_dir",
        "make_managed_temp_dir",
        "create_managed_temp_dir",
        "managed_tempdir",
        "mkdtemp_in",
        "temp_dir_in",
        "coordinator_temp_root",
        "fleet_temp_root",
    }
)

# (repo-relative module path, enclosing function) pairs whose write from a literal
# scratch/tmp or gettempdir-derived path is deliberate. Closed: a new site is a
# finding until its reason is named here.
OUTSIDE_SCRATCH_ROOT_WRITERS = frozenset(
    {
        # Engine writers into the repo's own `scratch/` or `state/scratch/` tree: the lifetime
        # and root of each is owned by the cruft plan's migration rows, not by this scan.
        ("coordinator_core/architecture_survey/mechanical_report.py", "produce"),
        ("coordinator_core/ops/app_session.py", "_validate"),
        ("coordinator_core/ops/cartography_chunk_table.py", "write_chunk_table"),
        ("coordinator_core/ops/cartography_symbols.py", "write_symbols_artifact"),
        ("coordinator_core/ops/dispatch_emit/ask_brief.py", "load_em_brief"),
        ("coordinator_core/ops/dispatch_emit/deferred_emit_script.py", "_spill_long_brief"),
        ("coordinator_core/ops/dispatch_emit/deferred_emit_script.py", "write_deferred_script"),
        ("coordinator_core/ops/dispatch_emit/run_scratch_reap.py", "_tombstone"),
        ("coordinator_core/ops/distill_disposal_manifest.py", "write_disposal_manifest"),
        ("coordinator_core/ops/distill_scope.py", "_write_run_file"),
        ("coordinator_core/ops/research_reground.py", "_reground_sync"),
        ("coordinator_core/ops/scratch_delete.py", "delete_declared"),
        ("coordinator_core/ops/worker_cleanup.py", "remove_scratch"),
        ("coordinator_core/publish/dry_refire_gate.py", "_stage"),
        ("coordinator_core/rea_ingest/ghidra_runner.py", "ingest"),
        ("coordinator_core/write_guards/validate_frontmatter_schema_deny.py", "_capture_guard_forensics"),
        # Engine writers rooted at gettempdir()-derived paths that the READ_ONLY_GETTEMPDIR
        # reasons already name: a one-file sentinel dir and the isolated codex child home.
        ("coordinator_core/lifecycle.py", "global_sentinel_dir"),
        ("coordinator_core/codex_runner/codex_spawn_env.py", "_home_writable"),
        ("coordinator_core/codex_runner/codex_spawn_env.py", "_isolate_codex_home"),
        ("coordinator_core/codex_runner/codex_spawn_env.py", "shape_codex_child"),
        # Operator measurement scripts: fixture trees under scratch/tmp, torn down by the script.
        ("coordinator_core/benchmarks/agent_stop_reap.py", "run"),
        ("coordinator_core/benchmarks/ask_script_bytes_probe.py", "_install_sha_modules"),
        ("coordinator_core/benchmarks/backlog_first_pass_tokens.py", "invoke_triage_pass"),
        ("coordinator_core/benchmarks/cold_ask_gate_real_shape_waits.py", "throwaway_run"),
        ("coordinator_core/benchmarks/distill_disposal_large_corpus.py", "run"),
        ("coordinator_core/benchmarks/distill_run_cost.py", "run_scope"),
        ("coordinator_core/benchmarks/emit_worker_e2e.py", "measure"),
        ("coordinator_core/benchmarks/hook_run_direct_exec_census.py", "run"),
        ("coordinator_core/benchmarks/isolated_clone.py", "mkdtemp_for_clone"),
        ("coordinator_core/benchmarks/land_blobs_parity.py", "main"),
        ("coordinator_core/benchmarks/land_legs_pre_integrity_profile.py", "main"),
        ("coordinator_core/benchmarks/land_run_dirty_scan_bytes.py", "main"),
        ("coordinator_core/benchmarks/land_run_halt_path_profile.py", "main"),
        ("coordinator_core/benchmarks/land_run_leg_budget_profile.py", "seed_real_record"),
        ("coordinator_core/benchmarks/land_swap_hook_window.py", "run_old_pair_spike"),
        ("coordinator_core/benchmarks/land_swap_hook_window.py", "run_production_swap"),
        ("coordinator_core/benchmarks/land_swap_hook_window.py", "run_publish_round"),
        ("coordinator_core/benchmarks/preuse_dispatch_phase_cost.py", "_build_fixture"),
        ("coordinator_core/benchmarks/publish_cross_process_reuse_probe.py", "run_child"),
        ("coordinator_core/benchmarks/publish_under_live_engine.py", "main"),
        ("coordinator_core/benchmarks/update_docs_ops_budget.py", "main"),
        ("coordinator_core/benchmarks/warm_write_stall_probe.py", "measure"),
        ("coordinator_core/benchmarks/warp_rundir_scan_budget.py", "build_fixture"),
    }
)

OUTSIDE_SCRATCH_KIND = "writer-outside-scratch-root"
_SCRATCH_SEGMENTS = frozenset({"scratch", "tmp"})
_SHUTIL_WRITERS = frozenset({"copy", "copy2", "copyfile", "copytree", "move"})
_PATH_WRITE_METHODS = frozenset({"mkdir", "write_text", "write_bytes"})
_OS_MKDIRS = frozenset({"mkdir", "makedirs"})


class Finding(NamedTuple):
    path: str
    line: int
    kind: str
    function: str


_DIR_CREATORS = frozenset({"mkdtemp", "TemporaryDirectory"})
_TEMP_LAYOUT_PATH = "coordinator_core/temp_layout.py"
UNMANAGED_KIND = "unmanaged-temp-create"


def _engine_scope(relpath: str) -> bool:
    """True for engine files where a directory creator must go through temp_layout."""
    return relpath.startswith("coordinator_core/") and relpath != _TEMP_LAYOUT_PATH


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


def _literal_is_scratch(value: str) -> bool:
    """True when `value` has a `scratch`/`tmp` path segment or is rooted at `/tmp`."""
    segs = [s for s in value.replace("\\", "/").split("/") if s]
    return any(s.lower() in _SCRATCH_SEGMENTS for s in segs)


def _mode_writes(mode: Optional[ast.expr]) -> bool:
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return False


def _arg(node: ast.Call, pos: int, kw: str) -> Optional[ast.expr]:
    if len(node.args) > pos:
        return node.args[pos]
    for k in node.keywords:
        if k.arg == kw:
            return k.value
    return None


def _write_target(node: ast.Call) -> Optional[ast.expr]:
    """The path expression an engine write call targets, or None when it is not a write."""
    f = node.func
    if isinstance(f, ast.Name) and f.id == "open":
        return node.args[0] if node.args and _mode_writes(_arg(node, 1, "mode")) else None
    if not isinstance(f, ast.Attribute):
        return None
    recv = f.value
    if isinstance(recv, ast.Name) and recv.id == "os" and f.attr in _OS_MKDIRS:
        return node.args[0] if node.args else None
    if isinstance(recv, ast.Name) and recv.id == "shutil" and f.attr in _SHUTIL_WRITERS:
        return _arg(node, 1, "dst")
    if f.attr in _PATH_WRITE_METHODS:
        return recv
    if f.attr == "open" and _mode_writes(_arg(node, 0, "mode")):
        return recv
    return None


def _assignments(body: Iterable[ast.stmt]) -> dict:
    """name -> value expressions assigned to it directly in `body` (nested defs excluded)."""
    env: dict = {}
    stack = list(body)
    while stack:
        st = stack.pop()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(st.value)
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value:
            env.setdefault(st.target.id, []).append(st.value)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                if isinstance(item.optional_vars, ast.Name):
                    env.setdefault(item.optional_vars.id, []).append(item.context_expr)
        stack.extend(c for c in ast.iter_child_nodes(st) if isinstance(c, ast.stmt))
    return env


def _resolves_outside_scratch_root(expr: ast.expr, env: dict, seen: frozenset = frozenset()) -> bool:
    """True when `expr` derives from a scratch/tmp literal or gettempdir() and no sanctioned resolver."""
    tainted = False
    for n in ast.walk(expr):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name in SANCTIONED_ROOT_CALLS:
                return False
            if name == "gettempdir":
                tainted = True
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _literal_is_scratch(n.value):
            tainted = True
        elif isinstance(n, ast.Name) and n.id in env and n.id not in seen:
            for v in env[n.id]:
                if _sanctioned_in(v, env, seen | {n.id}):
                    return False
                if _resolves_outside_scratch_root(v, env, seen | {n.id}):
                    tainted = True
    return tainted


def _sanctioned_in(expr: ast.expr, env: dict, seen: frozenset) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and _call_name(n) in SANCTIONED_ROOT_CALLS:
            return True
        if isinstance(n, ast.Name) and n.id in env and n.id not in seen:
            if any(_sanctioned_in(v, env, seen | {n.id}) for v in env[n.id]):
                return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, relpath: str, module_env: Optional[dict] = None) -> None:
        self.relpath = relpath
        self.findings: List[Finding] = []
        self._funcs: List[str] = []
        self._envs: List[dict] = [module_env or {}]
        self._with_ctx: set[int] = set()

    def _fn(self) -> str:
        return self._funcs[-1] if self._funcs else "<module>"

    def _env(self) -> dict:
        merged: dict = {}
        for e in self._envs:
            merged.update(e)
        return merged

    def _check_writer(self, node: ast.Call) -> None:
        if not _engine_scope(self.relpath):
            return
        target = _write_target(node)
        if target is None or (self.relpath, self._fn()) in OUTSIDE_SCRATCH_ROOT_WRITERS:
            return
        if _resolves_outside_scratch_root(target, self._env()):
            self.findings.append(
                Finding(self.relpath, node.lineno, OUTSIDE_SCRATCH_KIND, self._fn())
            )

    def _visit_func(self, node) -> None:
        self._funcs.append(node.name)
        self._envs.append(_assignments(node.body))
        self.generic_visit(node)
        self._envs.pop()
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
        self._check_writer(node)
        if name == "gettempdir":
            if (self.relpath, self._fn()) not in READ_ONLY_GETTEMPDIR:
                self.findings.append(
                    Finding(self.relpath, node.lineno, "gettempdir", self._fn())
                )
        elif name in _DIR_CREATORS and _engine_scope(self.relpath):
            self.findings.append(
                Finding(self.relpath, node.lineno, UNMANAGED_KIND, self._fn())
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
    v = _Visitor(relpath, _assignments(tree.body))
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
        hint = (
            "  -> use temp_layout.managed_temp_dir / make_managed_temp_dir"
            if f.kind == UNMANAGED_KIND
            else "  -> resolve through temp_layout.scratch_root / managed_temp_dir"
            if f.kind == OUTSIDE_SCRATCH_KIND
            else ""
        )
        print(f"{f.path}:{f.line}: {f.kind} in {f.function}{hint}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
