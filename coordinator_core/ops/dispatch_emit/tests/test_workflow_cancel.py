"""workflow.cancel: marks a live run cancelled, drops its index row and claims, stays refire-resumable."""

import json

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import halt_refire, halted_run as hr, live_run_index as idx, run_cancel
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SIZING = "state/sizings/2026-11/2026-11-15-foo.yaml"
RUN = "warp-2026-11-15-aaaaaa-1-foo-20261115T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _isolated_machine_local(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _run_dir(root, run=RUN):
    d = root / RECORD_DIR_ROOT / run
    d.mkdir(parents=True)
    return d


def _live_run(root, run=RUN):
    """A live run: a record dir bound to a sizing through run.json, indexed with a claim marker."""
    d = _run_dir(root, run)
    (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    assert idx.claim(root, run, ["a.py"]) == {}
    return d


def _cancel(root, **params):
    return run_cancel._handler({"run_id": RUN, **params}, repo_root=root)


def test_cancel_writes_cancelled_halt_with_refire_next_action(tmp_path):
    root = _root(tmp_path)
    d = _live_run(root)
    reply = _cancel(root, reason="PM stopped it")
    assert reply["ok"] is True and reply["halted"] == "cancelled" and reply["already_cancelled"] is False
    marker = json.loads((d / hr.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == hr.STATUS_HALTED and marker["halted"] == run_cancel.CANCELLED
    assert marker["sizing_rel"] == SIZING and marker["reason"] == "PM stopped it"
    assert halt_refire.is_refire_next_action(marker["next_action"])
    assert marker["next_action"]["resume"]["run_id"] == RUN


def test_cancel_drops_index_row_and_releases_claims(tmp_path):
    root = _root(tmp_path)
    _live_run(root)
    assert RUN in idx.read(root, 1000)
    assert idx.run_holds_no_claim(root, RUN) is False
    assert _cancel(root)["ok"] is True
    assert RUN not in idx.read(root, 1000)
    assert idx.run_holds_no_claim(root, RUN) is True


def test_cancelled_refusal_reads_the_marker(tmp_path):
    root = _root(tmp_path)
    d = _live_run(root)
    assert run_cancel.cancelled_refusal(d) is None
    _cancel(root)
    assert RUN in run_cancel.cancelled_refusal(d)
    assert hr.mark_resumed(d) is True
    assert run_cancel.cancelled_refusal(d) is None


def test_cancelled_refusal_ignores_other_halts_and_absent_marker(tmp_path):
    d = tmp_path / "run"
    assert run_cancel.cancelled_refusal(d) is None
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert run_cancel.cancelled_refusal(d) is None


def test_refire_resolves_the_cancelled_run(tmp_path):
    root = _root(tmp_path)
    _live_run(root)
    _cancel(root)
    assert hr.halted_run_to_resume(root, SIZING, run_id=RUN) == RUN


def test_cancel_is_idempotent(tmp_path):
    root = _root(tmp_path)
    d = _live_run(root)
    _cancel(root, reason="first")
    first = (d / hr.HALTED_FILE).read_text(encoding="utf-8")
    reply = _cancel(root, reason="second")
    assert reply["ok"] is True and reply["already_cancelled"] is True
    assert (d / hr.HALTED_FILE).read_text(encoding="utf-8") == first


def test_unknown_run_is_a_typed_error(tmp_path):
    root = _root(tmp_path)
    assert "unknown run" in _cancel(root)["error"]
    assert not (root / RECORD_DIR_ROOT / RUN).exists()


def test_unsafe_run_id_is_refused(tmp_path):
    root = _root(tmp_path)
    assert "error" in run_cancel._handler({"run_id": "../x"}, repo_root=root)


@pytest.mark.parametrize("terminal", ["landed", "closed", "superseded"])
def test_ended_run_is_refused_and_untouched(tmp_path, terminal):
    root = _root(tmp_path)
    d = _live_run(root)
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    marker = json.loads((d / hr.HALTED_FILE).read_text(encoding="utf-8"))
    (d / hr.HALTED_FILE).write_text(json.dumps({**marker, "status": terminal}), encoding="utf-8")
    before = (d / hr.HALTED_FILE).read_text(encoding="utf-8")
    assert terminal in _cancel(root)["error"]
    assert (d / hr.HALTED_FILE).read_text(encoding="utf-8") == before


def test_resumed_run_is_live_and_cancellable(tmp_path):
    root = _root(tmp_path)
    d = _live_run(root)
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    hr.mark_resumed(d)
    assert _cancel(root)["ok"] is True
    assert json.loads((d / hr.HALTED_FILE).read_text(encoding="utf-8"))["halted"] == "cancelled"


def test_run_bound_to_no_sizing_is_refused(tmp_path):
    root = _root(tmp_path)
    d = _run_dir(root)
    assert "no sizing" in _cancel(root)["error"]
    assert not (d / hr.HALTED_FILE).exists()


def test_strict_params_refuse_unknown_keys_and_missing_run_id(tmp_path):
    root = _root(tmp_path)
    assert "error" in run_cancel._handler({"run_id": RUN, "bogus": 1}, repo_root=root)
    assert "error" in run_cancel._handler({}, repo_root=root)
    assert "error" in run_cancel._handler({"run_id": RUN}, repo_root=None)
