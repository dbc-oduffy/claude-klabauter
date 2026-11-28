"""atomic_write_bytes leaves no temp behind a killed writer or an interrupted write."""

import os
from pathlib import Path

import pytest

from coordinator_core import atomic_replace
from coordinator_core.atomic_replace import atomic_write_bytes

REPO = Path(__file__).resolve().parents[2]


def _temps(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.startswith(".atomic-write."))


def test_temp_name_carries_target_and_pid(tmp_path, monkeypatch):
    seen = []
    real = atomic_replace._replace_with_retry

    def spy(src, dst):
        seen.append(Path(src).name)
        real(src, dst)

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", spy)
    atomic_write_bytes(tmp_path / "out.txt", b"x")
    assert seen[0].startswith(f".atomic-write.out.txt.{os.getpid()}.")
    assert _temps(tmp_path) == []


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_base_exception_unlinks_temp(tmp_path, monkeypatch, exc):
    def boom(src, dst):
        raise exc()

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", boom)
    with pytest.raises(exc):
        atomic_write_bytes(tmp_path / "out.txt", b"x")
    assert _temps(tmp_path) == []
    assert not (tmp_path / "out.txt").exists()


def test_sweep_parses_dotted_target_names_and_spares_malformed_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_replace, "_pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(atomic_replace, "_last_sweep", {})
    dead = tmp_path / ".atomic-write.my.dotted.name.txt.999999.abcd"
    live = tmp_path / f".atomic-write.my.dotted.name.txt.{os.getpid()}.abcd"
    malformed = tmp_path / ".atomic-write.nopid"
    for p in (dead, live, malformed):
        p.write_bytes(b"x")
    atomic_write_bytes(tmp_path / "out.txt", b"y")
    assert not dead.exists()
    assert live.exists()
    assert malformed.exists()
