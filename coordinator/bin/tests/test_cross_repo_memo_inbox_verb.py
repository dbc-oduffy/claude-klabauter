from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import pathlib
import sys

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BIN_DIR.parent.parent))


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "crm_inbox_under_test", str(_BIN_DIR / "cross-repo-memo.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_BIN_DIR))
    loader.exec_module(mod)
    return mod


def _memo(inbox: pathlib.Path, name: str, status: str, sender: str, title: str) -> None:
    (inbox / f"{name}.md").write_text(
        f'---\ntitle: "{title}"\nfrom: "{sender}"\nstatus: {status}\n---\n\nbody\n',
        encoding="utf-8",
    )


def test_inbox_lists_status_sender_title_open_first(tmp_path, monkeypatch, capsys):
    mod = _load()
    inbox = tmp_path / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    _memo(inbox, "2026-01-01-a", "actioned", "sender-a", "Done thing")
    _memo(inbox, "2026-01-02-b", "open", "sender-b", "Pending thing")
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(tmp_path))

    assert mod._cmd_inbox(argparse.Namespace()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "open  sender-b  2026-01-02-b  :: Pending thing"
    assert lines[1] == "actioned  sender-a  2026-01-01-a  :: Done thing"
    assert lines[2] == "2 memo(s), 1 open"


def test_inbox_empty_and_outside_repo(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(tmp_path))
    assert mod._cmd_inbox(argparse.Namespace()) == 0
    assert "no inbound memos" in capsys.readouterr().out
    monkeypatch.setattr(mod, "_current_repo_root", lambda: None)
    assert mod._cmd_inbox(argparse.Namespace()) == 2


def test_main_dispatches_inbox_verb(tmp_path, monkeypatch, capsys):
    mod = _load()
    inbox = tmp_path / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    _memo(inbox, "2026-01-02-b", "open", "sender-b", "Pending thing")
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(tmp_path))

    assert mod.main(["inbox"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "open  sender-b  2026-01-02-b  :: Pending thing"
    assert out[-1] == "1 memo(s), 1 open"
    monkeypatch.setattr(mod, "_current_repo_root", lambda: None)
    assert mod.main(["inbox"]) == 2


def test_inbox_is_a_registered_verb():
    mod = _load()
    assert "inbox" in mod._SUBCOMMAND_VERBS
    assert mod._build_combined_parser().parse_args(["inbox"]).verb == "inbox"
