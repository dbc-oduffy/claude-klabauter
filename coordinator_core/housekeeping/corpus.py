"""
coordinator_core.housekeeping.corpus — Step A of the housekeeping pseudocode:
the ONE read of the live corpus.

Cite (BINDING): docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2
Pseudocode, step A — ``live = {path: head_scan(path) for path in
list_live_handoffs(repo)}``. This module is that step: it reads the live
handoff corpus (frontmatter only, ~250 files) exactly once per cycle and
hands the result to every downstream consumer (close, sweep, terminality —
plan chunk C3's own body, "Nothing else re-reads it"). C4's archive index,
C5's resolver, and C6's gate evaluation all consume this module's output;
none of them re-scans ``state/handoffs/``.

Contract 7 (`docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md`,
chunk C3): the live and archived scan roots differ and BOTH matter —

  - live: ``state/handoffs/*.md``, NON-recursive. The sibling
    ``state/handoffs/.archive/`` holds stale local copies and must never be
    descended into.
  - archived: ``archive/handoffs/`` recursively, covering both ``YYYY-MM/``
    subdirectories and root-level records.

This module exposes a scanning primitive for both roots (``list_live_handoffs``,
``list_archived_handoffs``) built on ``os.scandir``/``os.walk`` with error
capture, never ``Path.glob``/``rglob`` — glob silently swallows
``PermissionError``, which collapses "a directory could not be listed" into
"there is nothing here", indistinguishable from a corpus that is genuinely
empty. A scan gap is reported back to the caller as a distinguishable fact
(``scan_gaps``), never folded into an empty/absent result.

Step A itself (``read_live_corpus``) only ever touches the LIVE root — the
archive scan is a sibling primitive here for C4 to build on (its own
revalidation logic is C4's job; this module supplies the correct-roots
walk, not the index).

Negative-spec: this module does not evaluate gates, does not compute the
terminal set, and does not decide what any ``deployment_state`` value
means — it returns raw frontmatter fields per record and nothing more.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple, Union

from coordinator_core.housekeeping.head_scan import scan_keys

#: Frontmatter keys Step A needs to serve close, sweep AND terminality in one
#: pass — deployment_state for sweep/terminality, handoff_id for identity,
#: stub_id because that is the id a gate's blockers actually NAME, and
#: blocked_by because that is the field that actually carries them.
#:
#: There is no `gate_blocker_id`. An earlier revision of this module read
#: one, and it does not exist: 0 of 283 live and 0 of 878 archived records
#: carry that key. The real field is `blocked_by`, a LIST of `stub_id`
#: values (`blocked_by: [sat-06]`), on 15 live and 82 archived records. A
#: cycle reading `gate_blocker_id` clears no gate, ever, while a fixture
#: written from the same wrong spec reports that it does.
#:
#: Cost of the two added keys: `stub_id` is quoted and `blocked_by` is a
#: flow sequence, so both trip contract 8's decline and fall through to a
#: full parse of that one file. That is 15 of 283 records (5%), not 283 —
#: 268 records carry no `blocked_by` at all and stay on the fast path.
LIVE_CORPUS_KEYS: Tuple[str, ...] = (
    "handoff_id",
    "stub_id",
    "deployment_state",
    "blocked_by",
)

#: Leg budget for this step, asserted independently per the plan's budget
#: table. RESTATED from the table's 20 ms with the measurement that refutes
#: it, per that table's own rule ("If a budget turns out to be wrong, restate
#: it out loud with the measurement and take the consequence -- never absorb
#: it into the total quietly").
#:
#: The 20 ms row cited "measured 15.6 ms at 248 files". That number is below
#: this leg's own I/O floor and is not reproducible: reading 4096 bytes from
#: each of 249 files, with no parsing at all, costs ~18 ms on this box. 20 ms
#: therefore left ~2 ms for the head-scan of every record, which the leg
#: cannot do. Both 15.6 ms and an earlier 17.97 ms reading of this leg were
#: first-trial warm-cache artifacts -- in a 12-trial run the first trial
#: reports 19.5 ms and the remaining eleven land at 35.5-40.6 ms.
#:
#: Measured steady state, 12 trials x 5 samples x K=40, median of trial
#: medians: 36.7 ms, max 40.6 ms, min 19.5 ms (that lone first trial). The
#: 2.1x spread is box load -- ~50 concurrent sessions contending for the same
#: disk -- not variance in the code, so the budget is set above the observed
#: max rather than at the median.
#:
#: CONSEQUENCE, recorded rather than absorbed: the plan's budget table sums
#: the legs to 195 ms against a 200 ms cycle criterion with 5 ms slack.
#: Restating this leg from 20 ms to 50 ms puts the nominal sum at 225 ms,
#: over that criterion. C7 measures the assembled cycle and is the binding
#: test; this arithmetic is a projection, not a verdict.
LEG_BUDGET_MS = 50.0

PathLike = Union[str, Path]


def _scan_error_message(context: PathLike, exc: OSError) -> str:
    return f"{context}: {exc}"


def list_live_handoffs(live_dir: PathLike) -> Tuple[List[Path], List[str]]:
    """Non-recursive listing of ``live_dir``'s ``*.md`` files.

    Never descends into a sibling directory (e.g. ``.archive/``) because
    ``os.scandir`` only ever yields ``live_dir``'s direct entries and this
    function filters to files. Returns ``(paths, scan_gaps)`` — a gap is a
    distinguishable OSError (typically PermissionError) encountered while
    listing, never folded into an empty ``paths`` list the way
    ``Path.glob`` would silently produce.
    """
    paths: List[Path] = []
    gaps: List[str] = []
    try:
        it = os.scandir(live_dir)
    except OSError as exc:
        gaps.append(_scan_error_message(live_dir, exc))
        return paths, gaps

    try:
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:
                    gaps.append(_scan_error_message(live_dir, exc))
                    break
                try:
                    if entry.is_file() and entry.name.endswith(".md"):
                        paths.append(Path(entry.path))
                except OSError as exc:
                    gaps.append(_scan_error_message(entry.path, exc))
    except OSError as exc:
        gaps.append(_scan_error_message(live_dir, exc))

    return paths, gaps


def list_archived_handoffs(archive_dir: PathLike) -> Tuple[List[Path], List[str]]:
    """Recursive listing of ``archive_dir``'s ``*.md`` files, covering both
    ``YYYY-MM/``-nested and root-level records (contract 7's archived root).

    Uses ``os.walk`` with an ``onerror`` callback so a directory this
    process cannot list (e.g. PermissionError) is captured as a scan gap in
    the returned list rather than silently narrowing the walk, the way
    ``Path.rglob`` would.
    """
    paths: List[Path] = []
    gaps: List[str] = []

    def _onerror(exc: OSError) -> None:
        gaps.append(_scan_error_message(getattr(exc, "filename", archive_dir), exc))

    for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=_onerror):
        for name in filenames:
            if name.endswith(".md"):
                paths.append(Path(dirpath) / name)

    return paths, gaps


@dataclass
class LiveCorpusResult:
    """Step A's whole output: every live record's scanned frontmatter,
    keyed by path, plus any scan gaps encountered while listing the
    directory and a count of per-record reads performed this call."""

    records: Dict[Path, Dict[str, Any]] = field(default_factory=dict)
    scan_gaps: List[str] = field(default_factory=list)
    read_count: int = 0
    process_time_ms: float = 0.0


def read_live_corpus(
    live_dir: PathLike,
    keys: Iterable[str] = LIVE_CORPUS_KEYS,
    *,
    reader: Callable[[PathLike, Iterable[str]], Dict[str, Any]] = scan_keys,
) -> LiveCorpusResult:
    """Step A: the ONE read of the live corpus. Frontmatter only.

    Reads ``live_dir`` non-recursively (contract 7), then reads each
    record's frontmatter exactly once via ``reader`` (``scan_keys`` by
    default — the declining head-scan, falling through to a full parse of
    that one file only on decline, per contract 8). Nothing in this
    function re-reads a record it has already read, and nothing outside
    this function should re-scan ``live_dir`` for the same cycle — close,
    sweep, and terminality all consume the returned ``records`` dict.

    A directory-listing scan gap is preserved in ``scan_gaps`` and does NOT
    prevent the successfully-listed records from being read and returned —
    a partial listing is still worth reading.
    """
    start = time.process_time()
    paths, gaps = list_live_handoffs(live_dir)

    records: Dict[Path, Dict[str, Any]] = {}
    read_count = 0
    key_tuple = tuple(keys)
    for path in paths:
        records[path] = reader(path, key_tuple)
        read_count += 1

    elapsed_ms = (time.process_time() - start) * 1000.0

    return LiveCorpusResult(
        records=records,
        scan_gaps=gaps,
        read_count=read_count,
        process_time_ms=elapsed_ms,
    )
