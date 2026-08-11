"""The machine-readable carve-out register: sanctioned shell-shaped spawns.

Parses the single fenced ```yaml shell-out-allowlist``` block out of
`docs/reference/shell-out-carve-outs.md`. That doc is the register — this
module reads it, never mints a second parallel list.

Membership is enumerative, not inferred: a site that merely satisfies a
carve-out class's rationale, without being named by an exact structural
match, is NOT sanctioned. See `is_sanctioned`.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from collections.abc import Sequence

import yaml

from .detect import SpawnSite, site_key

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CARVE_OUTS_DOC = _REPO_ROOT / "docs" / "reference" / "shell-out-carve-outs.md"

_BLOCK_RE = re.compile(
    r"```yaml shell-out-allowlist\n(.*?)\n```", re.DOTALL
)


@dataclasses.dataclass(frozen=True)
class AllowlistEntry:
    cls: str
    path: str
    enclosing: str
    argv0: str
    ordinal: int
    argv_digest: str | None
    reason: str
    ruled_on: str


def load_allowlist(doc: pathlib.Path = DEFAULT_CARVE_OUTS_DOC) -> list[AllowlistEntry]:
    """Parse the single fenced ```yaml shell-out-allowlist``` block out of the register doc.

    Zero or two-or-more such blocks is an error, not a fallback.
    """
    text = pathlib.Path(doc).read_text(encoding="utf-8")
    matches = _BLOCK_RE.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"{doc}: expected exactly one 'yaml shell-out-allowlist' block, "
            f"found {len(matches)}"
        )

    raw = yaml.safe_load(matches[0]) or []
    entries: list[AllowlistEntry] = []
    for item in raw:
        entries.append(
            AllowlistEntry(
                cls=str(item["cls"]),
                path=str(item["path"]),
                enclosing=str(item["enclosing"]),
                argv0=str(item["argv0"]),
                ordinal=int(item["ordinal"]),
                argv_digest=(
                    None if item.get("argv_digest") is None else str(item["argv_digest"])
                ),
                reason=str(item["reason"]),
                ruled_on=str(item["ruled_on"]),
            )
        )
    return entries


def is_sanctioned(site: SpawnSite, entries: Sequence[AllowlistEntry]) -> bool:
    """True only on an EXACT structural match: site_key() equal AND argv_digest equal.

    An ordinal match with a changed argv_digest is a HARD MISMATCH (returns False) —
    a different call inserted before a sanctioned one must never inherit its slot.
    Membership is enumerative: satisfying a class's rationale without being named
    in the block is NOT sanctioned.

    UNPINNED entries (argv_digest is None) match on site_key() alone. This is the
    bootstrap state only — see `unpinned_entries`.
    """
    site_id = site_key(site)
    for entry in entries:
        if site_key(entry) != site_id:
            continue
        if entry.argv_digest is None:
            return True
        return entry.argv_digest == site.argv_digest
    return False


def unpinned_entries(entries: Sequence[AllowlistEntry]) -> list[AllowlistEntry]:
    """Entries whose argv_digest is None. The gate and the census both surface these."""
    return [entry for entry in entries if entry.argv_digest is None]
