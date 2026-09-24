"""test_lesson_add_second_writer.py — P144-C2 regression pin.

C1's spike (state/bug-backlog/2026-08-31-lesson-add-reports-duplicates-of-files-
that-do-not-exist.yaml :: dedupe_root_cause) confirmed the "second writer" live
at HEAD: in State-1 (native `coordinator_core.invoke` seam absent),
`coordinator-queue-append.py :: legacy_fn` reached `_output_path`'s
`queue_scope == "central"` branch whenever `_claude_klabauter_root()` resolved to ANY
tree, including a resolvable-but-mirror-shaped one. That branch mints a
hash-less `<date>-<slug40>.yaml` name (via `_write_out_path_excl`'s `-N`
collision retry) that disagrees with the native op's
`<date>-<slug>-<digest12>.yaml` scheme, and a write landing in a mirror tree
is silently rewritten away by the next publish round.

P144-C2 closes that route: `legacy_fn` now refuses ANY central-scope write
unconditionally (before validating, building, or writing anything) rather
than reaching `_output_path`'s central branch. This test proves the refusal
fires, prints the documented diagnostic naming the skipped write, and lands
no file anywhere under the isolation root — one Windows-shaped root, one
POSIX-shaped root — without spawning a subprocess (`_cc_route`/`route()` is
monkeypatched to simulate State-1 in-process) and with no `cadence` marker.
"""
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
    """Drive `main()` for `--schema lessons --queue-scope central` with the
    native seam simulated absent (State-1), in-process, zero-spawn.

    Returns (exit_code, stdout, stderr).
    """
    mod = _load_module(f"coordinator_queue_append_{id(tmp_path)}")

    # Avoid a real `git rev-parse` subprocess spawn — cwd-repo resolution is
    # irrelevant to the central-scope refusal, which fires before any of it
    # is consulted.
    monkeypatch.setattr(
        mod, "resolve_checked_repo_root", lambda explicit_root=None: (None, {"verdict": "OK"})
    )

    # schema.describe (called by main() before queue_scope is even resolved,
    # to derive the shared-schema required-field set) is the one other
    # native-seam call reached before legacy_fn's refusal. Answering it with
    # an empty required/optional set is sufficient — the refusal below fires
    # before any field is read.
    def _fake_route(op_name, params, repo_root, legacy_fn):
        if op_name == "schema.describe":
            return {"required": [], "optional": [], "enums": {}, "applies_to": None}
        if op_name == "queue.append":
            # State-1 shape: route() calls the legacy callback in-process and
            # returns its result (None) directly — no transport, no spawn.
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
    # A Windows-shaped isolation root (drive letter + backslashes) exercised
    # purely as a string — no real Windows filesystem is required, since the
    # refusal fires before _output_path (and any os.path join against this
    # value) is ever reached.
    claude_klabauter_root = r"C:\Users\test\mirror-shaped-root"

    rc, stdout, stderr = _run_central_scope_legacy(monkeypatch, tmp_path, claude_klabauter_root)

    assert rc is None or rc == 0
    assert stdout == "", f"central-scope refusal must print no landing path, got: {stdout!r}"
    assert (
        "central-scope writes require the native queue.append op" in stderr
    ), f"expected the refusal diagnostic, got:\n{stderr}"
