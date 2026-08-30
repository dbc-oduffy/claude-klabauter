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

Measured cost of the spawning leg: ~25ms wall, 1 process, k=6, on a commit
carrying three `.cmd` paths (2026-08-30). That is process CREATION, not query
work -- it matches `CLAUDE.md`'s own `git --version` 25.3ms floor almost
exactly, so the count of paths in the batch is nearly free and the count of
SPAWNS is the whole budget. Against the 200ms per-commit bar this leaves ~8x
headroom, and against the 500ms brightline ~20x.

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

#: The classes this module has an opinion about. Deliberately NOT every text
#: file: K-019's own labour census found 0 violations across 593 executable
#: files and all 43 violations on DATA files (`.diff`/`.patch`/`.sha`) under
#: scratch dirs (`state/review-slices/`, `state/subagent-share/`). A scratch
#: `.diff` with the wrong line ending harms nothing; a `.cmd` with the wrong
#: one does not run. Widening this tuple re-adopts the noise the census
#: already measured and the baton's anti-scope names by hand.
EXECUTABLE_SUFFIXES = (".cmd", ".ps1", ".sh", ".bat")

#: The two endings a declaration can name that this module can verify and
#: repair. `git ls-files --eol` also reports `none` (a file with no line
#: terminator at all) and `mixed`; neither is a DECLARATION, they are
#: observations, and neither appears on the left of this membership test.
_REPAIRABLE_DECLARATIONS = ("lf", "crlf")

_BYTES_FOR = {"lf": b"\n", "crlf": b"\r\n"}

#: `i/<index-eol> w/<worktree-eol> attr/<attrs><TAB><path>`, per `git ls-files
#: --eol`. The attribute field is space-padded and itself CONTAINS spaces
#: (`text eol=crlf`), which is why the path is taken off the tab rather than
#: off a field count. Under `-z` git NUL-terminates records and does NOT
#: C-quote unusual paths, so `.*` is safe for the tail.
_RECORD = re.compile(r"^i/(\S+)\s+w/(\S+)\s+attr/(.*?)\s*\t(.*)$", re.DOTALL)

_DECLARED = re.compile(r"\beol=(\w+)")


class Drift(NamedTuple):
    """One executable whose on-disk line ending contradicts its declaration.

    `declared` is what `.gitattributes` says the file must be; `on_disk` is
    what `git ls-files --eol` reports the working tree actually holds. They
    are never equal in an instance of this type -- construction is gated on
    the mismatch.
    """

    path: str
    declared: str
    on_disk: str

    def describe(self) -> str:
        """One-line operator-facing rendering, in the terse register
        `docs/wiki/guard-messaging.md` § Register asks for: the fact, once.
        """
        return f"{self.path} (declared {self.declared}, on disk {self.on_disk})"


def executable_paths(paths: Iterable[str]) -> List[str]:
    """The subset of `paths` this module has an opinion about, de-duplicated
    and order-preserving.

    The whole budget case rests on this function: it spawns nothing, opens
    nothing, and returns `[]` for the overwhelming majority of commits, which
    lets every caller below skip its git call entirely. Suffix matching is
    case-insensitive because Windows path casing is not stable across the
    producers that write these files.
    """
    seen: dict = {}
    for path in paths:
        if path.lower().endswith(EXECUTABLE_SUFFIXES):
            seen.setdefault(path, None)
    return list(seen)


def find_declared_eol_drift(
    repo_root: Path | str, paths: Sequence[str], *, timeout: Optional[float] = None
) -> List[Drift]:
    """Executables among `paths` whose working-tree bytes contradict their
    declared `eol=`. One git spawn, or zero when `paths` carries no executable.

    Never raises and never blocks a caller: a non-zero git exit, an
    unparseable record, or a path git does not track folds to "no drift
    found". This is a detector on a commit path, and a detector that can fail
    a commit is a worse defect than the drift it looks for.

    A path with no `eol=` declaration is not a finding -- there is nothing for
    the bytes to contradict. Nor is a declaration of anything but `lf`/`crlf`,
    nor a working tree git reports as `none` (no line terminator present) or
    as already matching.
    """
    candidates = executable_paths(paths)
    if not candidates:
        return []

    result = run_git(
        ["-C", str(repo_root), "ls-files", "--eol", "-z", "--", *candidates],
        timeout=timeout,
        binary=True,
    )
    # `stdout_bytes`, NOT `stdout`: under `binary=True` the decoded `stdout`
    # view is empty by construction, and a `-z` reader needs the undecoded
    # stream anyway (see `GitResult`'s own field docs). Reading the wrong one
    # here folds every commit to "no drift found" and the detector reports
    # clean forever -- exactly the silent-pass failure this module exists to
    # end, so it is pinned by `test_reads_stdout_bytes_not_stdout`.
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
        # The index leg is deliberately unread: check-in normalization means
        # `i/` says what the blob holds, and the blob is never what breaks a
        # launcher. `w/` is the only field that describes the bytes cmd.exe
        # will actually be handed.
        _index_eol, worktree_eol, attrs, path = match.groups()
        declaration = _DECLARED.search(attrs)
        if declaration is None:
            continue
        declared = declaration.group(1)
        if declared not in _REPAIRABLE_DECLARATIONS:
            continue
        if worktree_eol in (declared, "none"):
            continue
        drifts.append(Drift(path=path, declared=declared, on_disk=worktree_eol))
    return drifts


def repair_declared_eol_drift(
    repo_root: Path | str, drifts: Sequence[Drift]
) -> List[str]:
    """Rewrite each drifted file's line endings to its declaration. Returns the
    paths actually repaired, in the order given.

    WHY REPAIRING IS SAFE HERE, AND WHY IT IS NOT A CONTENT CHANGE. The bytes
    written are exactly the bytes git's own checkout filter would produce for
    that declaration, and check-in normalization maps both the before and the
    after to the SAME index blob -- so a commit taken across this repair
    carries identical content either way. K-062 fixed its live `.cmd` this
    way by hand and observed precisely that: "no commit resulted: the
    corrected working copy hashes identically to the index". The repair
    restores the state the next checkout would establish anyway; it does not
    invent one.

    WHY REPAIR RATHER THAN WARN. A warning is the "declaration nobody reads"
    problem this whole requirement opens with, one level up -- and the
    north star (`CLAUDE.md`) asks for the artifact that discharges a rule, not
    for the operator to remember. The caller still reports what was repaired;
    seeing it is not what makes it correct.

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
        try:
            raw = target.read_bytes()
        except OSError:
            continue
        # Collapse every existing terminator to LF first, then expand once to
        # the declared ending. Going straight to CRLF would double the \r on
        # any line already correct, which is the classic in-place-rewrite bug
        # this ordering exists to make unrepresentable.
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
