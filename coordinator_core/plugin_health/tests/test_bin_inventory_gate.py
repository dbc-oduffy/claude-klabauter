"""
coordinator_core.plugin_health.tests.test_bin_inventory_gate

Coverage for the un-recorded-disappearance gate (see bin_inventory_gate.py's
own module docstring for the "moved is answerable, gone with no ledger
entry is not" gap this closes).

Spec backlink: cross-repo/archive/2026-07-26-example-cockpit-repo-em-guard-title-
false-positive-and-validator-rehoming.md Finding 2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.plugin_health import bin_inventory_gate as big


def _write_inventory(path: Path, names: list) -> None:
    path.write_text(
        json.dumps({"schema": "claude-klabauter-bin-inventory/v1", "entries": names}),
        encoding="utf-8",
    )


def _write_ledger(path: Path, entries: list) -> None:
    path.write_text(json.dumps({"schema": "claude-klabauter-relocation-ledger/v1", "entries": entries}), encoding="utf-8")


def _moved_entry(**overrides) -> dict:
    base = {
        "disposition": "moved",
        "old_repo": "claude_klabauter",
        "old_path": "coordinator/bin/gone-tool.py",
        "new_repo": "claude_klabauter",
        "new_path": "coordinator/bin/renamed-tool.py",
        "new_runtime": "python3.11+",
        "forwarder": "none",
        "moved_at": "2026-07-27",
        "moved_by_commit": "deadbeef",
        "reason": "test fixture",
    }
    base.update(overrides)
    return base


def _retired_entry(**overrides) -> dict:
    base = {
        "disposition": "retired",
        "old_repo": "claude_klabauter",
        "old_path": "coordinator/bin/gone-tool.py",
        "reason": "test fixture retirement",
        "retired_at": "2026-07-27",
    }
    base.update(overrides)
    return base


def _make_agent_bin(tmp_path: Path, names: list) -> Path:
    agent_bin = tmp_path / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    for name in names:
        (agent_bin / f"{name}.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return agent_bin


def test_gate_ok_when_all_inventory_entries_are_live(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is True
    assert result.disappeared == []


def test_gate_fails_on_unrecorded_disappearance(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "vanished-tool"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert result.disappeared == ["vanished-tool"]
    assert "vanished-tool" in result.lines[0]


def test_gate_ok_when_disappearance_is_recorded_moved(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "gone-tool"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_moved_entry()])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is True
    assert result.disappeared == []


def test_gate_ok_when_disappearance_is_recorded_retired(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "gone-tool"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_retired_entry()])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is True
    assert result.disappeared == []


def test_gate_fails_loud_on_missing_inventory_file(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "nope.json"
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert "not found" in result.lines[0]


def test_gate_fails_loud_on_empty_inventory_entries(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, [])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert "empty" in result.lines[0]


def test_gate_fails_loud_on_malformed_inventory_json(tmp_path: Path) -> None:
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text("{not valid json", encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert "not valid JSON" in result.lines[0]


def test_gate_fails_loud_when_agent_bin_empty_scan(tmp_path: Path) -> None:
    """An explicit `agent_bin` override that resolves but scans empty is the
    empty-live-scan failure mode -- covered directly by
    `test_gate_fails_loud_on_empty_live_scan` below. This test instead
    exercises the sibling precondition: `_resolve_claude_klabauter_agent_bin()`
    itself returning None (an unresolvable claude-klabauter root or an absent
    `coordinator/bin/`) via the real, no-override resolution path, which
    `check_bin_inventory_gate()` must refuse to treat as a clean skip."""
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    # coordinator_claude_klabauter_root() resolves this checkout's own real root in
    # this test process (there is no way to force it to fail without
    # mutating process-wide env/registry state a parallel test run may also
    # depend on), so this asserts the CONTRACT via the explicit-override
    # path instead: a directory that exists but is not a real
    # coordinator/bin/ (empty) must still refuse to report green, exactly
    # like an unresolvable one would.
    missing_dir = tmp_path / "does-not-exist"
    result = big.check_bin_inventory_gate(
        agent_bin=missing_dir, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False


def test_gate_fails_loud_on_empty_live_scan(tmp_path: Path) -> None:
    agent_bin = tmp_path / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert "zero oracles" in result.lines[0]


def test_extra_oracle_dirs_widen_live_scan(tmp_path: Path) -> None:
    """2026-07-27 widening (see bin_inventory_gate.py's own spec backlink):
    an inventory entry whose real oracle lives in `<repo-root>/bin/` or
    `coordinator/lib/`, not `coordinator/bin/`, must be treated as live --
    the same shared `plugin_health.oracle_surface` surface `fleet_
    reachability.py` already scans."""
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    repo_root_bin = tmp_path / "bin"
    coordinator_lib = tmp_path / "coordinator" / "lib"
    repo_root_bin.mkdir(parents=True)
    coordinator_lib.mkdir(parents=True)
    (repo_root_bin / "claude-klabauter-doctor-probe.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (coordinator_lib / "resolve-coordinator-clone.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "claude-klabauter-doctor-probe", "resolve-coordinator-clone"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin,
        extra_oracle_dirs=[repo_root_bin, coordinator_lib],
        inventory_path=inventory_path,
        ledger_path=ledger_path,
    )
    assert result.ok is True
    assert result.disappeared == []


def test_extra_oracle_dirs_not_auto_populated_when_agent_bin_overridden(tmp_path: Path) -> None:
    """Negative-spec companion to the test above, mirroring fleet_
    reachability's own isolation contract: an explicit `agent_bin` override
    with NO `extra_oracle_dirs` override must NOT silently widen to this
    machine's real `<repo-root>/bin/`/`coordinator/lib/` contents -- every
    pre-existing fixture in this file relies on that isolation. An
    inventory entry present only in a sibling dir the caller did not pass
    stays a genuine disappearance."""
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "claude-klabauter-doctor-probe"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert result.disappeared == ["claude-klabauter-doctor-probe"]


def test_generated_windows_siblings_not_inventoried_as_independent_oracles(tmp_path: Path) -> None:
    """A `.cmd`/`.ps1` twin must never surface as its own live name across
    ANY of the three scanned directories -- pins the widened live scan
    against the same regression class `oracle_surface`'s own unit tests
    cover, at the gate's own diff boundary."""
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    (agent_bin / "still-here.cmd").write_text("@echo off\n", encoding="utf-8")
    repo_root_bin = tmp_path / "bin"
    repo_root_bin.mkdir(parents=True)
    (repo_root_bin / "shell-init-guard.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo_root_bin / "shell-init-guard.cmd").write_text("@echo off\n", encoding="utf-8")

    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "shell-init-guard"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin,
        extra_oracle_dirs=[repo_root_bin],
        inventory_path=inventory_path,
        ledger_path=ledger_path,
    )
    assert result.ok is True
    assert result.disappeared == []


def test_known_old_path_extensions_parity_with_fleet_reachability() -> None:
    """Review: code-reviewer (Finding 4, nit) -- drift-visibility pin for
    `_KNOWN_OLD_PATH_EXTENSIONS`'s intentional, documented divergence from
    `fleet_reachability._KNOWN_ORACLE_EXTENSIONS` (see this module's own
    comment above `_KNOWN_OLD_PATH_EXTENSIONS` for why the two hand-kept
    sets are allowed to differ, rather than sharing one constant). Pins
    TODAY's known divergence (`.ps1` unique to this gate's set) explicitly,
    so a future UNNAMED divergence -- either set widening with no
    corresponding update to the other -- fails loud instead of silently
    reducing match quality."""
    from coordinator_core.plugin_health import fleet_reachability as fr

    known_divergence = {".ps1"}
    this_set = set(big._KNOWN_OLD_PATH_EXTENSIONS)
    other_set = set(fr._KNOWN_ORACLE_EXTENSIONS)
    assert this_set - other_set == known_divergence
    assert other_set - this_set == set()


def test_external_old_repo_entry_does_not_dispose_future_disappearance(tmp_path: Path) -> None:
    """Review: code-reviewer (Finding 1, P1) -- regression for the
    "vaccination" bug: a ledger entry recording a DIFFERENT repo's
    relocation INTO claude-klabauter (`old_repo` != claude-klabauter's own self id) must NOT
    silently explain away a later, unrelated disappearance of a
    same-stemmed oracle FROM claude-klabauter's own tree. This is the exact shape of
    the one production `docs/install/relocation-ledger.json` entry
    (`old_repo: "coordinator-claude (DoE-claude)"`), which records a
    DoE-claude -> claude-klabauter adoption, not a claude-klabauter-internal move -- so its
    stem must never dispose an unrelated future disappearance of
    `validate-install-contract` from claude-klabauter's own tree."""
    agent_bin = _make_agent_bin(tmp_path, ["still-here"])
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, ["still-here", "validate-install-contract"])
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(
        ledger_path,
        [
            _moved_entry(
                old_repo="coordinator-claude (DoE-claude)",
                old_path="bin/validate-install-contract.sh",
                new_repo="claude_klabauter",
                new_path="coordinator/bin/validate-install-contract.py",
            )
        ],
    )

    result = big.check_bin_inventory_gate(
        agent_bin=agent_bin, inventory_path=inventory_path, ledger_path=ledger_path
    )
    assert result.ok is False
    assert result.disappeared == ["validate-install-contract"]


@pytest.mark.real_home  # live-tree oracle: resolves the real CLAUDE_KLABAUTER_ROOT via the machine-local
# registry, which the suite-root `_quarantine_real_home` autouse fixture would otherwise
# route into an empty per-test HOME -- see that fixture's own docstring for the opt-out
# contract. Deliberately NOT a skip-on-unresolvable test the way
# `test_relocation_ledger.py::test_real_tracked_ledger_passes_integrity` is: THIS gate's
# whole contract is "never pass vacuously" (module docstring), so silently skipping the
# one test that exercises the real tracked inventory against the real tree would be
# exactly the vacuous-pass this module exists to refuse elsewhere.
def test_real_tracked_inventory_passes_gate() -> None:
    """The tracked `docs/install/bin-inventory.json` must resolve clean
    against the real, live claude-klabauter tree -- no tmp_path fixture. This is the
    delete-time gate itself: a future disappearance with no ledger entry
    fails THIS test, in the normal pytest path (no git hook -- see module
    docstring)."""
    result = big.check_bin_inventory_gate()
    assert result.ok, result.lines + result.disappeared
