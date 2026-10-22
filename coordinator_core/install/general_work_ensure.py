"""Install step: converge the machine-local general-work root into a full coordinator repo.

Delegates to DoE's `coordinator/bin/general-work-root.py ensure`, loaded in-process (no
subprocess, no shell). That script owns the tree build and the commit of only the paths it
seeded; this module never runs git, so `tax-2025/` is never staged from here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPT_REL = ("coordinator", "bin", "general-work-root.py")
_EXIT_REFUSED = 3


@dataclass(frozen=True)
class GeneralWorkEnsureOutcome:
    """status: ensured | refused | script_missing | failed. Never raised; install continues."""

    status: str
    root: str | None = None
    detail: str = ""


def _load_script(script: Path):
    spec = importlib.util.spec_from_file_location("_general_work_root_cli", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def ensure_general_work_root(coordinator_root: Path | str | None) -> GeneralWorkEnsureOutcome:
    """Run `general-work-root ensure --json` once against the resolved coordinator-claude root."""
    if coordinator_root is None:
        return GeneralWorkEnsureOutcome("script_missing", detail="coordinator-claude root unresolved")
    script = Path(coordinator_root).joinpath(*_SCRIPT_REL)
    if not script.is_file():
        return GeneralWorkEnsureOutcome("script_missing", detail=f"{script} not found")
    out, err = io.StringIO(), io.StringIO()
    try:
        module = _load_script(script)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(["ensure", "--json"])
    except (Exception, SystemExit) as exc:  # install must continue past a DoE-side failure, incl. argparse/sys.exit
        return GeneralWorkEnsureOutcome("failed", detail=f"{type(exc).__name__}: {exc}")
    if code == _EXIT_REFUSED:
        return GeneralWorkEnsureOutcome("refused", detail=(err.getvalue() or out.getvalue()).strip())
    if code != 0:
        return GeneralWorkEnsureOutcome("failed", detail=f"exit {code}: {err.getvalue().strip()}")
    try:
        root = json.loads(out.getvalue().strip().splitlines()[-1]).get("root")
    except (ValueError, IndexError, AttributeError):
        root = None
    return GeneralWorkEnsureOutcome("ensured", root=root)
