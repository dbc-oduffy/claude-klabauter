"""Restore-evicted op over a real git repo: timeout-resume restore, typed no-op refusal, drift fence."""

import json

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE
from coordinator_core.ops.dispatch_emit.restore_evicted_op import RESTORE_NOTHING_REFUSAL, _handler
from coordinator_core.ops.dispatch_emit.tests.test_halt_tree_evict import _declare, _git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_timeout_resume_restores_seven_recorded_paths_without_landed_state(tmp_path):
    """c94de0 shape: no live touch attribution or landed-state SHA, own deltas on disk."""
    run_id = "warp-c94de0"
    owned = [f"row_{i}.txt" for i in range(7)]
    peer = "peer.txt"
    root = _git_repo(tmp_path, {rel: "base\n" for rel in [*owned, peer]})
    _declare(root, run_id, [*owned, peer])
    rec = record_dir(root, run_id)
    (rec / "resume.json").write_text('{"kind":"timeout"}', encoding="utf-8")
    common = resolve_git_common_dir(root)
    for rel in owned:
        (root / rel).write_bytes(b"base\n")
    base = write_object(common, b"blob", b"base\n")
    own = write_object(common, b"blob", b"base\nown\n")
    mixed = write_object(common, b"blob", b"base\nown\npeer\n")
    all_paths = [*owned, peer]
    (rec / "evicted.json").write_text(
        json.dumps({rel: {"blob": mixed, "head": base} for rel in all_paths}),
        encoding="utf-8",
    )
    (rec / WITHHELD_OWN_DELTA_FILE).write_text(
        json.dumps({"paths": {rel: {"blob": own} for rel in all_paths}}), encoding="utf-8",
    )
    (root / peer).write_bytes(b"live peer edit\n")

    result = _handler({"run_id": run_id}, root)["paths"]

    assert {rel for rel, item in result.items() if item["status"] == "restored"} == set(owned), result
    assert all((root / rel).read_bytes() == b"base\nown\n" for rel in owned)
    assert result[peer] == {"status": "skipped", "reason": "restore-drift"}
    assert (root / peer).read_bytes() == b"live peer edit\n"


def _evicted_run(tmp_path, own_delta: bool):
    run_id = "warp-aa11bb"
    root = _git_repo(tmp_path, {"a.txt": "base\n"})
    _declare(root, run_id, ["a.txt"])
    (root / "a.txt").write_bytes(b"base\n")  # Trap: _git_repo text writes may be CRLF
    rec = record_dir(root, run_id)
    common = resolve_git_common_dir(root)
    base = write_object(common, b"blob", b"base\n")
    own = write_object(common, b"blob", b"base\nown\n")
    (rec / "evicted.json").write_text(json.dumps({"a.txt": {"blob": own, "head": base}}), encoding="utf-8")
    if own_delta:
        (rec / WITHHELD_OWN_DELTA_FILE).write_text(json.dumps({"paths": {"a.txt": {"blob": own}}}), encoding="utf-8")
    return root, run_id


def test_nothing_restored_is_a_typed_refusal_not_success(tmp_path):
    root, run_id = _evicted_run(tmp_path, own_delta=False)

    out = _handler({"run_id": run_id}, root)

    assert out["refused"] == RESTORE_NOTHING_REFUSAL
    assert out["paths"]["a.txt"] == {"status": "skipped", "reason": "missing-own-delta"}


def test_restore_success_carries_no_refusal(tmp_path):
    root, run_id = _evicted_run(tmp_path, own_delta=True)

    out = _handler({"run_id": run_id}, root)

    assert "refused" not in out
    assert out["paths"]["a.txt"]["status"] == "restored"


def test_foreign_write_between_check_and_replace_survives(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import live_write_guard

    root, run_id = _evicted_run(tmp_path, own_delta=True)
    real = live_write_guard.check_live_write

    def racing(*args, **kwargs):
        verdict = real(*args, **kwargs)
        (root / "a.txt").write_bytes(b"foreign mid-window\n")
        return verdict

    monkeypatch.setattr(live_write_guard, "check_live_write", racing)

    out = _handler({"run_id": run_id}, root)

    assert (root / "a.txt").read_bytes() == b"foreign mid-window\n"
    assert out["paths"]["a.txt"] == {"status": "skipped", "reason": "restore-drift"}
