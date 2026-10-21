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
    # "sent/" is NOT checked here: the receiver-side path this fake legitimately
    # prints (`state/memo-outbox/sent/some-topic.md`) contains that substring by
    # construction, and asserting its absence would fail on the real, wanted
    # "Receiver-side:" line rather than on any ledger narration. "ledger" alone
    # is the load-bearing check for item 1's non-finding.
    assert "ledger" not in out.lower(), (
        "the success path must stay silent on the sent-ledger — see "
        "docs/plans/2026-09-26-silent-engine-bookkeeping.md item 1 "
        "(verified non-finding, R1); a ledger mention here would be a "
        "regression back into EM-visible bookkeeping"
    )


def test_successful_send_names_the_receiver_path_and_no_pm_step(monkeypatch):
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
    import os

    receiver_line = next(line for line in out.splitlines() if line.startswith("Receiver-side:"))
    assert receiver_line.split("Receiver-side:", 1)[1].strip() == os.path.abspath(
        "state/memo-outbox/sent/some-topic.md"
    )
    assert "PM" not in out


def test_sender_commit_failure_receipt_still_names_the_recovery_path(monkeypatch, capsys):
    mod = _load_cli_module()

    fake_cc_invoke = types.SimpleNamespace(
        route_mutation=lambda op, payload, sender_root, legacy: {
            "exit_code": 0,
            "acted": [
                {
                    "id": "state/memo-outbox/sent/some-topic.md",
                    "sender_committed": False,
                    "sender_commit_stderr": "fatal: could not lock config file",
                }
            ],
        }
    )
    monkeypatch.setitem(sys.modules, "cc_invoke", fake_cc_invoke)
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)

    args = argparse.Namespace(topic="some-topic")
    rc = mod._cmd_send(args)
    captured = capsys.readouterr()

    assert rc == 1
    assert (
        "recover: commit the staged sent/ copy and ledger row by path."
        in captured.err
    ), (
        "the sender-commit-failure branch is the one place the ledger row "
        "IS narrated on purpose — this pins it unchanged (item 1's own "
        "non-finding scope: every ledger mention sits on this failure path)"
    )
    assert "fatal: could not lock config file" in captured.err


def _send_raising(monkeypatch, capsys, exc):
    mod = _load_cli_module()

    def _raise(op, payload, sender_root, legacy):
        raise exc

    monkeypatch.setitem(sys.modules, "cc_invoke", types.SimpleNamespace(route_mutation=_raise))
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)
    rc = mod._cmd_send(argparse.Namespace(topic="some-topic"))
    return rc, capsys.readouterr()


class _EngineRefusal(RuntimeError):
    result = {"exit_code": 1}
    op_stderr = ""


def test_successful_send_prints_no_recover_line(monkeypatch, capsys):
    mod = _load_cli_module()
    monkeypatch.setitem(
        sys.modules,
        "cc_invoke",
        types.SimpleNamespace(
            route_mutation=lambda op, payload, sender_root, legacy: {
                "exit_code": 0,
                "acted": [{"id": "x/some-topic.md", "sender_committed": True}],
            }
        ),
    )
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)
    rc = mod._cmd_send(argparse.Namespace(topic="some-topic"))
    captured = capsys.readouterr()
    assert rc == 0
    assert "recover" not in captured.out + captured.err


def test_local_precondition_refusal_omits_the_pr_comment_fallback(monkeypatch, capsys):
    rc, captured = _send_raising(
        monkeypatch, capsys,
        _EngineRefusal("route_mutation: op='memo.send' refused (exit_code=1, failed=0)"),
    )
    assert rc == 1
    assert "Fallback" not in captured.err


def test_delivery_failure_keeps_the_pr_comment_fallback(monkeypatch, capsys):
    rc, captured = _send_raising(
        monkeypatch, capsys, RuntimeError("claude-klabauter engine seam not found")
    )
    assert rc == 1
    assert captured.err.count("Fallback: post the memo as a comment") == 1


def test_receiver_resolution_refusal_names_the_fallback_once(monkeypatch, capsys):
    refusal = _EngineRefusal(
        "route_mutation: refused\n  child stderr: memo.send: UNKNOWN RECEIVER "
        "Fallback: post the memo as a comment on the session's PR."
    )
    rc, captured = _send_raising(monkeypatch, capsys, refusal)
    assert rc == 1
    assert captured.err.count("Fallback: post the memo as a comment") == 1
