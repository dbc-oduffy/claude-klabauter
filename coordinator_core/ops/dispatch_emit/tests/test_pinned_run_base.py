"""`pinned_run_base` is the run's own baseline; a sibling commit moving manifest head_sha never replaces it."""

import json

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import ask_stage, halted_run_landed

RUN = "warp-pin-1"
BASE, SIBLING, HALTED = "8" * 40, "6" * 40, "4" * 40


def _record(tmp_path, *, run_base=None, halted=None, head=SIBLING):
    rec = tmp_path / "state" / "warp" / RUN
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"head_sha": head}), encoding="utf-8")
    if run_base:
        (tmp_path / contract.run_base_rel(RUN)).parent.mkdir(parents=True)
        (tmp_path / contract.run_base_rel(RUN)).write_text(run_base + "\n", encoding="utf-8")
    if halted:
        (rec / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": halted}), encoding="utf-8")
    return rec


def test_order_is_run_base_file_then_halted_base_then_head_sha(tmp_path):
    _record(tmp_path, run_base=BASE, halted=HALTED)
    assert contract.pinned_run_base(tmp_path, RUN) == BASE
    (tmp_path / contract.run_base_rel(RUN)).write_text("", encoding="utf-8")
    assert contract.pinned_run_base(tmp_path, RUN) == HALTED


def test_head_sha_is_only_the_fallback(tmp_path):
    _record(tmp_path)
    assert contract.pinned_run_base(tmp_path, RUN) == SIBLING


def test_landing_sha_is_the_pinned_base_not_the_sibling_moved_head_sha(tmp_path, monkeypatch):
    _record(tmp_path, run_base=BASE)
    monkeypatch.setattr(halted_run_landed, "_in_run_unclean", lambda *_a: None)
    assert halted_run_landed._derive_landing_sha(tmp_path, RUN) == (BASE, None)


def test_stage_relays_the_pinned_base_beside_a_moved_head_sha(tmp_path, monkeypatch):
    _record(tmp_path, run_base=BASE)
    monkeypatch.setattr(ask_stage, "read_head_sha", lambda _root: SIBLING)
    manifest = type("M", (), {"to_json": lambda self: {}, "dropped_rows": ()})()
    payload = ask_stage.manifest_payload(tmp_path, manifest, RUN)
    assert (payload["head_sha"], payload["run_base_sha"]) == (SIBLING, BASE)
