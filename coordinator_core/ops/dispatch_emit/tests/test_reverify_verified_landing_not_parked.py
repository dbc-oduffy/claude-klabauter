"""A landing whose run already passed its overlay verify is not parked at `reverify_on_mismatch` by peer
churn in files the run never wrote: the verdict is keyed on the run's own blobs plus the base sha, and a
legacy verdict (no recorded components) is cleared only against the paths the run itself declared, using the
stored `measured-rev.json` it still hashes to. A change to a declared path's landed blob still mismatches
and parks."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import kept_work, land_deadline
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RUN = "run1"
SEEDS = ("t/test_x.py::test_a",)


def _measured(blob: str = "1" * 40, own: str = "1" * 40, sha: str = "a" * 40) -> dict:
    return {
        "sha": sha,
        "base_sha": "b" * 40,
        "paths": ["src/x.py", "src/shared.py"],
        "blobs": {"src/x.py": blob, "src/shared.py": "2" * 40},
        "own_blobs": {"src/x.py": own},
        "peer_hunks": {"src/shared.py": {"owner": "peer-run", "lines": "3,2", "split": False}},
    }


def _verdict(root: Path, measured: dict, *, legacy: bool = False) -> None:
    """Write a green verdict; `legacy` rewrites it as a pre-components file keyed by `legacy_landing_key`
    and stores `measured` as the run's `measured-rev.json`."""
    cov.write_verdict(root, RUN, measured, cov.OverlayVerdict("green", "ok", SEEDS))
    if legacy:
        path = cov.verdict_path(root, RUN)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["keyed"]
        data["key"] = cov.legacy_landing_key(measured)
        path.write_text(json.dumps(data), encoding="utf-8")
        record = measured_rev_record_path(root, RUN)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(measured), encoding="utf-8")


def _reverify(root: Path, measured: dict) -> tuple:
    spawned: list = []
    stale = cov.OverlayVerdict("mismatch", "stale", SEEDS)
    try:
        out = cov.reverify_on_mismatch(root, RUN, measured, stale, spawn=lambda *a: spawned.append(a) or True)
    except land_deadline.LandDeadlineExceeded:
        return None, spawned
    return out, spawned


def _legacy_measured(root: Path, *, now_x: str, now_shared: str = "2" * 40, landed_x: str = "1" * 40) -> dict:
    """A legacy verdict over `_measured()`, the run's kept blob for `src/x.py`, and a current record whose
    `src/x.py` and `src/shared.py` blobs are the given ones."""
    _verdict(root, _measured(), legacy=True)
    kept_work.write(cov.verdict_path(root, RUN).parent, {"c1": {"paths": {"src/x.py": landed_x}}}, None)
    current = _measured(sha="c" * 40)
    current["own_blobs"] = {}
    current["blobs"] = {"src/x.py": now_x, "src/shared.py": now_shared}
    return current


def test_a_peer_edit_to_a_shared_path_keeps_the_green_verdict_and_never_spawns(tmp_path):
    _verdict(tmp_path, _measured())
    moved = _measured(sha="c" * 40)
    moved["blobs"]["src/shared.py"] = "9" * 40

    assert cov.check_verdict(tmp_path, RUN, _measured()).verdict == "green"
    assert cov.check_verdict(tmp_path, RUN, moved).verdict == "green"
    out, spawned = _reverify(tmp_path, moved)
    assert out is not None and out.verdict == "green"
    assert spawned == []


def test_a_change_to_the_runs_own_blob_still_mismatches_and_parks(tmp_path):
    _verdict(tmp_path, _measured())
    edited = _measured(blob="7" * 40, own="7" * 40, sha="c" * 40)

    assert cov.check_verdict(tmp_path, RUN, edited).verdict == "mismatch"
    out, spawned = _reverify(tmp_path, edited)
    assert out is None
    assert len(spawned) == 1


def test_a_legacy_verdict_is_green_when_every_declared_blob_is_unmoved(tmp_path):
    current = _legacy_measured(tmp_path, now_x="1" * 40, now_shared="2" * 40)

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "green"
    out, spawned = _reverify(tmp_path, current)
    assert out is not None and out.verdict == "green"
    assert spawned == []


def test_a_legacy_verdict_survives_a_declared_path_whose_landed_blob_is_the_verified_one(tmp_path, monkeypatch):
    """The worktree file moved (a peer's edit) but the run's own landed bytes are what it verified."""
    current = _legacy_measured(tmp_path, now_x="8" * 40)
    current["blobs"]["src/shared.py"] = "2" * 40
    monkeypatch.setattr(cov, "_same_blob_content", lambda _root, landed, want: landed == want)

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "green"
    out, spawned = _reverify(tmp_path, current)
    assert out is not None and out.verdict == "green"
    assert spawned == []


def test_a_legacy_verdict_parks_when_a_declared_paths_landed_blob_differs_from_the_verified_one(tmp_path, monkeypatch):
    current = _legacy_measured(tmp_path, now_x="8" * 40, landed_x="5" * 40)
    monkeypatch.setattr(cov, "_same_blob_content", lambda _root, landed, want: landed == want)

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "mismatch"
    out, spawned = _reverify(tmp_path, current)
    assert out is None and len(spawned) == 1


def test_a_legacy_verdict_parks_when_a_moved_declared_path_has_no_recorded_landed_blob(tmp_path):
    current = _legacy_measured(tmp_path, now_x="1" * 40, now_shared="9" * 40)
    current["peer_hunks"] = {}

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "mismatch"


def test_a_legacy_verdict_survives_a_moved_declared_path_with_a_peer_hunk_when_the_landed_bytes_are_the_verified_ones(tmp_path, monkeypatch):
    current = _legacy_measured(tmp_path, now_x="1" * 40, now_shared="9" * 40)
    kept_work.write(cov.verdict_path(tmp_path, RUN).parent, {"c1": {"paths": {"src/x.py": "1" * 40, "src/shared.py": "2" * 40}}}, None)
    monkeypatch.setattr(cov, "_same_blob_content", lambda _root, landed, want: landed == want)

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "green"
    out, spawned = _reverify(tmp_path, current)
    assert out is not None and out.verdict == "green" and spawned == []


def test_a_legacy_verdict_parks_when_a_moved_declared_path_with_a_peer_hunk_has_different_landed_bytes(tmp_path, monkeypatch):
    current = _legacy_measured(tmp_path, now_x="1" * 40, now_shared="9" * 40)
    kept_work.write(cov.verdict_path(tmp_path, RUN).parent, {"c1": {"paths": {"src/x.py": "1" * 40, "src/shared.py": "4" * 40}}}, None)
    monkeypatch.setattr(cov, "_same_blob_content", lambda _root, landed, want: landed == want)

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "mismatch"


def test_a_legacy_verdict_ignores_a_path_the_run_never_declared(tmp_path):
    current = _legacy_measured(tmp_path, now_x="1" * 40)
    current["blobs"]["src/other.py"] = "9" * 40

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "green"


def test_a_legacy_verdict_whose_stored_record_no_longer_hashes_to_its_key_mismatches(tmp_path):
    current = _legacy_measured(tmp_path, now_x="1" * 40)
    record = measured_rev_record_path(tmp_path, RUN)
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored["blobs"]["src/x.py"] = "6" * 40
    record.write_text(json.dumps(stored), encoding="utf-8")

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "mismatch"


def test_a_legacy_verdict_with_no_stored_record_mismatches(tmp_path):
    current = _legacy_measured(tmp_path, now_x="1" * 40)
    measured_rev_record_path(tmp_path, RUN).unlink()

    assert cov.check_verdict(tmp_path, RUN, current).verdict == "mismatch"
