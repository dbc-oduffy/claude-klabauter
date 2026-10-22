"""Tests for coordinator_core.ops.register_p4_workspaces (p4 spawn stubbed, registry under tmp_path)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import coordinator_core.ops.register_p4_workspaces as rpw
from coordinator_core.install import resolution_journal as journal_mod
from coordinator_core.install.write_surface import validate
from coordinator_core.machine_resolver import merged_flat_registry

HOST = "Box.local"


def _info(user="alice", host=HOST):
    return (
        f"info1: userName {user}\ninfo1: clientName c0\ninfo1: clientHost {host}\n"
        "info1: serverAddress p4:1666\nexit: 0\n"
    )


def _clients(*recs):
    lines = []
    for name, root, rec_host in recs:
        lines.append(f"info1: client {name}")
        lines.append(f"info1: Root {root}")
        if rec_host is not None:
            lines.append(f"info1: Host {rec_host}")
        lines.append("info1: Description multi word text")
    lines.append("exit: 0")
    return "\n".join(lines) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    reg_dir = tmp_path / "reg"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(tmp_path / "journal.jsonl"))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return reg_dir


def _stub(monkeypatch, info_out, clients_out, calls=None, info_ok=True, clients_ok=True):
    def fake(args):
        if calls is not None:
            calls.append(list(args))
        if args[0] == "info":
            return SimpleNamespace(ok=info_ok, stdout=info_out if info_ok else "")
        return SimpleNamespace(ok=clients_ok, stdout=clients_out if clients_ok else "")

    monkeypatch.setattr(rpw, "_run_discovery", fake)


def _ext():
    return {k: v for k, v in merged_flat_registry().items() if k.startswith("external_workspaces.")}


def _root(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_one_matching_client_registers_four_fields(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "slice")
    _stub(monkeypatch, _info(), _clients(("my-slice", str(root), HOST)))
    assert rpw.main([]) == 0
    assert _ext() == {
        "external_workspaces.p4_my_slice.path": str(root).replace("\\", "/"),
        "external_workspaces.p4_my_slice.vcs": "p4",
        "external_workspaces.p4_my_slice.kind": "external-readonly",
        "external_workspaces.p4_my_slice.p4_client": "my-slice",
    }


def test_empty_host_registers(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "r")
    _stub(monkeypatch, _info(), _clients(("c", str(root), None)))
    rpw.main([])
    assert "external_workspaces.p4_c.path" in _ext()


def test_other_host_missing_root_and_git_root_skipped(env, tmp_path, monkeypatch):
    foreign = _root(tmp_path, "foreign")
    gitroot = _root(tmp_path, "gitroot")
    (gitroot / ".git").mkdir()
    _stub(
        monkeypatch,
        _info(),
        _clients(
            ("foreign", str(foreign), "Other"),
            ("missing", str(tmp_path / "nope"), HOST),
            ("gitc", str(gitroot), HOST),
        ),
    )
    assert rpw.main([]) == 0
    assert _ext() == {}


def test_registered_repo_path_and_p4_repo_client_skipped(env, tmp_path, monkeypatch):
    repo = _root(tmp_path, "repo")
    other = _root(tmp_path, "other")
    env.mkdir(parents=True)
    (env / "registry.local.toml").write_text(
        f"\"repos.r\" = '{repo}'\n\"p4.somerepo.client\" = 'wc'\n", encoding="utf-8"
    )
    _stub(
        monkeypatch,
        _info(),
        _clients(("rc", str(repo), HOST), ("wc", str(other), HOST)),
    )
    rpw.main([])
    assert _ext() == {}


def _seed_fifa(env, root):
    env.mkdir(parents=True, exist_ok=True)
    (env / "registry.local.toml").write_text(
        f"\"external_workspaces.p4_fifa_slice.path\" = '{root}'\n"
        "\"external_workspaces.p4_fifa_slice.kind\" = 'external-readonly'\n"
        "\"external_workspaces.p4_fifa_slice.vcs\" = 'p4'\n"
        "\"external_workspaces.p4_fifa_slice.p4_client\" = 'example-operator-mac-slice'\n"
        "\"external_workspaces.p4_fifa_slice.venture\" = 'example_fleet_fifa'\n",
        encoding="utf-8",
    )


def test_existing_entry_preserved_byte_identical_across_two_runs(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "fifa")
    _seed_fifa(env, root)
    before = (env / "registry.local.toml").read_bytes()
    _stub(monkeypatch, _info(), _clients(("example-operator-mac-slice", str(root), HOST)))
    rpw.main([])
    rpw.main([])
    assert (env / "registry.local.toml").read_bytes() == before
    assert _ext()["external_workspaces.p4_fifa_slice.venture"] == "example_fleet_fifa"


def test_same_directory_different_client_name_writes_nothing(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "fifa")
    _seed_fifa(env, root)
    before = (env / "registry.local.toml").read_bytes()
    _stub(monkeypatch, _info(), _clients(("renamed", str(root), HOST)))
    rpw.main([])
    assert (env / "registry.local.toml").read_bytes() == before


def test_key_collision_with_different_client_writes_nothing_under_key(env, tmp_path, monkeypatch):
    other = _root(tmp_path, "other")
    elsewhere = _root(tmp_path, "elsewhere")
    env.mkdir(parents=True)
    (env / "registry.local.toml").write_text(
        f"\"external_workspaces.p4_a_b.path\" = '{elsewhere}'\n", encoding="utf-8"
    )
    before = (env / "registry.local.toml").read_bytes()
    _stub(monkeypatch, _info(), _clients(("a.b", str(other), HOST)))
    rpw.main([])
    assert (env / "registry.local.toml").read_bytes() == before


@pytest.mark.parametrize("which", ["info", "clients"])
def test_p4_failure_registers_nothing_exit_zero(env, tmp_path, monkeypatch, which):
    root = _root(tmp_path, "r")
    _stub(
        monkeypatch,
        _info(),
        _clients(("c", str(root), HOST)),
        info_ok=which != "info",
        clients_ok=which != "clients",
    )
    assert rpw.main([]) == 0
    assert _ext() == {}


def test_no_user_registers_nothing(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "r")
    _stub(monkeypatch, "exit: 0\n", _clients(("c", str(root), HOST)))
    assert rpw.main([]) == 0
    assert _ext() == {}


def test_discovery_exception_still_exits_zero(env, monkeypatch):
    def boom(args):
        raise RuntimeError("x")

    monkeypatch.setattr(rpw, "_run_discovery", boom)
    assert rpw.main([]) == 0


def test_at_most_two_discovery_calls_with_five_clients(env, tmp_path, monkeypatch):
    recs = [(f"c{i}", str(_root(tmp_path, f"r{i}")), HOST) for i in range(5)]
    calls: list = []
    _stub(monkeypatch, _info(), _clients(*recs), calls=calls)
    rpw.main([])
    assert len(calls) == 2
    assert calls[1] == ["clients", "-u", "alice"]
    assert len({k.split(".")[1] for k in _ext()}) == 5


def test_check_only_writes_nothing(env, tmp_path, monkeypatch, capsys):
    root = _root(tmp_path, "r")
    _stub(monkeypatch, _info(), _clients(("c", str(root), HOST)))
    assert rpw.main(["--check-only"]) == 0
    assert "would register external_workspaces.p4_c" in capsys.readouterr().out
    assert _ext() == {}
    assert not (env / "registry.local.toml").exists()


def test_venture_never_written(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "r")
    _stub(monkeypatch, _info(), _clients(("c", str(root), HOST)))
    rpw.main([])
    assert not any(k.endswith(".venture") for k in _ext())


def test_mutation_kill_switch_writes_nothing(env, tmp_path, monkeypatch):
    root = _root(tmp_path, "r")
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    _stub(monkeypatch, _info(), _clients(("c", str(root), HOST)))
    assert rpw.main([]) == 0
    assert _ext() == {}


def test_write_surface_validates():
    assert validate(rpw.WRITE_SURFACE) == ()
