"""Install-integration check: every ``docs/install/bin-inventory.json`` entry
that is still live in ``coordinator/bin/`` (no relocation-ledger disposition)
actually LANDS in ``bin_dst`` after a real ``_install_bin_resolvers`` pass.

Spec backlink: dispatch 2026-08-07 "check-auto-memory-drained installer
gap" — `bin_inventory_gate.check_bin_inventory_gate` only diffs the
inventory against SOURCE-TREE presence (`coordinator/bin/`, via
`oracle_surface.live_oracle_names`), never against what the installer
actually WRITES to `<settings-home>/bin`. That left a real class of defect
invisible: an inventory entry present on disk in `coordinator/bin/` but
silently skipped by `_install_bin_resolvers`'s selection/exclusion logic
(exec-bit filtering, a stale exclusion rule, a manifest/scan divergence)
would pass `bin_inventory_gate` cleanly while still 127-ing at its
documented settings-home invocation. This test closes that gap by running
the SAME real, on-disk install pass `test_bin_family_freshness.py` already
exercises (`_install_bin_resolvers` against this repo's own
`coordinator/bin/`, not a synthetic fixture) and asserting every live
inventory name is present in the resulting `bin_dst` — no code-path
inference, an actual install output.

Scope: `bin_inventory_gate` diffs against a WIDER three-directory oracle
surface (`coordinator/bin/`, `<repo-root>/bin/`, `coordinator/lib/`) than
`_install_bin_resolvers` ever forwards — a `<repo-root>/bin/` bootstrap
script like `install-substrate` or `bootstrap-repo` is invoked directly,
never installed as a settings-home forwarder, so it is out of scope for
"would the installer install this". This test therefore intersects the
inventory with `_derive_agent_helper_target_map(coordinator/bin)`'s own
key set — precisely the candidate set `_install_bin_resolvers`'s Step 3b
loop iterates — rather than the full inventory, so it asserts install-time
parity only for entries the installer is actually responsible for landing.

Negative-spec: an inventory entry that has moved/retired (a matching
relocation-ledger disposition, `bin_inventory_gate`'s own
`_stem_of_old_path`/`disposed_stems` logic) is correctly expected to be
ABSENT from a live install and is excluded here the same way
`check_bin_inventory_gate` excludes it — this test is install-time parity
for LIVE, forwarder-eligible entries only, not a second copy of the
disappearance gate and not an assertion over the wider three-directory
oracle surface.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.install.substrate import (
    _derive_agent_helper_target_map,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)
from coordinator_core.plugin_health import bin_inventory_gate as big
from coordinator_core.plugin_health import relocation_ledger as rl

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_every_live_bin_inventory_entry_lands_in_a_real_install(monkeypatch, tmp_path):
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"ml-source-content::{entry.name}\n")
    from coordinator_core.install.substrate import _CH_FAMILY_FILES

    for f, _exec_bit in _CH_FAMILY_FILES:
        _write(ch_bin / f, f"ch-source-content::{f}\n")

    # MAKIMA_ROOT points at THIS real checkout -- `_derive_agent_helper_target_map`
    # (called inside `_install_bin_resolvers`) scans the real `coordinator/bin/`,
    # exactly the tree `bin-inventory.json` was seeded from, matching
    # `test_bin_family_freshness.py`'s own precedent for this same reason.
    monkeypatch.setenv("MAKIMA_ROOT", str(_REPO_ROOT))

    forwarder_candidates = set(_derive_agent_helper_target_map(_REPO_ROOT / "coordinator" / "bin"))
    assert forwarder_candidates, (
        "the forwarder-candidate scan must be non-empty for this test to assert anything"
    )

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin="/usr/bin/python3",
    )

    inventory = big.load_bin_inventory()
    assert inventory.ok, f"bin-inventory.json failed to load: {inventory.error}"

    ledger_entries = rl.load_relocation_ledger()
    disposed_stems = {
        big._stem_of_old_path(entry.old_path)
        for entry in ledger_entries
        if entry.old_repo == big._SELF_OLD_REPO_ID
    }

    installed_names = {p.name for p in bin_dst.iterdir()}

    missing = [
        name for name in inventory.names
        if name in forwarder_candidates
        and name.lower() not in disposed_stems
        and name not in installed_names
    ]
    assert not missing, (
        f"{len(missing)} bin-inventory.json entr(y/ies) are forwarder-eligible "
        f"(present in coordinator/bin/'s _derive_agent_helper_target_map scan, no "
        f"relocation-ledger disposition) but did NOT land in a real "
        f"_install_bin_resolvers pass: {sorted(missing)}"
    )
