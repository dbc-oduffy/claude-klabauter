"""`args.resume_in_place` keeps the named halted run across an engine swap; the default still closes it fresh."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_fresh_refire import NEW, OLD, RUN, _marker, _repo, _route
from coordinator_core.ops import engine_ask_submit

pytestmark = pytest.mark.complete


def test_resume_in_place_is_an_accepted_key_on_both_surfaces():
    assert "resume_in_place" in mod._ARGS_KEYS
    assert {"resume_in_place", "resume_run_id"} <= engine_ask_submit._KNOWN_KEYS


def test_a_differing_stamp_resumes_the_named_run_when_kept_in_place(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, _result, retired, _ = _route(root, monkeypatch, NEW, resume_in_place=True)
    assert envelope is not None
    assert retired == []
    assert _marker(root)["status"] == "halted"


def test_fresh_outranks_resume_in_place(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=OLD)
    envelope, result, retired, _ = _route(root, monkeypatch, NEW, resume_in_place=True, fresh=True)
    assert (envelope, result) == (None, None)
    assert retired == [RUN]
    assert _marker(root)["status"] == "superseded"


@pytest.mark.parametrize("value", ["true", 1, False])
def test_only_the_bool_true_keeps_the_run(tmp_path, monkeypatch, value):
    root = _repo(tmp_path, stamp=OLD)
    _envelope, _result, retired, _ = _route(root, monkeypatch, NEW, resume_in_place=value)
    assert retired == [RUN]
