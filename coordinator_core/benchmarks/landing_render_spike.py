"""Before/after spike for the landing's op-catalog render: process time and spawn count of `land_run`.

    python coordinator_core/benchmarks/landing_render_spike.py [<scratch dir>]
    python coordinator_core/benchmarks/landing_render_spike.py --arm base|hook <scratch dir>

A tmp repo is seeded with HEAD's real `_registry_map.py`, the three generated files (the full op
catalog) and one real single-op module; a docstring edit to that module is landed through
`run_landing.land_run` at default params. Arms, each in a fresh process:
  base  the render hook switched off (the landing as it behaved before the hook).
  hook  the delivered landing.
Process time (`time.process_time`) and `subprocess.Popen` count only, never wall clock. The landing
commit's tree is read with git spawns after the measured window. Landing modules are imported before
the timer starts (the engine is warm); `import_ms` is reported apart.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.git.run import run_git
from coordinator_core.temp_layout import mkdtemp_in
from coordinator_core.win_portability import no_console_creationflags

ARMS = ("base", "hook")
REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_MAP = "coordinator_core/ops/_registry_map.py"
GENERATED = (
    "coordinator_core/contract/op-catalog.json",
    "coordinator_core/contract/mcp-tools.json",
    "docs/reference/op-catalog.md",
)
RENDER_BAR_MS = 50.0
LAND_BAR_MS = 500.0


def _head_blob(rel: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=str(REPO_ROOT), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _single_op_module(map_source: str):
    """`(op, module file)` of the first module that owns exactly one op, declared by `@register_op`
    on a handler in its own file and present at HEAD."""
    table = None
    for node in ast.parse(map_source).body:
        value = getattr(node, "value", None)
        if isinstance(value, ast.Dict) and any(
            getattr(t, "id", None) == "OP_MODULE_MAP" for t in getattr(node, "targets", [node.target] if hasattr(node, "target") else [])
        ):
            table = {ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(value.keys, value.values)}
    if not table:
        raise RuntimeError("OP_MODULE_MAP not found in the registry map")
    owners: dict = {}
    for op, module in table.items():
        owners.setdefault(module, []).append(op)
    for module, ops in sorted(owners.items()):
        if len(ops) != 1:
            continue
        rel = module.replace(".", "/") + ".py"
        try:
            source = _head_blob(rel).decode("utf-8")
        except subprocess.CalledProcessError:
            continue
        if f'@register_op("{ops[0]}")' in source:
            return ops[0], rel, source
    raise RuntimeError("no single-op module with a decorated in-file handler at HEAD")


def _edit_handler_doc(source: str, op: str) -> str:
    """`source` with the decorated handler's docstring first line carrying a marker."""
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(ast.unparse(d).replace("'", '"').startswith(f'register_op("{op}"') for d in fn.decorator_list):
            continue
        doc = fn.body[0] if fn.body else None
        if not (isinstance(doc, ast.Expr) and isinstance(getattr(doc, "value", None), ast.Constant)
                and isinstance(doc.value.value, str)):
            raise RuntimeError(f"{op}: handler has no docstring to edit")
        lines = source.splitlines(keepends=True)
        line = lines[doc.lineno - 1]
        for quote in ('"""', "'''"):
            at = line.find(quote)
            if at >= 0:
                lines[doc.lineno - 1] = line[: at + 3] + "Spike-edited. " + line[at + 3:]
                return "".join(lines)
        raise RuntimeError(f"{op}: docstring quote not found on its first line")
    raise RuntimeError(f"{op}: decorated handler not found")


def build_fixture(parent: Path) -> tuple:
    """A git repo at HEAD holding the full catalog and the registry map plus one real op module, edited
    in the worktree; returns `(repo, op module rel path)`."""
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    repo = parent / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
        ["config", "core.autocrlf", "false"],
    ):
        run_git(args, cwd=str(repo))
    map_text = _head_blob(REGISTRY_MAP)
    op, rel, source = _single_op_module(map_text.decode("utf-8"))
    files = {REGISTRY_MAP: map_text, rel: source.encode("utf-8"), **{g: _head_blob(g) for g in GENERATED}}
    for path, data in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    run_git(["add", "."], cwd=str(repo))
    run_git(["commit", "-q", "-m", "seed"], cwd=str(repo))
    (repo / rel).write_bytes(_edit_handler_doc(source, op).encode("utf-8"))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="spike", paths=(rel,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    return repo, rel


def _spawn_counter():
    counts = {"n": 0}
    real = subprocess.Popen

    class _Popen(real):
        def __init__(self, *a, **kw):
            counts["n"] += 1
            super().__init__(*a, **kw)

    return counts, real, _Popen


def measure(arm: str, repo: Path) -> dict:
    t0 = time.process_time()
    from coordinator_core.ops.dispatch_emit import run_landing
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
    from coordinator_core.contract import landing_render

    import_ms = (time.process_time() - t0) * 1000.0
    render_ms = {"v": 0.0, "calls": 0}
    real_render = landing_render.render_for_landing

    def timed_render(root, changed):
        start = time.process_time()
        try:
            return real_render(root, changed)
        finally:
            render_ms["v"] += (time.process_time() - start) * 1000.0
            render_ms["calls"] += 1

    step_ms = {"v": 0.0}
    real_step = run_landing._render_op_catalog

    def timed_step(root, declared, restore):
        start = time.process_time()
        try:
            return real_step(root, declared, restore)
        finally:
            step_ms["v"] += (time.process_time() - start) * 1000.0

    if arm == "base":
        run_landing._render_op_catalog = lambda root, declared, restore: []
    else:
        landing_render.render_for_landing = timed_render
        run_landing._render_op_catalog = timed_step
    params = {"inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": []}
    counts, real_popen, counting = _spawn_counter()
    subprocess.Popen = counting
    started = time.process_time()
    try:
        reply = run_landing.land_run(params, repo / ".git")
    finally:
        land_ms = (time.process_time() - started) * 1000.0
        subprocess.Popen = real_popen
    names = set(run_git(["show", "--name-only", "--format=", "HEAD"], cwd=str(repo)).stdout.split())
    return {
        "arm": arm, "committed": bool(reply.get("committed")), "refused": reply.get("refused"),
        "three_files_in_commit": all(g in names for g in GENERATED),
        "render_ms": render_ms["v"], "render_calls": render_ms["calls"], "hook_step_ms": step_ms["v"],
        "land_run_ms": land_ms, "spawns": counts["n"], "import_ms": import_ms,
    }


def run_arm(arm: str, parent: Path) -> int:
    declare_benchmark_origin()
    parent.mkdir(parents=True, exist_ok=True)
    work = mkdtemp_in("landing-render-", parent)
    try:
        with fixture_settings_home(work):
            repo, _rel = build_fixture(work)
            result = measure(arm, repo)
        print(json.dumps(result))
        return 0 if result["committed"] else 1
    finally:
        shutil.rmtree(work, onexc=_make_writable_and_retry)


def _make_writable_and_retry(func, path, _exc) -> None:
    """Git objects are read-only on Windows; a plain rmtree leaves them behind."""
    os.chmod(path, 0o700)
    func(path)


def main(argv: list) -> int:
    if "--arm" in argv:
        arm = argv[argv.index("--arm") + 1]
        rest = [a for a in argv if a not in ("--arm", arm)]
        if arm not in ARMS or len(rest) != 1:
            print("usage: landing_render_spike.py --arm base|hook <scratch dir>", file=sys.stderr)
            return 2
        return run_arm(arm, Path(rest[0]))
    parent = Path(argv[0]) if argv else Path(tempfile.gettempdir()) / "landing-render-spike"
    results = {}
    for arm in ARMS:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--arm", arm, str(parent)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, **no_console_creationflags(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"arm {arm} failed: {proc.stdout[-400:]}{proc.stderr[-1200:]}", file=sys.stderr)
            return 1
        results[arm] = json.loads(proc.stdout.strip().splitlines()[-1])
    base, hook = results["base"], results["hook"]
    delta = hook["spawns"] - base["spawns"]
    verdict = (
        hook["three_files_in_commit"] and not base["three_files_in_commit"]
        and hook["render_ms"] < RENDER_BAR_MS and hook["land_run_ms"] < LAND_BAR_MS and delta == 0
    )
    for arm, r in results.items():
        print(
            f"{arm:5s} three_files_in_commit={'yes' if r['three_files_in_commit'] else 'no'} "
            f"render_ms={r['render_ms']:.1f} hook_step_ms={r['hook_step_ms']:.1f} land_run_process_ms={r['land_run_ms']:.1f} "
            f"spawns={r['spawns']} import_ms={r['import_ms']:.1f}"
        )
    print(f"spawn_delta={delta} render_bar<{RENDER_BAR_MS:.0f}ms land_bar<{LAND_BAR_MS:.0f}ms verdict={'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
