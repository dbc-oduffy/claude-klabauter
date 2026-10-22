"""Research route: list targets, allowlisted local probes bound onto the scouts segment, and from_sizing question targets."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest
from coordinator_core.ops.dispatch_emit.research_emit import probe_allowlist

_TESTS = Path(__file__).parent / "fixtures"
_PROBE_ROOT = _TESTS / "pipeline_research_probe"
_PLAIN_ROOT = _TESTS / "pipeline_research"


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _PROBE_ROOT)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-probe-targets")
    return tmp_path


def _ask(repo, questions, **extra):
    ctx, _, _ = op_module._research_route_setup({"scout_questions": questions, **extra}, repo, None, "Feasibility ask")
    return ctx


def _scouts(ctx):
    return next(s for s in ctx["shape"]["segments"] if s["pipeline"] == "scouts")


def _sizing(repo, **research):
    block = {"value_class": "scouts", "appetite": "medium", **research}
    path = repo / "state" / "sizings" / "s.yaml"
    path.write_text(yaml.safe_dump({"research": block}), encoding="utf-8")
    return "state/sizings/s.yaml"


def _from_sizing(repo, rel, **params):
    ctx, _, _ = op_module._research_route_setup(params, repo, rel, None)
    return ctx


_MIXED = [
    {"text": "Q1?", "targets": ["web", "repo:src"]},
    {"text": "Q2?", "targets": ["local:credential-entries:github"]},
    "Q3?",
]


def test_fixture_manifest_declares_the_probe_list_and_flag():
    manifest = load_manifest(_PROBE_ROOT, "scouts")
    assert rc.PROBES_LIST in manifest.lists and rc.LOCAL_PROBE_FLAG in manifest.flags


def test_split_binds_the_slug_of_the_local_question(repo):
    seg = _scouts(_ask(repo, _MIXED))
    slugs = seg["lists"]["questions"]
    assert seg["lists"][rc.PROBES_LIST] == [slugs[1]]
    assert seg["flags"][rc.LOCAL_PROBE_FLAG] is True


def test_combine_binds_the_combined_item(repo):
    seg = _scouts(_ask(repo, _MIXED, scout_mode="combine"))
    assert seg["lists"][rc.PROBES_LIST] == ["question"]
    assert seg["flags"][rc.LOCAL_PROBE_FLAG] is True


def test_no_local_target_binds_no_probe_and_keeps_the_scout_stage(repo):
    ctx = _ask(repo, ["Q1?", {"text": "Q2?", "targets": ["web", "repo:src"]}])
    seg = _scouts(ctx)
    assert seg["lists"].get(rc.PROBES_LIST, []) == []
    assert rc.LOCAL_PROBE_FLAG not in seg["flags"]
    assert [m.pipeline for m, _, _ in ctx["segments"]] == ["scouts"]


def test_list_targets_reach_the_reply_and_ask(repo):
    ctx = _ask(repo, _MIXED)
    slugs = _scouts(ctx)["lists"]["questions"]
    assert ctx["shape"]["scout_targets"][0] == {
        "slug": slugs[0], "targets": [{"target": "web", "ref": ""}, {"target": "repo", "ref": "src"}],
    }
    ask = (repo / ctx["brief"]).read_text(encoding="utf-8")
    assert ask.count("Target:") == 3 and "Target: web" in ask and "Target: local `credential-entries:github`" in ask


@pytest.mark.parametrize(
    "ref",
    ["local:not-a-probe", "local:credential-entries:a;rm", "local:claude-json-keys:x", "local:credential-entries"],
)
def test_bad_local_refs_are_refused_naming_the_question(repo, ref):
    with pytest.raises(PipelineEmitRefused, match="scout question 2"):
        _ask(repo, ["Q1?", {"text": "Q2?", "targets": [ref]}])


def _cli_ask(repo, ref):
    return _ask(repo, ["Q1?", {"text": "Q2?", "targets": [ref]}])


def test_cli_probe_with_an_allowlisted_verb_emits_cleanly(repo):
    seg = _scouts(_cli_ask(repo, "local:p4-readonly:depots"))
    assert seg["lists"][rc.PROBES_LIST] == [seg["lists"]["questions"][1]]
    assert seg["flags"][rc.LOCAL_PROBE_FLAG] is True


def test_probe_allowlist_maps_the_cli_probe_to_its_verbs_in_order():
    assert probe_allowlist(_PROBE_ROOT)["p4-readonly"] == (
        "info", "depots", "clients", "streams", "users", "groups", "labels", "servers",
    )


@pytest.mark.parametrize("ref", ["local:p4-readonly:submit", "local:p4-readonly:depots;rm", "local:p4-readonly"])
def test_cli_probe_with_a_non_allowlisted_or_missing_verb_is_refused(repo, ref):
    with pytest.raises(PipelineEmitRefused, match="scout question 2"):
        _cli_ask(repo, ref)


def test_refused_verb_message_names_the_allowed_verbs(repo):
    with pytest.raises(PipelineEmitRefused) as err:
        _cli_ask(repo, "local:p4-readonly:submit")
    assert "info" in str(err.value) and "depots" in str(err.value)


def _content_root_with_p4_entry(tmp_path, mutate):
    root = tmp_path / "content"
    shutil.copytree(_PROBE_ROOT, root)
    path = root / rc.SCOUTS_ASK_FRAGMENT
    doc = json.loads(path.read_text(encoding="utf-8"))
    entry = next(e for e in doc[rc.PROBE_ALLOWLIST_KEY] if e["id"] == "p4-readonly")
    mutate(entry)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return root


def _drop_verbs(entry):
    del entry[rc.PROBE_VERBS_KEY]


def _empty_verbs(entry):
    entry[rc.PROBE_VERBS_KEY] = {}


def _unsafe_verb(entry):
    entry[rc.PROBE_VERBS_KEY] = {"changes;rm": "x"}


def _verbs_not_a_map(entry):
    entry[rc.PROBE_VERBS_KEY] = ["depots"]


def _blank_verb_question(entry):
    entry[rc.PROBE_VERBS_KEY] = {"depots": ""}


def _drop_tool(entry):
    del entry[rc.PROBE_TOOL_KEY]


def _blank_tool(entry):
    entry[rc.PROBE_TOOL_KEY] = " "


def _drop_class(entry):
    del entry[rc.PROBE_CLASS_KEY]


def _wrong_class(entry):
    entry[rc.PROBE_CLASS_KEY] = "shell"


@pytest.mark.parametrize(
    "mutate",
    [_drop_verbs, _empty_verbs, _unsafe_verb, _verbs_not_a_map, _blank_verb_question,
     _drop_tool, _blank_tool, _drop_class, _wrong_class],
)
def test_malformed_cli_entry_is_refused_naming_the_file(repo, monkeypatch, tmp_path, mutate):
    root = _content_root_with_p4_entry(tmp_path, mutate)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    with pytest.raises(PipelineEmitRefused, match=r"scouts-ask-fragment.*CLI probe 'p4-readonly'"):
        _cli_ask(repo, "local:p4-readonly:depots")


def test_existing_takes_arg_probe_still_emits_beside_a_cli_probe(repo):
    seg = _scouts(_ask(repo, _MIXED))
    assert seg["flags"][rc.LOCAL_PROBE_FLAG] is True


def test_manifest_without_probes_drops_nothing_for_a_plain_ask_and_refuses_a_local_one(repo, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _PLAIN_ROOT)
    seg = _scouts(_ask(repo, ["Q1?", {"text": "Q2?", "targets": ["repo:src"]}]))
    assert rc.PROBES_LIST not in seg["lists"] and rc.LOCAL_PROBE_FLAG not in seg["flags"]
    with pytest.raises(PipelineEmitRefused, match="scouts-ask-fragment"):
        _ask(repo, _MIXED)


def test_local_target_against_a_probe_less_manifest_names_the_probes_list(repo, monkeypatch, tmp_path):
    root = tmp_path / "content"
    (root / "contract").mkdir(parents=True)
    (root / "contract" / "scouts-ask-fragment.json").write_bytes(
        (_PROBE_ROOT / rc.SCOUTS_ASK_FRAGMENT).read_bytes()
    )
    pipes = root / "pipelines" / "deep-research"
    pipes.mkdir(parents=True)
    for f in (_PLAIN_ROOT / "pipelines" / "deep-research").iterdir():
        (pipes / f.name).write_bytes(f.read_bytes())
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    with pytest.raises(PipelineEmitRefused, match="needs the probes list"):
        _ask(repo, _MIXED)


def test_from_sizing_question_target_index_out_of_range_is_refused(repo):
    for bad in (0, 3):
        rel = _sizing(repo, questions=["A?", "B?"], question_targets=[{"question": bad, "source": "web"}])
        with pytest.raises(PipelineEmitRefused, match=r"outside 1\.\.2"):
            _from_sizing(repo, rel)


def test_from_sizing_targets_without_questions_are_refused(repo):
    rel = _sizing(repo, question_targets=[{"question": 1, "source": "web"}])
    with pytest.raises(PipelineEmitRefused, match=r"outside 1\.\.0"):
        _from_sizing(repo, rel)


def test_from_sizing_emits_the_same_ask_as_the_scout_questions_param(repo):
    rel = _sizing(
        repo,
        questions=["Q1?", "Q2?"],
        question_targets=[
            {"question": 1, "source": "web"},
            {"question": 1, "source": "repo", "ref": "src"},
            {"question": 2, "source": "local", "ref": "credential-entries:github"},
        ],
    )
    sized = _from_sizing(repo, rel)
    questions = [
        {"text": "Q1?", "targets": ["web", "repo:src"]},
        {"text": "Q2?", "targets": ["local:credential-entries:github"]},
    ]
    param, _, _ = op_module._research_route_setup({"scout_questions": questions}, repo, None, None)
    assert (repo / sized["brief"]).read_bytes().replace(sized["run_id"].encode(), b"") == (
        (repo / param["brief"]).read_bytes().replace(param["run_id"].encode(), b"")
    )
    assert _scouts(sized)["lists"][rc.PROBES_LIST] == _scouts(sized)["lists"]["questions"][1:]
    assert sized["next_action"]["params"]["sizing"] == rel


def test_from_sizing_question_mode_combine_yields_the_combine_item(repo):
    rel = _sizing(repo, questions=["Q1?", "Q2?"], question_mode="combine")
    ctx = _from_sizing(repo, rel)
    assert ctx["shape"]["scout_mode"] == "combine"
    assert _scouts(ctx)["lists"]["questions"] == ["question"]


def test_explicit_scout_mode_that_differs_from_the_sizing_is_refused(repo):
    rel = _sizing(repo, questions=["Q1?"], question_mode="combine")
    with pytest.raises(PipelineEmitRefused, match="differs from the sizing"):
        _from_sizing(repo, rel, scout_mode="split")
    assert _from_sizing(repo, rel, scout_mode="combine")["shape"]["scout_mode"] == "combine"


def test_sizing_targets_change_the_run_id(repo):
    base = {"questions": ["Q1?", "Q2?"]}
    plain = _from_sizing(repo, _sizing(repo, **base))["run_id"]
    targeted = _from_sizing(
        repo, _sizing(repo, **base, question_targets=[{"question": 1, "source": "repo", "ref": "src"}])
    )["run_id"]
    assert plain != targeted


def test_emit_spawns_no_process(repo, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the research route spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    for attr in ("posix_spawn", "fork"):
        if hasattr(os, attr):
            monkeypatch.setattr(os, attr, refuse)
    _ask(repo, _MIXED)
