"""review.reverify list mode returns an unavailable tripwire scan as error, never a guessed set."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.review_tripwires import TripwireScanUnavailable
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-own"
PREFIX = "prep-t"
BASE = "abc1234"


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = b"VALUE = 1\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / sha).write_bytes(data)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE,
            "recorded_at": "2026-10-19T00:00:00Z",
            "paths": ["pkg/mod.py"],
            "files": {"pkg/mod.py": sha},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    (tmp_path / "pkg/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    return tmp_path


def _list(root):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION)


def test_unavailable_scan_is_an_error_with_other_fields_kept(root, monkeypatch):
    def unavailable(*_a, **_k):
        raise TripwireScanUnavailable("split index")

    monkeypatch.setattr(rr, "tracked_test_bytes", unavailable)
    out = _list(root)
    assert out["error"] == "tripwire scan unavailable: split index"
    assert out["degraded"] == []
    assert out["tripwires"] == []
    assert out["edited"] == ["pkg/mod.py"]
    assert out["created"] == []
    assert out["foreign"] == []
    assert out["snapshot"].endswith(f"{PREFIX}.pre-review/manifest.json")


def test_a_failed_comment_scan_stays_an_error_beside_a_degraded_overrun(root, monkeypatch):
    def land(_root, _base, notes=None, degraded=None, paths=None):
        notes.append("comment scan failed: OSError")
        degraded.append("comment scan incomplete: 1 file(s) over the 0.3s budget not lexed")
        return [], []

    monkeypatch.setattr(rr, "tracked_test_bytes", lambda *_a, **_k: [])
    monkeypatch.setattr(rr, "_land_findings", land)
    out = _list(root)
    assert out["error"] == "comment scan failed: OSError"
    assert out["degraded"] == ["comment scan incomplete: 1 file(s) over the 0.3s budget not lexed"]


def test_available_scan_has_no_error(root, monkeypatch):
    monkeypatch.setattr(rr, "tracked_test_bytes", lambda *_a, **_k: [("tests/test_x.py", b'X = "pkg/mod.py"')])
    out = _list(root)
    assert out["error"] is None
    assert out["tripwires"] == ["tests/test_x.py"]


def _with_created(monkeypatch, created):
    monkeypatch.setattr(rr, "_created", lambda *_a, **_k: (list(created), []))
    monkeypatch.setattr(rr, "tracked_test_bytes", lambda *_a, **_k: [])


def test_list_unions_tree_scanners_when_created(root, monkeypatch):
    _with_created(monkeypatch, ["pkg/new.py"])
    seen = {}

    def scan(_root, created, exclude=()):
        seen["created"], seen["exclude"] = list(created), set(exclude)
        return ["tests/test_walk.py"]

    monkeypatch.setattr(rr, "tree_scanners", scan)
    (root / "pkg/new.py").write_text("X = 1\n", encoding="utf-8")
    out = _list(root)
    assert out["tripwires"] == ["tests/test_walk.py"]
    assert seen["created"] == ["pkg/new.py"]
    assert {"pkg/mod.py", "pkg/new.py"} <= seen["exclude"]


def test_list_skips_tree_scanners_without_created(root, monkeypatch):
    _with_created(monkeypatch, [])
    monkeypatch.setattr(rr, "tree_scanners", lambda *_a, **_k: pytest.fail("scanned"))
    assert _list(root)["tripwires"] == []


def test_unavailable_tree_scan_is_an_error(root, monkeypatch):
    _with_created(monkeypatch, ["pkg/new.py"])

    def unavailable(*_a, **_k):
        raise TripwireScanUnavailable("split index")

    monkeypatch.setattr(rr, "tree_scanners", unavailable)
    (root / "pkg/new.py").write_text("X = 1\n", encoding="utf-8")
    out = _list(root)
    assert out["error"] == "tripwire scan unavailable: split index"
    assert out["tripwires"] == []


def test_a_fix_legs_later_write_is_judged_from_its_live_bytes_not_the_manifest_digest(root, monkeypatch):
    monkeypatch.setattr(rr, "tracked_test_bytes", lambda *_a, **_k: [])
    # `root` already holds the review edit (VALUE = 2); the fix leg then rewrites the same path.
    (root / "pkg/mod.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert _list(root)["edited"] == ["pkg/mod.py"]

    # A fix leg that returns the file to the snapshot's own bytes leaves nothing edited.
    (root / "pkg/mod.py").write_bytes(b"VALUE = 1\n")
    assert _list(root)["edited"] == []


def test_implication_names_created_paths_under_walked_dir(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_walk.py").write_text('ROOT = "pkg"\nlist(ROOT.rglob("*"))\n', encoding="utf-8")
    (tmp_path / "tests/test_other.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(rr, "tree_scanners", lambda *_a, **_k: ["tests/test_walk.py"])
    out = rr._implication(
        tmp_path, ["tests/test_walk.py::t", "tests/test_other.py::t"], [], ["pkg/new.py", "other/x.py"]
    )
    assert out["tests/test_walk.py::t"] == {"pkg/new.py"}
    assert out["tests/test_other.py::t"] == set()
