"""dispatch.ask_gate echoes the sizing path the run is bound to as `sizing_path`, only on a successful bind."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put


def _repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _put(tmp_path, tshirt="S", route="spec-dispatch")
    return tmp_path


def test_bound_run_reply_names_the_bound_sizing(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)

    out = _handler({"sizing_path": REL, "run_id": "r1"}, repo_root=repo)

    assert out["sizing_path"] == REL, out


def test_backslash_spelling_is_echoed_forward_slash(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)

    out = _handler({"sizing_path": REL.replace("/", "\\"), "run_id": "r1"}, repo_root=repo)

    assert out["sizing_path"] == REL, out


def test_no_run_id_means_no_sizing_path_key(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)

    out = _handler({"sizing_path": REL}, repo_root=repo)

    assert "sizing_path" not in out
