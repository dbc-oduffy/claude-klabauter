"""Ledger rulings section: normalised-token scope match, feed records, and a visible unreadable line."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_requirements_ledger_pm_rulings import (
    RUN, _repo, _ruling, _start, _view,
)
from coordinator_core.rulings import store


def test_scope_matches_by_normalised_token_not_exact(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-lic", ["license-gates"], "No licence gate.")
    _ruling(root, "rl-miss", ["gateway"], "Unrelated.")
    _start(root)
    view = _view(root)
    assert "rl-lic" in view
    assert "rl-miss" not in view


def test_stopword_scope_does_not_match(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-stop", ["for-the"], "Noise.")
    _start(root)
    assert rl.RULINGS_HEADING not in _view(root)


def test_pm_decision_record_feed_is_carried(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    (root / "docs" / "decisions").mkdir(parents=True)
    (root / "docs" / "decisions" / "DR-9-lic.md").write_text(
        "---\ntitle: Licensing stance\nstatus: accepted\nauthority: PM\n---\n\n## PM verbatim\n\nPrivate repo.\n",
        encoding="utf-8", newline="\n",
    )
    (root / "docs" / "decisions" / "DR-8-em.md").write_text(
        "---\ntitle: Licensing em call\nstatus: accepted\ndeciders:\n  - em\n---\nbody\n",
        encoding="utf-8", newline="\n",
    )
    _start(root)
    view = _view(root)
    assert '- DR-9-lic (pm_turn: docs/decisions/DR-9-lic.md): "Private repo."' in view
    assert "DR-8-em" not in view


def test_unreadable_register_file_renders_visible_line(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-live", ["licensing"], "Live.")
    (root / "state" / "pm-rulings" / "broken.yaml").write_text("id: [", encoding="utf-8")
    _start(root)
    view = _view(root)
    assert "rl-live" in view
    assert f"{rl.RULINGS_UNREADABLE}: broken: unreadable" in view


def test_load_error_renders_visible_line_not_empty(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")

    def boom(_root):
        raise OSError("disk gone")

    monkeypatch.setattr(store, "scan", boom)
    _start(root)
    assert f"{rl.RULINGS_UNREADABLE}: OSError: disk gone" in _view(root)


def test_clean_empty_register_still_renders_nothing(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _start(root)
    assert rl.RULINGS_UNREADABLE not in _view(root)
