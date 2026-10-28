"""review.reverify classes a red as foreign when its cause lies only in another live run's declared
paths, and never when this run's own edited path implicates it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-live"
PREFIX = "prep-run-a"
BASE = "abc1234"
MOD = b"VALUE = 1\n"
SIBLING_TEST = "tests/test_sibling_owned.py"
SIBLING_MOD = "pkg/sibling_probe.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for rel, body in (
        ("pkg/mod.py", MOD),
        (SIBLING_MOD, b"X = 1\n"),
        (SIBLING_TEST, b"def test_a():\n    pass\n"),
        ("tests/test_imports_sibling.py", b"from pkg import sibling_probe\n\ndef test_b():\n    pass\n"),
        ("tests/test_own.py", b"from pkg import mod\n\ndef test_c():\n    pass\n"),
        ("tests/test_nobody.py", b"def test_d():\n    pass\n"),
    ):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / _sha(MOD)).write_bytes(MOD)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE, "recorded_at": "2026-10-19T00:00:00Z",
            "paths": ["pkg/mod.py"], "files": {"pkg/mod.py": _sha(MOD)},
        }),
        encoding="utf-8",
    )
    # Review edited pkg/mod.py after the snapshot, so tests importing it are this run's.
    (tmp_path / "pkg/mod.py").write_bytes(b"VALUE = 2\n")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(
        review_edits, "live_sibling_declared",
        lambda *_a, **_k: {SIBLING_TEST: "other-run", SIBLING_MOD: "other-run"},
    )
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    return tmp_path


def _revert(world, nodes):
    return rr.review_reverify(world, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=nodes)


def test_red_in_sibling_owned_test_file_is_foreign(world):
    out = _revert(world, [f"{SIBLING_TEST}::test_a"])
    assert out["error"] is None
    assert out["foreign"] == [f"{SIBLING_TEST}::test_a"]
    assert out["reverted"] == []


def test_red_importing_sibling_owned_module_is_foreign(world):
    node = "tests/test_imports_sibling.py::test_b"
    assert _revert(world, [node])["foreign"] == [node]


def test_red_naming_sibling_owned_path_by_literal_is_foreign(world):
    node = "tests/test_names_sibling.py::test_e"
    (world / "tests/test_names_sibling.py").write_text(
        f'PATH = "{SIBLING_MOD}"\n\ndef test_e():\n    pass\n', encoding="utf-8"
    )
    assert _revert(world, [node])["foreign"] == [node]


def test_red_implicating_own_edited_path_is_not_foreign(world):
    out = _revert(world, ["tests/test_own.py::test_c"])
    assert out["foreign"] == []


def test_red_owned_by_nobody_is_not_foreign(world):
    out = _revert(world, ["tests/test_nobody.py::test_d"])
    assert out["foreign"] == []
    assert out["unattributed"] == ["tests/test_nobody.py::test_d"]
