"""Resolve the host provisioned interpreter: the one example-retrieval-repo pins and
provisions with tree-sitter + tree-sitter-pwsh. Never raises; an unresolved
pin is a result with `path=None` and a reason, never a PATH fallback."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.install.write_surface import WriteSurfaceDeclaration

# Read-only: the pin is read in-process from example-retrieval-repo's reader module.
WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="provisioned-interpreter",
    source_module="coordinator_core.install.provisioned_interpreter",
    clauses=(),
)

_READER_REL = Path("project_rag_scripts") / "lib" / "read_claude_pin.py"
_INSTALLER_REL = Path("project_rag_scripts") / "install_example_retrieval_repo_plugin.py"
_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProvisionedInterpreter:
    path: str | None
    reason: str = ""
    example_retrieval_repo_root: str | None = None
    never_installed: bool = False  # reader exit 3: registered, no host pin written
    install_command: str = ""  # set with never_installed: the one command that writes the pin


def resolve_provisioned_interpreter(example_retrieval_repo_root: str | None = None) -> ProvisionedInterpreter:
    """Return the interpreter example-retrieval-repo's installer pinned for `example_retrieval_repo_root`
    (default: machine-local `repos.project_rag`)."""
    root = example_retrieval_repo_root
    if not root:
        try:
            from coordinator_core.machine_resolver import registry_get

            root = registry_get("repos.project_rag") or ""
        except Exception as exc:  # noqa: BLE001
            return ProvisionedInterpreter(None, f"machine-local read failed: {exc}")
    if not root:
        return ProvisionedInterpreter(None, "repos.project_rag unset")
    if not Path(root).is_dir():
        return ProvisionedInterpreter(None, f"repos.project_rag is set to {root} but that directory is missing")
    reader = Path(root) / _READER_REL
    if not reader.is_file():
        return ProvisionedInterpreter(None, f"pin reader absent: {reader}", root)
    try:
        code, pin, err = _read_pin_in_process(reader)
    except Exception as exc:  # noqa: BLE001
        return ProvisionedInterpreter(None, f"pin reader failed: {exc}", root)
    if code != 0 or not pin or not os.path.exists(pin):
        reason = f"no usable pin (exit {code}): {err.strip()[:200]}"
        if code != 3:
            return ProvisionedInterpreter(None, reason, root)
        command = f"{sys.executable} {_INSTALLER_REL.as_posix()} (in {root})"
        reason += f" -- example-retrieval-repo is registered but never host-installed here; run {command}"
        return ProvisionedInterpreter(None, reason, root, never_installed=True, install_command=command)
    return ProvisionedInterpreter(pin, "", root)


def _read_pin_in_process(reader: Path) -> tuple[int, str, str]:
    """`(exit_code, pin, stderr)` from example-retrieval-repo's `read_pin`, called in-process (stdlib-only module, no spawn).

    Trap: the module is executed unregistered in `sys.modules` so a test's fake reader never shadows a real one."""
    spec = importlib.util.spec_from_file_location("_example_retrieval_repo_read_claude_pin", reader)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {reader}")
    module = importlib.util.module_from_spec(spec)
    err = io.StringIO()
    # The reader prepends example-retrieval-repo's root to sys.path and imports example_retrieval_repo_* packages; the engine
    # is long-lived, so both are rolled back rather than left to shadow claude-klabauter's own modules.
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                spec.loader.exec_module(module)
                # Contract (example-retrieval-repo interpreter-pin.md § Consumer contract): the argument is the CONSUMER's root.
                pin = module.read_pin(str(_CLAUDE_KLABAUTER_ROOT))
            except SystemExit as exc:
                return (exc.code if isinstance(exc.code, int) else 1), "", err.getvalue()
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            if name.startswith("project_rag"):
                del sys.modules[name]
    if getattr(module, "_is_uwp_path", lambda _p: False)(pin):
        return 4, "", err.getvalue()
    return 0, pin, err.getvalue()
