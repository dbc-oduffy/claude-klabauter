"""Falsifier: what one percolate publish round costs at default `process_target` arguments, cold and warm.

Builds a local source repo and a mirror clone under scratch, drives the real `publish.process_target`
for one cold round (empty gate caches) and one warm round (gate caches filled, a few source files
changed), each in its own measured child process, and reports per round: process CPU of the row
(root and descendants), files copied into the staging tree, `ast` parse calls made by the gates,
git spawns, plus the CPU of a plain `git fetch` + `git checkout` of the same commit as the reference.
Never pushes to a remote; never cites wall clock.

Run: python coordinator_core/benchmarks/percolate_publish_git_native_cost.py --json [--files N]
"""

from __future__ import annotations

import argparse
import builtins
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_FILES = 5000
DEFAULT_CHANGED = 20
_PACKAGE = "coordinator_core"
_ROW = "bench-git-native-core"
_PLUGINS = 25
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: What the harness stubs so the real row runs on a generated fixture; the measured legs are the
#: sync copy, the pre-sync gates (closure, commit import-name, module-attr) and the parity gate.
FAKED = (
    "dispatch_percolate_pre_rsync",
    "dispatch_percolate_post_rsync",
    "dispatch_percolate_inject",
    "dispatch_percolate_pre_ci",
    "dispatch_standalone_guards",
    "dispatch_preswap_function_gate",
    "assert_dest_on_declared_ref",
    "assert_dest_engine_root_viable",
    "assert_authored_parity",
    "_field7_declaration_check",
    "emit_door_name_map_for_publish_row",
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=bench", "-c", "user.email=bench@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main",
            *args,
        ],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    )
    return result.stdout.strip()


def _module_text(index: int, plugins: int) -> str:
    sibling = (index + 1) % plugins
    return (
        f"from {_PACKAGE}.plug{sibling:02d}.helpers import helper_{sibling}\n"
        f"from {_PACKAGE}.plug{index % plugins:02d} import helpers\n\n\n"
        f"def work_{index}(value, scale=1):\n"
        f"    return helper_{sibling}(value) * scale + {index}\n\n\n"
        f"def other_{index}(value):\n"
        f"    return work_{index}(value, scale=2) + helpers.helper_{index % plugins}(value)\n"
    )


def build_fixture(root: Path, files: int) -> Dict[str, Any]:
    """Source repo at `root/src` with `files` files under `coordinator_core/`, and an origin + mirror
    clone with one seed commit. Returns the paths and the first pinned sha."""
    plugins = min(_PLUGINS, max(1, files // 2))
    src = root / "src"
    package = src / _PACKAGE
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for p in range(plugins):
        plug = package / f"plug{p:02d}"
        plug.mkdir()
        (plug / "__init__.py").write_text("", encoding="utf-8")
        (plug / "helpers.py").write_text(f"def helper_{p}(value):\n    return value + {p}\n", encoding="utf-8")
    written = 1 + plugins * 2
    index = 0
    while written < files:
        (package / f"plug{index % plugins:02d}" / f"mod{index:05d}.py").write_text(
            _module_text(index, plugins), encoding="utf-8", newline="\n"
        )
        index += 1
        written += 1
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "source one")
    sha = _git(src, "rev-parse", "HEAD")

    origin = root / "origin.git"
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    (seed / "README.md").write_text("mirror\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "mirror seed")
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    mirror = root / "mirror"
    _git(root, "clone", "-q", str(origin), str(mirror))
    return {"src": src, "mirror": mirror, "sha": sha, "plugins": plugins, "files_written": written}


def change_source(src: Path, count: int, plugins: int) -> str:
    """Rewrite `count` module files and commit; returns the new head sha."""
    mods = sorted((src / _PACKAGE).glob("plug*/mod*.py"))[:count]
    for n, path in enumerate(mods):
        path.write_text(path.read_text(encoding="utf-8") + f"\n\ndef added_{n}(value):\n    return value\n", encoding="utf-8", newline="\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "source two")
    return _git(src, "rev-parse", "HEAD")


def land_staging(staging: Path, mirror: Path, source_sha: str) -> None:
    """Outside the measured rounds: put the staged row into the mirror and commit it with the source-head trailer."""
    dest = mirror / _PACKAGE
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(staging, dest)
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-q", "-m", f"publish\n\nPercolate-Source-Head: {source_sha}\n")


def _load_publish() -> Any:
    spec = importlib.util.spec_from_file_location(
        "percolate_publish_cost_publish", _REPO_ROOT / "coordinator" / "bin" / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Counters:
    def __init__(self) -> None:
        self.active = False
        self.ast_parse_calls = 0
        self.copy_calls = 0
        self.spawns: List[str] = []


def _install_counters(publish: Any, counters: _Counters) -> None:
    """`ast.parse` reaches `compile` with PyCF_ONLY_AST, so one `compile` wrapper counts every parse."""
    real_compile = builtins.compile
    import ast

    def counting_compile(source: Any, filename: Any, mode: str, flags: int = 0, *args: Any, **kwargs: Any) -> Any:
        if counters.active and flags & ast.PyCF_ONLY_AST:
            counters.ast_parse_calls += 1
        return real_compile(source, filename, mode, flags, *args, **kwargs)

    setattr(builtins, "compile", counting_compile)

    def audit(event: str, args: tuple) -> None:
        if event == "subprocess.Popen" and counters.active:
            argv = args[1]
            first = argv if isinstance(argv, str) else (argv[0] if argv else "")
            counters.spawns.append(os.path.basename(str(first).split(" ")[0]).lower().removesuffix(".exe"))

    sys.addaudithook(audit)

    real_copy = publish._publish_copy_file

    def counting_copy(src_file: Path, dst_file: Path, *, dry_run: bool, out: Any = sys.stdout, **kwargs: Any) -> Any:
        if counters.active and not dry_run:
            counters.copy_calls += 1
        return real_copy(src_file, dst_file, dry_run=dry_run, out=out, **kwargs)

    publish._publish_copy_file = counting_copy


def _stub_engine(publish: Any, monkey: Dict[str, Any]) -> None:
    def set_attr(name: str, value: Any) -> None:
        monkey[name] = getattr(publish, name)
        setattr(publish, name, value)

    def post_rsync(_ctx: Any, _store: Any, sync_target: Any, _src: Any, *, visited_sink: Any = None, sync_changed_paths: Any = None, **_k: Any) -> Any:
        staged = [p for p in sync_target.dest_dir.rglob("*") if p.is_file()]
        if visited_sink is not None:
            visited_sink.update(staged)
        return None, (frozenset(sync_changed_paths) if sync_changed_paths is not None else None)

    set_attr("dispatch_percolate_pre_rsync", lambda *a, **k: None)
    set_attr("dispatch_percolate_post_rsync", post_rsync)
    set_attr("dispatch_percolate_inject", lambda *a, **k: ())
    set_attr("dispatch_percolate_pre_ci", lambda *a, **k: None)
    set_attr("dispatch_standalone_guards", lambda *a, **k: None)
    set_attr("dispatch_preswap_function_gate", lambda *a, **k: True)
    set_attr("assert_dest_on_declared_ref", lambda *a, **k: True)
    set_attr("assert_dest_engine_root_viable", lambda *a, **k: True)
    set_attr("assert_authored_parity", lambda *a, **k: None)
    set_attr("_field7_declaration_check", lambda *a, **k: (True, "", frozenset()))
    set_attr("emit_door_name_map_for_publish_row", lambda *a, **k: None)
    set_attr("_door_emission_is_reportable", lambda *a, **k: False)
    set_attr("_import_percolate_store_resolve_target", lambda: (lambda _store, _name: {"basename_rename": []}))
    basename = publish._import_percolate_rewrite_basename_module()
    monkey["rename_ledger_path"] = basename.rename_ledger_path
    basename.rename_ledger_path = lambda _name: Path(tempfile.gettempdir()) / "percolate-bench-absent-ledger.json"


def run_round_child(workdir: Path, name: str, sha: str, plugins: int) -> int:
    """Child entry: one `process_target` round over the fixture at `workdir`; writes `round-<name>.json`."""
    publish = _load_publish()
    publish._bootstrap_engine()
    counters = _Counters()
    _install_counters(publish, counters)
    _stub_engine(publish, {})
    src = workdir / "src"
    mirror = workdir / "mirror"
    source_dir = src / _PACKAGE
    entries = ["__init__.py"] + [f"plug{p:02d}" for p in range(plugins)]
    target = publish.ResolvedTarget(
        name=_ROW, mode="mirror", source_dir=source_dir, dest_dir=mirror / _PACKAGE, allowlist=",".join(entries)
    )
    totals = publish.RunTotals()
    out = io.StringIO()
    setup_dir = workdir / "setup"
    setup_dir.mkdir(exist_ok=True)
    pinned = {str(src.resolve()): sha}
    ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})
    cpu_before = time.process_time()
    counters.active = True
    try:
        result = publish.process_target(
            target,
            setup_dir,
            totals,
            identity_file_exists=True,
            identity=None,
            dry_run=False,
            round_pinned_shas=pinned,
            engine_ctx=ctx,
            percolate_store_path=workdir / "store.yaml",
            publish_sync_module=publish._import_publish_sync(setup_dir),
            published_files_sink=set(),
            out=out,
        )
    finally:
        counters.active = False
    staged_files = sum(1 for p in result.staging_dir.rglob("*") if p.is_file()) if result is not None else 0
    record = {
        "name": name,
        "succeeded": result is not None and staged_files > 0,
        "synced": staged_files,
        "decline_reason": totals.decline_reason,
        "child_cpu_before_round_s": cpu_before,
        "per_file_worktree_writes": counters.copy_calls,
        "ast_parse_calls": counters.ast_parse_calls,
        "git_spawns": sum(1 for s in counters.spawns if s == "git"),
        "spawns_total": len(counters.spawns),
        "staging_dir": str(result.staging_dir) if result is not None else None,
        "changed_files": len(result.row_changed_files) if result is not None and result.row_changed_files is not None else None,
        "output_tail": out.getvalue()[-1500:],
    }
    (workdir / f"round-{name}.json").write_text(json.dumps(record), encoding="utf-8")
    return 0


def _tree_cpu_s(cmd: List[str], cwd: Optional[Path], log_dir: Path, label: str) -> Dict[str, Any]:
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    measured = single_invocation_tree_process_time(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout_path=str(log_dir / f"{label}.out"),
        stderr_path=str(log_dir / f"{label}.err"),
    )
    return {"cpu_s": measured["process_time_ms"] / 1000.0, "procs": measured["procs"], "rc": measured["rc"]}


def _reference_cpu_s(source: Path, sha: str, workdir: Path, label: str, ref_repo: Path) -> float:
    git = shutil.which("git") or "git"
    if not (ref_repo / ".git").exists():
        ref_repo.mkdir(parents=True)
        _git(ref_repo, "init", "-q")
    base = [git, "-C", str(ref_repo), "-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
    fetch = _tree_cpu_s([*base, "fetch", "-q", "--no-tags", str(source), sha], None, workdir, f"{label}-fetch")
    checkout = _tree_cpu_s([*base, "checkout", "-q", "--detach", sha], None, workdir, f"{label}-checkout")
    if fetch["rc"] != 0 or checkout["rc"] != 0:
        raise RuntimeError(f"reference git fetch/checkout failed for {label}: rc {fetch['rc']}/{checkout['rc']}")
    return fetch["cpu_s"] + checkout["cpu_s"]


def _measured_round(workdir: Path, name: str, sha: str, plugins: int, reference_s: float) -> Dict[str, Any]:
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child-round", name,
        "--workdir", str(workdir), "--sha", sha, "--plugins", str(plugins),
    ]
    tree = _tree_cpu_s(cmd, None, workdir, f"round-{name}")
    record_path = workdir / f"round-{name}.json"
    if tree["rc"] != 0 or not record_path.is_file():
        err = (workdir / f"round-{name}.err").read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"{name} round child failed (rc {tree['rc']}): {err}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not record["succeeded"]:
        raise RuntimeError(f"{name} round did not stage a payload ({record['decline_reason']}): {record['output_tail']}")
    cpu = max(tree["cpu_s"] - record["child_cpu_before_round_s"], 0.0)
    return {
        "publish_cpu_s": round(cpu, 3),
        "per_file_worktree_writes": record["per_file_worktree_writes"],
        "ast_parse_calls": record["ast_parse_calls"],
        "git_spawns": record["git_spawns"],
        "spawns_total": record["spawns_total"],
        "changed_files": record["changed_files"],
        "synced": record["synced"],
        "reference_fetch_checkout_cpu_s": round(reference_s, 3),
        "cpu_vs_reference": round(cpu / reference_s, 2) if reference_s > 0 else None,
        "staging_dir": record["staging_dir"],
    }


def run(files: int, changed: int, workdir: Path) -> Dict[str, Any]:
    fixture = build_fixture(workdir, files)
    src, mirror, sha1, plugins = fixture["src"], fixture["mirror"], fixture["sha"], fixture["plugins"]
    ref_repo = workdir / "reference"
    cold_ref = _reference_cpu_s(src, sha1, workdir, "ref-cold", ref_repo)
    cold = _measured_round(workdir, "cold", sha1, plugins, cold_ref)
    land_staging(Path(cold.pop("staging_dir")), mirror, sha1)
    sha2 = change_source(src, changed, plugins)
    warm_ref = _reference_cpu_s(src, sha2, workdir, "ref-warm", ref_repo)
    warm = _measured_round(workdir, "warm", sha2, plugins, warm_ref)
    warm.pop("staging_dir", None)
    return {
        "files": fixture["files_written"],
        "changed_source_files": changed,
        "row": _ROW,
        "faked_engine_legs": list(FAKED),
        "cold": cold,
        "warm": warm,
        "falsifier_live": cold["per_file_worktree_writes"] > 0 and warm["per_file_worktree_writes"] > 0,
    }


def _make_writable_and_retry(func: Any, path: str, _exc: Any) -> None:
    """git's object files are read-only; Windows refuses to delete them until they are writable."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=DEFAULT_FILES, help="source files in the fixture (default 5000)")
    parser.add_argument("--changed", type=int, default=DEFAULT_CHANGED, help="files changed before the warm round")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--workdir", type=Path, default=None, help="fixture directory (default: a new dir under <repo>/scratch)")
    parser.add_argument("--keep", action="store_true", help="keep the fixture directory")
    parser.add_argument("--child-round", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sha", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--plugins", type=int, default=_PLUGINS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.environ.setdefault("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    if args.child_round:
        return run_round_child(args.workdir, args.child_round, args.sha, args.plugins)
    own_dir = args.workdir is None
    if own_dir:
        scratch = _REPO_ROOT / "scratch"
        scratch.mkdir(exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="percolate-git-native-cost-", dir=str(scratch)))
    else:
        workdir = args.workdir
        workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = run(args.files, args.changed, workdir)
    finally:
        if own_dir and not args.keep:
            shutil.rmtree(workdir, onerror=_make_writable_and_retry)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name in ("cold", "warm"):
            row = result[name]
            print(f"{name}: " + " ".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
