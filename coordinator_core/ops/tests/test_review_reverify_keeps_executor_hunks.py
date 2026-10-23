"""review.reverify keeps the executor's pre-review hunks: it inverts only finding-attributed review
edits, and a failure that also fails with the findings undone is recorded as pre-existing."""

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

SESSION = "sess-own"
PREFIX = "prep-keep"
BASE = "abc1234"
FAILING = ["tests/test_mod.py::test_v"]
SIDECARS = ["review.md"]

SNAPSHOT = {
    "pkg/mod.py": b"VALUE = 1\n",
    "tests/test_mod.py": b"from pkg.mod import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n",
}
EXECUTOR = "EXECUTOR_HUNK = 'product'\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _row(fid, before, after):
    return {"id": fid, "file": "pkg/mod.py", "before": before, "after": after}


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
            "recorded_at": "2026-10-19T00:00:00Z",
            "paths": list(SNAPSHOT),
            "files": {rel: _sha(data) for rel, data in SNAPSHOT.items()},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("review.reverify spawned"))
    )
    return tmp_path


def _ledger(monkeypatch, *rows):
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, list(rows)))


def _call(root, **kw):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, **kw)


def _probe(root, still_failing_under_probe):
    """Open and close a probe; return ``still_failing_under_probe``, the ids the test-runner's
    re-run (not executed here) reports red, so the caller passes them as ``failing_before``."""
    opened = _call(root, failing=FAILING, sidecars=SIDECARS, probe="open")
    assert opened["error"] is None and opened["rerun"] == FAILING
    closed = _call(root, failing=FAILING, probe="close")
    assert closed["error"] is None
    return still_failing_under_probe


def test_nit_review_edit_in_a_file_whose_pre_review_state_already_fails_keeps_every_hunk(world, monkeypatch):
    root = world
    post = ("VALUE = 1\n" + EXECUTOR + "_unused = 2\n").encode()
    (root / "pkg/mod.py").write_bytes(post)
    _ledger(monkeypatch, _row("F-nit", "unused = 2\n", "_unused = 2\n"))

    failing_before = _probe(root, FAILING)
    assert (root / "pkg/mod.py").read_bytes() == post

    out = _call(root, failing=FAILING, failing_before=failing_before, sidecars=SIDECARS)

    assert out["error"] is None
    assert (root / "pkg/mod.py").read_bytes() == post
    assert out["reverted"] == [] and out["kept"] == []
    assert out["pre_existing"] == FAILING and out["unattributed"] == []
    record = json.loads((root / out["record"]).read_text(encoding="utf-8"))
    assert record["pre_existing"] == FAILING and record["reverted"] == []


def test_review_edit_that_newly_breaks_a_test_reverts_only_that_edits_hunks(world, monkeypatch):
    root = world
    mod = root / "pkg/mod.py"
    mod.write_text("VALUE = 2\n" + EXECUTOR + "NAME = 'renamed'\nGONE = 3\n", encoding="utf-8")
    _ledger(
        monkeypatch,
        _row("F1", "VALUE = 1\n", "VALUE = 2\n"),
        _row("F2", "NAME = 'old'\n", "NAME = 'renamed'\n"),
        _row("F3", "LATER = 4\n", "LATER = 5\n"),
    )

    failing_before = _probe(root, [])
    out = _call(root, failing=FAILING, failing_before=failing_before, sidecars=SIDECARS)

    assert out["error"] is None
    assert mod.read_text(encoding="utf-8") == "VALUE = 1\n" + EXECUTOR + "NAME = 'old'\nGONE = 3\n"
    [entry] = out["reverted"]
    assert (entry["path"], entry["action"], entry["source"]) == ("pkg/mod.py", "reverted-findings", "findings")
    assert entry["findings"] == [{"sidecar": "review.md", "id": "F1"}, {"sidecar": "review.md", "id": "F2"}]
    assert out["kept"] == [{
        "path": "pkg/mod.py",
        "tests": FAILING,
        "findings": [{"sidecar": "review.md", "id": "F3", "reason": "after-absent"}],
    }]
    assert out["pre_existing"] == []


def test_failing_before_without_a_closed_probe_is_ignored_and_the_id_still_reverts(world, monkeypatch):
    root = world
    mod = root / "pkg/mod.py"
    mod.write_text("VALUE = 2\n" + EXECUTOR, encoding="utf-8")
    _ledger(monkeypatch, _row("F1", "VALUE = 1\n", "VALUE = 2\n"))

    out = _call(root, failing=FAILING, failing_before=FAILING, sidecars=SIDECARS)

    assert out["error"] is None and out["pre_existing"] == []
    assert mod.read_text(encoding="utf-8") == "VALUE = 1\n" + EXECUTOR
    assert [e["path"] for e in out["reverted"]] == ["pkg/mod.py"]


def test_failing_before_naming_an_id_the_closed_probe_did_not_re_run_still_reverts(world, monkeypatch):
    root = world
    mod = root / "pkg/mod.py"
    mod.write_text("VALUE = 2\n" + EXECUTOR, encoding="utf-8")
    _ledger(monkeypatch, _row("F1", "VALUE = 1\n", "VALUE = 2\n"))
    _probe(root, [])

    other = ["tests/test_mod.py::test_other"]
    out = _call(root, failing=FAILING + other, failing_before=other, sidecars=SIDECARS)

    assert out["pre_existing"] == []
    assert mod.read_text(encoding="utf-8") == "VALUE = 1\n" + EXECUTOR
