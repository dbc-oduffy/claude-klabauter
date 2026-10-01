"""Each sizing arm through `_dispatch_emit(sizing_path=...)`, real arms, over tmp_path fixture repos."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import op, sizing_fire, sizing_m_delegate
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_S,
    ARM_XS,
    S_STAGE1_PHASES,
    XS_PHASES,
    s_plan_path,
)

WRITES = ["src/widget.py"]


def _sizing(tshirt: str, route: str) -> dict:
    return {
        "schema": "sizing-object",
        "status": "sized",
        "estimate": {"tshirt": tshirt},
        "route": route,
        "interaction_mode": "fire-and-forget",
        "intent": "Add the widget",
        "exit_criterion": {
            "statement": "Widgets work end to end",
            "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "fire-and-forget"},
        },
    }


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    return tmp_path


def _write(repo: Path, name: str, doc: dict) -> str:
    (repo / "state" / "sizings" / name).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return f"state/sizings/{name}"


def _phases(text: str) -> list[str]:
    return re.findall(r"phase\('([^']+)'\)", text)


def _first_phase_index(text: str, constant: str) -> int:
    for i, p in enumerate(_phases(text)):
        if p.lower().replace(" ", "-").startswith(constant):
            return i
    raise AssertionError(f"no phase for {constant!r} in {_phases(text)}")


def test_xs_emits_executor_review_and_terminal_marker(repo):
    rel = _write(repo, "xs-job.yaml", _sizing("XS", "dispatch"))
    out = repo / "docs" / "xs-job.workflow.mjs"
    reply = op._dispatch_emit(
        {"sizing_path": rel, "output_path": str(out), "writes": WRITES}, repo_root=repo
    )
    assert reply["arm"] == ARM_XS
    text = out.read_text(encoding="utf-8")

    assert "Execute X1:" in text and "work:X1" in text
    execute, review = (_first_phase_index(text, c) for c in XS_PHASES[:2])
    assert execute < review
    assert "coordinator:code-reviewer" in text

    req = parse_marker(text)
    assert req is not None
    assert [c.id for c in req.chunks] == ["X1"]
    assert set(WRITES) <= {p for c in req.chunks for p in c.paths}


def test_s_emits_plan_author_then_fire_execute_over_the_plan_doc(repo):
    rel = _write(repo, "s-job.yaml", _sizing("S", "spec-dispatch"))
    out = repo / "docs" / "s-job.workflow.mjs"
    reply = op._dispatch_emit({"sizing_path": rel, "output_path": str(out)}, repo_root=repo)
    assert reply["arm"] == ARM_S
    text = out.read_text(encoding="utf-8")

    plan = s_plan_path(rel)
    assert _phases(text)[:2] == list(S_STAGE1_PHASES[:2])
    assert plan in text
    assert text.index("coordinator:plan-author") < text.index("emit-dispatch-workflow --plan")

    req = parse_marker(text)
    assert req is not None
    assert [p for c in req.chunks for p in c.paths] == [plan]


def test_m_plus_reaches_emit_wave_fire_with_from_sizing(repo, monkeypatch):
    rel = _write(repo, "m-job.yaml", _sizing("M", "plan"))
    plugin = repo / "doctrine" / "coordinator"
    (plugin / "workflows").mkdir(parents=True)
    (plugin / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    (plugin / "agents").mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(repo / "no-install"))

    seen: dict = {}
    real_load = sizing_m_delegate._load_emit_wave_fire

    def load(path):
        mod = real_load(path)
        mod._bind = lambda script_path, args, live=False: "const args = " + json.dumps(args) + ";\n"
        mod._load_mint = lambda: lambda sizing_rel, root: {
            "id": "hnd-1", "path": "state/handoffs/b1.md", "title": "Minted baton", "created": True,
        }
        real_main = mod.main

        def main(argv):
            seen["argv"] = list(argv)
            return real_main(
                [*argv, "--plugin-root", str(plugin), "--live-engine-tree"]
            )

        mod.main = main
        return mod

    monkeypatch.setattr(sizing_m_delegate, "_load_emit_wave_fire", load)
    (repo / "state" / "handoffs").mkdir()
    (repo / "state" / "handoffs" / "b1.md").write_text(
        "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n", encoding="utf-8"
    )
    trail = repo / "trail"

    reply = op._dispatch_emit({"sizing_path": rel, "trail_dir": str(trail)}, repo_root=repo)

    assert reply["arm"] == ARM_M_PLUS
    assert seen["argv"][seen["argv"].index("--from-sizing") + 1] == rel
    assert Path(reply["path"]).name == "fire-0-1.mjs"
    assert Path(reply["path"]).is_file()
    assert sizing_fire.ARM_ROUTE[ARM_M_PLUS] == "plan"
