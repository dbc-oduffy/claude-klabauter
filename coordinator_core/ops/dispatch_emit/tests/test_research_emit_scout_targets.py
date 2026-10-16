"""Scout questions with lists of targets and the split/combine mode: ask.md rendering, validation, segment lists."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

SCRATCH = "scratch/run"
SHAPE: rc.ShapeResult = {"tier": "scouts", "reason": "t", "pipelines": ["scouts"], "flags": {}}  # type: ignore[typeddict-item]
ALLOW = {"claude-json-keys": False, "claude-settings-tree": False, "credential-entries": True}


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
        {"text": "Is the token stored?", "target": "local", "ref": "credential-entries:github"},
    ]


def _write(tmp_path, qs, mode="split"):
    rel = re_.write_ask(tmp_path, SCRATCH, "Feasibility", qs, mode=mode, allowlist=ALLOW)
    return (tmp_path / rel).read_text(encoding="utf-8")


def _lists(qs, mode="split"):
    seg = re_.segments_for(SHAPE, brief_rel="b", scratch_rel=SCRATCH, questions=qs, mode=mode)[0][1]
    return seg.lists, seg.flags


def test_split_writes_target_lines_only_for_repo_and_local(tmp_path):
    text = _write(tmp_path, _mixed(tmp_path))
    assert "Research questions:\n\n1. Why is the sky blue?\n2. What does pkg do?\n3. Is the token stored?" in text
    assert "## why-is-the-sky-blue\n\nWhy is the sky blue?" in text
    assert "## what-does-pkg-do\n\nTarget: repo `pkg`\n\nWhat does pkg do?" in text
    assert "## is-the-token-stored\n\nTarget: local `credential-entries:github`\n\nIs the token stored?" in text
    assert text.count("Target: ") == 2


def test_combine_writes_one_numbered_section_and_one_item(tmp_path):
    qs = _mixed(tmp_path)
    text = _write(tmp_path, qs, "combine")
    assert text.count("\n## ") == 1
    section = text.split("## question\n\n", 1)[1]
    assert section.startswith("1. Why is the sky blue?\n2. Target: repo `pkg`\n   What does pkg do?\n3. Target: local")
    assert _lists(qs, "combine")[0]["questions"] == (rc.COMBINED_SCOUT_ITEM,)


def test_a_question_lists_several_targets_in_order(tmp_path):
    (tmp_path / "src").mkdir()
    q = {"text": "Mixed?", "targets": ["web", "repo:src", "local:credential-entries:github"]}
    (n,) = re_.normalize_scout_questions(tmp_path, [q], ALLOW)
    assert n["targets"] == [
        {"target": "web", "ref": ""},
        {"target": "repo", "ref": "src"},
        {"target": "local", "ref": "credential-entries:github"},
    ]
    text = _write(tmp_path, [q])
    assert "Target: web\nTarget: repo `src`\nTarget: local `credential-entries:github`\n\nMixed?" in text
    assert text.count("Target: ") == 3
    combined = _write(tmp_path, [q], "combine")
    assert "1. Target: web\n   Target: repo `src`\n   Target: local `credential-entries:github`\n   Mixed?" in combined


def test_mapping_entries_and_normalised_output_are_valid_input(tmp_path):
    (tmp_path / "src").mkdir()
    q = {"text": "Q?", "targets": [{"target": "repo", "ref": "src"}, "web"]}
    first = re_.normalize_scout_questions(tmp_path, [q], ALLOW)
    assert re_.normalize_scout_questions(tmp_path, first, ALLOW) == first


def test_legacy_single_repo_form_renders_the_single_line(tmp_path):
    (tmp_path / "pkg").mkdir()
    text = _write(tmp_path, [{"text": "What?", "target": "repo", "ref": "pkg"}])
    assert text == "Feasibility\n\n## what\n\nTarget: repo `pkg`\n\nWhat?\n"


def test_web_only_split_is_byte_identical_to_str_call(tmp_path):
    qs = ["Why is the sky blue?", "How/why do tides work?"]
    a = re_.write_ask(tmp_path / "a", SCRATCH, "Topic", qs)
    b = re_.write_ask(
        tmp_path / "b", SCRATCH, "Topic", [{"text": q, "target": "web", "ref": ""} for q in qs], mode="split"
    )
    c = re_.write_ask(tmp_path / "c", SCRATCH, "Topic", [{"text": q, "targets": ["web"]} for q in qs])
    assert (tmp_path / "a" / a).read_bytes() == (tmp_path / "b" / b).read_bytes() == (tmp_path / "c" / c).read_bytes()
    assert "Target:" not in (tmp_path / "a" / a).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "q", "target": "repo", "ref": "missing/path"},
        {"text": "q", "target": "repo", "ref": ""},
        {"text": "q", "target": "ftp", "ref": "x"},
        {"text": "q", "target": "web", "ref": "x"},
        {"text": "q", "target": "local", "ref": ""},
        {"text": "q", "target": "local", "ref": "a\nb"},
        {"text": "q", "target": "local", "ref": "netstat -an"},
        {"text": "q", "target": "local", "ref": "not-a-probe"},
        {"text": "q", "targets": ["local:not-a-probe"]},
        {"text": "q", "targets": ["local:credential-entries:a;rm"]},
        {"text": "q", "targets": ["local:credential-entries:a b"]},
        {"text": "q", "targets": ["local:credential-entries:$(x)"]},
        {"text": "q", "targets": ["local:credential-entries"]},
        {"text": "q", "targets": ["local:credential-entries:"]},
        {"text": "q", "targets": ["local:claude-json-keys:x"]},
        {"text": "q", "targets": []},
        {"text": "q", "targets": ["web", "web"]},
        {"text": "q", "targets": ["ftp:x"]},
        {"text": "q", "targets": ["web:x"]},
        {"text": "q", "targets": [7]},
        {"text": "q", "target": "web", "targets": ["web"]},
    ],
)
def test_bad_targets_are_refused_naming_the_question(tmp_path, bad):
    with pytest.raises(PipelineEmitRefused) as err:
        re_.normalize_scout_questions(tmp_path, ["ok", bad], ALLOW)
    assert "scout question 2" in str(err.value.args)


@pytest.mark.parametrize("ref", ["claude-json-keys", "credential-entries:a.b_c-1", "claude-settings-tree"])
def test_allowlisted_local_refs_are_accepted_verbatim(tmp_path, ref):
    (q,) = re_.normalize_scout_questions(tmp_path, [{"text": "q", "target": "local", "ref": ref}], ALLOW)
    assert q["targets"] == [{"target": "local", "ref": ref}]


def test_any_local_target_is_refused_without_an_allowlist(tmp_path):
    with pytest.raises(PipelineEmitRefused) as err:
        re_.normalize_scout_questions(tmp_path, [{"text": "q", "targets": ["local:claude-json-keys"]}])
    assert "scout question 1" in str(err.value.args)


def test_every_refusal_is_collected(tmp_path):
    bad = {"text": "q", "target": "local", "ref": ""}
    with pytest.raises(PipelineEmitRefused) as err:
        re_.normalize_scout_questions(tmp_path, [bad, bad], ALLOW)
    text = str(err.value.args)
    assert "scout question 1" in text and "scout question 2" in text


def test_repo_ref_outside_root_is_written_absolute(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (q,) = re_.normalize_scout_questions(root, [{"text": "q", "target": "repo", "ref": str(outside)}])
    assert q["targets"][0]["ref"] == outside.resolve().as_posix()
    (q,) = re_.normalize_scout_questions(root, [{"text": "q", "target": "repo", "ref": str(root / "..")}])
    assert q["targets"][0]["ref"] == tmp_path.resolve().as_posix()


def test_probe_allowlist_reads_the_fragment(tmp_path):
    (tmp_path / "contract").mkdir()
    (tmp_path / rc.SCOUTS_ASK_FRAGMENT).write_text(
        json.dumps({rc.PROBE_ALLOWLIST_KEY: [
            {"id": "a", "ref": rc.PROBE_NO_REF}, {"id": "b", "ref": "service-name suffix"},
        ]}),
        encoding="utf-8",
    )
    assert re_.probe_allowlist(tmp_path) == {"a": False, "b": True}


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"probe_allowlist": [1]}', '{"probe_allowlist": [{"ref": "none"}]}'])
def test_probe_allowlist_refuses_a_missing_or_malformed_fragment(tmp_path, content):
    if content is not None:
        (tmp_path / "contract").mkdir()
        (tmp_path / rc.SCOUTS_ASK_FRAGMENT).write_text(content, encoding="utf-8")
    with pytest.raises(PipelineEmitRefused) as err:
        re_.probe_allowlist(tmp_path)
    assert rc.SCOUTS_ASK_FRAGMENT in str(err.value.args)


def test_split_segments_have_one_slug_per_question(tmp_path):
    lists, _ = _lists(_mixed(tmp_path))
    assert lists["questions"] == ("why-is-the-sky-blue", "what-does-pkg-do", "is-the-token-stored")


def test_split_probes_are_the_slugs_of_questions_with_a_local_target(tmp_path):
    qs = _mixed(tmp_path) + [{"text": "Another probe?", "targets": ["web", "local:claude-json-keys"]}]
    lists, flags = _lists(qs)
    assert lists[rc.PROBES_LIST] == ("is-the-token-stored", "another-probe")
    assert flags[rc.LOCAL_PROBE_FLAG] is True


def test_combine_probes_is_the_one_item_when_any_question_is_local(tmp_path):
    lists, flags = _lists(_mixed(tmp_path), "combine")
    assert lists[rc.PROBES_LIST] == (rc.COMBINED_SCOUT_ITEM,)
    assert flags[rc.LOCAL_PROBE_FLAG] is True


@pytest.mark.parametrize("mode", ["split", "combine"])
def test_no_local_target_binds_neither_probes_nor_the_flag(tmp_path, mode):
    """Unbound, not empty: a manifest without the probe stage refuses an undeclared list or flag."""
    (tmp_path / "pkg").mkdir()
    qs = ["Why?", {"text": "What?", "targets": ["web", "repo:pkg"]}]
    lists, flags = _lists(qs, mode)
    assert rc.PROBES_LIST not in lists and rc.LOCAL_PROBE_FLAG not in flags
    assert lists["questions"]


def test_a_bare_ask_binds_no_probes():
    lists, flags = _lists([])
    assert rc.PROBES_LIST not in lists and rc.LOCAL_PROBE_FLAG not in flags
    assert lists["questions"] == (rc.COMBINED_SCOUT_ITEM,)


def test_probes_bind_on_the_scouts_segment_only():
    shape: rc.ShapeResult = {"tier": "corpus", "reason": "t", "pipelines": ["scouts", "web"], "flags": {}}  # type: ignore[typeddict-item]
    segs = dict(re_.segments_for(shape, brief_rel="b", scratch_rel=SCRATCH, questions=[]))
    assert rc.PROBES_LIST not in segs["web"].lists and rc.LOCAL_PROBE_FLAG not in segs["web"].flags


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
