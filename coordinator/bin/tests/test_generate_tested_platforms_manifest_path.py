"""test_generate_tested_platforms_manifest_path.py — coverage for
`generate-tested-platforms::_manifest_path()`'s dual-layout resolution.

Context: `_MANIFEST_RELATIVE` used to be a single hardcoded
`coordinator/docs/install/agent-install-manifest.json` (example-doctrine-repo's layout).
In claude-klabauter's own checkout the manifest lives at
`docs/install/agent-install-manifest.json` (no `coordinator/` prefix), so the
tool's *default* invocation (no `--repo-root`, defaulting to claude-klabauter's own
checkout) resolved a path that does not exist. `--repo-root` is explicitly
designed to let this tool probe a foreign repo, so a single hardcoded
relative layout cannot serve both shapes.

Tests: example-doctrine-repo layout resolves, claude-klabauter layout resolves, neither-exists
raises FileNotFoundError naming both attempted paths.

Spec backlink: slice-3 review carried loose end, claude-klabauter
state/subagent-share/451e7b37-e354-4aab-9cad-b810b8fadf90/
coordinatorcode-reviewer-da1ad9fb.md (finding 5).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_HELPER_PATH = _BIN_DIR / "generate-tested-platforms"


def _load_module(path: Path, module_name: str):
    """Load the extension-less/hyphenated helper as a Python module for testing."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_helper = _load_module(_HELPER_PATH, "generate_tested_platforms_manifest_path_test")


def test_resolves_example_doctrine_repo_layout(tmp_path) -> None:
    manifest_dir = tmp_path / "coordinator" / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_file = manifest_dir / "agent-install-manifest.json"
    manifest_file.write_text("{}", encoding="utf-8")

    resolved = _helper._manifest_path(str(tmp_path))
    assert resolved == str(manifest_file)


def test_resolves_claude_klabauter_layout(tmp_path) -> None:
    manifest_dir = tmp_path / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_file = manifest_dir / "agent-install-manifest.json"
    manifest_file.write_text("{}", encoding="utf-8")

    resolved = _helper._manifest_path(str(tmp_path))
    assert resolved == str(manifest_file)


def test_prefers_example_doctrine_repo_layout_when_both_present(tmp_path) -> None:
    doe_dir = tmp_path / "coordinator" / "docs" / "install"
    doe_dir.mkdir(parents=True)
    doe_file = doe_dir / "agent-install-manifest.json"
    doe_file.write_text("{}", encoding="utf-8")

    claude_klabauter_dir = tmp_path / "docs" / "install"
    claude_klabauter_dir.mkdir(parents=True)
    claude_klabauter_file = claude_klabauter_dir / "agent-install-manifest.json"
    claude_klabauter_file.write_text("{}", encoding="utf-8")

    resolved = _helper._manifest_path(str(tmp_path))
    assert resolved == str(doe_file)


def test_neither_layout_present_raises_naming_both_attempted_paths(tmp_path) -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        _helper._manifest_path(str(tmp_path))

    message = str(excinfo.value)
    expected_doe = str(tmp_path / "coordinator" / "docs" / "install" / "agent-install-manifest.json")
    expected_claude_klabauter = str(tmp_path / "docs" / "install" / "agent-install-manifest.json")
    assert expected_doe in message
    assert expected_claude_klabauter in message
