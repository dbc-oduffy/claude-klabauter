"""A 2-file edit under ops/dispatch_emit selects fewer than 30 test files, owned first, at default arguments."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.index_write import _build_entry
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.reverify_scope import _SUITE_BOUND, TRIPWIRE_BOUND, select_for_paths, select_tests

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

PKG = "ops/dispatch_emit"
EDITED = [f"{PKG}/requirements_ledger.py", f"{PKG}/terminal_commit_ledger.py"]
BOUND = _SUITE_BOUND + TRIPWIRE_BOUND


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _importing(mod: str) -> str:
    return f"from ops.dispatch_emit import {mod}\n\n\ndef test_x():\n    assert {mod}\n"


def _naming(mod: str) -> str:
    return f'NAME = "{PKG}/{mod}.py"\n\n\ndef test_n():\n    assert NAME\n'


@pytest.fixture
def shaped(tmp_path: Path) -> Path:
    """ops/dispatch_emit with 2 leaves, 2 owned tests, 40 importing tests and 40 naming tests."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["ops"]\n')
    _write(tmp_path, f"{PKG}/__init__.py", "")
    for leaf in ("requirements_ledger", "terminal_commit_ledger"):
        _write(tmp_path, f"{PKG}/{leaf}.py", "X = 1\n")
        _write(tmp_path, f"{PKG}/tests/test_{leaf}.py", _importing(leaf))
    for i in range(20):
        _write(tmp_path, f"{PKG}/tests/test_imp_a_{i:03d}.py", _importing("requirements_ledger"))
        _write(tmp_path, f"ops/other/tests/test_imp_b_{i:03d}.py", _importing("terminal_commit_ledger"))
        _write(tmp_path, f"ops/other/tests/test_name_a_{i:03d}.py", _naming("requirements_ledger"))
        _write(tmp_path, f"ops/other/tests/test_name_b_{i:03d}.py", _naming("terminal_commit_ledger"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_select_tests_for_a_two_file_dispatch_emit_edit_is_bounded_owned_first(shaped: Path) -> None:
    base = _git(shaped, "rev-parse", "HEAD")
    for rel in EDITED:
        _write(shaped, rel, "X = 2\n")
    _git(shaped, "add", "-A")
    _git(shaped, "commit", "-q", "-m", "edit")
    out = select_tests(shaped, base)
    selected = out["tests"] + out["tripwires"]
    assert len(selected) < 30
    assert len(selected) <= BOUND
    assert len(out["tests"]) <= _SUITE_BOUND
    assert len(out["tripwires"]) <= TRIPWIRE_BOUND
    assert len(out["tripwires_not_selected"]) == 32 - TRIPWIRE_BOUND
    assert not set(out["tests"]) & set(out["tripwires"])
    assert f"{PKG}/tests/test_requirements_ledger.py" in out["tests"]
    assert f"{PKG}/tests/test_terminal_commit_ledger.py" in out["tests"]
    assert all(t.startswith(f"{PKG}/tests/") for t in out["tripwires"][:5])


def _track(root: Path) -> None:
    rels = sorted(_git(root, "ls-files").splitlines())
    entries = b"".join(_build_entry(rel.encode(), 0o100644, "ab" * 20, os.stat(root / rel)) for rel in rels)
    raw = b"DIRC" + struct.pack(">II", 2, len(rels)) + entries + b"\x00" * 20
    (root / ".git" / "index").write_bytes(raw)


def test_select_for_paths_list_call_is_bounded_owned_first_and_spawn_free(
    shaped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.review_tripwires import _tracked_test_bytes, literal_namers

    for rel in EDITED:
        _write(shaped, rel, "X = 2\n")
    _track(shaped)
    namers = sorted(literal_namers(shaped, EDITED))

    def boom(*_a, **_k):
        raise AssertionError("select_for_paths spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    out = select_for_paths(shaped, EDITED, _tracked_test_bytes(shaped, EDITED))
    selected = out["owned"] + out["tripwires"]
    assert len(selected) < 30
    assert len(out["owned"]) <= _SUITE_BOUND and len(out["tripwires"]) <= TRIPWIRE_BOUND
    assert len(out["tripwires_not_selected"]) >= 30
    assert not set(out["owned"]) & set(out["tripwires"])
    assert f"{PKG}/tests/test_requirements_ledger.py" in out["owned"]
    assert f"{PKG}/tests/test_terminal_commit_ledger.py" in out["owned"]
    assert all(t.startswith(f"{PKG}/tests/") for t in out["tripwires"][:5])
    assert "reverify tripwires capped" in out["notice"]
    assert not any("test_name_" in t for t in out["tripwires"])
    assert all(t in out["tripwires_not_selected"] for t in namers)


def test_review_reverify_list_mode_names_owned_first_and_caps_tripwires(
    shaped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    from coordinator_core.ops.dispatch_emit import review_edit_source, review_edits
    from coordinator_core.session.machinery_paths import share_dir

    session, prefix = "sess-bound", "prep-bound"
    orig = {rel: b"X = 1\n" for rel in EDITED}
    snap = Path(share_dir(str(shaped), session)) / f"{prefix}.pre-review"
    snap.mkdir(parents=True)
    for data in orig.values():
        (snap / hashlib.sha256(data).hexdigest()).write_bytes(data)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": "abc1234", "recorded_at": "2026-10-19T00:00:00Z", "paths": list(orig),
            "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in orig.items()},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: {})
    for rel in EDITED:
        _write(shaped, rel, "X = 2\n")
    _track(shaped)
    out = rr.review_reverify(shaped, base="abc1234", slice_prefix=prefix, session_id=session)
    assert out["error"] is None
    assert out["edited"] == sorted(EDITED)
    assert len(out["owned"]) + len(out["tripwires"]) < 30
    assert len(out["owned"]) <= _SUITE_BOUND and len(out["tripwires"]) <= TRIPWIRE_BOUND
    assert len(out["tripwires_not_selected"]) >= 30
    assert not set(out["owned"]) & set(out["tripwires"])
    assert f"{PKG}/tests/test_requirements_ledger.py" in out["owned"]
    assert f"{PKG}/tests/test_terminal_commit_ledger.py" in out["owned"]
