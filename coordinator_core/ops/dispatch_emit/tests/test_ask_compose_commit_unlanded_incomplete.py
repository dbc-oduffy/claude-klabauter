"""A landed commit's unlanded rows (`incomplete_reasons`) fold into the returned `incomplete`."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_in_run_close import _script

_FOLD = "for (const _uk of Object.keys(_commit.incomplete_reasons ?? {}))"


def test_fold_sits_in_committed_branch_before_next_action_and_return(monkeypatch):
    script = _script(monkeypatch)
    assert script.count(_FOLD) == 1
    at = script.index(_FOLD)
    branch = script.index("if (_commit && _commit.committed === true) { const _blockedRows")
    assert branch < at
    assert at < script.index("_nextAction = { kind: 'refire_after_sibling'", branch)
    assert at < script.index("_nextAction = { kind: 'close_job'", branch)
    assert at < script.index("incomplete: _incompleteChunks, ")
    assert "if (!_incompleteChunks.includes(_uk)) _incompleteChunks.push(_uk);" in script


def test_fold_absent_without_in_run_commit(monkeypatch):
    assert _FOLD not in _script(monkeypatch, in_run_commit=False)
