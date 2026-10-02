"""A git timeout in a delivery-evidence helper reads as indeterminate, never as not-shipped."""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.git.run import GitResult

_TIMED_OUT = GitResult(returncode=-1, stdout="", stderr="", timed_out=True)
_REFUSED = GitResult(returncode=128, stdout="", stderr="fatal", timed_out=False)

_LEDGER = """## Dispatch Ledger

| chunk-id | status |
|---|---|
| C1 | committed abcdef1234567 |
"""


def test_rev_list_ancestor_shas_timeout_raises_but_refusal_is_none(monkeypatch):
    monkeypatch.setattr(coas, "_run_git", lambda *_a, **_k: _TIMED_OUT)
    with pytest.raises(coas._GitIndeterminate):
        coas._rev_list_ancestor_shas(Path("."))
    monkeypatch.setattr(coas, "_run_git", lambda *_a, **_k: _REFUSED)
    assert coas._rev_list_ancestor_shas(Path(".")) is None


def test_batch_cat_file_timeout_raises(monkeypatch):
    import coordinator_core.git.run as git_run

    monkeypatch.setattr(git_run, "run_git", lambda *_a, **_k: _TIMED_OUT)
    with pytest.raises(coas._GitIndeterminate):
        coas._batch_git_cat_file_check(["abcdef1"], Path("."))


@pytest.mark.parametrize("target", ["cat_file", "rev_list"])
def test_dispatch_ledger_delivered_reports_timeout_as_error(monkeypatch, target):
    def _boom(*_a, **_k):
        raise coas._GitIndeterminate(coas.GIT_TIMED_OUT_INDETERMINATE)

    if target == "cat_file":
        monkeypatch.setattr(coas, "_batch_git_cat_file_check", _boom)
    else:
        monkeypatch.setattr(coas, "_batch_git_cat_file_check", lambda shas, _r: {s: s for s in shas})
        monkeypatch.setattr(coas, "_rev_list_ancestor_shas", _boom)
    shipped, missing, error = coas._dispatch_ledger_delivered(_LEDGER, Path("."))
    assert (shipped, missing) == (False, [])
    assert error == "git timed out: indeterminate"
