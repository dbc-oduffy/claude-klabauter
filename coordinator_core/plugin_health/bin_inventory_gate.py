"""
coordinator_core.plugin_health.bin_inventory_gate — FAIL-LOUD gate for an
UN-RECORDED disappearance from claude-klabauter's own `coordinator/bin/` executable
surface.

Purpose: `relocation_ledger.py` answers "where did this move to" for an
entry that IS in the ledger, and `check_relocation_ledger_integrity` catches
an entry whose `new_path` has itself gone stale. Neither catches a NEW
disappearance that lands with NO ledger entry at all -- so remembering to
add an entry at migration time was still discipline, which is exactly the
"the operator remembers" shape this repo's north-star discharge test
rejects (`docs/install/CLAUDE.md` § North star) and exactly how the
`b644d5a9` re-homing silently broke a downstream probe the first time (see
`relocation_ledger.py`'s own module docstring for that incident).

This module closes that gap with a TRACKED INVENTORY
(`docs/install/bin-inventory.json`, git-tracked so its own history is an
audit trail) of every installed-name oracle stem known to have existed
across claude-klabauter's fleet-invocable oracle surface, plus
`check_bin_inventory_gate()` -- the assertion that every inventory entry
either (a) still exists on disk, or (b) has a relocation-ledger entry
recording it as `"moved"` or `"retired"`. An entry satisfying neither is an
UN-RECORDED disappearance: something left and nobody said so.
`test_bin_inventory_gate.py` wires this as the pytest gate this repo's
build has no CI/git-hook surface to run it from otherwise (see this repo's
CLAUDE.md: new local git hooks require PM approval this pass does not
have, so a normal-test-path pytest is the sanctioned enforcement surface).

The live-oracle-set enumeration is NOT re-implemented here. It is
`plugin_health.oracle_surface.live_oracle_names()` -- the single definition
of "which oracles currently exist" across the SAME fixed, finite
three-directory surface (`coordinator/bin/`, `<repo-root>/bin/`,
`coordinator/lib/`) `fleet_reachability.py` scans on its own supply side.
That module's own docstring names the incident this sharing fixes: this
gate's inventory was seeded from `coordinator/bin/` alone while `fleet_
reachability`'s supply side was independently widened to three directories,
leaving `claude-klabauter-doctor-probe.py` (repo-root `bin/`) invisible to BOTH gates
at once -- see `oracle_surface`'s own docstring and this module's spec
backlink below. `oracle_surface.live_oracle_names()` itself reuses the same
`coordinator_core.install.substrate._derive_agent_helper_target_map` scan
`forwarder_drift.py` also reuses for the identical per-directory question --
see that function's own docstring for the exclusion/stem-dedup rules
(generated `.cmd`/`.ps1` Windows-launcher siblings, in-tree test fixtures,
`_`-prefixed/dotfiles, subdirectories are all excluded, so none of those
ever appear as independent inventory entries).

Ergonomics-over-enforcement (this repo's CLAUDE.md § North star): a
legitimate removal has exactly two cheap correct moves -- record the
relocation-ledger entry (`"moved"` or `"retired"`) and note the removal in
the same commit -- and one expensive wrong one (this gate goes red). The
inventory itself is NOT updated to drop the removed name; a name that has a
matching ledger entry stays in the inventory forever as a permanent,
resolvable historical record, exactly like the ledger's own entries do.

Negative-spec:
    - Does NOT auto-detect a NEW oracle landing in `coordinator/bin/` and
      require it to be added to the inventory before the gate will pass --
      an inventory entry not yet present is simply not checked; this gate
      only ever asserts about names IN the inventory. A brand-new CLI with
      no inventory entry is invisible to this gate until someone adds it
      (see `docs/install/bin-inventory.json`'s own doc block). This is a
      DELIBERATE, NAMED residual: the moment this gate also required
      "every live oracle must have an inventory entry" it would fail on
      every ordinary CLI addition, which is not this pass's mandate ("make
      a disappearance detectable", not "make an addition detectable") and
      would turn a targeted disappearance gate into an addition-bureaucracy
      gate that punishes routine growth of `coordinator/bin/`.
    - Does NOT pass vacuously. A missing/unreadable/malformed/empty
      inventory file, an unresolvable `coordinator/bin/` root, or an empty
      live-oracle scan are each a LOUD failure (`ok=False`, a `lines` entry
      naming exactly which precondition failed) -- never a silent skip-to-
      green. Unlike `relocation_ledger`'s own loader (which degrades a
      missing/malformed ledger to "nothing recorded" for callers that
      merely want to query it) or `fleet_reachability`'s legitimate
      unresolvable-sibling-repo skip (an OSS consumer without a DoE
      checkout genuinely has nothing to compare), THIS gate's inventory and
      `coordinator/bin/` are both always-present, in-tree, self-contained
      surfaces on any checkout of this repo -- there is no legitimate
      persona for which either being absent/empty is expected, so an
      absent/empty precondition here is itself the failure, not a signal
      to skip.

Spec backlink: cross-repo/archive/2026-07-26-example-cockpit-repo-em-guard-title-
false-positive-and-validator-rehoming.md Finding 2 (original gate). Widened
2026-07-27 (commit f622297b90d98f7ccd8f5796b53fe034ab4b190d's own "FOLLOW-UP
FOUND WHILE VERIFYING THAT TRADE" note) to scan the full three-directory
oracle surface via `plugin_health.oracle_surface`, not `coordinator/bin/`
alone -- closing the shared blind spot that left `claude-klabauter-doctor-probe.py`
invisible to this gate (and, independently, to `fleet_reachability.py`).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.plugin_health import relocation_ledger as rl

_PROG = "bin-inventory-gate"
_INVENTORY_REL_PARTS = ("docs", "install", "bin-inventory.json")

# Extension-stripping applied to a relocation-ledger `old_path` basename so
# it can be compared against an inventory entry (already .py-stripped, per
# `_derive_agent_helper_target_map`'s own stem-dedup rule). Mirrors
# `fleet_reachability._normalize`'s known-oracle-extension set -- kept as an
# independent tuple rather than importing that module's private helper,
# since the two modules' extension sets could legitimately diverge (this
# gate's inventory only ever holds names derived by
# `_derive_agent_helper_target_map`, which strips ONLY `.py`; the wider
# `.js`/`.sh`/`.cmd` set here is for matching a ledger entry's `old_path`,
# which may record any of those extensions verbatim, not the derived-name
# set itself).
_KNOWN_OLD_PATH_EXTENSIONS = (".py", ".js", ".sh", ".cmd", ".ps1")

# Review: code-reviewer (Finding 1, P1) -- the `old_repo` value a ledger
# entry must carry to be treated as recording CLAUDE-KLABAUTER'S OWN history for this
# gate's disposition-match purposes. `relocation-ledger.json`'s own
# `_entry_shape` doc calls `old_repo` "informational only, not resolved" --
# there is no registry-backed self-check the way `new_repo` gets via
# `sibling_fact` (see relocation_ledger.py's own "REPO ROOT RESOLUTION"
# section) -- but every entry this repo has authored to record its OWN
# churn already uses this exact string, matching the `repos.claude_klabauter`
# registry id that same section names as claude-klabauter's own self-identifier, and
# every fixture in this module's own test file already assumes it. An entry
# recording a DIFFERENT repo's history (the one production entry records a
# DoE-claude -> claude-klabauter adoption, not a claude-klabauter-internal move) must never
# "explain away" a disappearance from claude-klabauter's OWN tree merely because its
# `old_path` stem happens to collide with a tracked inventory name.
_SELF_OLD_REPO_ID = "claude_klabauter"


def _stem_of_old_path(old_path: str) -> str:
    """Reduce a relocation-ledger entry's `old_path` to the bare stem an
    inventory name can be compared against: basename, known-extension
    stripped, lowercased."""
    name = old_path.strip().replace("\\", "/").rsplit("/", 1)[-1]
    for ext in _KNOWN_OLD_PATH_EXTENSIONS:
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name.lower()


def _stem_of_inventory_name(name: str) -> str:
    """Reduce an inventory name to the same stem `_stem_of_old_path` produces.

    NEGATIVE SPEC: the comparison must be symmetric. This used to strip
    extensions from the LEDGER side only, against the belief -- stated in
    `_KNOWN_OLD_PATH_EXTENSIONS`' own comment -- that inventory names arrive
    already `.py`-stripped by `_derive_agent_helper_target_map`. Two entries
    (`detect-hardware.py`, `spawn-hidden.sh`) carry their extension verbatim,
    so no ledger entry could ever match them: the gate demanded a record it
    would then refuse to see, and the only way to satisfy it was to write an
    `old_path` that misnamed the file. Normalising both sides is what makes an
    honest ledger entry sufficient."""
    return _stem_of_old_path(name)


def default_inventory_path() -> Path:
    """Resolve the tracked inventory file's on-disk path via claude-klabauter's own
    root -- self-resolution, matching `relocation_ledger.default_ledger_path`'s
    own precedent (this repo resolving its own tree, not a sibling's)."""
    root = Path(coordinator_claude_klabauter_root())
    for part in _INVENTORY_REL_PARTS:
        root = root / part
    return root


@dataclass
class InventoryLoadResult:
    """`ok=True` -> `names` carries a non-empty, well-formed inventory.
    `ok=False` -> `error` names exactly which precondition failed (missing
    file, unreadable, malformed JSON, wrong schema shape, or empty
    `entries`) -- this loader is deliberately NOT permissive the way
    `relocation_ledger.load_relocation_ledger` is; see module docstring's
    "Does NOT pass vacuously" section for why."""

    ok: bool
    names: List[str] = field(default_factory=list)
    error: str = ""


def load_bin_inventory(inventory_path: Optional[Path] = None) -> InventoryLoadResult:
    """Load the tracked inventory, failing loud (never silently returning an
    empty/partial list) on any malformed precondition."""
    path = inventory_path if inventory_path is not None else default_inventory_path()
    if not path.is_file():
        return InventoryLoadResult(ok=False, error=f"inventory file not found at {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return InventoryLoadResult(ok=False, error=f"inventory file unreadable at {path}: {exc}")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return InventoryLoadResult(ok=False, error=f"inventory file is not valid JSON at {path}: {exc}")
    if not isinstance(data, dict):
        return InventoryLoadResult(ok=False, error=f"inventory file at {path} is not a JSON object")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        return InventoryLoadResult(
            ok=False,
            error=f"inventory file at {path} has an empty or missing 'entries' list",
        )
    names: List[str] = []
    for raw in raw_entries:
        if not isinstance(raw, str) or not raw.strip():
            return InventoryLoadResult(
                ok=False,
                error=f"inventory file at {path} has a non-string or blank entry: {raw!r}",
            )
        names.append(raw)
    return InventoryLoadResult(ok=True, names=names)


@dataclass
class BinInventoryGateResult:
    """`ok=True` -> every inventory entry is either still live or has a
    recorded relocation-ledger disposition (`"moved"` or `"retired"`).
    `ok=False` -> `disappeared` names at least one inventory entry that is
    neither on disk nor in the ledger under either disposition -- an
    UN-RECORDED disappearance, the exact defect this gate exists to catch.
    `lines` always carries a human-readable summary; a failure never
    reports merely "gate ran" without naming what it found (see module
    docstring's "Does NOT pass vacuously" section)."""

    ok: bool
    disappeared: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


def check_bin_inventory_gate(
    *,
    agent_bin: Optional[Path] = None,
    extra_oracle_dirs: Optional[List[Path]] = None,
    inventory_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
) -> BinInventoryGateResult:
    """Core gate: diff the tracked inventory against the live oracle set
    across claude-klabauter's full fleet-invocable surface (`coordinator/bin/` plus
    `<repo-root>/bin/` and `coordinator/lib/` -- via `plugin_health.
    oracle_surface.live_oracle_names()`, the single definition of that
    surface `fleet_reachability.py` also consumes) and the relocation
    ledger. FAIL LOUD -- never a silent skip -- on any of: `coordinator/
    bin/` unresolvable, an empty live scan, or a malformed/empty inventory.
    `agent_bin`/`extra_oracle_dirs`/`inventory_path`/`ledger_path` are
    explicit overrides for tests; a caller that omits them gets the real
    resolution ladder against this checkout's own tree.

    `extra_oracle_dirs` is deliberately independent of `agent_bin`, mirroring
    `fleet_reachability.check_fleet_reachability`'s own contract: a test
    that overrides `agent_bin` alone gets NO extra directories scanned
    unless it also passes `extra_oracle_dirs` explicitly -- this keeps every
    pre-existing fixture in this module's test file exactly as narrow as it
    was before this gate's live scan was widened. Only the fully-real
    invocation (neither override given) auto-derives both from
    `oracle_surface`.
    """
    from coordinator_core.plugin_health.oracle_surface import (
        resolve_agent_bin,
        resolve_extra_oracle_dirs,
    )

    if agent_bin is not None:
        resolved_agent_bin: Optional[Path] = agent_bin
        resolved_extra_dirs = extra_oracle_dirs if extra_oracle_dirs is not None else []
    else:
        resolved_agent_bin = resolve_agent_bin()
        resolved_extra_dirs = extra_oracle_dirs if extra_oracle_dirs is not None else resolve_extra_oracle_dirs()

    if resolved_agent_bin is None:
        return BinInventoryGateResult(
            ok=False,
            lines=[
                f"[fail] {_PROG}: claude-klabauter root (or its coordinator/bin/) unresolvable -- "
                "cannot verify the inventory against a live tree, refusing to report green"
            ],
        )

    # Lazy import, mirroring fleet_reachability._claude_klabauter_oracle_names: a
    # live install-surface module is imported at call time, not module
    # load, so an in-flight edit there can't break this module merely by
    # being imported alongside it.
    from coordinator_core.plugin_health.oracle_surface import live_oracle_names

    live_names: Set[str] = live_oracle_names([resolved_agent_bin] + resolved_extra_dirs)
    if not live_names:
        return BinInventoryGateResult(
            ok=False,
            lines=[
                f"[fail] {_PROG}: live scan of {resolved_agent_bin} returned zero oracles -- "
                "this looks like an incomplete scan, not a genuinely empty coordinator/bin/, "
                "refusing to report green"
            ],
        )

    inventory = load_bin_inventory(inventory_path)
    if not inventory.ok:
        return BinInventoryGateResult(
            ok=False,
            lines=[f"[fail] {_PROG}: {inventory.error}"],
        )

    ledger_entries = rl.load_relocation_ledger(ledger_path)
    disposed_stems = {
        _stem_of_old_path(entry.old_path)
        for entry in ledger_entries
        if entry.old_repo == _SELF_OLD_REPO_ID
    }

    live_stems: Set[str] = {_stem_of_inventory_name(live_name) for live_name in live_names}

    disappeared: List[str] = []
    for name in inventory.names:
        if name in live_names:
            continue
        if _stem_of_inventory_name(name) in live_stems:
            continue
        if _stem_of_inventory_name(name) in disposed_stems:
            continue
        disappeared.append(name)

    if disappeared:
        return BinInventoryGateResult(
            ok=False,
            disappeared=disappeared,
            lines=[
                f"[fail] {_PROG}: {len(disappeared)} tracked bin/ oracle(s) no longer exist and "
                f"have NO relocation-ledger entry (neither 'moved' nor 'retired') -- "
                f"{', '.join(sorted(disappeared))}. Record the removal in "
                "docs/install/relocation-ledger.json before this can pass."
            ],
        )

    return BinInventoryGateResult(
        ok=True,
        lines=[
            f"[ok] {_PROG}: all {len(inventory.names)} tracked bin/ oracle(s) are either "
            "still live or have a recorded relocation-ledger disposition"
        ],
    )


def main(argv: List[str]) -> int:
    del argv  # no flags accepted today
    result = check_bin_inventory_gate()
    for line in result.lines:
        print(line, file=sys.stderr if not result.ok else sys.stdout)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
