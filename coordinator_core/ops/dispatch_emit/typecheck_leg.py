"""The terminal test phase's TypeScript typecheck leg.

vitest and jest do not typecheck, so a row that fails ``tsc --noEmit`` passes the
scoped tests. ``typecheck_leg`` names one ``tsc --noEmit -p <dir>`` per distinct
governing ``tsconfig.json`` of the run's written ``.ts``/``.tsx`` files; the
emitter folds it into the existing ``coordinator:test-runner`` prompt, so a
typecheck failure fails that phase's ``tests`` status as a test failure does.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

_TS_SUFFIXES = (".ts", ".tsx")
_TSC_BIN_NAMES = ("tsc", "tsc.cmd")


@dataclass(frozen=True)
class TypecheckLeg:
    """``commands``: repo-root-relative argv text, one per distinct tsconfig dir.
    ``not_run``: ``(tsconfig dir, reason)`` for each dir where no tsc resolved."""

    commands: tuple[str, ...]
    not_run: tuple[tuple[str, str], ...]


def _governing_tsconfig_dir(path: str, root: Path) -> Optional[str]:
    """Repo-relative posix dir ('.' for the root) of the nearest ancestor of
    ``path`` holding a ``tsconfig.json``, bounded at the repo root."""
    parent = PurePosixPath(path).parent
    while True:
        if (root / parent / "tsconfig.json").is_file():
            return parent.as_posix()
        if parent == PurePosixPath("."):
            return None
        parent = parent.parent


def _local_tsc(tsconfig_dir: str, root: Path) -> Optional[str]:
    """Repo-relative posix path of ``node_modules/.bin/tsc`` nearest the
    tsconfig dir, walking up to the repo root (a hoisted workspace install)."""
    directory = PurePosixPath(tsconfig_dir)
    while True:
        for name in _TSC_BIN_NAMES:
            candidate = directory / "node_modules" / ".bin" / name
            if (root / candidate).is_file():
                return candidate.as_posix()
        if directory == PurePosixPath("."):
            return None
        directory = directory.parent


def typecheck_leg(written_paths: list[str], repo_root: Optional[Path]) -> Optional[TypecheckLeg]:
    """The leg for ``written_paths``, or ``None`` when no written ``.ts``/``.tsx``
    file has a governing tsconfig. tsc resolves to the repo's own
    ``node_modules/.bin`` first, else ``npx --no-install tsc`` when ``npx`` is on
    PATH; neither makes that dir ``not_run``, never a pass."""
    if repo_root is None:
        return None
    dirs = sorted(
        {
            d
            for p in written_paths
            if p.replace("\\", "/").endswith(_TS_SUFFIXES)
            for d in [_governing_tsconfig_dir(p.replace("\\", "/"), repo_root)]
            if d is not None
        }
    )
    if not dirs:
        return None
    npx = shutil.which("npx")
    commands: list[str] = []
    not_run: list[tuple[str, str]] = []
    for d in dirs:
        tsc = _local_tsc(d, repo_root)
        if tsc is not None:
            commands.append(f"{tsc} --noEmit -p {d}")
        elif npx is not None:
            commands.append(f"npx --no-install tsc --noEmit -p {d}")
        else:
            not_run.append((d, "no node_modules/.bin/tsc up to the repo root and no npx on PATH"))
    return TypecheckLeg(tuple(commands), tuple(not_run))


def typecheck_prompt_clause(leg: TypecheckLeg) -> str:
    """The prompt sentences that make the leg part of the phase's verdict."""
    parts: list[str] = []
    if leg.commands:
        parts.append(
            "Also run the typecheck leg from the repo root, each command to completion: "
            + "; ".join(f"`{c}`" for c in leg.commands)
            + ". Any non-zero exit fails this phase exactly as a failing test does: "
            "status `fail`, build_clean false, the tsc errors in summary. "
            "A command that cannot resolve tsc is `typecheck not_run`, never a pass: "
            "say so and why at the head of summary and set build_clean null."
        )
    for d, reason in leg.not_run:
        parts.append(
            f"Typecheck for `{d}` is not_run: {reason}. Open summary with "
            f"`typecheck not_run ({d}): {reason}` and set build_clean null."
        )
    return " ".join(parts)
