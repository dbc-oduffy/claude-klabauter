"""Smoke test for the `coordinator-git-maintenance` trampoline.

Two claims, both of which a ceremony call site depends on and neither of which
the op's own tests can make:

  1. The trampoline resolves `coordinator_core.ops.git_maintenance`'s entry
     point for every TIER value. A call site naming a bin that cannot reach its
     op is a broken instruction an EM runs every morning.
  2. The bin is in `docs/install/bin-inventory.json`'s `entries` list. The
     tracked baseline `bin_inventory_gate.py` diffs against the live tree, so a
     new oracle with no inventory entry reads as an undocumented disappearance
     risk in the other direction.

The trampoline is loaded by PATH rather than imported: its filename carries
hyphens, so it is not an importable module name, and that is deliberate — the
installed door is a bareword `coordinator-git-maintenance`, not a Python import.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_BIN = Path(__file__).with_name("coordinator-git-maintenance.py")
_INVENTORY = Path(__file__).resolve().parents[2] / "docs" / "install" / "bin-inventory.json"


def _load():
    spec = importlib.util.spec_from_file_location("coordinator_git_maintenance_bin", _BIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_trampoline_is_on_disk_where_the_call_sites_name_it():
    assert _BIN.is_file()


@pytest.mark.parametrize("tier", ["hourly", "daily", "weekly"])
def test_trampoline_routes_every_tier_to_the_op(tier, monkeypatch):
    module = _load()
    seen = {}

    def fake_runner(op_module, argv):
        seen["op_module"] = op_module
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_import_runner", lambda: fake_runner)

    assert module.main([tier]) == 0
    assert seen["op_module"] == "coordinator_core.ops.git_maintenance"
    assert seen["argv"] == [tier]


def test_engine_root_failure_is_transport_code_two_not_a_silent_zero(monkeypatch):
    """A caller must never read an unreachable engine as a successful run."""
    module = _load()

    def boom():
        raise RuntimeError("no engine root")

    monkeypatch.setattr(module, "_import_runner", boom)
    assert module.main(["hourly"]) == 2


def test_unimportable_op_is_transport_code_two(monkeypatch):
    module = _load()

    def runner(op_module, argv):
        raise ImportError("no such module")

    monkeypatch.setattr(module, "_import_runner", lambda: runner)
    assert module.main(["weekly"]) == 2


def test_tier_argument_is_passed_through_unparsed(monkeypatch):
    """No argv parsing beyond the tier — the op owns validation, so a bad tier
    reaches it rather than being rejected twice in two places."""
    module = _load()
    seen = {}
    monkeypatch.setattr(
        module, "_import_runner", lambda: lambda op, argv: seen.setdefault("argv", list(argv)) and 0
    )

    module.main(["monthly"])
    assert seen["argv"] == ["monthly"]


def test_bin_is_in_the_tracked_inventory():
    entries = json.loads(_INVENTORY.read_text(encoding="utf-8"))["entries"]
    assert "coordinator-git-maintenance" in entries
