"""terminal_commit resolves the run's source memo from the plan, else the plan's sizing."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.run_close import close_job_path

X = "state/cross-repo/inbox/x.md"
Y = "state/cross-repo/inbox/y.md"


def _write(root, fm_extra, sizing_text=None):
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text(f"---\ntitle: p\n{fm_extra}---\n\nbody\n", encoding="utf-8")
    if sizing_text is not None:
        (root / "state" / "sizings" / "s.yaml").write_text(
            "requirement_register:\n  rows:\n  - id: R1\n    source_text: '" + sizing_text + "'\n",
            encoding="utf-8",
        )


def _legs(root, monkeypatch):
    monkeypatch.setattr(terminal_commit, "_launch_close_job", lambda *a, **k: True)
    ctx = {
        "run_id": "run-1",
        "plan_rel": "plan.md",
        "presets": {},
        "legs": {},
        "inline_review": {},
    }
    return terminal_commit._background_legs(root, ctx)


def _record(root):
    return json.loads((root / close_job_path("run-1")).read_text(encoding="utf-8"))


def test_plan_source_memo_falls_back_to_sizing(tmp_path):
    _write(tmp_path, "sizing_object: state/sizings/s.yaml\n", f"close {X} on landing")
    assert terminal_commit._plan_source_memo(tmp_path, "plan.md") == X


def test_plan_frontmatter_wins(tmp_path):
    _write(tmp_path, f"source_memo: {Y}\nsizing_object: state/sizings/s.yaml\n", X)
    assert terminal_commit._plan_source_memo(tmp_path, "plan.md") == Y


def test_no_memo_anywhere_is_none(tmp_path):
    _write(tmp_path, "sizing_object: state/sizings/s.yaml\n", "nothing here")
    assert terminal_commit._plan_source_memo(tmp_path, "plan.md") is None
    assert terminal_commit._plan_source_memo(tmp_path, None) is None


def test_background_legs_queue_memo_lifecycle_from_sizing(tmp_path, monkeypatch):
    _write(tmp_path, "sizing_object: state/sizings/s.yaml\n", f"close {X}")
    legs = _legs(tmp_path, monkeypatch)
    assert legs["memo-lifecycle"].status != "not-applicable"
    assert _record(tmp_path)["source_memo"] == X


def test_background_legs_not_applicable_without_memo(tmp_path, monkeypatch):
    _write(tmp_path, "sizing_object: state/sizings/s.yaml\n", "nothing here")
    legs = _legs(tmp_path, monkeypatch)
    assert legs["memo-lifecycle"].status == "not-applicable"
    assert "plan names no source memo" in legs["memo-lifecycle"].detail
