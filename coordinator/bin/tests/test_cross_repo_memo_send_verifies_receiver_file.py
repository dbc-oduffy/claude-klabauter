"""send/redeliver report `Receiver-side:` only for a file that exists on disk."""
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
        "cross_repo_memo_verify", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_verify", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _wire(monkeypatch, mod, acted_item):
    monkeypatch.setitem(
        sys.modules,
        "cc_invoke",
        types.SimpleNamespace(
            route_mutation=lambda op, payload, sender_root, legacy: {
                "exit_code": 0,
                "acted": [acted_item],
            }
        ),
    )
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)


def test_send_absent_file_with_anchor_reports_not_materialized(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    missing = tmp_path / "inbox" / "memo.md"
    _wire(monkeypatch, mod, {"id": str(missing), "delivery_commit_sha": "abc123"})

    rc = mod._cmd_send(argparse.Namespace(topic="some-topic"))
    out = capsys.readouterr().out

    assert "Receiver-side:" not in out
    assert "not materialized" in out
    assert str(missing) in out
    assert "memo.heal_inbox" in out
    assert "/workday-start" in out
    assert "sweep --apply" in out
    assert "heal restores it from the anchor" not in out
    assert rc == 0


def _sweep_raising(monkeypatch, mod, message):
    def _raise(op, payload, sender_root, legacy):
        raise RuntimeError(message)

    monkeypatch.setitem(sys.modules, "cc_invoke", types.SimpleNamespace(route_mutation=_raise))
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))


def test_sweep_method_not_found_names_stale_engine(monkeypatch, capsys):
    mod = _load_cli_module()
    _sweep_raising(monkeypatch, mod, "op error: code=-32601 message=\"Method not found: 'memo.sweep_ref_only'\"")

    rc = mod._cmd_sweep(argparse.Namespace(apply=True, repo=["r"], limit=None))
    err = capsys.readouterr().err

    assert rc == 1
    assert "predates `memo.sweep_ref_only`" in err


def test_sweep_other_failure_gets_no_stale_engine_hint(monkeypatch, capsys):
    mod = _load_cli_module()
    _sweep_raising(monkeypatch, mod, "route_mutation: refused (exit_code=1)")

    rc = mod._cmd_sweep(argparse.Namespace(apply=True, repo=["r"], limit=None))
    err = capsys.readouterr().err

    assert rc == 1
    assert "predates" not in err


def test_send_absent_file_without_anchor_reports_undelivered(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    missing = tmp_path / "inbox" / "memo.md"
    _wire(monkeypatch, mod, {"id": str(missing)})

    rc = mod._cmd_send(argparse.Namespace(topic="some-topic"))
    out = capsys.readouterr().out

    assert "Receiver-side:" not in out
    assert "undelivered" in out
    assert "memo.heal_inbox" not in out
    assert rc == 1


def test_send_present_file_still_prints_receiver_side(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    present = tmp_path / "memo.md"
    present.write_text("x", encoding="utf-8")
    _wire(monkeypatch, mod, {"id": str(present)})

    rc = mod._cmd_send(argparse.Namespace(topic="some-topic"))
    out = capsys.readouterr().out

    assert f"Receiver-side: {present}" in out
    assert "not materialized" not in out
    assert rc == 0


def test_redeliver_absent_file_does_not_print_receiver_side(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    memo = tmp_path / "memo-src.md"
    memo.write_text("x", encoding="utf-8")
    missing = tmp_path / "inbox" / "memo.md"
    _wire(monkeypatch, mod, {"id": str(missing), "delivery_commit_sha": "abc123"})

    rc = mod._cmd_redeliver(argparse.Namespace(memo_file=str(memo)))
    out = capsys.readouterr().out

    assert "Receiver-side:" not in out
    assert "not materialized" in out
    assert rc == 0


def test_redeliver_absent_file_without_anchor_is_undelivered(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    memo = tmp_path / "memo-src.md"
    memo.write_text("x", encoding="utf-8")
    _wire(monkeypatch, mod, {"id": str(tmp_path / "inbox" / "memo.md")})

    rc = mod._cmd_redeliver(argparse.Namespace(memo_file=str(memo)))
    out = capsys.readouterr().out

    assert "Receiver-side:" not in out
    assert "undelivered" in out
    assert rc == 1


def test_redeliver_present_file_still_prints_receiver_side(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    memo = tmp_path / "memo-src.md"
    memo.write_text("x", encoding="utf-8")
    _wire(monkeypatch, mod, {"id": str(memo)})

    rc = mod._cmd_redeliver(argparse.Namespace(memo_file=str(memo)))
    out = capsys.readouterr().out

    assert f"Receiver-side: {memo}" in out
    assert rc == 0
