"""installed_plugin_roots follows a marketplace-root installPath to its entry's source directory."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "plugin_review_signals_under_test", Path(__file__).resolve().parent.parent / "plugin_review_signals.py"
)
prs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prs)


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, install: Path) -> None:
    plugins = tmp_path / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"plugins": {key: [{"installPath": str(install)}]}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv(prs.PLUGIN_ROOTS_ENV, raising=False)


def _marketplace_root(tmp_path: Path, entries: list, make_source: bool = True) -> Path:
    root = tmp_path / "mroot"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": entries}), encoding="utf-8")
    if make_source:
        (root / "plugin").mkdir()
    return root


def test_marketplace_root_resolves_to_entry_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [{"name": "other", "source": "./nope"}, {"name": "project-rag", "source": "./plugin"}])
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert [p.resolve() for p in prs.installed_plugin_roots()] == [(root / "plugin").resolve()]


def test_install_with_plugin_json_resolves_to_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [{"name": "project-rag", "source": "./plugin"}])
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "project-rag"}), encoding="utf-8")
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert [p.resolve() for p in prs.installed_plugin_roots()] == [root.resolve()]


@pytest.mark.parametrize(
    "entries,make_source",
    [
        ([{"name": "project-rag", "source": "./plugin"}], False),
        ([{"name": "someone-else", "source": "./plugin"}], True),
        ([{"name": "project-rag", "source": {"source": "github"}}], True),
        ("not-a-list", True),
    ],
)
def test_unresolvable_marketplace_entry_keeps_install_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entries, make_source: bool
) -> None:
    root = _marketplace_root(tmp_path, entries, make_source)
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert [p.resolve() for p in prs.installed_plugin_roots()] == [root.resolve()]


def test_source_escaping_install_root_keeps_install_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    root = _marketplace_root(tmp_path, [{"name": "project-rag", "source": "../outside"}])
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert [p.resolve() for p in prs.installed_plugin_roots()] == [root.resolve()]


def test_malformed_marketplace_json_raises_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [])
    (root / ".claude-plugin" / "marketplace.json").write_text("{not json", encoding="utf-8")
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert [p.resolve() for p in prs.installed_plugin_roots()] == [root.resolve()]
