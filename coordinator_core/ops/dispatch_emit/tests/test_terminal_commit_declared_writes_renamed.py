"""terminal_commit reply names renamed/undelivered caller-declared writes from the landing set."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.terminal_commit import _declared_reconcile


def _run_dir(tmp_path: Path, declared) -> Path:
    if declared is not None:
        (tmp_path / "declared_writes.json").write_text(json.dumps(declared), encoding="utf-8")
    return tmp_path


def test_rename_in_same_dir_is_named(tmp_path):
    out = _declared_reconcile(
        _run_dir(tmp_path, ["runs/census.md"]), ["runs/crosstab.md"],
    )
    assert out == {
        "renamed": [{"declared": "runs/census.md", "delivered": "runs/crosstab.md"}],
        "undelivered": [],
    }


def test_declared_path_delivered_is_silent(tmp_path):
    out = _declared_reconcile(_run_dir(tmp_path, ["runs/a.md"]), ["runs/a.md", "x/other.md"])
    assert out == {"renamed": [], "undelivered": []}


def test_undelivered_without_a_rename_partner(tmp_path):
    out = _declared_reconcile(_run_dir(tmp_path, ["runs/a.md"]), ["elsewhere/b.md"])
    assert out == {"renamed": [], "undelivered": ["runs/a.md"], "undeclared": ["elsewhere/b.md"]}


def test_no_declared_file_omits_keys(tmp_path):
    assert _declared_reconcile(_run_dir(tmp_path, None), ["a.md"]) == {}
    assert _declared_reconcile(None, ["a.md"]) == {}


def test_empty_or_unreadable_declared_omits_keys(tmp_path):
    assert _declared_reconcile(_run_dir(tmp_path, []), ["a.md"]) == {}
    (tmp_path / "declared_writes.json").write_text("{not json", encoding="utf-8")
    assert _declared_reconcile(tmp_path, ["a.md"]) == {}
