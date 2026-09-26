from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_QUEUE_APPEND_CLI = _BIN_DIR / "coordinator-queue-append.py"


def _load_module(name: str):
    loader = SourceFileLoader(name, str(_QUEUE_APPEND_CLI))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _run_central_scope_legacy(monkeypatch, tmp_path, claude_klabauter_root) -> tuple[int | None, str, str]:
    mod = _load_module(f"coordinator_queue_append_{id(tmp_path)}")

    monkeypatch.setattr(
        mod, "resolve_checked_repo_root", lambda explicit_root=None: (None, {"verdict": "OK"})
    )

    def _fake_route(op_name, params, repo_root, legacy_fn):
        if op_name == "schema.describe":
            return {"required": [], "optional": [], "enums": {}, "applies_to": None}
        if op_name == "queue.append":
            return legacy_fn()
        raise AssertionError(f"unexpected op in State-1 simulation: {op_name}")

    monkeypatch.setattr(mod, "_cc_route", _fake_route)
    monkeypatch.setattr(mod, "_claude_klabauter_root", lambda: claude_klabauter_root)

    argv = [
        "--schema",
        "lessons",
        "--queue-scope",
        "central",
        "--title",
        "second writer regression",
        "--body",
        "body",
    ]

    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = mod.main(argv)

    return rc, out_buf.getvalue(), err_buf.getvalue()


def test_central_scope_refused_no_file_written_posix_shaped_root(monkeypatch, tmp_path) -> None:
    claude_klabauter_root = str(tmp_path / "mirror-shaped-root")
    os.makedirs(claude_klabauter_root, exist_ok=True)

    rc, stdout, stderr = _run_central_scope_legacy(monkeypatch, tmp_path, claude_klabauter_root)

    assert rc is None or rc == 0
    assert stdout == "", f"central-scope refusal must print no landing path, got: {stdout!r}"
    assert (
        "central-scope writes require the native queue.append op" in stderr
    ), f"expected the refusal diagnostic, got:\n{stderr}"
    lessons_dir = os.path.join(claude_klabauter_root, "state", "lessons")
    assert not os.path.isdir(lessons_dir) or not os.listdir(lessons_dir), (
        "legacy_fn must not land any file under the resolved claude-klabauter root's "
        "state/lessons/ for a central-scope write"
    )


def test_central_scope_refused_no_file_written_windows_shaped_root(monkeypatch, tmp_path) -> None:
    claude_klabauter_root = r"C:\Users\test\mirror-shaped-root"

    rc, stdout, stderr = _run_central_scope_legacy(monkeypatch, tmp_path, claude_klabauter_root)

    assert rc is None or rc == 0
    assert stdout == "", f"central-scope refusal must print no landing path, got: {stdout!r}"
    assert (
        "central-scope writes require the native queue.append op" in stderr
    ), f"expected the refusal diagnostic, got:\n{stderr}"
