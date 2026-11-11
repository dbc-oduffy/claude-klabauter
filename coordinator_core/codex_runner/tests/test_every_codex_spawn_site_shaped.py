"""Ratchet: every engine module that spawns a sandboxed codex child shapes its env first.

A module is a codex spawn site when it spawns a process (Popen, a Popen alias, subprocess.run/check_output/
check_call/call) and also reaches codex through resolve_codex, `shutil.which("codex")`, the `workspace-write`
sandbox literal, an agent adapter's spawn_argv/resume_argv/proxy_argv, or KIND_CODEX. A new site must join SITES.
In a SITES module every spawn call must be dominated by shape_codex_child: on every path to the spawn in its
enclosing function, a shape call has already run (a guard on a "codex" comparison covers the codex arm only).
A spawn whose `env=` is one of its own function parameters passes: the caller owns the shaping.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2]

SITES = (
    "codex_runner/execute.py",
    "worker_contracts/worker.py",
    "codex_runner/workflow.py",
    "ops/workflow_fire/headless_agents.py",
    "codex_runner/review.py",
    "agent_pipe/reviewer_run.py",
    "agent_pipe/peer.py",
    "agent_pipe/__main__.py",
    "agent_pipe/codex_app_server.py",
)

EXEMPT = {
    "agent_pipe/bridge.py": "spawns the agy adapter's resume_argv, not a sandboxed codex child",
    "ops/agent_pipe.py": "spawns the python agent_pipe worker (`-m coordinator_core.agent_pipe`), which shapes its own codex child; it only reads proxy_argv to record a binding",
    "codex_runner/sandbox_preflight.py": "is the preflight: its Popen probes `codex sandbox` and builds no child env",
}

_ARGV_ATTRS = {"spawn_argv", "resume_argv", "proxy_argv"}
_RUNNERS = {"run", "check_output", "check_call", "call"}
_SHAPE = "shape_codex_child"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def _is_popen_ref(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Popen") or (
        isinstance(node, ast.Attribute) and node.attr == "Popen"
    )


def _popen_aliases(tree: ast.AST) -> set[str]:
    """Names bound to a Popen reference (`factory = subprocess.Popen if x is None else x`)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(_is_popen_ref(n) for n in ast.walk(node.value)):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return aliases


def _is_runner_call(node: ast.Call) -> bool:
    fn = node.func
    return isinstance(fn, ast.Attribute) and fn.attr in _RUNNERS and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call that starts a process: Popen, a Popen alias, or a subprocess runner."""
    aliases = _popen_aliases(tree)
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if _is_popen_ref(fn) or (isinstance(fn, ast.Name) and fn.id in aliases) or _is_runner_call(node):
            out.append(node)
    return out


def _references_spawn(tree: ast.AST) -> bool:
    return bool(_spawn_calls(tree)) or any(_is_popen_ref(n) for n in ast.walk(tree))


def _is_which_codex(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))):
        return False
    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
    return name == "which" and bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "codex"


def _is_codex_spawn_site(tree: ast.AST) -> bool:
    if not _references_spawn(tree):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {"resolve_codex", "KIND_CODEX"}:
            return True
        if isinstance(node, ast.Attribute) and (node.attr == "resolve_codex" or node.attr in _ARGV_ATTRS):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "workspace-write" in node.value:
            return True
        if _is_which_codex(node):
            return True
    return False


def _is_production(path: Path) -> bool:
    rel = path.relative_to(CORE).parts
    return not (
        "tests" in rel[:-1]
        or "fixtures" in rel[:-1]
        or "__pycache__" in rel
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def _production_modules() -> list[Path]:
    return [p for p in CORE.rglob("*.py") if _is_production(p)]


def _is_codex_guard(test: ast.AST) -> bool:
    """An `== "codex"` / `in (...)` test: its body is the codex arm. `!= "codex"` guards the other arm and never counts."""
    return any(
        isinstance(n, ast.Compare)
        and all(isinstance(op, (ast.Eq, ast.In)) for op in n.ops)
        and any(isinstance(c, ast.Constant) and c.value == "codex" for c in [n.left, *n.comparators])
        for n in ast.walk(test)
    )


def _ends_terminal(block: list[ast.stmt]) -> bool:
    return bool(block) and isinstance(block[-1], (ast.Return, ast.Raise, ast.Continue))


def _shapes_block(block: list[ast.stmt]) -> bool:
    return any(_shapes(s) for s in block)


def _shapes(stmt: ast.stmt) -> bool:
    """True when running `stmt` to completion has called shape_codex_child on every path that continues past it."""
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _shapes_block(stmt.body)
    if isinstance(stmt, ast.Try):
        if _shapes_block(stmt.body) or _shapes_block(stmt.finalbody):
            return all(_ends_terminal(h.body) or _shapes_block(h.body) for h in stmt.handlers)
        return False
    if isinstance(stmt, ast.If):
        if _is_codex_guard(stmt.test):
            return _shapes_block(stmt.body)
        return _shapes_block(stmt.body) and _shapes_block(stmt.orelse)
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    return any(isinstance(n, ast.Call) and _call_is_shape(n) for n in ast.walk(stmt))


def _call_is_shape(call: ast.Call) -> bool:
    fn = call.func
    return (isinstance(fn, ast.Name) and fn.id == _SHAPE) or (isinstance(fn, ast.Attribute) and fn.attr == _SHAPE)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _dominated(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """A shaping statement precedes `call` in some block enclosing it, up to its function."""
    node: ast.AST = call
    while node in parents:
        parent = parents[node]
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and node in block:
                if _shapes_block(block[: block.index(node)]):
                    return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        node = parent
    return False


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _env_is_caller_param(call: ast.Call, fn: ast.AST | None) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
    return any(kw.arg == "env" and isinstance(kw.value, ast.Name) and kw.value.id in params for kw in call.keywords)


def _unshaped_spawns(tree: ast.AST) -> list[str]:
    """`function:line` for every spawn call that no shape_codex_child dominates and no caller-owned env covers."""
    parents = _parents(tree)
    bad: list[str] = []
    for call in _spawn_calls(tree):
        fn = _enclosing_function(call, parents)
        if _dominated(call, parents) or _env_is_caller_param(call, fn):
            continue
        bad.append(f"{getattr(fn, 'name', '<module>')}:{call.lineno}")
    return sorted(bad)


@pytest.mark.parametrize("site", SITES)
def test_site_calls_shape_codex_child(site: str) -> None:
    path = CORE / site
    assert path.is_file(), f"SITES entry missing on disk: {site}"
    assert _SHAPE in _called_names(_tree(path)), f"{site} spawns codex without shape_codex_child"


@pytest.mark.parametrize("site", SITES)
def test_shape_codex_child_dominates_every_spawn(site: str) -> None:
    unshaped = _unshaped_spawns(_tree(CORE / site))
    assert not unshaped, f"{site} spawns without shape_codex_child on every path (function:line): {unshaped}"


@pytest.mark.parametrize("site", SITES)
def test_every_listed_site_is_a_detected_spawn_site(site: str) -> None:
    assert _is_codex_spawn_site(_tree(CORE / site)), f"{site} is in SITES but no longer matches the spawn-site scan"


def test_no_unlisted_codex_spawn_site() -> None:
    known = {*SITES, *EXEMPT}
    unlisted = sorted(
        p.relative_to(CORE).as_posix()
        for p in _production_modules()
        if p.relative_to(CORE).as_posix() not in known and _is_codex_spawn_site(_tree(p))
    )
    assert not unlisted, f"codex spawn sites missing from SITES (and from shape_codex_child): {unlisted}"


@pytest.mark.parametrize("exempt", sorted(EXEMPT))
def test_exemptions_are_live_and_unshaped(exempt: str) -> None:
    tree = _tree(CORE / exempt)
    assert _is_codex_spawn_site(tree), f"{exempt} no longer matches the scan; drop its EXEMPT entry"
    assert _SHAPE not in _called_names(tree), f"{exempt} shapes its env; move it to SITES"


@pytest.mark.parametrize("argv_only", ["agent_pipe/codex.py", "codex_runner/resolve.py", "agent_pipe/worker_mode.py", "install/host_targets.py"])
def test_argv_only_modules_are_not_sites(argv_only: str) -> None:
    path = CORE / argv_only
    assert path.is_file(), f"fixture module missing: {argv_only}"
    assert not _is_codex_spawn_site(_tree(path)), f"{argv_only} now spawns; treat it as a site"


def test_scan_flags_an_unshaped_indirect_argv_spawn() -> None:
    src = "import subprocess\n\ndef go(adapter):\n    return subprocess.Popen(adapter.spawn_argv())\n"
    tree = ast.parse(src)
    assert _is_codex_spawn_site(tree)
    assert _SHAPE not in _called_names(tree)


def test_scan_ignores_popen_without_codex_reference() -> None:
    assert not _is_codex_spawn_site(ast.parse("import subprocess\nsubprocess.Popen(['git'])\n"))


@pytest.mark.parametrize(
    "src",
    [
        "import subprocess, shutil\ndef go():\n    subprocess.run([shutil.which('codex')])\n",
        "import subprocess\ndef go(a):\n    f = subprocess.Popen\n    f(a.proxy_argv())\n",
        "import subprocess\ndef go():\n    subprocess.check_output(['codex'], env=KIND_CODEX)\n",
    ],
)
def test_scan_detects_runner_alias_and_which_codex_sites(src: str) -> None:
    assert _is_codex_spawn_site(ast.parse(src))


_PRE = "import subprocess\n"


@pytest.mark.parametrize(
    "body",
    [
        "def go(a):\n    env = shape_codex_child({})\n    subprocess.Popen(a, env=env)\n",
        "def go(a, agent):\n    env = {}\n    if agent == 'codex':\n        env = shape_codex_child(env)\n    subprocess.Popen(a, env=env)\n",
        "def go(a, agent):\n    if a:\n        env = shape_codex_child({})\n    else:\n        env = shape_codex_child({1: 1})\n    subprocess.Popen(a, env=env)\n",
        "def go(a):\n    try:\n        env = shape_codex_child({})\n    except OSError:\n        return None\n    subprocess.Popen(a, env=env)\n",
        "def go(a):\n    env = shape_codex_child({})\n    with open('x') as f:\n        subprocess.Popen(a, env=env, stderr=f)\n",
        "def go(a, env):\n    subprocess.Popen(a, env=env)\n",
    ],
)
def test_dominance_accepts_shaped_spawns(body: str) -> None:
    assert _unshaped_spawns(ast.parse(_PRE + body)) == []


@pytest.mark.parametrize(
    "body",
    [
        "def go(a):\n    subprocess.Popen(a, env={})\n",
        "def go(a, req):\n    env = {}\n    if not req:\n        env = shape_codex_child(env)\n    subprocess.Popen(a, env=env)\n",
        "def go(a, req):\n    if req:\n        env = shape_codex_child({})\n    subprocess.Popen(a, env={})\n",
        "def go(a):\n    subprocess.Popen(a, env={})\n    shape_codex_child({})\n",
        "def go(a):\n    try:\n        env = shape_codex_child({})\n    except OSError:\n        env = {}\n    subprocess.Popen(a, env=env)\n",
        "def go(a):\n    for _ in a:\n        env = shape_codex_child({})\n    subprocess.Popen(a, env=env)\n",
        "def go(a, agent):\n    env = {}\n    if agent != 'codex':\n        env = shape_codex_child(env)\n    subprocess.Popen(a, env=env)\n",
        "def go(a):\n    f = subprocess.Popen\n    f(a, env={})\n",
        "def go(a):\n    subprocess.run(a, env={})\n",
        "def go(a):\n    subprocess.check_output(a, env={})\n",
        "def go(a):\n    env = {}\n    subprocess.Popen(a, env=env)\n",
    ],
)
def test_dominance_rejects_unshaped_spawns(body: str) -> None:
    assert _unshaped_spawns(ast.parse(_PRE + body)) != []
