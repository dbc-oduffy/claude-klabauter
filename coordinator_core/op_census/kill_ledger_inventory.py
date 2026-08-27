"""Mechanical derivation of the kill-ledger's population — the artifact that
discharges *"did we get them all"* for a revival roadmap.

`state/kill-ledger.md` is prose with a stable heading shape (`## K-NNN — <what>`)
and a stable field vocabulary (`**Status:**`, `**Date`, `**Cost`, `**Breaks`,
`**What would have to be true...**`). A roadmap built by hand-transcribing that
file inherits whatever the transcriber's eye skipped. This module parses every
`## K-` heading, joins each entry against three live sources of truth — the op
registry (`live_registry_op_names`), `op_budget_suspension.SUSPENDED_OPS`, and
on-disk module presence — and refuses to emit an inventory in which any entry is
left UNCLASSIFIED.

Negative spec — what this module does NOT do:

- It does not decide dispositions. Verdicts (KEEP/MERGE/DEFER/DROP/MOVE) are a
  roadmap-planning judgment written into `reconciliation.md` by hand; this module
  supplies the population those verdicts must balance against, and nothing else.
- It does not measure anything. Every cost figure it surfaces is quoted from the
  ledger entry that recorded it, wall clock and all. Producing process-time
  baselines is `occupancy_scan.py`'s job, not this module's.
- It is not an op and must never be registered as one. It reads the whole ledger
  and imports both op packages eagerly; that is a dev-time cost, deliberately
  outside the 500ms brightline, which governs dispatched ops.
- It does not write to `state/`. Callers redirect stdout to wherever the
  inventory belongs, so a regeneration is diffable rather than silent.

Population vocabulary, derived not asserted:

- ``LANDED``     — a cut that reached disk. Status names a landing/removal and
                   the named op (if any) is absent from the live registry.
- ``CANDIDATE``  — nominated, never convicted. Status carries ``CANDIDATE`` and
                   the named op is still live.
- ``CONVICTED``  — convicted, cut not yet landed. The op is expected to still be
                   live; that is the state the entry describes, not a
                   disagreement with it. An op already gone while its entry
                   still reads CONVICTED is NOT quietly promoted to LANDED —
                   that would infer a population from liveness. It is
                   CONTESTED, like every other unstated fact.
- ``NON_CUT``    — proposed-and-blocked, closed-superseded, acquitted, or a
                   relocation.
- ``CUT_ELSEWHERE`` — the cut landed in another plane (the ledger entry says so
                   in as many words) while this repo's own registration survives
                   by design. Liveness here is the stated outcome, so it is not
                   contested; the note carries the cross-plane fact forward.
- ``REBUILT``    — the cut landed, and the requirement was later answered by a
                   from-scratch rebuild the entry records. The op is live again
                   on purpose. The entry's cost figures belong to the dead
                   predecessor and must not be quoted against the rebuild.
- ``CONTESTED``  — the ledger's status and the live registry disagree. Never
                   silently resolved: a CONTESTED row is a defect report about
                   the ledger, surfaced by name.

A status vocabulary the rules cannot place is itself a defect — but it is the
*classifier's*, not the ledger's, and it drowns the real disagreements in noise.
Every disposition the ledger actually writes is placed by a named rule above.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
KILL_LEDGER = REPO_ROOT / "state" / "kill-ledger.md"

_ENTRY_SPLIT = re.compile(r"^## (?=K-\d)", re.M)
_HEADING = re.compile(r"^K-(\d+)\s*[—-]\s*(.*)$")

#: An op name in a heading or a `**What:**` line is written as `` `dotted.name` ``.
#: Anchored to the ledger's own convention: lowercase, dot-separated, no spaces.
_OP_NAME = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`")

#: Registered ops whose names are a single bare word, no dot — `ping` is the only
#: one in the ledger, and it would be invisible to `_OP_NAME` above.
_BARE_OP_NAME = re.compile(r"^`([a-z][a-z0-9_]*)`$")

_LANDED_MARKERS = (
    "landed",
    "removed",
    "deleted",
    "killed",
    "relocated",
    # A gravestone is a cut that landed with nothing replacing it — the
    # requirement retired, not deferred. It reads as LANDED here because that is
    # what it is on the registry: the op is gone and stays gone. Absent from this
    # tuple the status matched no rule at all and K-059 fell to CONTESTED, which
    # is this module's own stated defect ("a status vocabulary the rules cannot
    # place is the classifier's, not the ledger's") reported against the ledger.
    "gravestone",
)
_NON_CUT_MARKERS = (
    "not yet cut",
    "not executed",
    "closed",
    "acquitted",
    "relocated",
)
#: A cut executed in a sibling plane, with this repo's registration surviving on
#: purpose. Quoted from K-006, the only entry that states it — kept a literal
#: phrase rather than a heuristic so a new cross-plane entry has to say so too.
_CROSS_PLANE_MARKERS = ("not by this repo",)
#: A cut that landed and was later answered by a from-scratch rebuild. Literal,
#: for the same reason as `_CROSS_PLANE_MARKERS`: an entry earns this population
#: by recording the rebuild, never by the classifier inferring one from liveness.
_REBUILT_MARKERS = ("then rebuilt",)


@dataclass
class LedgerEntry:
    """One `## K-NNN` section, parsed. Field text is quoted from the ledger."""

    number: int
    key: str
    title: str
    status_text: str
    op_name: Optional[str]
    cost_text: str
    breaks_text: str
    returns_when_text: str
    body_chars: int
    population: str = "UNCLASSIFIED"
    op_live: Optional[bool] = None
    op_suspended: bool = False
    notes: List[str] = field(default_factory=list)


def _field(body: str, *labels: str, limit: int = 400) -> str:
    """First matching `**Label...**` run in `body`, whitespace-collapsed."""
    for label in labels:
        match = re.search(r"\*\*" + re.escape(label) + r"[^*]*\*\*[:.]?\s*(.+?)(?=\n\n|\Z)", body, re.S)
        if match:
            return " ".join(match.group(1).split())[:limit]
    return ""


def _op_name_for(title: str, body: str) -> Optional[str]:
    """The op a K-entry is about, or None for entries that name a function or a
    mechanism rather than a registered op."""
    def _reject(name: Optional[str]) -> Optional[str]:
        """A dotted token ending in a file extension is a path, not an op —
        `coverage.py` reads as `coverage`.`py` to any dotted-name regex."""
        if name and name.rsplit(".", 1)[-1] in {"py", "md", "json", "yaml", "cmd", "ps1", "sh"}:
            return None
        return name

    bare = _BARE_OP_NAME.match(title.strip())
    if bare:
        return bare.group(1)
    heading_hit = _OP_NAME.search(title)
    if heading_hit:
        return _reject(heading_hit.group(1))
    what = _field(body, "What is removed", "What", limit=200)
    registered = re.search(r"`([a-z][a-z0-9_.]*)`,\s*registered at", body)
    if registered:
        return _reject(registered.group(1))
    what_hit = _OP_NAME.search(what)
    return _reject(what_hit.group(1)) if what_hit else None


def parse_ledger(text: str) -> List[LedgerEntry]:
    entries: List[LedgerEntry] = []
    for section in _ENTRY_SPLIT.split(text)[1:]:
        heading, _, body = section.partition("\n")
        matched = _HEADING.match(heading.strip())
        if not matched:  # pragma: no cover - the split guarantees the shape
            raise ValueError(f"unparseable kill-ledger heading: {heading!r}")
        number = int(matched.group(1))
        entries.append(
            LedgerEntry(
                number=number,
                key=f"K-{matched.group(1)}",
                title=matched.group(2).strip(),
                status_text=_field(body, "Status", limit=260),
                op_name=_op_name_for(matched.group(2), body),
                cost_text=_field(body, "Cost", "Measured cost", limit=260),
                breaks_text=_field(body, "What breaks", "Breaks", limit=260),
                returns_when_text=_field(
                    body,
                    "Returns when",
                    "Comes back when",
                    "What would have to be true",
                    limit=260,
                ),
                body_chars=len(body),
            )
        )
    return entries


def _live_op_names() -> frozenset:
    from coordinator_core.op_census.occupancy_scan import live_registry_op_names

    return live_registry_op_names()


def _suspended_op_names() -> frozenset:
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS

    return frozenset(SUSPENDED_OPS)


def classify(
    entries: Sequence[LedgerEntry],
    *,
    live_ops: frozenset,
    suspended_ops: frozenset,
) -> None:
    """Assign every entry a population, in place. Never leaves UNCLASSIFIED —
    an entry the rules cannot place becomes CONTESTED with a stated reason."""
    for entry in entries:
        status = entry.status_text.lower()
        if entry.op_name is not None:
            entry.op_live = entry.op_name in live_ops
            entry.op_suspended = entry.op_name in suspended_ops

        is_candidate = "candidate" in status and "not yet convicted" in status
        is_landed = any(marker in status[:40] for marker in _LANDED_MARKERS)
        # Windowed like `is_landed`: every CANDIDATE status says "NOT YET
        # CONVICTED" further along, and `is_candidate` claims those first.
        is_convicted = "convicted" in status[:40] and not is_candidate
        # Windowed like `is_landed`/`is_non_cut` above, and for the same
        # reason (slice-b Finding 2): a status field runs to 260 chars, and a
        # phrase appearing late in one is prose ABOUT some other entry, not
        # this entry's own disposition. An unwindowed match would let a passing
        # reference to another op's rebuild earn this entry the population.
        is_cross_plane = any(marker in status[:100] for marker in _CROSS_PLANE_MARKERS)
        is_rebuilt = any(marker in status[:100] for marker in _REBUILT_MARKERS)
        # Windowed to the status line's opening, like `is_landed` above: a
        # LANDED entry's authority prose says things like "closed out by C1g",
        # and an unwindowed substring test reads that as a CLOSED status.
        is_non_cut = any(marker in status[:80] for marker in _NON_CUT_MARKERS)

        if is_candidate:
            entry.population = "CANDIDATE"
            if entry.op_live is False:
                entry.population = "CONTESTED"
                entry.notes.append(
                    f"status says CANDIDATE but `{entry.op_name}` is absent from the live registry"
                )
        elif is_landed and is_rebuilt:
            entry.population = "REBUILT"
            if entry.op_live is False:
                entry.population = "CONTESTED"
                entry.notes.append(
                    f"status records a rebuild but `{entry.op_name}` is absent from the live registry"
                )
        elif is_landed and is_cross_plane:
            entry.population = "CUT_ELSEWHERE"
            entry.notes.append(
                "cut landed in another plane; this repo's registration survives by design"
            )
        elif is_landed and not is_non_cut:
            entry.population = "LANDED"
            if entry.op_live is True and not entry.op_suspended:
                entry.population = "CONTESTED"
                entry.notes.append(
                    f"status says landed/removed but `{entry.op_name}` is still registered"
                )
        elif is_convicted:
            entry.population = "CONVICTED"
            if entry.op_live is False:
                # Review (slice-b Finding 1): promoting this to LANDED because
                # the op went missing is inferring a population from liveness --
                # the exact move CONVICTED/CUT_ELSEWHERE/REBUILT exist to
                # forbid, made two branches from where it is forbidden. A cut
                # that landed is a fact the ledger states; an op that quietly
                # vanished from the registry while its entry still reads
                # CONVICTED is a disagreement, and disagreements are surfaced.
                entry.population = "CONTESTED"
                entry.notes.append(
                    f"status says CONVICTED but `{entry.op_name}` is already absent from the "
                    "live registry -- record the landing in the entry"
                )
        elif is_non_cut or "not yet cut" in entry.title.lower() or "candidate" in entry.title.lower():
            entry.population = "NON_CUT"
        elif is_landed:
            entry.population = "LANDED"
        else:
            entry.population = "CONTESTED"
            entry.notes.append("status line matched no population rule")

        if entry.op_suspended:
            entry.notes.append("still listed in SUSPENDED_OPS — refusing at dispatch")


def _counts(entries: Sequence[LedgerEntry]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry.population] = counts.get(entry.population, 0) + 1
    return counts


def render(entries: Sequence[LedgerEntry], *, heading_count: int) -> str:
    counts = _counts(entries)
    lines: List[str] = []
    lines.append("<!-- GENERATED by coordinator_core/op_census/kill_ledger_inventory.py — do not hand-edit -->")
    lines.append("")
    lines.append("# Kill-ledger inventory — mechanically derived")
    lines.append("")
    lines.append(
        f"`## K-` headings found: **{heading_count}** · entries parsed: **{len(entries)}** · "
        "unclassified: **0** by construction (an unplaceable entry is CONTESTED, not omitted)."
    )
    lines.append("")
    lines.append(
        "Populations: "
        + " · ".join(f"**{name}** {count}" for name, count in sorted(counts.items()))
    )
    lines.append("")
    contested = [e for e in entries if e.population == "CONTESTED"]
    if contested:
        lines.append("## CONTESTED — the ledger and the live registry disagree")
        lines.append("")
        for entry in contested:
            lines.append(f"- **{entry.key}** — {entry.title} — " + "; ".join(entry.notes))
        lines.append("")
    else:
        lines.append("No CONTESTED rows: every entry's status agrees with the live registry.")
        lines.append("")

    for population in (
        "LANDED",
        "CANDIDATE",
        "CONVICTED",
        "REBUILT",
        "NON_CUT",
        "CUT_ELSEWHERE",
        "CONTESTED",
    ):
        rows = [e for e in entries if e.population == population]
        if not rows:
            continue
        lines.append(f"## {population} ({len(rows)})")
        lines.append("")
        lines.append("| K | what | op | live? | suspended | cost (as recorded) | breaks | returns-when |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for entry in sorted(rows, key=lambda e: e.number):
            live = "—" if entry.op_live is None else ("yes" if entry.op_live else "no")
            lines.append(
                "| {key} | {title} | {op} | {live} | {susp} | {cost} | {breaks} | {returns} |".format(
                    key=entry.key,
                    title=_cell(entry.title),
                    op=f"`{entry.op_name}`" if entry.op_name else "—",
                    live=live,
                    susp="yes" if entry.op_suspended else "—",
                    cost=_cell(entry.cost_text, 160),
                    breaks=_cell(entry.breaks_text, 160),
                    returns=_cell(entry.returns_when_text, 160),
                )
            )
        lines.append("")
    return "\n".join(lines)


def _cell(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split()).replace("|", "\\|")
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed or "—"


def build(ledger_path: Path = KILL_LEDGER) -> Tuple[List[LedgerEntry], int]:
    text = ledger_path.read_text(encoding="utf-8")
    heading_count = len(re.findall(r"^## K-\d", text, re.M))
    entries = parse_ledger(text)
    if len(entries) != heading_count:
        raise AssertionError(
            f"parsed {len(entries)} entries from {heading_count} `## K-` headings — "
            "the parser dropped a section"
        )
    classify(entries, live_ops=_live_op_names(), suspended_ops=_suspended_op_names())
    return entries, heading_count


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=KILL_LEDGER)
    parser.add_argument(
        "--fail-on-contested",
        action="store_true",
        help="exit 1 if any entry's status disagrees with the live registry",
    )
    args = parser.parse_args(argv)

    entries, heading_count = build(args.ledger)
    sys.stdout.write(render(entries, heading_count=heading_count) + "\n")

    contested = [e for e in entries if e.population == "CONTESTED"]
    if contested and args.fail_on_contested:
        for entry in contested:
            sys.stderr.write(f"CONTESTED {entry.key}: {'; '.join(entry.notes)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
