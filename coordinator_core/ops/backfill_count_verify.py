"""
coordinator_core.ops.backfill_count_verify — read-only commit-count verifier.

Re-derives the stated `Commits: N` of every week-changelog day file and changelog
block from its day, using `workday_complete_backfill_scan`'s count of record
(host-local day; union of work/*/*, main and HEAD, local and origin; deduped by
SHA), and reports each mismatch. Reports only: never rewrites a file.

Usage: python -m coordinator_core.ops.backfill_count_verify --repo <root>
           [--file <changelog.md> ...]
Exit: 0 all counts match; 1 mismatches listed; 2 usage/repo error.

Unit checked:
    - block: a `## YYYY-MM-DD — <machine>` section carrying `Commits: N`.
    - day_file: a `state/week-changelog/YYYY-MM-DD.md` with no block header but a
      file-level `Commits: N`, dated by its filename.
Git cost is two spawns (for-each-ref + one log) regardless of file or day count.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.session import record_homes
from coordinator_core.ops.workday_complete_backfill_scan import (
    _collect_union_refs,
    _commit_buckets,
)


_BLOCK_HEADER = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+?)\s*$")
_COMMITS_LINE = re.compile(r"^\W*Commits:\W*\s*(\d+)\b")
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


@dataclass(frozen=True)
class Claim:
    """A stated commit count found in a file."""

    kind: str  # "block" | "day_file"
    path: str
    day: str
    machine: str
    stated: int
    line: int


@dataclass(frozen=True)
class Mismatch:
    """A claim whose stated count differs from the count of record."""

    claim: Claim
    derived: int


def parse_claims(path: Path, text: str) -> List[Claim]:
    """Extract every stated commit count in `text`, attributed to its day."""
    claims: List[Claim] = []
    m = _DAY_FILE.match(path.name)
    file_day = m.group(1) if m else ""
    day = ""
    machine = ""
    in_block = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        hm = _BLOCK_HEADER.match(line)
        if hm:
            day, machine, in_block = hm.group(1), hm.group(2), True
            continue
        if line.startswith("## "):
            day, machine, in_block = "", "", False
            continue
        cm = _COMMITS_LINE.match(line)
        if not cm:
            continue
        if in_block and day:
            claims.append(Claim("block", str(path), day, machine, int(cm.group(1)), lineno))
        elif file_day:
            claims.append(Claim("day_file", str(path), file_day, "", int(cm.group(1)), lineno))
    return claims


def _discover_files(root: Path, extra: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    day_dir = Path(record_homes.home_dir(str(root), "week-changelog"))
    if day_dir.is_dir():
        files.extend(p for p in sorted(day_dir.glob("*.md")) if p.name != "HEADER.md")
    for e in extra:
        p = Path(e)
        if not p.is_absolute():
            p = root / p
        if p.is_file() and p not in files:
            files.append(p)
    return files


def verify(repo_root: str, extra_files: Sequence[str] = ()) -> Tuple[List[Mismatch], int]:
    """Return (mismatches, claims_checked) for the repo's day files and blocks."""
    root = Path(repo_root)
    claims: List[Claim] = []
    for path in _discover_files(root, extra_files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        claims.extend(parse_claims(path, text))
    if not claims:
        return [], 0
    days = sorted({c.day for c in claims})
    refs = _collect_union_refs(str(root))
    buckets: Dict[str, List[Tuple[int, str]]] = _commit_buckets(str(root), refs, days[0], days[-1])
    mismatches = [
        Mismatch(c, len(buckets.get(c.day, [])))
        for c in claims
        if c.stated != len(buckets.get(c.day, []))
    ]
    return mismatches, len(claims)


def format_mismatch(m: Mismatch, root: Optional[Path] = None) -> str:
    c = m.claim
    try:
        shown = str(Path(c.path).relative_to(root)) if root else c.path
    except ValueError:
        shown = c.path
    who = f" {c.machine}" if c.machine else ""
    return f"{c.kind}\t{shown}:{c.line}\t{c.day}{who}\tstated={c.stated}\tderived={m.derived}"


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="backfill_count_verify", description=__doc__.splitlines()[1])
    ap.add_argument("--repo", required=True, help="repo root holding state/week-changelog/")
    ap.add_argument("--file", action="append", default=[], help="extra changelog file to check")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    root = Path(args.repo)
    if not root.is_dir():
        print(f"backfill_count_verify: not a directory: {root}", file=sys.stderr)
        return 2
    mismatches, checked = verify(str(root), args.file)
    for m in mismatches:
        print(format_mismatch(m, root))
    print(f"backfill_count_verify: {checked} counts checked, {len(mismatches)} mismatched", file=sys.stderr)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
