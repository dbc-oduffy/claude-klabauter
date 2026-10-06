"""Falsifiers for the amplification gate's incremental scan: the uncached collector is the oracle.

Every comparison is over the whole AmpSite list, all fields, in order. The cached scan is
pre-suppression, so the oracle runs with both suppression registers emptied.
"""

from __future__ import annotations

import ast
import pathlib
import re
import shutil
import sys

import pytest

from coordinator_core.tests import _amp_scan_incremental as inc
from coordinator_core.tests import test_no_unbatched_per_item_git_spawn as gate
from coordinator_core.tests._amp_scan_incremental import scan_incremental

pytest_plugins = ("pytester",)

_ALL_ROUTES = {
    "a-direct",
    "b-local-helper",
    "c-cross-module",
    "d-injected",
    "e-generic-runner",
    "f-default-runner",
    "g-forwarded-runner",
}

_CORPUS: dict[str, str] = {
    "route_a.py": (
        "import subprocess\n\ndef check(paths):\n    for p in paths:\n"
        "        subprocess.run(['git', 'add', p], cwd='/repo')\n"
    ),
    "route_b.py": (
        "import subprocess\n\ndef _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n\n"
        "def check(paths):\n    for p in paths:\n        _git_add(p)\n"
    ),
    "helper_mod.py": (
        "import subprocess\n\ndef commit_one(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n"
    ),
    "caller.py": (
        "from helper_mod import commit_one\n\ndef check(paths):\n"
        "    for p in paths:\n        commit_one(p)\n"
    ),
    "pkg/__init__.py": "from .impl import g as h\n",
    "pkg/impl.py": (
        "import subprocess\n\ndef g(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n"
    ),
    "caller_rename.py": (
        "from pkg import h as g\n\ndef check(paths):\n    for p in paths:\n        g(p)\n"
    ),
    "pkg/reexp.py": "from .real import *\n",
    "pkg/real.py": (
        "import subprocess\n\ndef real_spawn(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n"
    ),
    "caller_star.py": (
        "from pkg.reexp import real_spawn\n\ndef check(paths):\n"
        "    for p in paths:\n        real_spawn(p)\n"
    ),
    "route_d.py": (
        "import subprocess\n\ndef _run(argv):\n    subprocess.run(argv, cwd='/repo')\n\n"
        "def trailer_foreign_shas(sha, session_id, run=_run):\n"
        "    return run(['git', 'log', sha])\n\n"
        "def check(shas):\n    for sha in shas:\n"
        "        trailer_foreign_shas(sha, 's1', run=_run)\n"
    ),
    "runner_mod.py": (
        "import subprocess\n\ndef _run(argv):\n    return subprocess.run(argv, cwd='/repo')\n"
    ),
    "route_e.py": (
        "import runner_mod\n\ndef check(shas):\n    for sha in shas:\n"
        "        runner_mod._run(['git', 'show', sha])\n"
    ),
    "seam.py": (
        "import asyncio\n\nasync def _update_index_with_retry(argv, *, cwd):\n"
        "    return await asyncio.create_subprocess_exec(*argv, cwd=cwd)\n\n"
        "async def resync(paths, *, cwd, run_git=_update_index_with_retry):\n"
        "    for path in paths:\n"
        "        await run_git(['git', 'restore', '--staged', '--', path], cwd=cwd)\n"
    ),
    "hop.py": (
        "import subprocess\n\ndef _resolve_range_shas(sha):\n"
        "    subprocess.run(['git', 'rev-list', sha], cwd='/repo')\n\n"
        "def _record_membership_shas(sha, get_range):\n    get_range(sha)\n\n"
        "def _collect_discharging_range_shas(shas, resolve_range_shas):\n"
        "    for sha in shas:\n"
        "        _record_membership_shas(sha, resolve_range_shas)\n\n"
        "def verdict(shas):\n"
        "    _collect_discharging_range_shas(shas, resolve_range_shas=_resolve_range_shas)\n"
    ),
    "direct.py": (
        "import subprocess\n\ndef _do_spawn(sha):\n"
        "    subprocess.run(['git', 'show', sha], cwd='/repo')\n\n"
        "def sweep(shas, injected):\n    for sha in shas:\n        injected(sha)\n\n"
        "def driver(shas):\n    sweep(shas, injected=_do_spawn)\n"
    ),
    "disc8_callee.py": (
        "import subprocess\n\n_PROG = 'git'\n\ndef spawn_prog(path):\n"
        "    subprocess.run([_PROG, 'show', path], cwd='/repo')\n"
    ),
    "disc8_caller.py": (
        "from disc8_callee import spawn_prog\n\ndef check(paths):\n"
        "    for p in paths:\n        spawn_prog(p)\n"
    ),
    "disc12.py": (
        "import subprocess\n\ndef _flags():\n    return {}\n\n"
        "def run_all(argvs):\n    for argv in argvs:\n"
        "        subprocess.call(argv, **_flags())\n\n"
        "def const_loop():\n    for name in ('a', 'b'):\n"
        "        subprocess.run(['git', 'show', name])\n"
    ),
    "plain.py": "def helper_plain(p):\n    return p\n",
    "prefilter.py": (
        "import subprocess\n\ndef sweep(items):\n    for q in items:\n"
        "        subprocess.run(['git', 'add', q])\n"
    ),
    "decorators.py": "def some_decorator(f):\n    return f\n",
}


def _swap_registers():
    saved = (gate._EXEMPT_SITES, gate._ORACLE_CLAIMS)
    gate._EXEMPT_SITES, gate._ORACLE_CLAIMS = frozenset(), {}
    return saved


def _uncached(roots):
    saved = _swap_registers()
    try:
        return gate.find_unbatched_per_item_spawns(tuple(roots))
    finally:
        gate._EXEMPT_SITES, gate._ORACLE_CLAIMS = saved


def _sync(root: pathlib.Path, files: dict[str, str | None]) -> None:
    wanted = {rel for rel, text in files.items() if text is not None}
    for path in list(root.rglob("*.py")):
        if path.relative_to(root).as_posix() not in wanted:
            path.unlink()
    for rel in wanted:
        target = root / rel
        data = files[rel].encode("utf-8")
        if target.exists() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _names(paths) -> set[str]:
    return {pathlib.PurePosixPath(p.replace("\\", "/")).name for p in paths}


def _assert_equal(root: pathlib.Path, cache_dir: pathlib.Path | None):
    cached, stats = scan_incremental((root,), cache_dir)
    assert cached == _uncached((root,))
    return stats


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    files: dict[str, str | None] = dict(_CORPUS)
    _sync(root, files)
    return root, files, tmp_path / "cache"


def test_oracle_is_not_vacuous(tree):
    root, _, _ = tree
    routes = {site.route for site in _uncached((root,))}
    assert _ALL_ROUTES <= routes, sorted(_ALL_ROUTES - routes)


def test_cold_then_warm_equal_the_uncached_scan(tree):
    root, _, cache = tree
    cold = _assert_equal(root, cache)
    assert not cold.tree_hit
    warm = _assert_equal(root, cache)
    assert warm.tree_hit and warm.reanalysed == () and warm.summarised == ()


def test_cache_dir_none_equals_uncached_and_writes_nothing(tree):
    root, _, cache = tree
    stats = _assert_equal(root, None)
    assert not stats.tree_hit
    assert not cache.exists()


def _delete_spawn(text: str) -> str:
    return re.sub(
        r"\b(subprocess\.(run|Popen|call)|asyncio\.create_subprocess_exec)\(", "print(", text
    )


def _rename_first_def(text: str) -> str:
    match = re.search(r"def (\w+)\(", text)
    if match is None:
        return text
    name = match.group(1)
    return re.sub(rf"\b{re.escape(name)}\b", f"{name}_x", text)


_EDITS = {
    "delete_spawn": _delete_spawn,
    "rename_callee": _rename_first_def,
    "add_alias": lambda t: t + "\nimport subprocess as _sp_alias\n",
    "shift_lines": lambda t: "\n" + t,
    "touch_comment": lambda t: t + "# touched\n",
}


@pytest.mark.parametrize("kind", sorted(_EDITS))
@pytest.mark.parametrize("rel", sorted(_CORPUS))
def test_single_file_edit_sweep(tree, rel, kind):
    root, files, cache = tree
    _assert_equal(root, cache)
    edited = _EDITS[kind](files[rel])
    if edited == files[rel]:
        pytest.skip("edit does not apply to this file")
    files[rel] = edited
    _sync(root, files)
    _assert_equal(root, cache)
    _assert_equal(root, cache)


def _mut_add_file(files):
    files["extra_spawn.py"] = (
        "import subprocess\n\ndef sweep(items):\n    for i in items:\n"
        "        subprocess.run(['git', 'add', i])\n"
    )


def _mut_delete_file(files):
    files["helper_mod.py"] = None


def _mut_syntax_error(files):
    files["helper_mod.py"] = "def (:\n"


def _mut_first_writer_flip(files):
    files["aaa_early.py"] = (
        "import subprocess\n\ndef _run(argv):\n    return subprocess.Popen(argv)\n"
    )


def _mut_star_target(files):
    files["pkg/real.py"] = files["pkg/real.py"].replace("real_spawn", "real_spawn2")


def _mut_decorator(files):
    files["route_b.py"] = files["route_b.py"].replace(
        "def _git_add", "@some_decorator\ndef _git_add"
    )


def _mut_param_default(files):
    files["seam.py"] = files["seam.py"].replace("run_git=_update_index_with_retry", "run_git=None")


def _mut_cross_prefilter_in(files):
    files["plain.py"] = (
        "import subprocess\n\ndef helper_plain(items):\n    for p in items:\n"
        "        subprocess.run(['git', 'add', p])\n"
    )


def _mut_cross_prefilter_out(files):
    files["prefilter.py"] = files["prefilter.py"].replace("import subprocess\n", "", 1)


def _mut_same_size_rename(files):
    files["caller.py"] = files["caller.py"].replace("commit_one", "commit_two")
    files["helper_mod.py"] = files["helper_mod.py"].replace("commit_one", "commit_two")


_MUTATIONS = {
    "add_file": _mut_add_file,
    "delete_file": _mut_delete_file,
    "syntax_error": _mut_syntax_error,
    "first_writer_flip": _mut_first_writer_flip,
    "star_reexport_target": _mut_star_target,
    "decorator": _mut_decorator,
    "param_default": _mut_param_default,
    "prefilter_in": _mut_cross_prefilter_in,
    "prefilter_out": _mut_cross_prefilter_out,
    "same_size_rename": _mut_same_size_rename,
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_cross_file_mutations_stay_equal(tree, name):
    root, files, cache = tree
    _assert_equal(root, cache)
    _MUTATIONS[name](files)
    _sync(root, files)
    _assert_equal(root, cache)
    assert _assert_equal(root, cache).tree_hit


_EXACT: dict[str, str] = {
    "a.py": (
        "from b import helper\n\ndef loop(paths):\n    for p in paths:\n        helper(p)\n"
    ),
    "b.py": (
        "import subprocess\n\ndef helper(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n\n"
        "def other():\n    return 1\n"
    ),
    "c.py": (
        "import subprocess\n\ndef cspawn(path):\n"
        "    subprocess.run(['git', 'status', path], cwd='/repo')\n"
    ),
    "d.py": "from c import cspawn\n\ndef use(p):\n    return cspawn(p)\n",
}


@pytest.fixture
def exact(tmp_path):
    root = tmp_path / "exact"
    root.mkdir()
    _sync(root, dict(_EXACT))
    cache = tmp_path / "cache"
    _assert_equal(root, cache)
    return root, dict(_EXACT), cache


def _edit_and_scan(root, files, cache, rel, old, new):
    assert old in files[rel]
    files[rel] = files[rel].replace(old, new, 1)
    _sync(root, files)
    return _assert_equal(root, cache)


def test_helper_body_edit_reanalyses_dependant_and_owner(exact):
    stats = _edit_and_scan(*exact, "b.py", "'git', 'add'", "'git', 'rm'")
    assert _names(stats.reanalysed) == {"a.py", "b.py"}


def test_unrelated_function_edit_reanalyses_owner_only(exact):
    stats = _edit_and_scan(*exact, "b.py", "return 1", "return 2")
    assert _names(stats.reanalysed) == {"b.py"}


def test_loop_free_module_text_edit_touches_only_its_file(exact):
    root, files, cache = exact
    files["c.py"] += "CONST = 2\n"
    _sync(root, files)
    stats = _assert_equal(root, cache)
    assert _names(stats.reanalysed) == {"c.py"}
    assert _names(stats.summarised) == {"c.py"}


def test_line_inserted_above_helper_reanalyses_dependant(exact):
    stats = _edit_and_scan(*exact, "b.py", "import subprocess\n", "import subprocess\n\n")
    assert _names(stats.reanalysed) == {"a.py", "b.py"}


def test_untouched_rerun_is_a_tree_hit(exact):
    root, _, cache = exact
    stats = _assert_equal(root, cache)
    assert stats.tree_hit and stats.reanalysed == ()


def test_corrupt_head_blob_reads_as_a_cold_cache(tree):
    root, _, cache = tree
    _assert_equal(root, cache)
    blobs = [p for p in cache.rglob("*") if p.is_file()]
    heads = [p for p in blobs if "head" in p.name]
    assert heads, [p.name for p in blobs]
    for head in heads:
        head.write_bytes(b"\x00not a marshal blob")
    stats = _assert_equal(root, cache)
    assert not stats.tree_hit


def test_cacheprovider_disabled_yields_no_cache_dir(pytester, tree):
    root, _, _ = tree
    pytester.makepyfile(
        test_inner=f"""
        import pathlib
        from coordinator_core.tests._amp_scan_incremental import gate_cache_dir, scan_incremental

        def test_inner(pytestconfig):
            assert gate_cache_dir(pytestconfig) is None
            sites, stats = scan_incremental((pathlib.Path({str(root)!r}),), None)
            assert sites and not stats.tree_hit
        """
    )
    before = {p for p in pathlib.Path(root).rglob("*")}
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not (pytester.path / ".pytest_cache").exists()
    assert {p for p in pathlib.Path(root).rglob("*")} == before
    assert _uncached((root,))


def _analyzer_sources() -> tuple:
    for module in (inc, gate, sys.modules.get("coordinator_core.tests._amp_scan_store")):
        if module is not None and hasattr(module, "_ANALYZER_SOURCES"):
            return module._ANALYZER_SOURCES
    pytest.fail("_ANALYZER_SOURCES is defined in none of the gate, driver, or store modules")


def _module_file(name: str) -> pathlib.Path | None:
    base = gate._REPO_ROOT.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _import_closure(start: pathlib.Path) -> set[pathlib.Path]:
    seen: set[pathlib.Path] = set()
    todo = [start.resolve()]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        rel = path.relative_to(gate._REPO_ROOT.resolve()).with_suffix("")
        package = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts[:-1])
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                if name.startswith("coordinator_core"):
                    target = _module_file(name)
                    if target is not None:
                        todo.append(target)
    return seen


def test_analyzer_sources_cover_the_gate_import_closure():
    declared = set()
    for entry in _analyzer_sources():
        path = pathlib.Path(entry)
        if not path.is_absolute():
            path = gate._REPO_ROOT / path
        declared.add(path.resolve())
    closure = _import_closure(pathlib.Path(gate.__file__))
    assert closure <= declared, sorted(str(p) for p in closure - declared)


@pytest.mark.cadence
def test_live_scope_cold_warm_and_per_edit_equal_uncached(tmp_path):
    cache = tmp_path / "live-cache"
    roots = gate._gate_scope_paths()
    oracle = _uncached(roots)
    cold, cold_stats = scan_incremental(roots, cache)
    assert cold == oracle and not cold_stats.tree_hit
    warm, warm_stats = scan_incremental(roots, cache)
    assert warm == oracle and warm_stats.tree_hit

    copy_root = tmp_path / "copy"
    copy_roots = []
    for root in roots:
        dest = copy_root / root.relative_to(gate._REPO_ROOT)
        shutil.copytree(
            root, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
        )
        copy_roots.append(dest)
    copy_cache = tmp_path / "copy-cache"
    copy_roots = tuple(copy_roots)
    copy_oracle = _uncached(copy_roots)
    assert scan_incremental(copy_roots, copy_cache)[0] == copy_oracle

    def def_file(callee: str) -> pathlib.Path | None:
        for root in copy_roots:
            for path in sorted(root.rglob("*.py")):
                if "tests" in path.parts:
                    continue
                if re.search(rf"^\s*(async )?def {re.escape(callee)}\(", path.read_text("utf-8"), re.M):
                    return path
        return None

    targets: list[pathlib.Path] = []
    for site in copy_oracle:
        if site.route == "c-cross-module":
            found = def_file(site.callee)
            if found is not None and found not in targets:
                targets.append(found)
        if len(targets) == 3:
            break
    for site in copy_oracle:
        if len(targets) == 3:
            break
        own = next((r / site.path for r in copy_roots if (r / site.path).is_file()), None)
        if own is not None and own.is_file() and own not in targets:
            targets.append(own)
    assert len(targets) == 3, "fewer than three callee files with dependants in the live scope"

    for target in targets:
        text = target.read_text("utf-8")
        head, _, rest = text.partition("\n")
        target.write_bytes((head + "\n\n" + rest).encode("utf-8"))
        cached, _ = scan_incremental(copy_roots, copy_cache)
        assert cached == _uncached(copy_roots), str(target)
