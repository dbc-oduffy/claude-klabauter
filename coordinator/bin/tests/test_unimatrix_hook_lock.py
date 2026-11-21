"""Concurrent SessionStart hooks must not overwrite each other's registrations."""
import json
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _hook() -> "ModuleType":
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "unimatrix-codex-hook-sessionstart.py"
    spec = importlib.util.spec_from_file_location("hook_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"name": "agy-dead", "vendor": "agy", "target": "dead-1", "extra": 1},
    {"name": "agy-live", "vendor": "agy", "target": "live-1"},
    {"name": "codex-same-target", "vendor": "codex", "target": "dead-1"},
]


def test_drop_peer_rows_removes_only_named_agy_targets_and_keeps_the_rest(tmp_path: Path) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    assert hook.drop_peer_rows(peers, {"dead-1", "ghost"}) == ["agy-dead"]
    assert json.loads(peers.read_text()) == ROWS[1:]


def test_drop_peer_rows_preserves_the_peers_container_shape(tmp_path: Path) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps({"version": 2, "peers": ROWS}))
    assert hook.drop_peer_rows(peers, {"dead-1"}) == ["agy-dead"]
    assert json.loads(peers.read_text()) == {"version": 2, "peers": ROWS[1:]}


def test_drop_peer_rows_writes_nothing_when_nothing_matches(tmp_path: Path) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    before = peers.stat().st_mtime_ns
    assert hook.drop_peer_rows(peers, {"ghost"}) == []
    assert peers.stat().st_mtime_ns == before
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


@pytest.mark.parametrize("content", ["{not json", '{"peers": "x"}', '"str"'])
def test_drop_peer_rows_never_writes_an_unparseable_or_non_list_file(tmp_path: Path, content) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(content)
    assert hook.drop_peer_rows(peers, {"dead-1"}) == []
    assert peers.read_text() == content


def test_drop_peer_rows_missing_file_is_a_noop(tmp_path: Path) -> None:
    hook = _hook()
    assert hook.drop_peer_rows(tmp_path / "peers.json", {"dead-1"}) == []
    assert not (tmp_path / "peers.json").exists()


def test_drop_peer_rows_runs_under_the_peers_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    seen = []
    real = hook._peers_lock

    import contextlib

    @contextlib.contextmanager
    def spy(path):
        seen.append(("enter", Path(path)))
        with real(path):
            yield
        seen.append(("exit", Path(path)))

    monkeypatch.setattr(hook, "_peers_lock", spy)
    hook.drop_peer_rows(peers, {"dead-1"})
    assert seen == [("enter", peers), ("exit", peers)]


def test_register_peer_still_registers_through_the_shared_lock(tmp_path: Path) -> None:
    hook = _hook()
    peers = tmp_path / "peers.json"
    assert hook.register_peer(peers, "p1", "codex", "t1") == "added"
    assert hook.register_peer(peers, "p1", "codex", "t1") == "present"
    assert [r["target"] for r in json.loads(peers.read_text())] == ["t1"]
