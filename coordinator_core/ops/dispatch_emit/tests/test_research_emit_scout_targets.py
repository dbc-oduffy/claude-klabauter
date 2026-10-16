"""Scout questions with targets and the split/combine mode: ask.md rendering, validation, segment lists."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

SCRATCH = "scratch/run"
SHAPE: rc.ShapeResult = {"tier": "scouts", "reason": "t", "pipelines": ["scouts"], "flags": {}}  # type: ignore[typeddict-item]


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _mixed(tmp_path):
    (tmp_path / "pkg").mkdir()
    return [
        "Why is the sky blue?",
        {"text": "What does pkg do?", "target": "repo", "ref": "pkg"},
        {"text": "Is port 80 open?", "target": "local", "ref": "netstat -an"},
    ]


def test_split_writes_target_lines_only_for_repo_and_local(tmp_path):
    rel = re_.write_ask(tmp_path, SCRATCH, "Feasibility", _mixed(tmp_path))
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "Research questions:\n\n1. Why is the sky blue?\n2. What does pkg do?\n3. Is port 80 open?" in text
    assert "## why-is-the-sky-blue\n\nWhy is the sky blue?" in text
    assert "## what-does-pkg-do\n\nTarget: repo `pkg`\n\nWhat does pkg do?" in text
    assert "## is-port-80-open\n\nTarget: local `netstat -an`\n\nIs port 80 open?" in text
    assert text.count("Target: ") == 2


def test_combine_writes_one_numbered_section_and_one_item(tmp_path):
    qs = _mixed(tmp_path)
    rel = re_.write_ask(tmp_path, SCRATCH, "Feasibility", qs, mode="combine")
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert text.count("\n## ") == 1
    section = text.split("## question\n\n", 1)[1]
    assert section.startswith("1. Why is the sky blue?\n2. Target: repo `pkg`\n   What does pkg do?\n3. Target: local")
    segs = re_.segments_for(SHAPE, brief_rel=rel, scratch_rel=SCRATCH, questions=qs, mode="combine")
    assert segs[0][1].lists["questions"] == (rc.COMBINED_SCOUT_ITEM,)


def test_split_segments_have_one_slug_per_question(tmp_path):
    qs = _mixed(tmp_path)
    segs = re_.segments_for(SHAPE, brief_rel="b", scratch_rel=SCRATCH, questions=qs)
    assert segs[0][1].lists["questions"] == ("why-is-the-sky-blue", "what-does-pkg-do", "is-port-80-open")


def test_web_only_split_is_byte_identical_to_str_call(tmp_path):
    qs = ["Why is the sky blue?", "How/why do tides work?"]
    a = re_.write_ask(tmp_path / "a", SCRATCH, "Topic", qs)
    b = re_.write_ask(
        tmp_path / "b", SCRATCH, "Topic", [{"text": q, "target": "web", "ref": ""} for q in qs], mode="split"
    )
    assert (tmp_path / "a" / a).read_bytes() == (tmp_path / "b" / b).read_bytes()


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "q", "target": "repo", "ref": "missing/path"},
        {"text": "q", "target": "repo", "ref": ""},
        {"text": "q", "target": "ftp", "ref": "x"},
        {"text": "q", "target": "web", "ref": "x"},
        {"text": "q", "target": "local", "ref": ""},
        {"text": "q", "target": "local", "ref": "a\nb"},
    ],
)
def test_bad_targets_are_refused_naming_the_question(tmp_path, bad):
    with pytest.raises(PipelineEmitRefused) as err:
        re_.normalize_scout_questions(tmp_path, ["ok", bad])
    assert "scout question 2" in str(err.value.args)


def test_every_refusal_is_collected(tmp_path):
    bad = {"text": "q", "target": "local", "ref": ""}
    with pytest.raises(PipelineEmitRefused) as err:
        re_.normalize_scout_questions(tmp_path, [bad, bad])
    text = str(err.value.args)
    assert "scout question 1" in text and "scout question 2" in text


def test_repo_ref_outside_root_is_written_absolute(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (q,) = re_.normalize_scout_questions(root, [{"text": "q", "target": "repo", "ref": str(outside)}])
    assert q["ref"] == outside.resolve().as_posix()
    (q,) = re_.normalize_scout_questions(root, [{"text": "q", "target": "repo", "ref": str(root / "..")}])
    assert q["ref"] == tmp_path.resolve().as_posix()


def test_local_ref_is_never_statted(tmp_path):
    (q,) = re_.normalize_scout_questions(tmp_path, [{"text": "q", "target": "local", "ref": "no/such/thing"}])
    assert q["ref"] == "no/such/thing"


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "a", ["q"], mode="merge")
    with pytest.raises(PipelineEmitRefused):
        re_.segments_for(SHAPE, brief_rel="b", scratch_rel=SCRATCH, questions=["q"], mode="merge")


def test_single_question_has_no_preamble(tmp_path):
    rel = re_.write_ask(tmp_path, SCRATCH, "A", ["only one?"])
    assert "Research questions:" not in (tmp_path / rel).read_text(encoding="utf-8")


def test_cap_applies_to_mappings(tmp_path):
    over = [{"text": f"question {n}", "target": "web", "ref": ""} for n in range(rc.MAX_SCOUT_QUESTIONS + 1)]
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "a", over)
