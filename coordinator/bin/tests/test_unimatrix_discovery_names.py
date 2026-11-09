"""Discovered peers retain repository identity and respect configured bindings."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest


BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "discovery_names_router")
codex = _load("unimatrix-peer-codex.py", "discovery_names_codex")


def _database(path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, source TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?, ?, 'cli')", rows)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def rollouts_exist(monkeypatch):
    """Short fixture ids cannot name a rollout file; the rollout gate is stubbed open."""
    monkeypatch.setattr(router, "codex_rollout_addressable", lambda home, ident, index: True)


@pytest.fixture(autouse=True)
def engine_addresses(monkeypatch):
    """Discovery admits only Codex threads with an engine address; each fixture id gets one."""
    def read(directory, ident, box):
        return {"schema_version": 1, "provider": "codex", "session_id": ident,
                "address_version": 1, "name": f"claude-klabauter-codex-test-{ident}", "box": box}
    monkeypatch.setattr(router._contract(), "read_codex_address", read)


def test_sqlite_metadata_enriches_live_names_without_duplicating_fixed_targets(tmp_path, monkeypatch):
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    for ident in ("configured", "live", "other", "stale"):
        (locks / f"{ident}.lock").touch()
    _database(tmp_path / "state_5.sqlite", [
        ("configured", "/work/claude-klabauter"),
        ("live", "/work/coordinator-content-repo"),
        ("other", "/work/coordinator-content-repo"),
        ("stale", "/work/old-project"),
    ])
    monkeypatch.setattr(router, "lock_held", lambda path: not str(path).endswith("stale.lock"))
    peers = router.load_lock_peers(locks, "codex", taken_targets={"configured"})
    by_target = {peer["target"]: peer for peer in peers}
    assert set(by_target) == {"live", "other"}
    assert len({peer["name"] for peer in peers}) == 2
    assert all(any(alias.startswith("codex-coordinator-content-repo-") for alias in peer["aliases"]) for peer in peers)
    assert all(peer["cwd"] == "/work/coordinator-content-repo" for peer in peers)
    assert peers == router.load_lock_peers(locks, "codex", taken_targets={"configured"})


def test_unreadable_database_does_not_hide_live_sessions(tmp_path, monkeypatch):
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    (locks / "live.lock").touch()
    (tmp_path / "state_5.sqlite").write_bytes(b"not a database")
    monkeypatch.setattr(router, "lock_held", lambda path: True)
    monkeypatch.setattr(router, "codex_rollout_identity", lambda home, ident, index: {"source": "cli"})
    peers = router.load_lock_peers(locks, "codex")
    assert len(peers) == 1 and peers[0]["target"] == "live"
    assert any(alias.startswith("codex-session-") for alias in peers[0]["aliases"])
    assert not (tmp_path / "state_5.sqlite-journal").exists()


def test_subagent_writer_locks_are_not_independently_messageable_peers(tmp_path, monkeypatch):
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    records = [
        ("root", "/work/claude-klabauter", '"cli"', "user", None),
        ("source-child", "/work/claude-klabauter", json.dumps({"subagent": {
            "thread_spawn": {"parent_thread_id": "root"}}}), None, None),
        ("source-kind-child", "/work/claude-klabauter", None, "subagent", None),
        ("path-child", "/work/claude-klabauter", None, None, "/root/reviewer"),
        ("root-path", "/work/claude-klabauter", '"cli"', None, "/root"),
    ]
    connection = sqlite3.connect(tmp_path / "state_5.sqlite")
    try:
        connection.execute("CREATE TABLE threads (id TEXT, cwd TEXT, source TEXT, thread_source TEXT, agent_path TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", records)
        connection.commit()
    finally:
        connection.close()
    for ident, *_ in records:
        (locks / f"{ident}.lock").touch()
    monkeypatch.setattr(router, "lock_held", lambda path: True)
    peers = router.load_lock_peers(locks, "codex")
    assert {peer["target"] for peer in peers} == {"root", "root-path"}
    # Identity metadata remains available even when a child is omitted from the messaging roster.
    assert set(router.codex_cwds(tmp_path)) == {record[0] for record in records}


def test_agy_leg_metadata_wins_over_presence_for_the_same_conversation(tmp_path, monkeypatch):
    legs = tmp_path / "agy-legs"
    presence = tmp_path / "presence"
    legs.mkdir()
    presence.mkdir()
    (legs / "conversation.json").write_text(json.dumps({
        "conversation": "conversation", "pid": os.getpid(), "cwd": "/work/claude-klabauter"}))
    (presence / "conversation.lock").touch()
    monkeypatch.setattr(router, "lock_held", lambda path: True)
    roster = router.Router([], sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                           legs_dir=legs, presence_dir=presence)
    peers = list(roster._live_discovered().values())
    assert len(peers) == 1
    assert peers[0]["target"] == "conversation"
    assert peers[0]["cwd"] == "/work/claude-klabauter"
    assert peers[0]["name"].startswith("agy-claude-klabauter-")


def test_database_versions_are_numeric(tmp_path):
    _database(tmp_path / "state_9.sqlite", [("live", "/work/old-project")])
    _database(tmp_path / "state_10.sqlite", [("live", "/work/current-project")])
    assert router.codex_cwds(tmp_path) == {"live": "/work/current-project"}


def test_repository_root_names_sessions_started_in_subdirectories(tmp_path):
    repo = tmp_path / "claude-klabauter"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "Plugins" / "Feature"
    nested.mkdir(parents=True)
    assert router.readable_peer_name("codex", "thread", str(nested)).startswith("codex-claude-klabauter-")


def test_repository_root_accepts_worktree_git_files(tmp_path):
    repo = tmp_path / "claude-klabauter-fix"
    nested = repo / "coordinator" / "bin"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /some/primary/.git/worktrees/fix\n")
    assert router.readable_peer_name("codex", "thread", str(nested)).startswith("codex-claude-klabauter-fix-")


def test_malformed_cwd_metadata_does_not_break_discovery():
    assert router.readable_peer_name("agy", "conversation", {"bad": "cwd"}).startswith("agy-session-")


def test_metadata_connections_are_closed_between_scans(tmp_path, monkeypatch):
    _database(tmp_path / "state_5.sqlite", [("live", "/work/repo")])
    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert router.codex_cwds(tmp_path) == {"live": "/work/repo"}
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_explicit_native_codex_path_is_preserved(tmp_path, monkeypatch):
    configured = str(tmp_path / "native codex.exe")
    monkeypatch.setenv("CODEX_BIN", configured)
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    assert codex.codex_binary() == configured


@pytest.mark.skipif(os.name != "nt", reason="Windows npm layout")
def test_npm_codex_wrapper_resolves_native_binary(tmp_path, monkeypatch):
    wrapper = tmp_path / "codex.CMD"
    wrapper.touch()
    native = (tmp_path / "node_modules/@openai/codex/node_modules/@openai/"
              "codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(wrapper))
    assert codex.codex_binary() == str(native)


@pytest.mark.skipif(os.name != "nt", reason="Windows npm layout")
def test_missing_native_binary_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(tmp_path / "codex.cmd"))
    with pytest.raises(RuntimeError, match="CODEX_BIN"):
        codex.codex_binary()
