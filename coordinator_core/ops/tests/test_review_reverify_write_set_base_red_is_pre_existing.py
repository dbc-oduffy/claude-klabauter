"""review.reverify measures a write-set red at the run base before counting it owned."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops import reverify_base_measure as rbm
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit import self_correction as sc
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-wsbase"
PREFIX = "prep-wsbase"
BASE = "a" * 40
TEST_FILE = "tests/test_install.py"
RED = f"{TEST_FILE}::test_red_at_base"
GREEN = f"{TEST_FILE}::test_green_at_base"
SNAPSHOT = {
    "pkg/mod.py": b"VALUE = 1\n",
    TEST_FILE: b"def test_red_at_base():\n    assert False\n\n\ndef test_green_at_base():\n    assert True\n",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    (tmp_path / TEST_FILE).write_bytes(SNAPSHOT[TEST_FILE] + b"# run edit\n")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(rr, "_rerun_alone", lambda _root, _ids: [])
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, []))
    return tmp_path


def _measure_stub(monkeypatch: pytest.MonkeyPatch, **result) -> list:
    calls: list = []

    def fake(sha, head_sha, ids, cwd, timeout_s=None):
        calls.append((sha, list(ids)))
        return {"red": [], "export": "ok", "runner_exit": 1, "passed_at_base": [], **result}

    monkeypatch.setattr(rbm, "measure", fake)
    return calls


def _call(root, **kw):
    return rr.review_reverify(root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, **kw)


def test_write_set_red_failing_at_base_is_pre_existing_and_classified_foreign(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=[RED], passed_at_base=[GREEN])
    out = _call(world, failing=[RED, GREEN], failing_at_base=[])
    assert out["error"] is None
    assert out["pre_existing"] == [RED]
    assert out["unattributed"] == [] and out["reverted"] == []
    assert calls == [(BASE, [RED, GREEN])]  # one batch for every unmeasured id
    split = sc.classify_reds([RED], [TEST_FILE], pre_existing=out["pre_existing"], reverted=out["reverted"])
    assert split.foreign == (RED,) and split.owned == ()
    record = json.loads((world / out["record"]).read_text(encoding="utf-8"))
    assert record["base_measured"] == {"red": [RED], "green": [GREEN]}


def test_write_set_red_green_at_base_stays_owned(world, monkeypatch) -> None:
    _measure_stub(monkeypatch, passed_at_base=[GREEN])
    out = _call(world, failing=[GREEN])
    assert out["pre_existing"] == []
    assert out["base_measured_here"] == [GREEN] and out["base_measure_error"] is None
    split = sc.classify_reds([GREEN], [TEST_FILE], pre_existing=out["pre_existing"], reverted=out["reverted"])
    assert split.owned == (GREEN,)


def test_unmeasurable_base_keeps_the_owned_verdict_and_says_so(world, monkeypatch) -> None:
    _measure_stub(monkeypatch, export="failed", runner_exit=None)
    out = _call(world, failing=[RED])
    assert out["pre_existing"] == []
    assert out["base_measure_error"] == "base export failed"
    split = sc.classify_reds([RED], [TEST_FILE], pre_existing=out["pre_existing"], reverted=out["reverted"])
    assert split.owned == (RED,)


def test_measure_fault_never_raises_and_keeps_owned(world, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("export denied")

    monkeypatch.setattr(rbm, "measure", boom)
    out = _call(world, failing=[RED])
    assert out["error"] is None and out["pre_existing"] == []
    assert "export denied" in out["base_measure_error"]


def test_a_measured_verdict_is_not_measured_again(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=[RED])
    assert _call(world, failing=[RED])["pre_existing"] == [RED]
    assert _call(world, failing=[RED])["pre_existing"] == [RED]
    assert len(calls) == 1


def test_caller_supplied_base_verdict_skips_the_measurement(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=[RED])
    out = _call(world, failing=[RED], failing_at_base=[RED])
    assert out["pre_existing"] == [RED] and calls == []
    assert "base_measured_here" not in out


def test_a_named_but_unreadable_base_export_is_never_remeasured(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=[RED])
    out = _call(world, failing=[RED], base_export_path=str(world / "missing.txt"))
    assert out["pre_existing"] == [] and calls == []
    assert out["base_unmeasured"] is True


def test_a_red_outside_the_write_set_is_never_measured(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=["tests/test_elsewhere.py::test_x"])
    out = _call(world, failing=["tests/test_elsewhere.py::test_x"], failing_at_base=[])
    assert out["pre_existing"] == [] and calls == []


def test_a_base_that_is_not_a_full_sha_is_unmeasurable(world, monkeypatch) -> None:
    calls = _measure_stub(monkeypatch, red=[RED])
    manifest_path = next(Path(share_dir(str(world), SESSION)).glob("*.pre-review")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["base"] = "abc1234"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = rr.review_reverify(world, base="abc1234", slice_prefix=PREFIX, session_id=SESSION, failing=[RED])
    assert out["pre_existing"] == [] and calls == []
    assert out["base_measure_error"] == "no 40-hex run base to measure at"
