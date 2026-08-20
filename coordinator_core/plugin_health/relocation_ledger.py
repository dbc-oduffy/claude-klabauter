"""
coordinator_core.plugin_health.relocation_ledger -- durable, machine-readable
record of executables that changed OWNER REPO across a re-homing, queryable
by any outside consumer without needing to reconstruct history from
cross-repo archives.

Purpose: a re-homing that changes owner repo AND runtime AND extension all at
once (the `b644d5a9` DoE-claude -> claude-klabauter executable-surface absorption) is
indistinguishable from a deletion to a consumer probing the old path. The
2026-07-26 cockpit incident (spec backlink below) shows the cost: cockpit's
own drift gate probed a dead `../coordinator-claude/bin/...` path,
reconstructed history from DoE's cross-repo archive, concluded the validator
had been destroyed, and carried a 13-day-open durability ask plus a reviewed
plan's `## External blocker` section around that wrong premise -- while the
validator was working the whole time, one path resolution away.

This module is NOT a fix for "operator remembers to leave a forwarding note"
(the ask's own suggested fixes -- a stub, a commit-message line, a memo to
known consumers -- are each exactly that, and are deliberately NOT adopted
here; see `docs/install/CLAUDE.md` north-star discharge test). It is the
mechanism: a durable ledger entry is the resolvable trace, and
`check_relocation_ledger_integrity` is what stops that ledger decaying into
an unenforced convention -- an entry whose `new_path` has itself gone stale
fails a test, rather than silently lying to the next outside prober who does
bother to consult it.

Sibling of `coordinator_core.plugin_health.fleet_reachability`, not an
extension of it. That module answers a narrower question -- "does every
`bin/<name>` DoE-claude's OWN skills/commands/hooks/pipelines cite still have
a surviving claude-klabauter oracle by normalized stem" -- via a live regex sweep of
one specific consumer's tree. It has no way to help a *different* sibling
repo (cockpit) that hardcodes a path into its own gate, because that repo's
citation is never swept by anything living in claude-klabauter. This module answers
the question fleet_reachability structurally cannot: "does claude-klabauter itself
publish a queryable record of executables that moved, so ANY outside
prober -- not just DoE-claude's own fenced citations -- can resolve a stale
path mechanically instead of guessing." The two modules solve adjacent but
distinct problems and neither subsumes the other.

Data file: `docs/install/relocation-ledger.json` (co-located with
`docs/install/agent-install-manifest.json`, the install-chain contract
external consumers already know to look at). Plain JSON, not a Python
module -- an outside consumer that does not import `coordinator_core` can
still `jq` or `json.load` the file directly; the loader/query/integrity API
in this module is a convenience for a Python-side consumer (or claude-klabauter's own
tests), not the load-bearing contract itself.

REPO ROOT RESOLUTION: `new_repo` in a ledger entry is a
`coordinator_core.machine_resolver.registry_get('repos.<new_repo>')`-
resolvable id (see the ledger's own `_entry_shape.new_repo` doc). Integrity
checking reuses `coordinator_core.sibling_fact.resolve_leg` (kind
`file_exists`) for that resolution -- never a fourth from-scratch sibling
resolver (per that module's own docstring, and per this repo's dispatch
brief which named `sibling_fact` explicitly as the primitive to consume).
`claude_klabauter` (claude-klabauter's own registry id) resolves through the exact same
call; there is no special-cased "self" branch, because `sibling_fact`
already treats any registered repo id uniformly, self included.

DISPOSITION (added to close the "moved-vs-gone" gap named in the same
Finding 2 follow-up): every entry carries a `disposition` of either
`"moved"` (the original, still-only-historically-required shape -- an
entry with no `disposition` key at all loads as `"moved"` for backward
compatibility with the one entry landed before this field existed) or
`"retired"` -- an artifact deliberately removed with NO relocation, e.g.
this repo's own carve-out-class-(c) `verify-<X>-sync.sh` scripts (see
`docs/install/CLAUDE.md`'s de-bash mandate, whose site list resolved to
zero when those four scripts were retired outright). A `"moved"` entry
still requires `new_repo`/`new_path` (unchanged contract); a `"retired"`
entry requires `retired_at`/`reason` instead, and MAY carry a `successor`
(free-text pointer to a replacement that is NOT a relocation of the same
artifact -- e.g. "functionality absorbed into X", not a `new_path`). Both
dispositions answer `find_relocation` -- a consumer probing a retired path
gets "deliberately retired at <date>, no successor" (or the named
successor) rather than a miss indistinguishable from "never existed".
`check_relocation_ledger_integrity` only staleness-checks `"moved"`
entries -- a `"retired"` entry has no `new_path` to go stale.

Negative-spec:
    - Does NOT attempt to auto-detect a NEW re-homing OR a NEW retirement as
      it happens (e.g. a git-history diff gate that notices a tracked
      oracle vanished this commit with no recorded disposition). That
      detection is `coordinator_core.plugin_health.bin_inventory_gate`'s
      job, not this module's -- this module is the queryable record of
      what WAS recorded, not the mechanism that catches an unrecorded
      disappearance. See that sibling module's own docstring for the
      un-recorded-disappearance gate, and for why "an entry must be added
      by hand at migration time" is still a discipline this module alone
      cannot discharge.
    - Does NOT write into a sibling repo. A stub at the OLD path would be a
      cross-repo code write (routes via memo + PM relay, never a direct
      write) -- see this repo's CLAUDE.md scope boundary. This module is
      claude-klabauter's own side of the mechanism only: the ledger, the query, the
      integrity check.

Spec backlink: cross-repo/archive/2026-07-26-example-cockpit-repo-em-guard-title-
false-positive-and-validator-rehoming.md Finding 2
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from coordinator_core.engine_root import coordinator_engine_root
from coordinator_core.sibling_fact import resolve_leg

_PROG = "relocation-ledger"
_LEDGER_REL_PARTS = ("docs", "install", "relocation-ledger.json")


_DISPOSITION_MOVED = "moved"
_DISPOSITION_RETIRED = "retired"
_VALID_DISPOSITIONS = (_DISPOSITION_MOVED, _DISPOSITION_RETIRED)


@dataclass(frozen=True)
class RelocationEntry:
    """One old-repo/old-path record, disposed either `"moved"`
    (old-repo/old-path -> new-repo/new-path, the original shape) or
    `"retired"` (deliberately removed, no relocation).

    Field semantics match `docs/install/relocation-ledger.json`'s own
    `_entry_shape` documentation block -- see that file, not this
    docstring, as the canonical field contract (this dataclass is a thin
    typed view over the same JSON shape, not a second source of truth).
    `new_repo`/`new_path`/`new_runtime`/`forwarder`/`moved_at`/
    `moved_by_commit` are populated for a `"moved"` entry and blank ("") for
    a `"retired"` one; `retired_at`/`retired_by_commit`/`successor` are the
    mirror image, populated only for `"retired"`. `reason` is shared by
    both dispositions (every entry has one, regardless of shape).
    """

    disposition: str
    old_repo: str
    old_path: str
    new_repo: str = ""
    new_path: str = ""
    new_runtime: str = ""
    forwarder: str = ""
    moved_at: str = ""
    moved_by_commit: str = ""
    reason: str = ""
    retired_at: str = ""
    retired_by_commit: str = ""
    successor: str = ""

    @property
    def is_retired(self) -> bool:
        """True iff this entry's disposition is `"retired"` (never moved)."""
        return self.disposition == _DISPOSITION_RETIRED

    def describe(self) -> str:
        """One-line human-readable answer for a consumer that resolved this
        entry via `find_relocation` -- the "moved to X" / "retired at Y,
        no successor" prose an outside prober actually wants, so callers
        (a probe's own error message) do not have to re-derive it from raw
        fields."""
        if self.is_retired:
            successor_note = f"; successor: {self.successor}" if self.successor else "; no successor"
            return f"deliberately retired at {self.retired_at} ({self.reason}){successor_note}"
        return f"moved to {self.new_repo}:{self.new_path} ({self.reason})"


@dataclass
class IntegrityResult:
    """`ok=True` -> every ledger entry's `new_path` resolved to a real file
    (or the entry's `new_repo` is unresolvable on this machine, which is a
    skip for that entry, not a failure -- an OSS consumer without a DoE
    checkout should not fail this check over a DoE-side entry). `stale`
    carries one line per entry whose `new_repo` DID resolve but whose
    `new_path` does not exist there -- a ledger entry that has itself gone
    stale, which is exactly the rot this check exists to catch."""

    ok: bool
    stale: List[str] = field(default_factory=list)
    skipped_entries: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


def default_ledger_path() -> Path:
    """Resolve the tracked ledger file's on-disk path via claude-klabauter's own root
    -- self-resolution, not sibling I/O, so this does not go through
    `sibling_fact` (that module resolves OTHER repos' roots; claude-klabauter
    resolving its own tree is `coordinator_engine_root`'s job)."""
    root = Path(coordinator_engine_root())
    for part in _LEDGER_REL_PARTS:
        root = root / part
    return root


def load_relocation_ledger(ledger_path: Optional[Path] = None) -> List[RelocationEntry]:
    """Load and parse the ledger. Returns an empty list (never raises) if
    the file is absent, unreadable, or malformed JSON -- a missing/corrupt
    ledger degrades to "nothing recorded", not a hard crash for every
    caller that merely wants to query it. `check_relocation_ledger_integrity`
    is the fail-loud surface for a ledger that SHOULD exist but doesn't;
    this loader stays permissive by design."""
    path = ledger_path if ledger_path is not None else default_ledger_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_entries = data.get("entries", [])
    if not isinstance(raw_entries, list):
        return []

    entries: List[RelocationEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        # Missing `disposition` key => "moved" -- backward compatibility
        # with the one entry landed before this field existed (see module
        # docstring's DISPOSITION section).
        disposition = str(raw.get("disposition", _DISPOSITION_MOVED))
        if disposition not in _VALID_DISPOSITIONS:
            # An unrecognized disposition is malformed the same way a
            # missing required key is -- skip this one entry, not the
            # whole load.
            continue
        try:
            if disposition == _DISPOSITION_RETIRED:
                entries.append(
                    RelocationEntry(
                        disposition=disposition,
                        old_repo=str(raw["old_repo"]),
                        old_path=str(raw["old_path"]),
                        reason=str(raw["reason"]),
                        retired_at=str(raw["retired_at"]),
                        retired_by_commit=str(raw.get("retired_by_commit", "")),
                        successor=str(raw.get("successor", "")),
                    )
                )
            else:
                entries.append(
                    RelocationEntry(
                        disposition=disposition,
                        old_repo=str(raw["old_repo"]),
                        old_path=str(raw["old_path"]),
                        new_repo=str(raw["new_repo"]),
                        new_path=str(raw["new_path"]),
                        new_runtime=str(raw.get("new_runtime", "")),
                        forwarder=str(raw.get("forwarder", "")),
                        moved_at=str(raw.get("moved_at", "")),
                        moved_by_commit=str(raw.get("moved_by_commit", "")),
                        reason=str(raw.get("reason", "")),
                    )
                )
        except KeyError:
            # A malformed entry missing a required key is skipped, not fatal
            # for the whole load -- one bad entry should not hide every
            # other entry's query results from a caller.
            continue
    return entries


def _normalize_path_fragment(value: str) -> str:
    """Slash-unify and strip leading `./`/`../` traversal so a caller can
    pass either a bare relative path or one shaped like cockpit's actual
    stale probe (`../coordinator-claude/bin/validate-install-contract.sh`)
    and still match the ledger's stored `old_path` (which is recorded
    without any leading traversal -- see the ledger's own `_entry_shape`
    doc)."""
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("../") or normalized.startswith("./"):
        normalized = normalized.split("/", 1)[1] if "/" in normalized else ""
    return normalized.rstrip("/")


def find_relocation(
    old_path: str,
    *,
    old_repo: Optional[str] = None,
    ledger_path: Optional[Path] = None,
) -> Optional[RelocationEntry]:
    """Query surface: does the ledger explain `old_path` as documented --
    either a `"moved"` relocation or a `"retired"` deliberate removal (see
    module docstring's DISPOSITION section)? This is the mechanism an
    outside consumer (or a future extension of `fleet_reachability`'s own
    missing-name handling) reaches for instead of concluding "gone" from a
    dead path. Callers that only care about the answer, not the entry's raw
    fields, can use the returned entry's own `.describe()` for a one-line
    "moved to X" / "retired at Y, no successor" prose answer.

    Matching is on the normalized fragment (see `_normalize_path_fragment`)
    with a suffix match either direction, so a caller holding a shorter or
    longer path than the ledger's stored form (e.g. missing a leading repo
    segment, or carrying one the ledger's entry omits) still resolves.
    `old_repo`, if given, narrows to entries recorded under that repo name
    (case-sensitive exact match against the ledger's free-text `old_repo`
    field) -- omit it to match on path alone across all recorded repos.
    """
    target = _normalize_path_fragment(old_path)
    if not target:
        return None
    for entry in load_relocation_ledger(ledger_path):
        if old_repo is not None and entry.old_repo != old_repo:
            continue
        candidate = _normalize_path_fragment(entry.old_path)
        if not candidate:
            continue
        if (
            target == candidate
            or target.endswith("/" + candidate)
            or candidate.endswith("/" + target)
        ):
            return entry
    return None


def check_relocation_ledger_integrity(ledger_path: Optional[Path] = None) -> IntegrityResult:
    """FAIL-LOUD check: for every `"moved"` entry, does its `new_path`
    actually exist under its `new_repo`'s resolved root, right now? An
    entry recorded once and never revisited is exactly how a ledger becomes
    the next generation's stale archive -- this is the assertion that keeps
    that from happening silently. `"retired"` entries are skipped here --
    they have no `new_path` to go stale (see module docstring's DISPOSITION
    section); a retired entry's own internal shape (required `retired_at`/
    `reason`) is enforced at load time, not here.

    Resolution reuses `sibling_fact.resolve_leg` (kind `file_exists`)
    uniformly for every `"moved"` entry, `claude_klabauter` (claude-klabauter's own
    registry id) included -- see module docstring's "REPO ROOT RESOLUTION"
    section for why there is no special-cased self-repo branch.

    An entry whose `new_repo` is unresolvable on THIS machine (unregistered,
    or registered but the clone is absent from this disk) is SKIPPED, not
    failed -- an OSS consumer without every sibling repo cloned locally
    should not fail this check over an entry naming a repo they don't have.
    Only an entry whose `new_repo` DID resolve but whose `new_path` does not
    exist there counts as `stale` and flips `ok` to `False`.
    """
    entries = load_relocation_ledger(ledger_path)
    stale: List[str] = []
    skipped: List[str] = []
    checked_count = 0

    for entry in entries:
        if entry.is_retired:
            continue
        checked_count += 1
        leg_id = f"{entry.old_repo}:{entry.old_path} -> {entry.new_repo}:{entry.new_path}"
        observation = resolve_leg(
            {
                "leg_id": leg_id,
                "kind": "file_exists",
                "repo": entry.new_repo,
                "path": entry.new_path,
            }
        )
        if not observation["read_ok"]:
            skipped.append(f"{leg_id} ({observation['error']})")
            continue
        if observation["observed"] is not True:
            stale.append(leg_id)

    if stale:
        return IntegrityResult(
            ok=False,
            stale=stale,
            skipped_entries=skipped,
            lines=[
                f"[fail] {_PROG}: {len(stale)} relocation-ledger entr{'y' if len(stale) == 1 else 'ies'} "
                f"point at a new_path that no longer exists -- {', '.join(stale)}"
            ],
        )

    resolved_count = checked_count - len(skipped)
    retired_count = len(entries) - checked_count
    return IntegrityResult(
        ok=True,
        skipped_entries=skipped,
        lines=[
            f"[ok] {_PROG}: {resolved_count} relocation-ledger entr"
            f"{'y' if resolved_count == 1 else 'ies'} resolved "
            f"({len(skipped)} skipped -- new_repo unresolvable on this machine, "
            f"{retired_count} retired -- not staleness-checked)"
        ],
    )


def main(argv: List[str]) -> int:
    del argv  # no flags accepted today
    result = check_relocation_ledger_integrity()
    for line in result.lines:
        print(line, file=sys.stderr if not result.ok else sys.stdout)
    for line in result.skipped_entries:
        print(f"[skip] {_PROG}: {line}", file=sys.stdout)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
