"""mise-prep-upgrade declares the plan it rewrites through the declared-writes seam."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from coordinator_core.session import declared_writes

_SCRIPT = Path(__file__).resolve().parents[1] / "mise-prep-upgrade.py"
_PLAN = "---\ntitle: x\n---\nbody\n"


def _load():
    spec = importlib.util.spec_from_file_location("mise_prep_upgrade_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, tmp_path, argv_extra):
    mod = _load()
    seen: list = []

    def fake_seam(*, cwd=None, max_paths=None):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            with declared_writes.collecting(seen) as got:
                yield got

        return _cm()

    monkeypatch.setattr(mod, "recording_declared_writes", fake_seam)
    monkeypatch.setattr(mod, "not_a_plan_reason", lambda *a, **k: None)
    monkeypatch.setattr(
        mod,
        "plan_report",
        lambda path, text=None, parsed=None: {
            "plan": path,
            "error": None,
            "derivable": {"census": []},
            "residue": [],
        },
    )
    plan = tmp_path / "plan.md"
    plan.write_text(_PLAN, encoding="utf-8")
    mod.main([str(plan), "--quiet", *argv_extra])
    return plan, seen


def test_write_run_declares_exactly_the_rewritten_plan(monkeypatch, tmp_path):
    plan, seen = _run(monkeypatch, tmp_path, [])
    assert plan.read_text(encoding="utf-8") != _PLAN
    assert [Path(p) for p in seen] == [plan]


def test_check_run_writes_and_declares_nothing(monkeypatch, tmp_path):
    plan, seen = _run(monkeypatch, tmp_path, ["--check"])
    assert plan.read_text(encoding="utf-8") == _PLAN
    assert seen == []
