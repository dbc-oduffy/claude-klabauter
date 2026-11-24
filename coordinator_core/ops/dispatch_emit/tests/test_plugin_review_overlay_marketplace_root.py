"""The engine overlay's installed_plugin_roots follows a marketplace-root installPath to its entry source."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import plugin_review_overlay as overlay


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, install: Path) -> None:
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {key: [{"installPath": str(install)}]}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv(overlay.PLUGIN_ROOTS_ENV, raising=False)


def _marketplace_root(tmp_path: Path, entries, make_source: bool = True) -> Path:
    root = tmp_path / "mroot"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": entries}), encoding="utf-8")
    if make_source:
        (root / "plugin").mkdir()
    return root


def _resolved() -> list[Path]:
    return [p.resolve() for p in overlay.installed_plugin_roots()]


def test_marketplace_root_resolves_to_entry_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [{"name": "other", "source": "./nope"}, {"name": "project-rag", "source": "./plugin"}])
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert _resolved() == [(root / "plugin").resolve()]


def test_install_with_plugin_json_resolves_to_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [{"name": "project-rag", "source": "./plugin"}])
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "project-rag"}), encoding="utf-8")
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert _resolved() == [root.resolve()]


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
    assert _resolved() == [root.resolve()]


def test_source_escaping_install_root_keeps_install_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    root = _marketplace_root(tmp_path, [{"name": "project-rag", "source": "../outside"}])
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert _resolved() == [root.resolve()]


def test_malformed_marketplace_json_raises_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _marketplace_root(tmp_path, [])
    (root / ".claude-plugin" / "marketplace.json").write_text("{not json", encoding="utf-8")
    _install(monkeypatch, tmp_path, "example-retrieval-repo@x", root)
    assert _resolved() == [root.resolve()]
