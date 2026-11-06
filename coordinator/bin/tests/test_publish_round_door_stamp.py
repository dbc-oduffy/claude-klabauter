"""The round's door build bakes the round's scoped engine stamp, never the destination's.

Run: python -m pytest coordinator/bin/tests/test_publish_round_door_stamp.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_round_door_stamp_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@pytest.fixture
def calls(monkeypatch):
    from coordinator_core.install import door_install

    seen: list[dict] = []

    def fake(mirror_root, engine_root, **kwargs):
        seen.append(kwargs)
        return False

    monkeypatch.setattr(door_install, "rebuild_door_for_mirror", fake)
    monkeypatch.setattr(publish, "_seed_union_from_head", lambda *a, **k: None)
    return seen


@pytest.mark.parametrize("warm", [True, False])
def test_stamp_is_passed_on_warm_and_cold_rounds(calls, tmp_path, warm):
    publish._rebuild_round_door(tmp_path, tmp_path / "u", warm=warm, engine_stamp="sha:abc123")
    assert calls == [{"engine_stamp": "sha:abc123"}]


def test_no_stamp_keeps_the_destination_behaviour(calls, tmp_path):
    publish._rebuild_round_door(tmp_path, tmp_path / "u", warm=False)
    assert calls == [{}]


def _door_union(tmp_path, *, core_text, recorded_core):
    import hashlib
    import json

    union = tmp_path / "u"
    door = union / publish._DOOR_DIR_REL
    door.mkdir(parents=True)
    (door / "door.c").write_bytes(b"int main;\n")
    (door / "door_core.c").write_bytes(core_text)
    (door / "door.exe.provenance.json").write_text(
        json.dumps(
            {
                "sources": {
                    "door.c": hashlib.sha256(b"int main;\n").hexdigest(),
                    "door_core.c": hashlib.sha256(recorded_core).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return union


def test_unrebuilt_door_with_drifted_provenance_is_refused(calls, tmp_path, capsys):
    union = _door_union(tmp_path, core_text=b"scrubbed\n", recorded_core=b"original\n")
    ok, files = publish._rebuild_round_door(tmp_path, union, warm=False)
    assert (ok, files) == (False, {})
    err = capsys.readouterr().err
    assert "FATAL" in err and "door_core.c" in err


def test_unrebuilt_door_with_matching_provenance_stays_ok(calls, tmp_path, capsys):
    union = _door_union(tmp_path, core_text=b"same\n", recorded_core=b"same\n")
    assert publish._rebuild_round_door(tmp_path, union, warm=False) == (True, {})
    assert "FATAL" not in capsys.readouterr().err


def test_round_stamp_is_the_scoped_sha_of_the_round_pin(monkeypatch, tmp_path):
    engine = SimpleNamespace(source_dir=tmp_path / "coordinator_core")
    seen: dict = {}

    def pin(root, shas, **kw):
        seen["pin_root"] = root
        return "pinnedsha"

    def scoped(root, sha, **kw):
        seen["scoped_args"] = (root, sha)
        return "scopedsha"

    monkeypatch.setattr(publish, "_round_pin_source_sha", pin)
    monkeypatch.setattr(publish, "_scoped_engine_stamp_sha", scoped)
    assert publish._round_engine_stamp(engine, {}) == "sha:scopedsha"
    assert seen["scoped_args"] == (engine.source_dir, "pinnedsha")


def test_no_engine_row_yields_no_stamp_and_says_so(capsys):
    assert publish._round_engine_stamp(None, {}) is None
    assert "no engine row" in capsys.readouterr().out
