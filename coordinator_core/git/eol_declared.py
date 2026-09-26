"""coordinator_core.git.eol_declared -- does an executable's on-disk line ending
match the `eol=` its gitattributes declares, for the paths ONE COMMIT touches.

THE REQUIREMENT, AND WHY GIT CANNOT DISCHARGE IT ITSELF. A `.cmd` declared
`eol=crlf` that sits LF-only on disk is a broken Windows launcher: `cmd.exe`
silently misparses it, in a repo whose `CLAUDE.md` calls Windows first-class.
Git cannot show you that file. Check-in normalization maps the LF working tree
to the same LF index blob the CRLF version normalizes to, so there is no
content difference to report -- `git status` shows a transient ` M` off the
stat cache alone, `git diff` comes back EMPTY, and the next `add` or refresh
returns the entry to clean with the wrong bytes still on disk. Measured
2026-08-30; `docs/reference/eol-drift-detection.md` carries the transcript.

`.gitattributes` does not close this. It governs the bytes git itself writes
(checkout) and reads (check-in normalization); it has no authority over bytes a
producer writes straight into the working tree, and there is no chokepoint on
those. That is how `coordinator/bin/reap-claims-for-repos.cmd` reached LF under
a `crlf` declaration (kill-ledger K-062) -- a real incident, not a hypothetical.

WHAT THIS MODULE IS NOT. It is not a revival of `coordinator_core/ops/eol/`,
deleted entire at K-064 and explicitly not a starting point. That family read
the WHOLE CORPUS under a fleet-wide `OpClass.MUTATING` write lock, and the lock
plus the O(corpus) walk -- not the millisecond figure -- is what killed it.
This module inverts both properties, per K-064's own returns-when spec:

  - O(paths-in-commit), never O(corpus). It sees the handful of paths a commit
    is already staging and nothing else.
  - Filter-first, so the common commit pays NOTHING. `executable_paths` is a
    pure suffix test over a list the caller already holds; a commit touching no
    `.cmd`/`.ps1`/`.sh`/`.bat` makes zero git calls, reads no file, and
    allocates one empty list. Same shape `commit_v2._guard_module_paths`
    already uses to keep the guard-class relay free on commits that miss it.
  - One batched spawn for however many executables a commit carries, never one
    per path -- the amplification class `test_no_unbatched_per_item_git_spawn`
    exists to catch.
  - No lock of any kind. Nothing here serializes another commit, queue write,
    or ceremony write behind it.

Budget measurement (spawn count, wall time, headroom against the 200ms/500ms
bars) lives in `docs/reference/eol-drift-detection.md`, which the close-out
keeps current -- a dated figure in this docstring would go stale silently.
<!-- Review: overengineering-reviewer (Kira) -- collapsed the dated
measured-cost paragraph to a doc pointer; structural claims above are kept
as spec, not measurement. -->

WHY `w/` IS THE TRUTH SOURCE AND NO FILE IS READ TO DETECT. `git ls-files
--eol` reports the working tree's actual line ending in its `w/` field. Reading
the file ourselves to re-derive that would be a second, slower, divergent
implementation of a fact the one spawn already returns. Files are opened only
on the repair leg, and only for paths already known to have drifted.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence

from coordinator_core.git.run import run_git

__all__ = [
    "EXECUTABLE_SUFFIXES",
    "Drift",
    "executable_paths",
    "find_declared_eol_drift",
    "repair_declared_eol_drift",
]

EXECUTABLE_SUFFIXES = (".cmd", ".ps1", ".sh", ".bat")

#: terminator at all) and `mixed`; neither is a DECLARATION, they are
_REPAIRABLE_DECLARATIONS = ("lf", "crlf")

_BYTES_FOR = {"lf": b"\n", "crlf": b"\r\n"}

#: --eol`. The attribute field is space-padded and itself CONTAINS spaces
_RECORD = re.compile(r"^i/(\S*)\s+w/(\S*)\s+attr/(.*?)\s*\t(.*)$", re.DOTALL)

_DECLARED = re.compile(r"\beol=(\w+)")


class Drift(NamedTuple):

    path: str
    declared: str
    on_disk: str

    def describe(self) -> str:
        return f"{self.path} (declared {self.declared}, on disk {self.on_disk})"


def executable_paths(paths: Iterable[str]) -> List[str]:
    seen: dict = {}
    for path in paths:
        if path.lower().endswith(EXECUTABLE_SUFFIXES):
            seen.setdefault(path, None)
    return list(seen)


def find_declared_eol_drift(
    repo_root: Path | str, paths: Sequence[str], *, timeout: Optional[float] = None
) -> List[Drift]:
    candidates = executable_paths(paths)
    if not candidates:
        return []

    result = run_git(
        ["-C", str(repo_root), "ls-files", "--eol", "-z", "--", *candidates],
        timeout=timeout,
        binary=True,
    )
    if result.returncode != 0 or not result.stdout_bytes:
        return []

    try:
        text = result.stdout_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return []

    drifts: List[Drift] = []
    for record in text.split("\0"):
        if not record:
            continue
        match = _RECORD.match(record)
        if match is None:
            continue
        _index_eol, worktree_eol, attrs, path = match.groups()
        declaration = _DECLARED.search(attrs)
        if declaration is None:
            continue
        declared = declaration.group(1)
        if declared not in _REPAIRABLE_DECLARATIONS:
            continue
        if worktree_eol in (declared, "none", "-text", ""):
            continue
        drifts.append(Drift(path=path, declared=declared, on_disk=worktree_eol))
    return drifts


def repair_declared_eol_drift(
    repo_root: Path | str, drifts: Sequence[Drift]
) -> List[str]:
    """Rewrite each drifted file's line endings to its declaration. Returns the
    paths actually repaired, in the order given.

    SAFETY PROPERTY. For a worktree eolinfo this module treats as drift (i.e.
    excluding `"none"`, `"-text"`, and `""`, and excluding any symlinked
    target), check-in normalization maps the drifted and repaired bytes to
    the SAME index blob, so a commit taken across the repair carries
    identical content either way -- see `docs/reference/eol-drift-detection.md`
    for the full argument and K-062's observed data point ("no commit
    resulted: the corrected working copy hashes identically to the index").

    Never raises. A file that cannot be read or written is skipped and omitted
    from the return -- the caller reports the drift it could not fix rather
    than failing a commit over a permission error.
    """
    repaired: List[str] = []
    root = Path(repo_root)
    for drift in drifts:
        want = _BYTES_FOR.get(drift.declared)
        if want is None:
            continue
        target = root / drift.path
        if target.is_symlink():
            continue
        try:
            raw = target.read_bytes()
        except OSError:
            continue
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        rewritten = normalized.replace(b"\n", want)
        if rewritten == raw:
            continue
        try:
            target.write_bytes(rewritten)
        except OSError:
            continue
        repaired.append(drift.path)
    return repaired
