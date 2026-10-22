"""assert-no-terminal-plans-in-live treats a `closed` plan as terminal, like `abandoned`."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "assert-no-terminal-plans-in-live.py"


def _load():
    spec = importlib.util.spec_from_file_location("assert_no_terminal_plans_in_live", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan(plans: Path, name: str, status: str, body: str = "") -> None:
    plans.joinpath(name).write_text(
        "---\nstatus: %s\n---\n%s\n" % (status, body), encoding="utf-8"
    )


def _run(mod, monkeypatch, tmp_path, holder_status: str) -> int:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    _plan(plans, "2026-01-01-target.md", "abandoned")
    _plan(plans, "2026-01-02-holder.md", holder_status, "see 2026-01-01-target.md")
    monkeypatch.setattr(mod, "_query_terminal_paths", lambda: ["docs/plans/2026-01-01-target.md"])
    monkeypatch.setattr(mod, "_coordinator_state_root", lambda: None)
    return mod.main(["--root", str(tmp_path)])


def test_closed_is_in_the_shared_terminal_tuple():
    mod = _load()
    assert "closed" in mod._TERMINAL_PLAN_STATUSES
    assert "abandoned" in mod._TERMINAL_PLAN_STATUSES


def test_closed_plan_is_excluded_from_live_plan_refs(monkeypatch, tmp_path):
    assert _run(_load(), monkeypatch, tmp_path, "closed") == 1


def test_abandoned_plan_is_excluded_from_live_plan_refs(monkeypatch, tmp_path):
    assert _run(_load(), monkeypatch, tmp_path, "abandoned") == 1


def test_active_plan_still_holds_its_reference(monkeypatch, tmp_path):
    assert _run(_load(), monkeypatch, tmp_path, "active") == 0
