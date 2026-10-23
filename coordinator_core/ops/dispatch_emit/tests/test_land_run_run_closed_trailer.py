"""land_run's message block stamps Checkpoint-Run-Closed for each base the landing run's checkpoints carry."""

import json

from coordinator_core.ops.dispatch_emit import land_run

H = "a" * 40
S = "b" * 40


def _manifest(tmp_path, run_id, head):
    d = tmp_path / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"head_sha": head}), encoding="utf-8")


def test_manifest_head_sha_is_stamped_closed(tmp_path):
    _manifest(tmp_path, "run-1", H)
    assert land_run._run_closed_lines(tmp_path, "run-1", {}) == [f"Checkpoint-Run-Closed: {H}"]


def test_manifest_base_and_differing_submission_base_both_stamped(tmp_path):
    _manifest(tmp_path, "run-1", H)
    lines = land_run._run_closed_lines(tmp_path, "run-1", {"run_base_sha": S})
    assert lines == [f"Checkpoint-Run-Closed: {H}", f"Checkpoint-Run-Closed: {S}"]


def test_no_resolvable_base_stamps_nothing(tmp_path):
    assert land_run._run_closed_lines(tmp_path, "run-none", {}) == []
    assert land_run._run_closed_lines(tmp_path, None, {}) == []
