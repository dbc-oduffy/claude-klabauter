"""host_install_grok.apply: trust tables appended, existing bytes kept, idempotent, lock-gated, check mode."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from coordinator_core.install import host_install_grok as hig
from coordinator_core.install.host_install_agy import HostInstallRefused


@pytest.fixture(autouse=True)
def _mutation_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite sets COORDINATOR_DISABLE_MACHINE_MUTATION=1; these tests write only under tmp_path.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"PATH": str(tmp_path / "emptybin"), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.update(extra)
    return env


def _file(tmp_path: Path) -> Path:
    return tmp_path / ".grok" / "trusted_folders.toml"


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "repo-a", tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    return a, b


def test_fresh_home_writes_a_trusted_table_per_checkout(tmp_path: Path) -> None:
    a, b = _dirs(tmp_path)
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a, b], now=1700000000)
    assert r.mcp == hig.MCP_WRITTEN
    assert r.config_path == _file(tmp_path)
    folders = tomllib.loads(_file(tmp_path).read_text())["folders"]
    assert folders == {
        str(a.resolve()): {"trusted": True, "decided_at": 1700000000},
        str(b.resolve()): {"trusted": True, "decided_at": 1700000000},
    }
    assert isinstance(folders[str(a.resolve())]["decided_at"], int)
    assert not (tmp_path / ".grok" / hig.LOCK_FILE).exists()


def test_existing_tables_are_kept_byte_for_byte_and_not_redecided(tmp_path: Path) -> None:
    a, b = _dirs(tmp_path)
    pre = f"[folders.'{a.resolve()}']\ntrusted = false\ndecided_at = 5\n".encode()
    _file(tmp_path).parent.mkdir()
    _file(tmp_path).write_bytes(pre)
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a, b], now=9)
    assert r.mcp == hig.MCP_WRITTEN
    got = _file(tmp_path).read_bytes()
    assert got.startswith(pre)
    folders = tomllib.loads(got.decode())["folders"]
    assert folders[str(a.resolve())] == {"trusted": False, "decided_at": 5}
    assert folders[str(b.resolve())] == {"trusted": True, "decided_at": 9}


def test_missing_trailing_newline_still_parses(tmp_path: Path) -> None:
    a, _ = _dirs(tmp_path)
    _file(tmp_path).parent.mkdir()
    _file(tmp_path).write_bytes(b"[other]\nx = 1")
    hig.apply(tmp_path, _env(tmp_path), checkouts=[a], now=1)
    doc = tomllib.loads(_file(tmp_path).read_text())
    assert doc["other"] == {"x": 1}
    assert str(a.resolve()) in doc["folders"]


def test_rerun_is_byte_identical(tmp_path: Path) -> None:
    a, b = _dirs(tmp_path)
    hig.apply(tmp_path, _env(tmp_path), checkouts=[a, b], now=1)
    first = _file(tmp_path).read_bytes()
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a, b], now=2)
    assert r.mcp == hig.MCP_CURRENT
    assert _file(tmp_path).read_bytes() == first


def test_held_lock_skips_with_a_deferral_note_and_writes_nothing(tmp_path: Path) -> None:
    import os

    a, _ = _dirs(tmp_path)
    lock = tmp_path / ".grok" / hig.LOCK_FILE
    lock.parent.mkdir()
    lock.write_text("held")
    holder = os.open(lock, os.O_RDWR)
    try:
        assert hig._try_lock(holder)
        r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a])
        assert r.mcp == hig.MCP_SKIPPED
        assert any("deferred" in n and "held" in n for n in r.notes)
        assert not _file(tmp_path).exists()
    finally:
        hig._unlock(holder)
        os.close(holder)
    assert lock.read_text() == "held"


def test_persistent_unheld_lock_file_is_taken_by_advisory_lock_and_never_removed(tmp_path: Path) -> None:
    # Grok's other *.lock files persist between runs (advisory-lock files), so an existing unheld
    # lock must not block the write, and a lock this writer did not create is never unlinked.
    a, _ = _dirs(tmp_path)
    lock = tmp_path / ".grok" / hig.LOCK_FILE
    lock.parent.mkdir()
    lock.write_text("grok-owned")
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a], now=1)
    assert r.mcp == hig.MCP_WRITTEN
    assert str(a.resolve()) in tomllib.loads(_file(tmp_path).read_text())["folders"]
    assert lock.read_text() == "grok-owned"


def test_old_lock_is_not_removed_by_age(tmp_path: Path) -> None:
    import os
    import time

    a, _ = _dirs(tmp_path)
    lock = tmp_path / ".grok" / hig.LOCK_FILE
    lock.parent.mkdir()
    lock.write_text("old")
    old = time.time() - 86400
    os.utime(lock, (old, old))
    hig.apply(tmp_path, _env(tmp_path), checkouts=[a], now=1)
    assert lock.exists()


def test_check_mode_writes_nothing(tmp_path: Path) -> None:
    a, _ = _dirs(tmp_path)
    r = hig.apply(tmp_path, _env(tmp_path), check=True, checkouts=[a])
    assert r.mcp == hig.MCP_WOULD_WRITE
    assert not (tmp_path / ".grok").exists()


def test_path_with_single_quote_is_skipped_with_a_note(tmp_path: Path) -> None:
    quoted = tmp_path / "it's"
    quoted.mkdir()
    ok, _ = _dirs(tmp_path)
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[quoted, ok], now=1)
    assert r.mcp == hig.MCP_WRITTEN
    assert any("quote" in n for n in r.notes)
    assert list(tomllib.loads(_file(tmp_path).read_text())["folders"]) == [str(ok.resolve())]


def test_malformed_file_is_left_byte_identical(tmp_path: Path) -> None:
    a, _ = _dirs(tmp_path)
    _file(tmp_path).parent.mkdir()
    _file(tmp_path).write_bytes(b"[folders.\nnot toml")
    r = hig.apply(tmp_path, _env(tmp_path), checkouts=[a])
    assert r.mcp == hig.MCP_MALFORMED
    assert _file(tmp_path).read_bytes() == b"[folders.\nnot toml"
    assert not (tmp_path / ".grok" / hig.LOCK_FILE).exists()


def test_grok_home_override_wins(tmp_path: Path) -> None:
    a, _ = _dirs(tmp_path)
    gh = tmp_path / "elsewhere"
    r = hig.apply(tmp_path, _env(tmp_path, GROK_HOME=str(gh)), checkouts=[a], now=1)
    assert r.config_path == gh / "trusted_folders.toml"
    assert r.config_path.exists()
    assert not _file(tmp_path).exists()


def test_mutation_switch_refuses_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, _ = _dirs(tmp_path)
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    with pytest.raises(HostInstallRefused):
        hig.apply(tmp_path, _env(tmp_path), checkouts=[a])
    assert not _file(tmp_path).exists()
    assert not (tmp_path / ".grok" / hig.LOCK_FILE).exists()


def test_default_checkouts_include_the_claude_klabauter_root(tmp_path: Path) -> None:
    root = tmp_path / "claude-klabauter"
    root.mkdir()
    assert root in hig.default_checkouts(root)
