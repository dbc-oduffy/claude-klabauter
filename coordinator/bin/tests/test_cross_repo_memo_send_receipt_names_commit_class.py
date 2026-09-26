from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import types
from contextlib import redirect_stdout

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo_receipt", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_receipt", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_successful_send_receipt_names_the_commit_class(monkeypatch):
    mod = _load_cli_module()

    fake_cc_invoke = types.SimpleNamespace(
        route_mutation=lambda op, payload, sender_root, legacy: {
            "exit_code": 0,
            "acted": [{"id": "state/memo-outbox/sent/some-topic.md"}],
        }
    )
    monkeypatch.setitem(sys.modules, "cc_invoke", fake_cc_invoke)
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)

    args = argparse.Namespace(topic="some-topic")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod._cmd_send(args)

    assert rc == 0
    out = buf.getvalue()
    assert "Receiver-side:" in out
    assert "channel, not a cross-repo write grant" in out, (
        "the send receipt must name the delivery commit's own class at the "
        "point of confusion, not leave it to CLAUDE.md prose neither side "
        "reads mid-send"
    )
