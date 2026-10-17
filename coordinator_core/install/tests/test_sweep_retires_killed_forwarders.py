"""The install sweep retires reap-claims-for-repos (killed, DR-344) in every installed
shape, leaves an unrelated bin file alone, and never lists a still-shipped CLI as killed."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install import substrate

_KILLED = ("reap-claims-for-repos",)


def test_five_stems_are_in_the_sweep_set() -> None:
    assert set(_KILLED) <= substrate._KILLED_OP_ORPHAN_NAMES


@pytest.mark.parametrize("suffix", ["", ".cmd", ".ps1"])
def test_sweep_removes_the_five_and_keeps_an_unrelated_file(monkeypatch, tmp_path, suffix) -> None:
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = [bin_dst / f"{stem}{suffix}" for stem in _KILLED]
    for entry in stale:
        entry.write_text("a forwarder whose target no longer exists\n", encoding="utf-8")
    keeper = bin_dst / "user-placed-tool"
    keeper.write_text("not ours\n", encoding="utf-8")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    assert [e.name for e in stale if e.exists()] == []
    assert keeper.exists()


def test_check_only_names_the_five_as_orphans(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    for stem in _KILLED:
        (bin_dst / stem).write_text("stale\n", encoding="utf-8")

    with pytest.raises(substrate.SubstrateFatalError) as exc:
        substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=True)

    for stem in _KILLED:
        assert stem in str(exc.value)


def test_no_shipped_cli_shares_a_stem_with_the_sweep_set() -> None:
    """A stem still shipped in coordinator/bin is protected from the sweep, so
    listing it as killed retires nothing."""
    bin_src = Path(substrate.__file__).resolve().parents[2] / "coordinator" / "bin"
    shipped = set(substrate._derive_agent_helper_target_map(bin_src))
    assert sorted(shipped & substrate._KILLED_OP_ORPHAN_NAMES) == []
