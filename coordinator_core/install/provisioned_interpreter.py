"""Resolve the host provisioned interpreter: the one example-retrieval-repo pins and
provisions with tree-sitter + tree-sitter-pwsh. Never raises; an unresolved
pin is a result with `path=None` and a reason, never a PATH fallback."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.install.write_surface import WriteSurfaceDeclaration

# Read-only: the one subprocess spawn reads example-retrieval-repo's pin back from stdout.
WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="provisioned-interpreter",
    source_module="coordinator_core.install.provisioned_interpreter",
    clauses=(),
)

_READER_REL = Path("project_rag_scripts") / "lib" / "read_claude_pin.py"
_READ_TIMEOUT_SECS = 5
_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProvisionedInterpreter:
    path: str | None
    reason: str = ""
    example_retrieval_repo_root: str | None = None


def resolve_provisioned_interpreter(example_retrieval_repo_root: str | None = None) -> ProvisionedInterpreter:
    """Return the interpreter example-retrieval-repo's installer pinned for `example_retrieval_repo_root`
    (default: machine-local `repos.project_rag`)."""
    root = example_retrieval_repo_root
    if not root:
        try:
            from coordinator_core.install._shared import ml_get

            root = ml_get("repos.project_rag")
        except Exception as exc:  # noqa: BLE001
            return ProvisionedInterpreter(None, f"machine-local read failed: {exc}")
    if not root or not Path(root).is_dir():
        return ProvisionedInterpreter(None, "repos.project_rag unset or missing")
    reader = Path(root) / _READER_REL
    if not reader.is_file():
        return ProvisionedInterpreter(None, f"pin reader absent: {reader}", root)
    try:
        proc = subprocess.run(
            # Contract (example-retrieval-repo interpreter-pin.md § Consumer contract): argv is the CONSUMER's root.
            [sys.executable, str(reader), str(_CLAUDE_KLABAUTER_ROOT)],
            capture_output=True,
            text=True,
            timeout=_READ_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProvisionedInterpreter(None, f"pin reader failed: {exc}", root)
    pin = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or len(pin) != 1 or not os.path.exists(pin[-1]):
        return ProvisionedInterpreter(
            None, f"no usable pin (exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}", root
        )
    return ProvisionedInterpreter(pin[-1], "", root)
