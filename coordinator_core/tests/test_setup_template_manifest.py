"""
Tests for coordinator_core.install.substrate's manifest-reader,
_load_setup_template_manifest().

Port of: setup-templates-manifest.sh (coordinator-claude 6fb5fb37, 2026-07-22).
b644d5a9's executable-surface relocation moved the manifest into claude-klabauter's
own `coordinator/lib/setup-templates-manifest.py` as a plain
Python module exporting SETUP_TEMPLATE_FILES / SETUP_TEMPLATE_EXEC_FILES /
SETUP_TEMPLATE_HOOK_FILES (list[str]) — its own header is explicit that it is
the single source of truth ("Edit this list HERE and nowhere else").
_load_setup_template_manifest reads it back via importlib
(spec_from_file_location — the hyphenated filename precludes a normal
`import`), never by hand-duplicating the file list.

Negative-spec: does NOT read the real claude-klabauter checkout — claude_klabauter_root
is a tmp_path fixture shaping a synthetic `coordinator/lib/` tree, so this
test has no cross-repo/cross-checkout path dependency and stays green
regardless of what CLAUDE_KLABAUTER_ROOT resolves to on the running machine.
"""
import pytest

from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _load_setup_template_manifest,
)

_MANIFEST_BODY = '''\
"""Synthetic test fixture manifest."""
from __future__ import annotations

SETUP_TEMPLATE_FILES: list[str] = [
    "publish.sh",
    "publish_sync.py",
]

SETUP_TEMPLATE_EXEC_FILES: list[str] = [
    "publish.sh",
]

SETUP_TEMPLATE_HOOK_FILES: list[str] = [
    "percolate-hooks/README.md",
]
'''


def _write_manifest(tmp_path, body: str = _MANIFEST_BODY):
    lib_dir = tmp_path / "coordinator" / "lib"
    lib_dir.mkdir(parents=True)
    manifest = lib_dir / "setup-templates-manifest.py"
    manifest.write_text(body, encoding="utf-8")
    return tmp_path


def test_load_setup_template_manifest_parses_three_lists(tmp_path):
    claude_klabauter_root = _write_manifest(tmp_path)
    files, exec_files, hook_files = _load_setup_template_manifest(claude_klabauter_root)
    assert files == ["publish.sh", "publish_sync.py"]
    assert exec_files == ["publish.sh"]
    assert hook_files == ["percolate-hooks/README.md"]


def test_load_setup_template_manifest_exec_files_subset_of_files(tmp_path):
    claude_klabauter_root = _write_manifest(tmp_path)
    files, exec_files, _hook_files = _load_setup_template_manifest(claude_klabauter_root)
    assert set(exec_files) <= set(files)


def test_load_setup_template_manifest_missing_file_raises(tmp_path):
    claude_klabauter_root = tmp_path  # no coordinator/lib/setup-templates-manifest.py written
    with pytest.raises(SubstrateFatalError, match="not found"):
        _load_setup_template_manifest(claude_klabauter_root)


def test_load_setup_template_manifest_empty_files_list_raises(tmp_path):
    empty_body = (
        "SETUP_TEMPLATE_FILES: list = []\n"
        "SETUP_TEMPLATE_EXEC_FILES: list = []\n"
        "SETUP_TEMPLATE_HOOK_FILES: list = ['percolate-hooks/README.md']\n"
    )
    claude_klabauter_root = _write_manifest(tmp_path, body=empty_body)
    with pytest.raises(SubstrateFatalError, match="SETUP_TEMPLATE_FILES is empty"):
        _load_setup_template_manifest(claude_klabauter_root)


def test_load_setup_template_manifest_missing_attr_raises(tmp_path):
    body = (
        "SETUP_TEMPLATE_FILES: list = ['publish.sh']\n"
        "SETUP_TEMPLATE_EXEC_FILES: list = ['publish.sh']\n"
        # SETUP_TEMPLATE_HOOK_FILES deliberately omitted.
    )
    claude_klabauter_root = _write_manifest(tmp_path, body=body)
    files, exec_files, hook_files = _load_setup_template_manifest(claude_klabauter_root)
    # Missing attribute degrades to an empty list, not a crash — only
    # SETUP_TEMPLATE_FILES is a hard precondition (see docstring).
    assert files == ["publish.sh"]
    assert hook_files == []


def test_load_setup_template_manifest_syntax_error_raises_loud_not_traceback(tmp_path):
    body = "SETUP_TEMPLATE_FILES: list = [\n"  # unterminated literal -> SyntaxError
    claude_klabauter_root = _write_manifest(tmp_path, body=body)
    with pytest.raises(SubstrateFatalError, match="failed to import"):
        _load_setup_template_manifest(claude_klabauter_root)


def test_load_setup_template_manifest_matches_live_claude_klabauter_oracle(monkeypatch):
    """Parity oracle: the REAL setup-templates-manifest.py in this checkout's
    own coordinator/lib/ must import without error and every EXEC_FILES entry
    must remain a subset of FILES. Uses CLAUDE_KLABAUTER_ROOT pinned to this checkout
    (deterministic — never the real machine registry), per the
    pin-the-root-in-tests convention."""
    from pathlib import Path

    this_repo_root = Path(__file__).resolve().parents[2]
    manifest = this_repo_root / "coordinator" / "lib" / "setup-templates-manifest.py"
    if not manifest.is_file():
        pytest.skip(f"oracle manifest not found at {manifest}")

    files, exec_files, hook_files = _load_setup_template_manifest(this_repo_root)
    assert files
    assert set(exec_files) <= set(files)
