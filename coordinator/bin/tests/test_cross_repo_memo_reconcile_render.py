
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import pathlib
import sys
import types

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _run(monkeypatch, capsys, envelope: dict, *, apply_: bool, root: str):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_repo_root", lambda: root)
    monkeypatch.setitem(sys.modules, "lib", types.ModuleType("lib"))
    stub = types.ModuleType("cc_invoke")
    stub.route_mutation = lambda *a, **k: envelope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cc_invoke", stub)

    code = mod._cmd_reconcile(argparse.Namespace(apply=apply_))

    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_reported_rows_note_is_printed(monkeypatch, capsys, tmp_path):
    envelope = {
        "candidates": [
            {
                "disposition": "report",
                "status": None,
                "filename": "lost.md",
                "note": "delivered but this repo holds no committed receipt",
            }
        ]
    }

    code, out, _ = _run(
        monkeypatch, capsys, envelope, apply_=False, root=str(tmp_path)
    )

    assert code == 0
    assert "lost.md" in out
    assert "delivered but this repo holds no committed receipt" in out


def test_a_noteless_row_prints_no_empty_continuation(monkeypatch, capsys, tmp_path):
    envelope = {
        "candidates": [
            {"disposition": "move", "status": "sent", "filename": "done.md", "note": None}
        ]
    }

    code, out, _ = _run(
        monkeypatch, capsys, envelope, apply_=False, root=str(tmp_path)
    )

    assert code == 0
    assert "└─" not in out


def test_a_tracked_legacy_move_is_told_to_commit(monkeypatch, capsys, tmp_path):
    root = str(tmp_path)
    envelope = {
        "acted": [
            {
                "filename": "done.md",
                "path": str(tmp_path / ".coordinator-local" / "memo-outbox" / "sent" / "done.md"),
                "source_path": str(tmp_path / "state" / "memo-outbox" / "done.md"),
            }
        ],
        "skipped": [],
    }

    code, out, _ = _run(monkeypatch, capsys, envelope, apply_=True, root=root)

    assert code == 0
    assert "Commit the batch" in out
    assert "nothing to commit" not in out


def test_a_move_that_began_in_the_untracked_root_has_nothing_to_commit(
    monkeypatch, capsys, tmp_path
):
    root = str(tmp_path)
    outbox = tmp_path / ".coordinator-local" / "memo-outbox"
    envelope = {
        "acted": [
            {
                "filename": "done.md",
                "path": str(outbox / "sent" / "done.md"),
                "source_path": str(outbox / "done.md"),
            }
        ],
        "skipped": [],
    }

    code, out, _ = _run(monkeypatch, capsys, envelope, apply_=True, root=root)

    assert code == 0
    assert "nothing to commit" in out
    assert "Commit the batch" not in out
