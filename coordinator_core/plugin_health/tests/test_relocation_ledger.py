"""
coordinator_core.plugin_health.tests.test_relocation_ledger

Coverage for the relocation-ledger loader/query/integrity API (see
relocation_ledger.py's own module docstring for the cockpit incident this
exists to prevent recurring).

Every scenario uses an explicit `ledger_path=` tmp_path fixture and the
`MACHINE_LOCAL_REPOS_<ID>` env-var test hook `sibling_fact`/`registry_get`
honor natively (see `sibling_fact.py`'s own docstring, "REPO ROOT
RESOLUTION") -- never the operator's actual claude-klabauter checkout or registry.

Spec backlink: cross-repo/archive/2026-07-26-example-cockpit-repo-em-guard-title-
false-positive-and-validator-rehoming.md Finding 2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.plugin_health import relocation_ledger as rl


def _write_ledger(path: Path, entries: list) -> None:
    path.write_text(json.dumps({"schema": "claude-klabauter-relocation-ledger/v1", "entries": entries}), encoding="utf-8")


def _entry(**overrides) -> dict:
    base = {
        "old_repo": "coordinator-claude (example-doctrine-repo)",
        "old_path": "bin/validate-install-contract.sh",
        "new_repo": "test_repo",
        "new_path": "coordinator/bin/validate-install-contract.py",
        "new_runtime": "python3.11+",
        "forwarder": "settings-home bin forwarder",
        "moved_at": "2026-07-22",
        "moved_by_commit": "8a28a6cac0",
        "reason": "test fixture",
    }
    base.update(overrides)
    return base


def _retired_entry(**overrides) -> dict:
    base = {
        "disposition": "retired",
        "old_repo": "claude_klabauter",
        "old_path": "coordinator/bin/retired-tool.sh",
        "reason": "test fixture retirement",
        "retired_at": "2026-07-27",
    }
    base.update(overrides)
    return base


def test_load_relocation_ledger_missing_file_returns_empty(tmp_path: Path) -> None:
    assert rl.load_relocation_ledger(tmp_path / "nope.json") == []


def test_load_relocation_ledger_malformed_json_returns_empty(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{not valid json", encoding="utf-8")
    assert rl.load_relocation_ledger(ledger_path) == []


def test_load_relocation_ledger_parses_entries(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry()])
    entries = rl.load_relocation_ledger(ledger_path)
    assert len(entries) == 1
    assert entries[0].old_path == "bin/validate-install-contract.sh"
    assert entries[0].new_repo == "test_repo"


def test_load_relocation_ledger_skips_entry_missing_required_key(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    good = _entry()
    bad = _entry()
    del bad["new_path"]
    _write_ledger(ledger_path, [bad, good])
    entries = rl.load_relocation_ledger(ledger_path)
    assert len(entries) == 1
    assert entries[0].old_path == good["old_path"]


# Review: code-reviewer (Finding 2, P2) -- direct coverage for the
# `retired`-disposition load path and `RelocationEntry.describe()`, neither
# of which had a direct test in this file before (both were exercised only
# transitively, if at all, via `bin_inventory_gate.py`'s disposed-stem
# outcome, which does not assert on the loaded entry's own typed fields or
# `.describe()`'s exact string output).
def test_load_relocation_ledger_parses_retired_entry_typed_fields(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_retired_entry(successor="check_bin_inventory_gate")])
    entries = rl.load_relocation_ledger(ledger_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.disposition == "retired"
    assert entry.is_retired is True
    assert entry.retired_at == "2026-07-27"
    assert entry.successor == "check_bin_inventory_gate"
    assert entry.reason == "test fixture retirement"
    # A retired entry has no relocation -- the "moved"-only fields stay blank.
    assert entry.new_repo == ""
    assert entry.new_path == ""


def test_moved_entry_is_retired_is_false(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry()])
    entries = rl.load_relocation_ledger(ledger_path)
    assert entries[0].is_retired is False


def test_describe_moved_entry() -> None:
    entry = rl.RelocationEntry(
        disposition="moved",
        old_repo="coordinator-claude (example-doctrine-repo)",
        old_path="bin/validate-install-contract.sh",
        new_repo="claude_klabauter",
        new_path="coordinator/bin/validate-install-contract.py",
        reason="test fixture",
    )
    assert entry.describe() == (
        "moved to claude_klabauter:coordinator/bin/validate-install-contract.py (test fixture)"
    )


def test_describe_retired_entry_without_successor() -> None:
    entry = rl.RelocationEntry(
        disposition="retired",
        old_repo="claude_klabauter",
        old_path="coordinator/bin/retired-tool.sh",
        reason="test fixture retirement",
        retired_at="2026-07-27",
    )
    assert entry.describe() == (
        "deliberately retired at 2026-07-27 (test fixture retirement); no successor"
    )


def test_describe_retired_entry_with_successor() -> None:
    entry = rl.RelocationEntry(
        disposition="retired",
        old_repo="claude_klabauter",
        old_path="coordinator/bin/retired-tool.sh",
        reason="test fixture retirement",
        retired_at="2026-07-27",
        successor="check_bin_inventory_gate",
    )
    assert entry.describe() == (
        "deliberately retired at 2026-07-27 (test fixture retirement); "
        "successor: check_bin_inventory_gate"
    )


# Review: code-reviewer (Finding 3, nit) -- the skip-on-invalid-disposition
# branch had no test confirming an unrecognized `disposition` value is
# silently dropped rather than raised or mis-typed as `"moved"`.
def test_load_relocation_ledger_skips_entry_with_unknown_disposition(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    good = _entry()
    bad = _entry(disposition="bogus")
    _write_ledger(ledger_path, [bad, good])
    entries = rl.load_relocation_ledger(ledger_path)
    assert len(entries) == 1
    assert entries[0].old_path == good["old_path"]


def test_find_relocation_matches_bare_old_path(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry()])
    found = rl.find_relocation("bin/validate-install-contract.sh", ledger_path=ledger_path)
    assert found is not None
    assert found.new_path == "coordinator/bin/validate-install-contract.py"


def test_find_relocation_matches_cockpit_shaped_stale_probe(tmp_path: Path) -> None:
    """The exact stale-path shape the incident named: a leading `../<repo>/`
    traversal segment prefixing the same bare path."""
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry()])
    found = rl.find_relocation(
        "../coordinator-claude/bin/validate-install-contract.sh", ledger_path=ledger_path
    )
    assert found is not None


def test_find_relocation_no_match_returns_none(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry()])
    assert rl.find_relocation("bin/some-other-tool.sh", ledger_path=ledger_path) is None


def test_find_relocation_narrows_by_old_repo(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry(old_repo="some-other-repo")])
    assert (
        rl.find_relocation(
            "bin/validate-install-contract.sh",
            old_repo="coordinator-claude (example-doctrine-repo)",
            ledger_path=ledger_path,
        )
        is None
    )
    assert (
        rl.find_relocation(
            "bin/validate-install-contract.sh",
            old_repo="some-other-repo",
            ledger_path=ledger_path,
        )
        is not None
    )


def test_integrity_ok_when_new_path_exists(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    target = repo_root / "coordinator" / "bin" / "validate-install-contract.py"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry(new_repo="zz_test_repo")])
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_ZZ_TEST_REPO", str(repo_root))

    result = rl.check_relocation_ledger_integrity(ledger_path)
    assert result.ok is True
    assert result.stale == []


def test_integrity_fails_when_new_path_missing(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry(new_repo="zz_test_repo")])
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_ZZ_TEST_REPO", str(repo_root))

    result = rl.check_relocation_ledger_integrity(ledger_path)
    assert result.ok is False
    assert len(result.stale) == 1


def test_integrity_skips_unresolvable_new_repo(tmp_path, monkeypatch) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [_entry(new_repo="zz_definitely_unregistered_repo")])
    monkeypatch.delenv("MACHINE_LOCAL_REPOS_ZZ_DEFINITELY_UNREGISTERED_REPO", raising=False)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "empty-registry"))

    result = rl.check_relocation_ledger_integrity(ledger_path)
    assert result.ok is True
    assert len(result.skipped_entries) == 1


def test_integrity_ok_on_empty_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    _write_ledger(ledger_path, [])
    result = rl.check_relocation_ledger_integrity(ledger_path)
    assert result.ok is True


@pytest.mark.real_home  # live-tree oracle: resolves the real CLAUDE_KLABAUTER_ROOT via the machine-local
# registry, which the suite-root `_quarantine_real_home` autouse fixture would otherwise hide —
# turning this oracle into an unconditional skip. Read-only (integrity check performs no writes),
# which is the marker's own sanctioned use per conftest.py's docstring.
def test_real_tracked_ledger_passes_integrity() -> None:
    """The tracked `docs/install/relocation-ledger.json` (backfilled with the
    validate-install-contract entry that prompted this module) must resolve
    against the real, live claude-klabauter tree -- no tmp_path fixture. SKIPS (visibly,
    via pytest.skip) rather than passing when claude-klabauter's own root cannot be
    resolved on this machine, mirroring `fleet_reachability`'s skip-not-fail
    contract for an unresolvable root.

    The skip is deliberately a `pytest.skip` and not a bare `return`: this
    test's entire value is asserting against the real tree, so a run in which
    it did not assert must be distinguishable from a run in which it did. A
    bare `return` reports as a pass, making "no oracle was available" read
    identically to "the oracle held" -- the vacuous-signal failure this
    module's own subject matter is about."""
    try:
        ledger_path = rl.default_ledger_path()
    except RuntimeError as exc:
        pytest.skip(f"claude-klabauter root unresolvable on this machine: {exc}")
    if not ledger_path.is_file():
        pytest.skip(f"tracked ledger absent at {ledger_path}")
    result = rl.check_relocation_ledger_integrity(ledger_path)
    assert result.ok, result.lines + result.stale
