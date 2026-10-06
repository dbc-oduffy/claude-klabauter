"""Every `coordinator_core.install` module that writes into the settings home or
the harness config dir either consults `COORDINATOR_DISABLE_MACHINE_MUTATION`
or is named in `substrate.SETTINGS_HOME_WRITER_CARVE_OUT`. An undeclared
settings-home writer fails; so does a roster entry that now gates.

Settings-home reach is read from the AST: a call to a settings-home resolver,
a `*claude_home*` name/parameter, or a `.claude` / `.coordinator-claude-settings`
literal. Modules that take their settings-home target from the caller
(door_install, door_uninstall) carry no such reference and are pinned by name.

`scripts/setup.py` is one module holding every kind of write, so it is read
per function instead: each function with a filesystem write either consults
the switch in its own body, is declared as a `scripts/setup.py::<function>`
roster entry, or is a project-local write named below. Its subprocess spawns
are delegations to other installers and are not judged here.
"""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.install import substrate
from coordinator_core.install.tests import test_write_reaching_modules_declare as discovery

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SETUP_PY = _REPO_ROOT / "scripts" / "setup.py"
_SETUP_SITE_PREFIX = "scripts/setup.py::"

# setup.py write sites that land in the project tree, never machine state.
_SETUP_WRITES_OUTSIDE_MACHINE_STATE = {
    "write_environment_import": "the repo's own .claude/environment.md",
}

_RESOLVERS = frozenset({"settings_home", "claude_config_dir", "machine_local_dir"})
_HOME_LITERALS = frozenset({".claude", ".coordinator-claude-settings"})

# Install-plane carve-outs whose writes land outside the settings home: the
# complement of SETTINGS_HOME_WRITER_CARVE_OUT within the install-plane class.
# A settings-home literal here is a read (forwarder_door_census), not a write.
_INSTALL_PLANE_OUTSIDE_SETTINGS_HOME = {
    "clone_sibling_repo": "git clone into a caller-supplied directory",
    "detect_test_cmd": "frontmatter keys in the target project's coordinator.local.md",
    "forwarder_door_census": "coordinator_core/ops/warm_entrypoint_allowlist.json in claude-klabauter's own tree",
    "scaffold_structure": "manifest-declared directories in the target project",
    "settings_env": "the caller-supplied settings.json, rewritten by the spawned check-settings-env.py",
}

# Modules that write into the settings home yet carry no AST reference to it.
_CALLER_TARGETED_SETTINGS_HOME_WRITERS = frozenset({"door_install", "door_uninstall"})

# Classes whose members never decide a settings-home write themselves.
_NON_DECIDING_CLASSES = frozenset({substrate.CARVE_OUT_CALLER_GATED, substrate.CARVE_OUT_NOT_A_WRITE})


def _reaches_settings_home(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (node.id in _RESOLVERS or "claude_home" in node.id):
            return True
        if isinstance(node, ast.Attribute) and (node.attr in _RESOLVERS or "claude_home" in node.attr):
            return True
        if isinstance(node, ast.arg) and "claude_home" in node.arg:
            return True
        if isinstance(node, ast.Constant) and node.value in _HOME_LITERALS:
            return True
    return False


def _carve_out_class(stem: str):
    entry = substrate.MACHINE_MUTATION_SWITCH_CARVE_OUTS.get(f"{stem}.py")
    return entry[0] if entry else None


def _undeclared_settings_home_writers(modules: list[Path], roster) -> list[str]:
    return sorted(
        p.stem
        for p in modules
        if discovery._flagged_calls(p)
        and not discovery._consults_kill_switch(p)
        and (_reaches_settings_home(p) or p.stem in _CALLER_TARGETED_SETTINGS_HOME_WRITERS)
        and p.stem not in roster
        and p.stem not in _INSTALL_PLANE_OUTSIDE_SETTINGS_HOME
        and _carve_out_class(p.stem) not in _NON_DECIDING_CLASSES
    )


def _module_roster() -> set[str]:
    return {e for e in substrate.SETTINGS_HOME_WRITER_CARVE_OUT if not e.startswith(_SETUP_SITE_PREFIX)}


def _setup_roster() -> set[str]:
    return {
        e.removeprefix(_SETUP_SITE_PREFIX)
        for e in substrate.SETTINGS_HOME_WRITER_CARVE_OUT
        if e.startswith(_SETUP_SITE_PREFIX)
    }


def _functions_by_qualname(tree: ast.Module) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                found[name] = child
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _is_sys_path_remove(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "remove"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "path"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "sys"
    )


def _function_consults_switch(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == discovery._SWITCH_GUARD_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == discovery._SWITCH_GUARD_NAME:
            return True
        if isinstance(node, ast.Constant) and node.value == discovery._SWITCH_ENV:
            return True
    return False


def _setup_write_sites(path: Path) -> dict[str, bool]:
    """Qualname of every function holding a filesystem write -> whether that
    function consults the switch. Spawns, `sys.path.remove`, and opens proven
    read-only are not filesystem writes."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    os_names = frozenset(discovery._module_imported_as(tree, "os"))
    io_names = frozenset(discovery._module_imported_as(tree, "io"))
    functions = _functions_by_qualname(tree)
    sites: dict[str, bool] = {}
    for hit in discovery._flagged_calls(path):
        if hit.name in discovery._SUBPROCESS_NAMES or _is_sys_path_remove(hit.node):
            continue
        if hit.name in ("open", "fdopen") and discovery._proves_read_only(hit.node, os_names, io_names):
            continue
        fn = functions.get(hit.qualname)
        sites[hit.qualname] = fn is not None and _function_consults_switch(fn)
    return sites


def _undeclared_setup_writes(path: Path, roster: set[str]) -> list[str]:
    return sorted(
        qualname
        for qualname, gated in _setup_write_sites(path).items()
        if not gated and qualname not in roster and qualname not in _SETUP_WRITES_OUTSIDE_MACHINE_STATE
    )


def test_every_setup_py_write_is_gated_or_declared() -> None:
    undeclared = _undeclared_setup_writes(_SETUP_PY, _setup_roster())
    assert not undeclared, (
        f"scripts/setup.py write site(s) {undeclared} neither consult COORDINATOR_DISABLE_MACHINE_MUTATION "
        f"nor appear in substrate.SETTINGS_HOME_WRITER_CARVE_OUT as {_SETUP_SITE_PREFIX}<function>"
    )


def test_setup_py_roster_entries_are_ungated_write_sites() -> None:
    sites = _setup_write_sites(_SETUP_PY)
    roster = _setup_roster()
    assert not roster & set(_SETUP_WRITES_OUTSIDE_MACHINE_STATE)
    problems = []
    for qualname in sorted(roster | set(_SETUP_WRITES_OUTSIDE_MACHINE_STATE)):
        if qualname not in sites:
            problems.append(f"{qualname} (no longer a write site)")
        elif sites[qualname]:
            problems.append(f"{qualname} (now consults the switch -- drop the entry)")
    assert not problems, f"stale scripts/setup.py carve-out entries: {problems}"


def test_an_ungated_setup_py_write_is_refused(tmp_path) -> None:
    fake = tmp_path / "setup.py"
    fake.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "def stray(p):\n"
        "    Path(p).unlink()\n"
        "def careful(p):\n"
        "    if _refuse_machine_mutation(p, what='x'):\n"
        "        return\n"
        "    Path(p).unlink()\n"
        "def declared(p):\n"
        "    Path(p).write_text('x')\n"
        "def harmless(p):\n"
        "    sys.path.remove(p)\n"
        "    with open(p) as f:\n"
        "        return f.read()\n",
        encoding="utf-8",
    )
    assert _undeclared_setup_writes(fake, {"declared"}) == ["stray"]


def test_every_settings_home_writer_is_gated_or_declared() -> None:
    undeclared = _undeclared_settings_home_writers(discovery._install_modules(), _module_roster())
    assert not undeclared, (
        f"settings-home writer(s) {undeclared} neither consult COORDINATOR_DISABLE_MACHINE_MUTATION "
        "nor appear in substrate.SETTINGS_HOME_WRITER_CARVE_OUT"
    )


def test_roster_entries_are_ungated_install_plane_writers() -> None:
    by_stem = {p.stem: p for p in discovery._install_modules()}
    problems = []
    for stem in sorted(_module_roster()):
        path = by_stem.get(stem)
        if path is None:
            problems.append(f"{stem} (module no longer exists)")
        elif not discovery._flagged_calls(path):
            problems.append(f"{stem} (no longer write-reaching)")
        elif discovery._consults_kill_switch(path):
            problems.append(f"{stem} (now consults the switch -- drop the entry)")
        elif _carve_out_class(stem) != substrate.CARVE_OUT_INSTALL_PLANE:
            problems.append(f"{stem} (not an install-plane MACHINE_MUTATION_SWITCH_CARVE_OUTS entry)")
    assert not problems, f"stale SETTINGS_HOME_WRITER_CARVE_OUT entries: {problems}"


def test_every_install_plane_carve_out_is_classified_by_destination() -> None:
    install_plane = {
        name.removesuffix(".py")
        for name, (kind, _) in substrate.MACHINE_MUTATION_SWITCH_CARVE_OUTS.items()
        if kind == substrate.CARVE_OUT_INSTALL_PLANE
    }
    roster = _module_roster()
    outside = set(_INSTALL_PLANE_OUTSIDE_SETTINGS_HOME)
    assert not roster & outside
    assert install_plane == roster | outside, (
        f"unclassified install-plane carve-outs: {sorted(install_plane - roster - outside)}; "
        f"classified but not install-plane: {sorted((roster | outside) - install_plane)}"
    )


def test_an_undeclared_settings_home_writer_is_refused(tmp_path) -> None:
    ghost = tmp_path / "ghost_writer.py"
    ghost.write_text(
        "import shutil\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def f():\n"
        "    shutil.rmtree(settings_home() / 'x')\n",
        encoding="utf-8",
    )
    gated = tmp_path / "careful_writer.py"
    gated.write_text(
        "import shutil\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def f():\n"
        "    if _refuse_machine_mutation('x', what='x'):\n"
        "        return\n"
        "    shutil.rmtree(settings_home() / 'x')\n",
        encoding="utf-8",
    )
    assert _undeclared_settings_home_writers([ghost, gated], set()) == ["ghost_writer"]
    assert _undeclared_settings_home_writers([ghost], {"ghost_writer"}) == []
