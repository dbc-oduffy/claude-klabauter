"""test_cross_repo_memo_send_receipt_names_commit_class — the send receipt
says the delivery commit is the channel, not a cross-repo write grant.

THE DEFECT THIS CLOSES. Nothing at the point of confusion told a sender that
`cross-repo-memo send`'s delivery commit only ever touches `cross-repo/` —
CLAUDE.md gates cross-repo commits behind per-session PM assent and says "a
memo is not cross-repo action" without ever stating the delivery commit is
inside that carve-out. A receiver read a sender's delivery commit as an
ungranted write into their tree and flagged it (cross-repo/inbox/2026-08-13-
doe-claude-em-memo-receipt-should-name-its-own-commit-class.md). The original
ask targeted a literal line ("Delivery verified: on disk and committed as
<sha>") that the pre-DR-210 CLI printed; that exact text is gone (`send`
rewritten 2026-08-25 to forward onto `memo.send` via `cc_invoke.route_
mutation`), but the receipt site itself survived as the `Receiver-side: ...`
print in `_cmd_send` — this pins the clarifying line there instead.

Unit-level: `cc_invoke.route_mutation` is monkeypatched via `sys.modules`
before `_cmd_send`'s local `import cc_invoke` resolves, so no engine, no
warm server, no real memo delivery.

Run: python -m pytest coordinator/bin/tests/test_cross_repo_memo_send_receipt_names_commit_class.py -q
"""
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
