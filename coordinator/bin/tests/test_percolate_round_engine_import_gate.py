"""A round refuses to push when a published `coordinator_core` module does not import at the
destination. Origin: a mirror shipped `terminal_commit.py` importing `within_handoffs_home`
from a `halted_record_commit.py` that lacked it, and the round pushed green.

Real interpreter probe over a tmp_path tree; the round-level test stubs the probe."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags  # noqa: F401  (spawn marker parity)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("percolate_round_engine_import_gate", _BIN_DIR / "percolate-round.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_PKG = "coordinator_core/ops/dispatch_emit"


def _tree(tmp_path: Path, *, provider_has_def: bool) -> Path:
    root = tmp_path / "dest"
    for d in ("coordinator_core", "coordinator_core/ops", _PKG):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "__init__.py").write_text("", encoding="utf-8")
    (root / _PKG / "halted_record_commit.py").write_text(
        "def within_handoffs_home():\n    return 1\n" if provider_has_def else "X = 1\n", encoding="utf-8"
    )
    (root / _PKG / "terminal_commit.py").write_text(
        "from coordinator_core.ops.dispatch_emit.halted_record_commit import within_handoffs_home\n",
        encoding="utf-8",
    )
    return root


def test_importer_of_a_missing_def_is_named(tmp_path):
    root = _tree(tmp_path, provider_has_def=False)

    failure = _mod._engine_import_failures(str(root), [f"{_PKG}/terminal_commit.py"])

    assert failure and "terminal_commit" in failure and "within_handoffs_home" in failure


def test_importer_alone_is_checked_even_when_only_the_provider_changed(tmp_path):
    root = _tree(tmp_path, provider_has_def=False)

    failure = _mod._engine_import_failures(str(root), [f"{_PKG}/halted_record_commit.py"])

    assert failure and "terminal_commit" in failure


def test_complete_tree_passes_and_writes_no_bytecode(tmp_path):
    root = _tree(tmp_path, provider_has_def=True)

    assert _mod._engine_import_failures(str(root), [f"{_PKG}/halted_record_commit.py"]) is None
    assert not list(root.rglob("__pycache__"))


def test_tests_and_foreign_paths_are_not_imported(tmp_path):
    root = _tree(tmp_path, provider_has_def=True)
    (root / "coordinator_core" / "tests").mkdir()
    (root / "coordinator_core" / "tests" / "test_x.py").write_text("raise ImportError('x')\n", encoding="utf-8")

    names = _mod._engine_modules_to_import(
        str(root), ["coordinator_core/tests/test_x.py", "docs/a.py", "coordinator_core/ops/dispatch_emit/__init__.py"]
    )

    assert names == [
        "coordinator_core.ops.dispatch_emit",
        "coordinator_core.ops.dispatch_emit.terminal_commit",
    ]


def test_one_interpreter_for_the_whole_list(tmp_path, monkeypatch):
    root = _tree(tmp_path, provider_has_def=True)
    calls = []
    real_run = _mod._run
    monkeypatch.setattr(_mod, "_run", lambda cmd, **kw: calls.append(cmd) or real_run(cmd, **kw))

    _mod._engine_import_failures(
        str(root), [f"{_PKG}/halted_record_commit.py", f"{_PKG}/terminal_commit.py"]
    )

    assert len(calls) == 1


def test_refusal_reason_carries_the_import_failure():
    reason = _mod._round_refusal_reason(
        real_returncode=0, declined_paths=[], has_review_warnings=False, engine_import_failure="boom"
    )
    assert reason == "boom"


def test_round_does_not_push_when_import_gate_fails(tmp_path, monkeypatch):
    import test_percolate_round as base  # same-directory harness

    monkeypatch.setattr(base._mod, "_engine_import_failures", lambda repo_root, pathspec: "mod -> ImportError: x")
    monkeypatch.setattr(base._mod, "_write_round_failure_marker", lambda *a, **k: None)
    monkeypatch.setattr(base.Path, "exists", lambda self: True)
    base._install_commit_pipeline_stub(monkeypatch)

    rc, out, spy, dest = base._run_round(tmp_path, monkeypatch)

    assert rc == base._mod._EXIT_FAIL
    assert " — FAIL" in out and " — PASS" not in out
    assert "engine-import-gate:  mod -> ImportError: x" in out
    assert not [c for c in spy.calls if c and str(c[0]) == "git" and "push" in [str(x) for x in c]]
