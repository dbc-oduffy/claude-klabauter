"""test_write_identity_file — pytest tests for coordinator/bin/write-identity-file.py.

Spec backlink: scratchpad/scout-D-claude-klabauter-sizing.md § Item 5 (install.md
operator-identity heredoc writes, install.md:669, 818). This trampoline is
thin plumbing over the already-built, already-registered
coordinator_core.ops.write_identity_file op -- these tests stub the
cc_invoke transport (via sys.modules injection) rather than exercising a
real claude-klabauter checkout, since the op's own contract is covered by
coordinator_core/ops/test_write_identity_file.py.

Coverage:
    test_no_fields_exits_one_before_any_transport_call
    test_op_success_exits_zero_and_prints_message
    test_op_error_exits_one_and_prints_error
    test_claude_klabauter_root_unresolvable_exits_two
    test_both_fields_forwarded_in_one_call
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "write_identity_file_cli",
        _BIN_DIR / "write-identity-file.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _install_fake_cc_invoke(monkeypatch, *, resolve_root=lambda: "/fake/claude-klabauter/root", invoke=None):
    """Inject a fake `cc_invoke` module into sys.modules so the CLI's deferred
    `from cc_invoke import _resolve_claude_klabauter_root, cc_invoke` (inside main())
    picks up stubs instead of the real transport."""
    fake = types.ModuleType("cc_invoke")
    fake._resolve_claude_klabauter_root = resolve_root  # type: ignore[attr-defined]
    fake.cc_invoke = invoke or (lambda op, params, root: {"exit_code": 0, "message": "ok"})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cc_invoke", fake)


def test_no_fields_exits_one_before_any_transport_call(capsys):
    rc = _mod.main(["--claude-home", "/some/home"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "at least one of --operator-name" in captured.err


def test_op_success_exits_zero_and_prints_message(monkeypatch, capsys):
    def _invoke(op, params, root):
        assert op == "install.write_identity_file"
        assert params == {"claude_home": "/some/home", "fields": {"operator_name": "Dax"}}
        return {"exit_code": 0, "written": True, "path": "/some/home/.claude/coordinator-identity.yaml", "message": "wrote 1 field(s)"}

    _install_fake_cc_invoke(monkeypatch, invoke=_invoke)

    rc = _mod.main(["--claude-home", "/some/home", "--operator-name", "Dax"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "wrote 1 field(s)" in captured.out


def test_op_error_exits_one_and_prints_error(monkeypatch, capsys):
    _install_fake_cc_invoke(
        monkeypatch,
        invoke=lambda op, params, root: {"exit_code": 1, "written": False, "path": "", "error": "boom"},
    )

    rc = _mod.main(["--claude-home", "/some/home", "--operator-name", "Dax"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err


def test_claude_klabauter_root_unresolvable_exits_two(monkeypatch, capsys):
    def _raise_resolve():
        raise RuntimeError("no engine root")

    _install_fake_cc_invoke(monkeypatch, resolve_root=_raise_resolve)

    rc = _mod.main(["--claude-home", "/some/home", "--operator-name", "Dax"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "engine-root resolution failed" in captured.err


def test_both_fields_forwarded_in_one_call(monkeypatch, capsys):
    seen = {}

    def _invoke(op, params, root):
        seen.update(params)
        return {"exit_code": 0, "message": "wrote 2 field(s)"}

    _install_fake_cc_invoke(monkeypatch, invoke=_invoke)

    rc = _mod.main(
        [
            "--claude-home", "/some/home",
            "--operator-name", "Dax",
            "--engagement-posture", "first-officer",
        ]
    )

    assert rc == 0
    assert seen["fields"] == {"operator_name": "Dax", "engagement_posture": "first-officer"}
