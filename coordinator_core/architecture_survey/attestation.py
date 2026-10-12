"""Restore any ``last_attested`` stamp a survey run wrote, from one git diff."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from coordinator_core.architecture_survey.contract import Decision
from coordinator_core.git.run import run_git

_RULE = "A survey run never writes last_attested; the diff against HEAD is reverted line by line."
_KEY = b"last_attested:"


class AttestationError(RuntimeError):
    """The diff spawn exited non-zero; the caller turns this into a failed step."""


def _run_diff(repo_root: Path, systems_dir: str) -> subprocess.CompletedProcess:
    result = run_git(
        [
            "--no-optional-locks", "-c", "core.quotepath=off", "diff", "HEAD", "-U0",
            "-G^last_attested:", "--diff-filter=AMR", "--", systems_dir,
        ],
        cwd=str(repo_root),
        binary=True,
    )
    return subprocess.CompletedProcess([], result.returncode, result.stdout_bytes, result.stderr.encode("utf-8"))


def _parse(diff: bytes) -> dict[str, tuple[bytes | None, bytes | None]]:
    """Map path -> (removed line, added line), each without its newline, or None."""
    out: dict[str, tuple[bytes | None, bytes | None]] = {}
    path: str | None = None
    old: bytes | None = None
    new: bytes | None = None

    def flush() -> None:
        if path is not None and (old is not None or new is not None):
            out[path] = (old, new)

    for raw in diff.split(b"\n"):
        line = raw.rstrip(b"\r")
        if line.startswith(b"diff --git "):
            flush()
            path, old, new = None, None, None
        elif line.startswith(b"+++ b/"):
            path = line[6:].decode("utf-8")
        elif line.startswith(b"-") and not line.startswith(b"---") and line[1:].startswith(_KEY):
            old = line[1:]
        elif line.startswith(b"+") and not line.startswith(b"+++") and line[1:].startswith(_KEY):
            new = line[1:]
    flush()
    return out


def _val(line: bytes | None) -> str:
    return "<none>" if line is None else line[len(_KEY):].strip().decode("utf-8", "replace")


def restore(
    repo_root: str | Path,
    *,
    systems_dir: str = "docs/architecture/systems/",
    runner: Callable[[Path, str], subprocess.CompletedProcess] | None = None,
) -> Decision:
    """Revert every last_attested change under systems_dir; one git spawn for all pages."""
    root = Path(repo_root)
    proc = (runner or _run_diff)(root, systems_dir)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() if proc.stderr else ""
        raise AttestationError(f"git diff exited {proc.returncode}: {err}")
    changes = _parse(proc.stdout or b"")
    evidence: list[str] = []
    for rel, (old, new) in sorted(changes.items()):
        target = root / rel
        lines = target.read_bytes().splitlines(keepends=True)
        result: list[bytes] = []
        done = False
        for ln in lines:
            if not done and ln.startswith(_KEY):
                done = True
                if old is None:
                    continue
                eol = ln[len(ln.rstrip(b"\r\n")):]
                ln = old + eol
            result.append(ln)
        if not done:
            continue
        target.write_bytes(b"".join(result))
        evidence.append(f"{rel}: {_val(old)} <- {_val(new)}")
    if not evidence:
        return Decision("attestation", "none-touched", _RULE, "no last_attested change in diff")
    return Decision("attestation", "restored", _RULE, "; ".join(evidence))
