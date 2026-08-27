"""coordinator_core.testing.fake_engine_root — build a stub tree that
``cc_invoke``'s engine-root gate accepts as a checkout.

Purpose: a fixture that pins ``COORDINATOR_ENGINE_ROOT`` at a synthetic
directory (to intercept an op with a stand-in module) only gets that
interception if the directory satisfies the gate in
``coordinator/bin/lib/engine_bootstrap.py :: _delegate_to_gate``. That gate
imports ``coordinator_core.engine_root`` from the candidate and calls
``coordinator_engine_root_with_class()``; a bare ``coordinator_core/__init__.py``
plus the op module under test is not enough, and the candidate is rejected with
"is not a valid claude-klabauter checkout" rather than being silently ignored.

Before C14 (`fb1421af2`) those fixtures pinned the now-retired ``CLAUDE_KLABAUTER_ROOT``,
which the gate no longer reads at all — so the stub was never consulted, the
child resolved the real checkout instead, and the test asserted against the
real op. Repointing such a fixture at the honoured variable is only half the
fix; the stub also has to answer the gate, which is what this module writes.

Negative-spec: this writes a stub whose ``coordinator_engine_root_with_class()``
answers the stub root itself with resolution class ``live-working-tree``. It is
NOT a stand-in for the real two-tier resolution in
``coordinator_core/engine_root.py`` and must never be used to test that gate's
own behaviour — only to satisfy it on the way to the module a fixture is
actually intercepting.
"""

from __future__ import annotations

import os

#: The resolution class the stub reports. `cc_invoke` does not branch on it
#: (see `_delegate_to_gate`'s "this rung only needs root" note); it is part of
#: the entry point's return contract, not a behaviour switch.
STUB_RESOLUTION_CLASS = "live-working-tree"

#: The live `coordinator_core` package directory — the fall-through target an
#: `overlay=True` stub appends to its own `__path__`.
_REAL_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_fake_engine_root(root: str | os.PathLike[str], *, overlay: bool = False) -> str:
    """Make ``root`` a directory the engine-root gate accepts, and return it.

    Creates ``coordinator_core/__init__.py`` and writes
    ``coordinator_core/engine_root.py`` exposing
    ``coordinator_engine_root_with_class() -> (root, STUB_RESOLUTION_CLASS)``.
    Idempotent, and safe to call before or after a fixture writes the op module
    it actually wants the child to import.

    ``overlay`` decides what happens to the submodules the fixture did NOT
    stub. A stub root shadows the whole ``coordinator_core`` package, so a CLI
    that imports any real submodule on its way to the stubbed one (say
    ``coordinator_core.git.repo_root`` from ``repo_identity``) dies on a
    ModuleNotFoundError that has nothing to do with what the test asserts.
    With ``overlay=True`` the stub package appends the REAL package directory
    to its ``__path__``, so the stub wins for the modules it defines and every
    other import falls through to the live engine. Leave it False when a test's
    subject IS a module's absence.

    ``__path__`` rescues sibling MODULES; it does nothing for the other names
    on a module the stub itself defines. ``engine_root.py`` shadows the real
    one wholesale, so a CLI reaching any other symbol on it dies on an
    ImportError naming the stub file — ``coordinator_core.liveness`` imports
    ``coordinator_engine_root`` at module scope, and that import sits under
    ``claim_state`` -> ``pickup_assemble`` -> ``repo_identity``, i.e. on the
    way into most CLIs. Under ``overlay=True`` the stub therefore loads the
    REAL ``engine_root`` by path and re-exports its globals before its own two
    definitions land, narrowing the shadow to the resolution entry points the
    gate actually calls. Under ``overlay=False`` it stays the two-function
    stub it always was.
    """
    root = os.fspath(root)
    pkg = os.path.join(root, "coordinator_core")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8", newline="\n") as fh:
        if overlay:
            fh.write(f"__path__.append({_REAL_PACKAGE_DIR!r})\n")
    with open(os.path.join(pkg, "engine_root.py"), "w", encoding="utf-8", newline="\n") as fh:
        if overlay:
            fh.write(_overlay_engine_root_prelude())
        fh.write(
            "def coordinator_engine_root_with_class():\n"
            f"    return ({root!r}, {STUB_RESOLUTION_CLASS!r})\n"
            "\n\n"
            "def coordinator_engine_root():\n"
            f"    return {root!r}\n"
        )
    return root


def _overlay_engine_root_prelude() -> str:
    """Source that re-exports the live ``engine_root``'s globals into a stub.

    Loaded by PATH rather than by import: at the time this source runs, the
    stub file IS ``coordinator_core.engine_root``, so importing that name
    would return the stub itself. Private names are copied along with public
    ones — the real module's own resets (``_reset_root_memo`` and friends) are
    reached by test code by their underscore names.
    """
    real = os.path.join(_REAL_PACKAGE_DIR, "engine_root.py")
    return (
        "import importlib.util as _ilu\n"
        f"_spec = _ilu.spec_from_file_location('_real_engine_root', {real!r})\n"
        "_real = _ilu.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_real)\n"
        "globals().update(\n"
        "    {k: v for k, v in vars(_real).items()\n"
        "     if not (k.startswith('__') and k.endswith('__'))}\n"
        ")\n"
        "\n\n"
    )


def write_overlay_package_init(root: str | os.PathLike[str], subpackage: str) -> str:
    """Make ``coordinator_core.<subpackage>`` under ``root`` fall through too.

    ``overlay`` on the top-level package only rescues submodules of
    ``coordinator_core`` itself. A fixture that stubs, say,
    ``coordinator_core/ops/changelog_ops.py`` also creates ``ops/__init__.py``,
    and THAT package shadows the real ``coordinator_core.ops`` wholesale — so
    every sibling op the CLI reaches for goes missing again, one level down.
    This writes the same ``__path__`` fall-through for that subpackage.

    Returns the directory it wrote into. Dotted names are accepted for deeper
    subpackages (``"ops.emit"``); each level needs its own call.
    """
    root = os.fspath(root)
    parts = subpackage.split(".")
    pkg_dir = os.path.join(root, "coordinator_core", *parts)
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(pkg_dir, "__init__.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"__path__.append({os.path.join(_REAL_PACKAGE_DIR, *parts)!r})\n")
    return pkg_dir


def write_fake_cli_entry(root: str | os.PathLike[str], *, overlay: bool = False) -> str:
    """Give ``root`` a ``coordinator_core.cli_entry.run_op_main`` and return it.

    A stub root SHADOWS the real ``coordinator_core`` package once the gate
    accepts it, so a CLI trampoline that reaches its op through
    ``cli_entry.run_op_main`` (the DR-276 scope-touch route) finds no
    ``cli_entry`` at all and dies with a link failure before the fixture's
    stand-in op is ever imported. This writes the minimum that route needs:
    import the named module, call its ``main(argv)``, return its code.

    ``ImportError`` is deliberately left to propagate — that is the signal a
    trampoline turns into its documented "op not importable at that root"
    exit, which is exactly what a seam-absent fixture is asserting.
    """
    root = os.fspath(root)
    write_fake_engine_root(root, overlay=overlay)
    with open(
        os.path.join(root, "coordinator_core", "cli_entry.py"), "w", encoding="utf-8", newline="\n"
    ) as fh:
        fh.write(
            "import importlib\n"
            "\n"
            "\n"
            "def run_op_main(module_name, argv):\n"
            "    return importlib.import_module(module_name).main(argv)\n"
        )
    return root
