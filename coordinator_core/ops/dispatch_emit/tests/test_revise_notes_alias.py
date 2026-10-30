"""workflow.revise: `notes` is an alias of `note` (str, or list of str joined by newline); both is refused."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl


def _persisted(repo):
    dir_ = rl.run_dir(repo, "run-one")
    lines = (dir_ / rl.NOTES_FILE).read_text(encoding="utf-8").splitlines()
    return [json.loads(x)["note"] for x in lines]


def test_notes_string_is_taken_as_note(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "notes": "fyi one"}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert _persisted(repo) == ["fyi one"]
    assert len(_rows(repo)) == 1


def test_notes_list_is_joined_by_newline(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "notes": ["line a", "line b"]}, repo_root=repo)
    assert reply["ok"] is True
    assert _persisted(repo) == ["line a\nline b"]


def test_notes_and_note_together_are_refused_naming_note(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "note": "a", "notes": "b"}, repo_root=repo)
    assert "error" in reply and "`note`" in reply["error"]
    assert len(_rows(repo)) == 1


def test_caller_params_are_not_mutated(repo):
    _bound(repo, "run-one", ask="first ask")
    params = {"run_id": "run-one", "notes": "x"}
    rl._handler(params, repo_root=repo)
    assert params == {"run_id": "run-one", "notes": "x"}
