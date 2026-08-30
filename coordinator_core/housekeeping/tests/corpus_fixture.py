"""
coordinator_core.housekeeping.tests.corpus_fixture — a synthetic corpus at
the real handoff corpus's shape, on disk.

Purpose: the housekeeping v2 cycle's every per-leg budget (C3-C7 of the plan
below) is meaningless against an empty or toy fixture — "a budget test whose
fixture cannot exercise the expensive path goes green and means nothing,
convincingly, because every number in it is real" (this module's own spec
backlink, verbatim). `build_corpus` produces, on disk under a caller-supplied
root:

  - ~250 live records directly under ``<root>/state/handoffs/*.md``
    (NON-recursive), each carrying a ``deployment_state`` in the real
    distribution (ready_to_fire 204, awaiting_gate 18, in_flight 22,
    shipped 2, closed 1, delivered 1, continued 1 == 249 records).
  - a sibling ``state/handoffs/.archive/`` populated with decoy records that
    a correct NON-recursive live scan must never descend into.
  - ~1,470 archived records under ``<root>/archive/handoffs/``, distributed
    across ``YYYY-MM/`` subdirectories AND directly at the archive root
    (the real corpus has both shapes; a scan that only globs
    ``archive/handoffs/*/*.md`` misses the root-level case).
  - at least 17 ``awaiting_gate`` live records, of which AT LEAST ONE
    (``CorpusFixture.clearing_record_id``) is wired, via a
    ``blocked_by`` frontmatter field (a LIST of stub ids), pointing at a record whose
    ``deployment_state`` is terminal — i.e. a gate that genuinely clears.
    Every OTHER ``awaiting_gate`` record points at a blocker id that does
    not resolve to anything terminal, so "at least one clears" is an exact
    fact about this fixture, never a vacuous one.

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C1 ("The fixture instrument — a corpus that can actually see a gate
  clear").

Negative-spec: this module does NOT implement the production gate-evaluation
logic. `coordinator_core/housekeeping/gate_clear.py` and
`coordinator_core/housekeeping/resolve.py` (plan chunks C5/C6, not yet built)
own that. `gate_clears` below is a minimal, self-contained, fixture-only
convention that exists solely to let this fixture PROVE it can produce a
clearing gate — later chunks are free to choose their own resolver shape and
frontmatter field names; this module's job is the corpus, not the cycle.
This module also does NOT use ``Path.glob``/``rglob`` anywhere a scan is
performed (this plan's own anti-scope: glob silently swallows
``PermissionError`` and is measured 16x slower) — ``os.walk``/``os.scandir``
throughout, matching the production scan roots the fixture exists to feed.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

#: Contract 4 of the predecessor handoff's § Correctness contracts, restated
#: verbatim: terminal deployment states are EXACTLY these four. `continued`
#: IS terminal -- a record with a successor is finished, not retained.
TERMINAL_STATES = ("closed", "abandoned", "continued", "shipped")

#: The real corpus's live deployment_state distribution, per ~249 live
#: records (plan's prime_exit_criterion + the predecessor handoff's "Build
#: the instrument first" section, both cited verbatim).
LIVE_STATE_COUNTS: Dict[str, int] = {
    "ready_to_fire": 204,
    "awaiting_gate": 18,
    "in_flight": 22,
    "shipped": 2,
    "closed": 1,
    "delivered": 1,
    "continued": 1,
}

TOTAL_LIVE = sum(LIVE_STATE_COUNTS.values())  # 249

#: "~1,470 archived records" (plan's prime_exit_criterion). A small root-level
#: slice plus the remainder spread across YYYY-MM/ directories -- both shapes
#: are real in the live corpus (this module's own docstring).
ARCHIVE_TOTAL_TARGET = 1470
ARCHIVE_ROOT_FRACTION = 0.05

#: Records per YYYY-MM/ directory, and therefore how many directories 1,470
#: records occupy. Measured off the live corpus: 875 archived records in two
#: month-directories plus 12 at the archive root, i.e. ~430/month. The month
#: count is NOT a free parameter -- per-directory `os.scandir` open/close is
#: the dominant term in the revalidation leg, so spreading a fixed record
#: count across more directories makes the leg arbitrarily more expensive
#: without modelling anything the corpus does. The archive-index spike
#: verdict's 1.95ms was measured at 1,470 records / 5 directories and
#: reproduces exactly at that shape.
ARCHIVE_RECORDS_PER_MONTH = 430

#: Decoy count under state/handoffs/.archive/ -- non-zero is all the shape
#: requires (the property under test is "never descended into", not scale).
DECOY_COUNT = 5

_BOGUS_BLOCKER_ID = "hnd-fixture-blocker-does-not-exist"

#: 2026-08-30, the actioned-memo class gets an occasion, C3 -- default number
#: of terminal (status: actioned) cross-repo/inbox memos woven into EVERY
#: `build_corpus` call, so the SAME fixture C7's brightline budget test
#: (`test_brightline.py`) already builds every rep exercises the memo
#: family's dirty-check/archival path too. A memo-leg regression (the memo
#: family getting its own git spawn, or the union dirty-check silently
#: excluding memo paths) fails that EXISTING test with zero new test code --
#: C3's own brief, "add only the assertion the existing brightline test
#: cannot already make".
MEMO_TERMINAL_COUNT = 5

#: Filename slug sized so a full relpath (`cross-repo/inbox/<name>`) sits in
#: the ~90-110 char band the plan's own Problem section measures for a real
#: memo filename -- long enough that ~57-65 terminal survivors overflow
#: `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` (6000,
#: `coordinator_core/git/argv_batch.py`). Used by both `build_corpus`'s
#: default memo records and `build_memo_overflow_corpus` below.
_MEMO_SLUG = "a-realistically-long-descriptive-memo-slug-for-argv-budget-sizing"


@dataclass
class CorpusFixture:
    """Everything a consuming test needs to make assertions about the corpus
    `build_corpus` just wrote to disk, without re-scanning it."""

    root: Path
    live_dir: Path
    live_archive_decoy_dir: Path
    archive_dir: Path
    live_records: List[dict] = field(default_factory=list)
    archived_records: List[dict] = field(default_factory=list)
    decoy_records: List[dict] = field(default_factory=list)
    clearing_record_id: str = ""
    clearing_blocker_id: str = ""
    #: 2026-08-30, the actioned-memo class gets an occasion, C3.
    memo_inbox_dir: Path = None  # type: ignore[assignment]
    memo_archive_dir: Path = None  # type: ignore[assignment]
    #: Terminal (status: actioned) memos -- expected to be archived by a
    #: correct `cycle.run` given a cap at least this large.
    memo_records: List[dict] = field(default_factory=list)
    #: Non-terminal (status: open) memo(s) -- a negative control expected to
    #: stay in the inbox untouched, regardless of cap.
    memo_noise_records: List[dict] = field(default_factory=list)

    def records_by_stub_id(self) -> Dict[str, dict]:
        """Indexed by the id a gate's ``blocked_by`` actually names."""
        return {
            rec["stub_id"]: rec
            for rec in (list(self.live_records) + list(self.archived_records))
            if rec.get("stub_id")
        }

    def records_by_handoff_id(self) -> Dict[str, dict]:
        by_id: Dict[str, dict] = {}
        for rec in self.live_records + self.archived_records:
            by_id[rec["handoff_id"]] = rec
        return by_id


def _needs_quoting(value: str) -> bool:
    """The real corpus quotes free-text (`title`, `summary`) and leaves
    identifiers and enums plain -- `status: claimed`, `deployment_state:
    in_flight`, `handoff_id: hnd-...`. Reproduce that split rather than
    quoting uniformly: a quoted value is one of contract 8's six closed
    decline triggers, so quoting every string makes every record decline the
    head-scan and fall through to a full parse, which measures the fallback
    path instead of the mechanism.
    """
    return any(ch in value for ch in ' #:') or not value


def _write_frontmatter(path: Path, fields: Dict[str, object], body: str = "Fixture record body.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and _needs_quoting(value):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def _archive_months(n_months: int) -> List[str]:
    months: List[str] = []
    year, month = 2024, 9
    for _ in range(n_months):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def build_corpus(
    root: Path, *, seed: int = 20260829, memo_terminal_count: int = MEMO_TERMINAL_COUNT
) -> CorpusFixture:
    """Write a real-shaped corpus under ``root`` and return the manifest of
    what was written. Deterministic across runs for a fixed ``seed``.

    Also writes ``memo_terminal_count`` terminal (``status: actioned``)
    memos plus one non-terminal (``status: open``) noise memo under
    ``<root>/cross-repo/inbox/`` -- the memo family C2 folded into
    `housekeeping.cycle` (2026-08-30, the actioned-memo class gets an
    occasion). Woven into every call, not a separate parallel fixture, so
    the SAME build this module's callers already use exercises both
    families.
    """
    rng = random.Random(seed)

    live_dir = root / "state" / "handoffs"
    decoy_dir = live_dir / ".archive"
    archive_dir = root / "archive" / "handoffs"
    live_dir.mkdir(parents=True, exist_ok=True)
    decoy_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    states: List[str] = []
    for state, count in LIVE_STATE_COUNTS.items():
        states.extend([state] * count)
    rng.shuffle(states)

    live_records: List[dict] = []
    awaiting_gate_ids: List[str] = []
    terminal_live_ids: List[str] = []

    for idx, state in enumerate(states, start=1):
        hid = f"hnd-fixture-live-{idx:04d}"
        path = live_dir / f"2026-01-{((idx - 1) % 28) + 1:02d}_{idx:06d}_fixture-live-{idx:04d}.md"
        fields = {
            "title": f"Fixture live record {idx}",
            "created": "2026-01-01",
            "status": "open",
            "handoff_id": hid,
            "deployment_state": state,
            # Quoted in the real corpus (`stub_id: "sat-08"`), so it trips
            # contract 8's decline and falls through to a full parse --
            # faithfully, because that is what the cycle really pays.
            "stub_id": f"fixture-stub-{idx:05d}",
        }
        rec = {
            "path": path,
            "handoff_id": hid,
            "stub_id": f"fixture-stub-{idx:05d}",
            "deployment_state": state,
        }
        live_records.append(rec)
        if state == "awaiting_gate":
            awaiting_gate_ids.append(hid)
        if state in TERMINAL_STATES:
            # Blockers are named by stub_id, never handoff_id.
            terminal_live_ids.append(rec["stub_id"])
        # Deferred write below, once blocked_by (awaiting_gate only) is
        # decided -- keeps this loop the single source of frontmatter fields.
        rec["_fields"] = fields

    if len(awaiting_gate_ids) < 17:
        raise AssertionError(
            f"fixture built only {len(awaiting_gate_ids)} awaiting_gate live records, "
            "need at least 17 -- LIVE_STATE_COUNTS drifted from the plan's own distribution"
        )
    if not terminal_live_ids:
        raise AssertionError(
            "fixture built zero terminal live records -- nothing exists for a gate to clear against"
        )

    clearing_record_id = awaiting_gate_ids[0]
    clearing_blocker_id = terminal_live_ids[0]

    for rec in live_records:
        if rec["deployment_state"] != "awaiting_gate":
            continue
        if rec["handoff_id"] == clearing_record_id:
            rec["_fields"]["blocked_by"] = [clearing_blocker_id]
        else:
            # Every other awaiting_gate record points at a blocker id that
            # resolves to nothing, so "at least one clears" stays an exact
            # fact about this fixture rather than a vacuous one -- if every
            # awaiting_gate record cleared, the assertion below would prove
            # nothing about the discriminating case the cycle must handle.
            rec["_fields"]["blocked_by"] = [_BOGUS_BLOCKER_ID]

    for rec in live_records:
        _write_frontmatter(rec["path"], rec["_fields"])
        del rec["_fields"]

    decoy_records: List[dict] = []
    for i in range(DECOY_COUNT):
        hid = f"hnd-fixture-decoy-{i:03d}"
        path = decoy_dir / f"decoy-{i:03d}.md"
        _write_frontmatter(
            path,
            {
                "title": "Stale local archive decoy -- must never be scanned as live",
                "handoff_id": hid,
                "deployment_state": "shipped",
            },
        )
        decoy_records.append({"path": path, "handoff_id": hid, "deployment_state": "shipped"})

    n_root = max(1, round(ARCHIVE_TOTAL_TARGET * ARCHIVE_ROOT_FRACTION))
    n_nested = ARCHIVE_TOTAL_TARGET - n_root
    months = _archive_months(max(1, -(-n_nested // ARCHIVE_RECORDS_PER_MONTH)))
    per_month, remainder = divmod(n_nested, len(months))

    archived_records: List[dict] = []
    counter = 0

    for mi, month in enumerate(months):
        month_dir = archive_dir / month
        count = per_month + (1 if mi < remainder else 0)
        for _ in range(count):
            counter += 1
            hid = f"hnd-fixture-archived-{counter:05d}"
            path = month_dir / f"{month}_{counter:06d}_fixture-archived-{counter:05d}.md"
            state = rng.choice(TERMINAL_STATES)
            sid = f"fixture-archived-stub-{counter:05d}"
            _write_frontmatter(
                path,
                {
                    "title": f"Archived fixture record {counter}",
                    "handoff_id": hid,
                    # Archived records carry stub_id too. Without it the
                    # archive index is EMPTY -- it keys on stub_id, so a
                    # fixture that omits it builds by_id == {} over ~1,470
                    # files and no archived blocker can ever resolve, while
                    # every count and duration in the suite stays truthful.
                    "stub_id": sid,
                    "deployment_state": state,
                },
            )
            archived_records.append(
                {"path": path, "handoff_id": hid, "stub_id": sid, "deployment_state": state}
            )

    for _ in range(n_root):
        counter += 1
        hid = f"hnd-fixture-archived-{counter:05d}"
        path = archive_dir / f"root_{counter:06d}_fixture-archived-{counter:05d}.md"
        state = rng.choice(TERMINAL_STATES)
        sid = f"fixture-archived-stub-{counter:05d}"
        _write_frontmatter(
            path,
            {
                "title": f"Archived fixture record {counter} (archive root)",
                "handoff_id": hid,
                "stub_id": sid,
                "deployment_state": state,
            },
        )
        archived_records.append(
            {"path": path, "handoff_id": hid, "stub_id": sid, "deployment_state": state}
        )

    # -- Memo family (2026-08-30, the actioned-memo class gets an occasion). --
    memo_inbox_dir = root / "cross-repo" / "inbox"
    memo_archive_dir = root / "cross-repo" / "archive"
    memo_inbox_dir.mkdir(parents=True, exist_ok=True)
    memo_archive_dir.mkdir(parents=True, exist_ok=True)

    memo_records: List[dict] = []
    for i in range(memo_terminal_count):
        day = (i % 28) + 1
        name = f"2026-01-{day:02d}-fixture-memo-{i:04d}-{_MEMO_SLUG}.md"
        path = memo_inbox_dir / name
        _write_frontmatter(
            path,
            {
                "title": f"Fixture actioned memo {i}",
                "created": f"2026-01-{day:02d}",
                "status": "actioned",
                "action_taken_at": f"2026-01-{day:02d}T00:00:00Z",
            },
        )
        memo_records.append({"path": path, "rel_name": name, "status": "actioned"})

    memo_noise_records: List[dict] = []
    noise_name = "2026-01-01-fixture-memo-noise-still-open.md"
    noise_path = memo_inbox_dir / noise_name
    _write_frontmatter(
        noise_path,
        {
            "title": "Fixture memo noise -- not terminal, must be retained",
            "created": "2026-01-01",
            "status": "open",
        },
    )
    memo_noise_records.append({"path": noise_path, "rel_name": noise_name, "status": "open"})

    return CorpusFixture(
        root=root,
        live_dir=live_dir,
        live_archive_decoy_dir=decoy_dir,
        archive_dir=archive_dir,
        live_records=live_records,
        archived_records=archived_records,
        decoy_records=decoy_records,
        clearing_record_id=clearing_record_id,
        clearing_blocker_id=clearing_blocker_id,
        memo_inbox_dir=memo_inbox_dir,
        memo_archive_dir=memo_archive_dir,
        memo_records=memo_records,
        memo_noise_records=memo_noise_records,
    )


def build_memo_overflow_corpus(inbox_dir: Path, *, count: int = 80) -> List[dict]:
    """Write ``count`` realistic-length-named terminal (``status: actioned``)
    memos directly under ``inbox_dir`` (``cross-repo/inbox/``).

    C3's own OVERFLOW FIXTURE contract (staff-eng Finding 3, major): each
    relpath sits in the ~90-110+ char band a real memo filename measures at,
    so the surviving candidate set overflows
    `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` (6000,
    `coordinator_core/git/argv_batch.py`) well before ``count`` is reached --
    the branch C2's generalised ``fallback_pathspecs`` fix changes behaviour
    on. A separate helper from `build_corpus` (not folded into its default
    count) because the brightline budget test's own N_OUTER=3 reps must stay
    cheap; only the dedicated overflow test pays for this corpus.

    Returns ``[{"path": Path, "rel_name": str}, ...]``, oldest-first by
    filename, so a caller can single out one entry to dirty afterward.
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)
    records: List[dict] = []
    for i in range(count):
        day = (i % 28) + 1
        name = f"2026-02-{day:02d}-overflow-fixture-memo-{i:04d}-{_MEMO_SLUG}.md"
        path = inbox_dir / name
        _write_frontmatter(
            path,
            {
                "title": f"Overflow fixture actioned memo {i}",
                "created": f"2026-02-{day:02d}",
                "status": "actioned",
                "action_taken_at": f"2026-02-{day:02d}T00:00:00Z",
            },
        )
        records.append({"path": path, "rel_name": name})
    return records


def _read_blocked_by(path: Path) -> List[str]:
    """`blocked_by` as it is actually written: a flow sequence of stub ids."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("blocked_by:"):
            raw = line.split(":", 1)[1].strip()
            if not raw.startswith("[") or not raw.endswith("]"):
                return []
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [part.strip().strip('"').strip("'") for part in inner.split(",")]
    return []


def gate_clears(fixture: CorpusFixture, record_handoff_id: str) -> bool:
    """Fixture-only gate-clearing convention (see module docstring's
    negative-spec): ``True`` iff ``record_handoff_id`` names an
    ``awaiting_gate`` record ALL of whose ``blocked_by`` stub ids (re-read
    from disk, mirroring contract 1's "truth from disk at mutation time")
    resolve, among this fixture's own live+archived records, to records
    whose ``deployment_state`` is terminal (contract 4's four-state set).

    All-or-nothing, matching `gate_clear.evaluate_gate_clear`: one
    unresolved or non-terminal blocker holds the gate shut."""
    by_stub = fixture.records_by_stub_id()
    record = fixture.records_by_handoff_id().get(record_handoff_id)
    if record is None or record["deployment_state"] != "awaiting_gate":
        return False
    blocker_ids = _read_blocked_by(record["path"])
    if not blocker_ids:
        return False
    for blocker_id in blocker_ids:
        blocker = by_stub.get(blocker_id)
        if blocker is None or blocker["deployment_state"] not in TERMINAL_STATES:
            return False
    return True


def scan_live_non_recursive(live_dir: Path) -> List[Path]:
    """Contract 7: live handoffs are ``state/handoffs/*.md``, NON-recursive
    -- the sibling ``.archive/`` must never be descended into. Uses
    ``os.scandir`` (never ``Path.glob``/``rglob``, per this plan's own
    anti-scope) so a future caller inherits the same PermissionError-visible
    convention this fixture is built to exercise."""
    out: List[Path] = []
    with os.scandir(live_dir) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith(".md"):
                out.append(Path(entry.path))
    return out


def scan_archive_recursive(archive_dir: Path) -> List[Path]:
    """Contract 7's archive counterpart: recursive, covering both
    ``YYYY-MM/`` nested records and root-level records. ``os.walk``, never
    ``Path.glob``/``rglob``."""
    out: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(archive_dir):
        for name in filenames:
            if name.endswith(".md"):
                out.append(Path(dirpath) / name)
    return out
