"""A pushed mirror's label drops the mDNS `.local` before sanitising (`machine-a.local` -> `machine-a`, never `machine-a-local`)."""
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
CLIENT = "10.0.0.9"


def load(mod_name, file):
    spec = importlib.util.spec_from_file_location(mod_name, BIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = load("relay_strip_local_t", "unimatrix-relay.py")
roster = load("roster_strip_local_t", "unimatrix-roster.py")
contract = load("contract_strip_local_t", "unimatrix-peer-contract.py")


def _label(monkeypatch, row):
    monkeypatch.setattr(relay, "entries", lambda: iter([(7, row, "k")]))
    return relay.client_box(CLIENT)[1]


def test_dot_local_remote_labels_without_local(monkeypatch):
    row = {"unimatrixRemote": "kim@machine-a.local", "unimatrixRemoteIp": CLIENT}
    assert _label(monkeypatch, row) == "machine-a"


def test_pushed_label_matches_the_pull_label_for_both_alias_generations(monkeypatch):
    for alias in ("kim@Mac-mini.local", "kim@Mac-mini"):
        label = _label(monkeypatch, {"unimatrixRemote": alias, "unimatrixRemoteIp": CLIENT})
        assert label == "Mac-mini" == roster.stable_label(alias)
        assert relay.mirror_entry({"name": "x"}, "x", None, label)["name"] == roster.labelled("x", alias)


def test_bare_local_host_and_box_fallback_are_not_emptied(monkeypatch):
    assert _label(monkeypatch, {"unimatrixRemote": "kim@local", "unimatrixRemoteIp": CLIENT}) == "local"
    assert _label(monkeypatch, {"unimatrixBox": "machine-a.local", "unimatrixRemoteIp": CLIENT}) == "machine-a"


def test_old_dash_local_name_stays_addressable():
    row = {"name": "p-0e.machine-a", "unimatrixRemote": "kim@machine-a.local"}
    assert "p-0e.machine-a-local" not in contract.mirror_addresses(row)
    old = {"name": "p-0e.machine-a-local", "unimatrixRemote": "kim@machine-a.local"}
    assert {"p-0e.machine-a-local", "p-0e.machine-a"} <= set(contract.mirror_addresses(old))
