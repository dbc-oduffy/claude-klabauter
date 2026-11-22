"""The ledger view carries accepted PM rulings whose scope matches the run's words."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling

REL = "state/sizings/2026-10-01-rulings.yaml"
RUN = "ask-20261001T000000-cccccc"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, intent: str) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "rulings", "intent": intent,
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-rulings-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _ruling(root: Path, rid: str, scope: list[str], text: str, status: str = "accepted") -> None:
    extra = {"superseded_by": "rl-live"} if status == "superseded" else {}
    out = store.append(root, Ruling(id=rid, pm_turn="s.yaml R1", scope=tuple(scope), text=text, status=status, **extra))
    assert out.get("applied") == [rid], out


def _view(root: Path) -> str:
    return (rl.run_dir(root, RUN) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


def _start(root: Path) -> None:
    rl.start(root, RUN)
    rl.bind(root, RUN, REL, ask_text="the ask")


def test_matching_accepted_ruling_is_quoted_with_id_turn_and_closer(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-live", ["licensing"], "Private repo: no licence gate.")
    _start(root)
    view = _view(root)
    assert rl.RULINGS_HEADING in view
    assert '- rl-live (pm_turn: s.yaml R1): "Private repo: no licence gate."' in view
    assert view.rstrip().endswith(rl.RULINGS_CLOSER)


def test_no_match_writes_no_section(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-other", ["unrelated-scope"], "Something else.")
    _start(root)
    assert rl.RULINGS_HEADING not in _view(root)


def test_superseded_ruling_is_not_carried(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-live", ["unrelated-scope"], "Live.")
    _ruling(root, "rl-old", ["licensing"], "Old wording.", status="superseded")
    _start(root)
    assert rl.RULINGS_HEADING not in _view(root)


def test_register_source_text_also_supplies_tokens(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "plain intent")
    _ruling(root, "rl-live", ["unicorns"], "Unicorns are settled.")
    _start(root)
    assert rl.RULINGS_HEADING not in _view(root)
    rl.revise(root, RUN, verbatim="also handle unicorns")
    assert "rl-live" in _view(root)


def test_unreadable_ruling_file_is_shown_not_rendered_as_no_rulings(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _ruling(root, "rl-live", ["licensing"], "Private repo: no licence gate.")
    (root / store.register_dir(root).relative_to(root) / "rl-broken.yaml").write_text(": : [\n", encoding="utf-8")
    _start(root)
    view = _view(root)
    assert "rl-live" in view
    assert rl.RULINGS_UNREADABLE in view


def test_run_without_register_dir_still_renders(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path, monkeypatch, "harden the licensing check")
    _start(root)
    assert rl.RULINGS_HEADING not in _view(root)
