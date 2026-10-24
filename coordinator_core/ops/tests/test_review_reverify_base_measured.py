"""review.reverify marks an id pre_existing only when it is also red at the run base."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-base"
PREFIX = "prep-base"
BASE = "abc1234"
FAILING = ["tests/test_mod.py::test_v"]
SIDECARS = ["review.md"]
SNAPSHOT = {
    "pkg/mod.py": b"VALUE = 1\n",
    "tests/test_mod.py": b"from pkg.mod import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    for rel, data in SNAPSHOT.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        (snap / _sha(data)).write_bytes(data)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": BASE,
            "recorded_at": "2026-10-24T00:00:00Z",
            "paths": list(SNAPSHOT),
            "files": {rel: _sha(data) for rel, data in SNAPSHOT.items()},
        }),
        encoding="utf-8",
    )
    (tmp_path / "pkg/mod.py").write_bytes(b"VALUE = 1\n_unused = 2\n")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(
        review_findings_ledger,
        "read_ledger",
        lambda _p: (True, [{"id": "F1", "file": "pkg/mod.py", "before": "unused = 2\n", "after": "_unused = 2\n"}]),
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("review.reverify spawned"))
    )
    return tmp_path


def _call(root, **kw):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, **kw)


def _closed_probe(root):
    assert _call(root, failing=FAILING, sidecars=SIDECARS, probe="open")["error"] is None
    assert _call(root, failing=FAILING, probe="close")["error"] is None


def test_red_before_and_under_probe_but_not_at_base_is_not_pre_existing(world):
    _closed_probe(world)
    out = _call(world, failing=FAILING, failing_before=FAILING, failing_at_base=[], sidecars=SIDECARS)
    assert out["error"] is None and out["pre_existing"] == []


def test_red_in_all_three_is_pre_existing(world):
    _closed_probe(world)
    out = _call(world, failing=FAILING, failing_before=FAILING, failing_at_base=FAILING, sidecars=SIDECARS)
    assert out["error"] is None and out["pre_existing"] == FAILING


def test_absent_failing_at_base_vouches_for_nothing(world):
    _closed_probe(world)
    out = _call(world, failing=FAILING, failing_before=FAILING, sidecars=SIDECARS)
    assert out["error"] is None and out["pre_existing"] == []


def _mod_bytes(root):
    return (root / "pkg/mod.py").read_bytes()


def test_red_at_base_without_probe_is_pre_existing_and_not_reverted(world):
    before = _mod_bytes(world)
    out = _call(world, failing=FAILING, failing_at_base=FAILING, sidecars=SIDECARS)
    assert out["error"] is None
    assert out["pre_existing"] == FAILING
    assert out["unattributed"] == [] and out["reverted"] == []
    assert _mod_bytes(world) == before


def test_pre_existing_id_implicating_an_applied_finding_is_not_inverted(world):
    before = _mod_bytes(world)
    out = _call(world, failing=FAILING, failing_before=FAILING, failing_at_base=FAILING, sidecars=SIDECARS)
    assert out["pre_existing"] == FAILING and out["reverted"] == []
    assert _mod_bytes(world) == before
    other = ["tests/test_mod.py::test_other"]
    out = _call(world, failing=other, failing_at_base=[], sidecars=SIDECARS)
    assert out["pre_existing"] == []


def test_at_base_id_outside_failing_is_never_reported(world):
    out = _call(world, failing=FAILING, failing_at_base=["tests/test_x.py::test_z"], sidecars=SIDECARS)
    assert out["pre_existing"] == []


def test_record_carries_pre_existing(world):
    out = _call(world, failing=FAILING, failing_at_base=FAILING, sidecars=SIDECARS)
    record = json.loads((world / out["record"]).read_text(encoding="utf-8"))
    assert record["pre_existing"] == FAILING


def test_handler_refuses_non_list_failing_at_base(world):
    out = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": FAILING, "failing_at_base": "x"},
        world,
    )
    assert "failing_at_base" in out["error"]
