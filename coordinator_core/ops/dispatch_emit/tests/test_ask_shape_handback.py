"""A shape-routed sizing ends --ask as a clean one-line hand-back, and a fresh ask sizes afresh."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_gate import gate, handback_line
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session import record_homes

_BASENAME = "2026-10-06-shape.yaml"
REL = Path(record_homes.record_path("", "sizings", _BASENAME)).as_posix()


def _put(repo, **over):
    Path(record_homes.home_dir(str(repo), "sizings")).mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "sizing-object",
        "name": "unclear job",
        "intent": "make the thing better somehow",
        "estimate": {"tshirt": "XL", "provisional": True},
        "route": "shape",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    doc.update(over)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_shape_sizing_hands_back_one_line_with_a_clean_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    _put(tmp_path)
    code = cli.main(["--sizing", REL, "--repo-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == (
        "Job unclear: unclear job. Needs a PM conversation (coordinator:shape room); "
        "re-run --ask once the JTBD is stated."
    )


def test_topic_falls_back_to_a_slice_of_intent():
    line = handback_line({"name": "", "intent": "x" * 200})
    assert line.startswith("Job unclear: " + "x" * 60 + ". ")


def test_gate_still_halts_shape_as_room(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    _put(tmp_path)
    v = gate(tmp_path, REL)
    assert v.halt["kind"] == "room" and v.halt["handback"].startswith("Job unclear: ")


def test_fresh_ask_sizes_afresh_never_reusing_a_shape_sizing():
    script = compose_ask_script(
        repo_root="REPO",
        prompt="the JTBD is now stated",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    assert "afresh, never reusing or editing an existing sizing routed `shape`" in script
