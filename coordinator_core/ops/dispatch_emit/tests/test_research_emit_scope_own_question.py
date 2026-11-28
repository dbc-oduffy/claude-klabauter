"""A research slice's ask.md carries the slice sizing's own intent and questions, never a parent run's ask."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.research_emit import PipelineEmitRefused

PARENT_ASK = "Synthesize the whole parent run corpus on talk practices 1-13"
SLICE_INTENT = "Which talk practices beyond the DIRECTORY.md ratchet should claude-klabauter incorporate?"
QUESTIONS = ["What does the Staff Engineer conclude per practice?", "What does Angelique conclude per practice?"]


def _text(tmp_path, **kw):
    rel = re_.write_ask(tmp_path, "scratch/run", PARENT_ASK, QUESTIONS, **kw)
    return (tmp_path / rel).read_text(encoding="utf-8")


def test_slice_intent_replaces_the_parent_ask(tmp_path):
    text = _text(tmp_path, intent=SLICE_INTENT)
    assert text.startswith(SLICE_INTENT)
    assert PARENT_ASK not in text
    assert all(q in text for q in QUESTIONS)


def test_no_intent_keeps_the_given_ask(tmp_path):
    text = _text(tmp_path)
    assert text.startswith(PARENT_ASK)


def test_blank_intent_keeps_the_given_ask(tmp_path):
    assert _text(tmp_path, intent="   ").startswith(PARENT_ASK)


def test_intent_alone_satisfies_the_non_empty_ask_rule(tmp_path):
    rel = re_.write_ask(tmp_path, "scratch/run", "", QUESTIONS, intent=SLICE_INTENT)
    assert (tmp_path / rel).read_text(encoding="utf-8").startswith(SLICE_INTENT)


def test_empty_ask_and_empty_intent_still_refuse(tmp_path):
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, "scratch/run", "  ", QUESTIONS, intent="")


def test_research_route_from_sizing_binds_ask_to_the_sizing_intent(monkeypatch, tmp_path):
    from pathlib import Path

    import coordinator_core.ops.dispatch_emit as pkg
    import coordinator_core.sizing_assemble as sa
    from coordinator_core.ops.dispatch_emit import op as op_module
    from coordinator_core.session import record_homes

    class _Admitted:
        @staticmethod
        def await_admission(start_dir, *, hold_allowed):
            return {"verdict": "disabled"}

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: Path(__file__).parent / "fixtures" / "pipeline_research")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research")
    name = "2026-10-08-own.yaml"
    home = Path(record_homes.home_dir(str(tmp_path), "sizings"))
    home.mkdir(parents=True, exist_ok=True)
    (home / name).write_text(
        'schema: sizing-object\nintent: "Research the slice own question"\nestimate:\n  tshirt: XS\n  provisional: true\n'
        "route: dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: draft\npremise:\n  provenance: unrecorded\n"
        "  evidence: PLACEHOLDER - cite the evidence\ndeliverable_id: null\n",
        encoding="utf-8",
    )
    rel = Path(record_homes.record_path("", "sizings", name)).as_posix()
    assert sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, "--research-class", "scouts",
                    "--research-source", "web", "--research-question", "One?", "--research-question", "Two?"]) == sa.EXIT_OK
    ctx, _, _ = op_module._research_route_setup({}, tmp_path, rel, PARENT_ASK)
    ask_md = (tmp_path / ctx["brief"]).read_text(encoding="utf-8")
    assert ask_md.startswith("Research the slice own question")
    assert PARENT_ASK not in ask_md
