"""resolve-move-memos: N inbox memos stamped with their own dispositions, moved to archive, no commit."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest.mock
from pathlib import Path

import pytest

from coordinator_core import archive_stamp

_BIN_DIR = Path(__file__).resolve().parent.parent

_OPEN_MEMO = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_resolve_move_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _run(capsys, argv):
    with unittest.mock.patch.object(_cli, "_import_module", lambda: archive_stamp):
        rc = _cli.main(argv)
    return rc, capsys.readouterr()


def test_requires_leading_memo_group(capsys):
    rc, _ = _run(capsys, ["resolve-move-memos", "a.md", "--actioned-note", "n"])
    assert rc == 2


def test_listed_in_usage_and_subcommand_table():
    assert "resolve-move-memos" in _cli._SUBCOMMANDS
    assert "resolve-move-memos" in _cli._SUBCOMMAND_USAGE
