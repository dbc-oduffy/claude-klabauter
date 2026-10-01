"""rebuild_door_for_mirror: the publish mirror's door is rebuilt from the
mirror's own transformed sources, and a missing compiler fails loudly."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from coordinator_core.install import door_install
from coordinator_core.warm.door import build as real_build

_STUB_BUILD = r'''
import hashlib, json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SOURCES = tuple(_DIR / n for n in ("door.c", "door_core.c", "door_core.h", "door_env_set.h"))
_PROVENANCE_SUFFIX = ".provenance.json"

def source_sha256(path):
    return hashlib.sha256(path.read_bytes().replace(bytes([13]), b"")).hexdigest()

def build(engine_root, *, python_bin=None, compiler=None, output=None):
    Path(output).write_bytes(b"MZ-rebuilt")
    (Path(output).parent / (Path(output).name + _PROVENANCE_SUFFIX)).write_text(
        json.dumps({"sources": {p.name: source_sha256(p) for p in SOURCES}}))
    (_DIR / "call.json").write_text(json.dumps(
        {"engine_root": str(engine_root), "output": str(output)}))
    return output
'''


def _mirror(tmp_path: Path, build_source: str) -> Path:
    door_dir = tmp_path / "mirror" / "coordinator_core" / "warm" / "door"
    door_dir.mkdir(parents=True)
    for name in ("door.c", "door_core.c", "door_core.h", "door_env_set.h"):
        (door_dir / name).write_text(f"// transformed {name} CONTENT_ROOT\n", encoding="utf-8")
    (door_dir / "door.exe").write_bytes(b"MZ-stale")
    (door_dir / "build.py").write_text(build_source, encoding="utf-8")
    return tmp_path / "mirror"


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


def test_rebuild_compiles_the_mirrors_sources_and_bakes_the_published_root(tmp_path):
    mirror = _mirror(tmp_path, _STUB_BUILD)
    published_root = tmp_path / "klabauter"

    assert door_install.rebuild_door_for_mirror(mirror, published_root) is True

    door_dir = mirror / "coordinator_core" / "warm" / "door"
    call = json.loads((door_dir / "call.json").read_text())
    assert call["engine_root"] == str(published_root)
    assert Path(call["output"]).parent != door_dir
    assert (door_dir / "door.exe").read_bytes() == b"MZ-rebuilt"
    assert (door_dir / "door.exe.provenance.json").exists()
    assert not (door_dir / "door.engine-root.txt").exists()


def test_missing_compiler_fails_the_round_naming_the_cause(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path, Path(real_build.__file__).read_text(encoding="utf-8"))
    engine_root = tmp_path / "klabauter"
    (engine_root / "coordinator_core").mkdir(parents=True)
    (engine_root / "coordinator_core" / "_engine_stamp").write_text("x")
    monkeypatch.setenv("PATH", "")

    with pytest.raises(door_install.MirrorDoorRebuildError, match="no C compiler found"):
        door_install.rebuild_door_for_mirror(mirror, engine_root)

    door_dir = mirror / "coordinator_core" / "warm" / "door"
    assert (door_dir / "door.exe").read_bytes() == b"MZ-stale"
