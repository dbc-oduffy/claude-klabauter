"""An empty fanout plan halts saying the file is empty; a non-empty plan lacking sizing_object keeps its own refusal."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.roadmap_fanout_commit import _resolve_plans


def _plan(root, name, text):
    d = root / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(text.encode("utf-8"))
    return f"docs/plans/{name}"


@pytest.mark.parametrize("text", ["", "  \n\t\n"])
def test_empty_plan_says_file_is_empty(tmp_path, text):
    plan = _plan(tmp_path, "p.md", text)
    out, err = _resolve_plans(tmp_path, [plan])
    assert out is None
    assert err["refused"] == "fanout-plan-empty"
    assert "empty" in err["error"] and plan in err["error"]
    assert "cites no sizing_object" not in err["error"]


def test_nonempty_plan_without_sizing_still_cites_no_sizing(tmp_path):
    plan = _plan(tmp_path, "p.md", "---\ntitle: x\n---\n# body\n")
    out, err = _resolve_plans(tmp_path, [plan])
    assert out is None
    assert err["refused"] == "fanout-no-sizing"
    assert "cites no sizing_object" in err["error"]
    assert "empty" not in err["error"]
