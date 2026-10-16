"""research_emit: segments from a shape result, the ask file, slugged scout questions, the deep roster."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops import research_shape
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import HaltCount, PipelineEmitRefused, PipelineInputs
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing

CONTENT = Path(__file__).parent / "fixtures" / "pipeline_research"
SCRATCH = "scratch/run-1"
BRIEF = f"{SCRATCH}/ask.md"


def _segments(research, **kw):
    shape = research_shape.shape(research)
    return shape, re_.segments_for(shape, brief_rel=BRIEF, scratch_rel=SCRATCH, **kw)


def _validate_all(segments):
    for pipeline, inputs in segments:
        validate(load_manifest(CONTENT, pipeline), inputs)


def test_bare_ask_writes_verbatim_and_yields_one_scouts_segment(tmp_path):
    ask = "What is X?\nWith detail."
    rel = re_.write_ask(tmp_path, SCRATCH, ask)
    assert rel == BRIEF
    assert (tmp_path / rel).read_bytes() == ask.encode()
    _, segs = _segments({"value_class": "scouts"})
    assert [p for p, _ in segs] == ["scouts"]
    inputs = segs[0][1]
    assert inputs.brief == rel
    assert len(inputs.lists["questions"]) == 1
    _validate_all(segs)


def test_two_questions_give_two_entries_and_headed_sections(tmp_path):
    qs = ["Why is the sky blue?", "How/why do tides work?"]
    rel = re_.write_ask(tmp_path, SCRATCH, "Topic: nature", qs)
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "## why-is-the-sky-blue\n\nWhy is the sky blue?" in text
    assert "## how-why-do-tides-work\n\nHow/why do tides work?" in text
    _, segs = _segments({"value_class": "scouts"}, questions=qs)
    assert segs[0][1].lists["questions"] == ("why-is-the-sky-blue", "how-why-do-tides-work")
    _validate_all(segs)


def test_three_questions_are_refused(tmp_path):
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "t", ["a", "b", "c"])
    with pytest.raises(PipelineEmitRefused):
        _segments({"value_class": "scouts"}, questions=["a", "b", "c"])


def test_empty_duplicate_and_unsluggable_questions_are_refused(tmp_path):
    for bad in (["?!"], ["Same?", "same"], [""]):
        with pytest.raises(PipelineEmitRefused):
            re_.scout_slugs(bad)
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "  ")


def test_halt_legs_joins_preflight_to_notebooklm_only_when_both_are_present():
    assert re_.halt_legs(["nlm-preflight", "web", "notebooklm"]) == {"nlm-preflight": "notebooklm"}
    assert re_.halt_legs(["web", "repo"]) == {}
    assert re_.halt_legs(["nlm-preflight"]) == {}
    assert re_.halt_legs(["notebooklm"]) == {}


def test_slug_is_filename_safe_and_capped():
    (slug,) = re_.scout_slugs(["What/why? " + "x" * 100])
    assert len(slug) <= re_.MAX_SLUG_LEN
    assert set(slug) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not slug.endswith("-") and not slug.startswith("-")


def test_notebooklm_and_web_corpus_yields_preflight_web_notebooklm_all_valid():
    shape, segs = _segments(
        {"value_class": "corpus", "sources": ["web", "notebooklm"]},
        targets=[{"source": "notebooklm", "ref": "A"}],
    )
    assert shape["pipelines"] == ["nlm-preflight", "web", "notebooklm"]
    assert [p for p, _ in segs] == ["nlm-preflight", "web", "notebooklm"]
    assert all(i.brief == BRIEF and i.scratch_dir == SCRATCH for _, i in segs)
    _validate_all(segs)
    assert load_manifest(CONTENT, "web").stages[0].produces_brief
    assert load_manifest(CONTENT, "nlm-preflight").stages[0].halts_unless == "ready"


def test_deep_yields_the_roster_roles_plus_a_specialist_per_source():
    _, segs = _segments({"value_class": "deep"}, sources=["web", "repo", "structured"])
    assert [p for p, _ in segs] == ["unblock"]
    roster = segs[0][1].lists["roster"]
    assert roster == (
        {"slug": "diagnose", "agent_type": "general-purpose"},
        {"slug": "decompose", "agent_type": "general-purpose"},
        {"slug": "challenge", "agent_type": "general-purpose"},
        {"slug": "web", "agent_type": "coordinator:research-specialist"},
        {"slug": "repo", "agent_type": "coordinator:repo-specialist"},
    )
    _validate_all(segs)


def test_notebooklm_targets_become_its_notebooks_list():
    targets = [{"source": "notebooklm", "ref": "A"}, {"source": "web", "ref": "x"}, {"source": "notebooklm", "ref": "B"}]
    _, segs = _segments({"value_class": "corpus", "sources": ["notebooklm"]}, targets=targets)
    lists = dict(segs)["notebooklm"].lists
    assert lists["notebooks"] == ("A", "B")
    _validate_all(segs)


def test_deep_with_no_sources_defaults_to_the_web_specialist():
    _, segs = _segments({"value_class": "deep"})
    assert [m["slug"] for m in segs[0][1].lists["roster"]][-1] == "web"


def test_deepest_depth_sets_the_repo_flag_only():
    shape, segs = _segments({"value_class": "corpus", "sources": ["web", "repo"], "depth": "deepest"})
    assert shape["pipelines"] == ["web", "repo"]
    assert dict(segs[0][1].flags) == {}
    assert dict(segs[1][1].flags) == {"sonnet_scouts": "true", "deepest": "true"}


def test_the_deep_roster_carries_its_member_prompt_once():
    from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_pipeline_script

    manifest = load_manifest(CONTENT, "unblock")
    _, segs = _segments({"value_class": "deep"}, sources=["web", "repo"])
    inputs = segs[0][1]
    script = compose_pipeline_script(manifest, inputs, validate(manifest, inputs), run_id="r", agent_type_host=None)
    assert script.count("You are roster member") == 1


def test_context_files_are_named_in_the_ask(tmp_path):
    (tmp_path / "notes.md").write_text("n\n", encoding="utf-8")
    rel = re_.write_ask(tmp_path, SCRATCH, "What is X?")
    assert re_.bind_context(tmp_path, SCRATCH, rel, ["notes.md"]) == rel
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert text.startswith("What is X?\n\n## Context files")
    assert "- `notes.md`" in text


def test_a_sizing_brief_is_wrapped_never_edited(tmp_path):
    sizing = tmp_path / "state" / "sizings" / "s.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("x: 1\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-ext.md"
    outside.write_text("e\n", encoding="utf-8")
    rel = re_.bind_context(tmp_path, SCRATCH, "state/sizings/s.yaml", [str(outside)])
    assert rel == f"{SCRATCH}/brief.md"
    assert sizing.read_text(encoding="utf-8") == "x: 1\n"
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "`state/sizings/s.yaml`" in text and f"`{outside.resolve().as_posix()}`" in text


def test_no_context_keeps_the_brief_and_a_missing_file_is_refused(tmp_path):
    assert re_.bind_context(tmp_path, SCRATCH, "b.md", []) == "b.md"
    with pytest.raises(PipelineEmitRefused, match="nope.md"):
        re_.bind_context(tmp_path, SCRATCH, "b.md", ["nope.md"])


def _gated():
    return re_.gate_notebooklm(load_manifest(CONTENT, "notebooklm"))


def _stage(manifest, sid):
    return next(s for s in manifest.stages if s.id == sid)


def test_gate_binds_the_scope_and_scout_halts_and_fans_workers_over_the_scope_return():
    manifest = _gated()
    scope, scout, workers = (_stage(manifest, s) for s in ("scope", "scout", "workers"))
    assert scope.halts_unless_count.fields == ("worker_count", "notebooks")
    assert scope.halts_unless_count.equals == "notebooks"
    assert manifest.schemas[scope.schema]["required"] == ["strategy_path", "worker_count", "notebooks"]
    assert scout.halts_unless_nonempty == "usable_sources"
    assert "403" in manifest.schemas[scout.schema]["properties"]["usable_sources"]["items"]["properties"]["ref"]["description"]
    assert (workers.fan_out.source, workers.fan_out.field) == ("scope", "notebooks")
    assert not workers.fan_out.over_inputs


@pytest.mark.parametrize("missing", ["scope", "scout", "workers", "sweep"])
def test_gate_refuses_a_manifest_lacking_a_gated_stage_id(missing):
    manifest = load_manifest(CONTENT, "notebooklm")
    stripped = replace(manifest, stages=tuple(s for s in manifest.stages if s.id != missing))
    with pytest.raises(PipelineEmitRefused, match=f"lacks stage '{missing}'"):
        re_.gate_notebooklm(stripped)


def test_gate_keeps_a_stage_s_own_declared_halt():
    manifest = load_manifest(CONTENT, "notebooklm")
    own = HaltCount(("worker_count",), "notebooks")
    stages = tuple(replace(s, halts_unless_count=own) if s.id == "scope" else s for s in manifest.stages)
    assert _stage(re_.gate_notebooklm(replace(manifest, stages=stages)), "scope").halts_unless_count is own


def test_gated_manifest_validates_and_names_the_claims_output():
    manifest = _gated()
    inputs = PipelineInputs(brief=BRIEF, subjects=(), scratch_dir=SCRATCH, flags={}, lists={"notebooks": ("A", "B")})
    validate(manifest, inputs)
    assert re_.claims_output(manifest) == "merged-claims.json"


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


def _emit_from_sizing(monkeypatch, tmp_path, capsys, *research_flags):
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: CONTENT)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research-gate")
    rel = _write_sizing(tmp_path, "--research-class", "corpus", "--research-source", "notebooklm", *research_flags)
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    return json.loads(capsys.readouterr().out)


def test_a_notebooklm_run_halts_on_scope_and_scout_and_requires_claims_under_its_dir(monkeypatch, tmp_path, capsys):
    reply = _emit_from_sizing(monkeypatch, tmp_path, capsys, "--research-target", "notebooklm=A", "--research-target", "notebooklm=B")
    assert reply["next_action"]["params"]["require_claims"] == ["notebooklm/merged-claims.json"]
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert "usable_sources" in script and "worker_count" in script


def test_a_notebooklm_manifest_missing_a_gated_id_is_refused_at_emit(monkeypatch, tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit import pipeline_manifest as pm

    original = pm.load_manifest

    def without_scout(root, name):
        m = original(root, name)
        return replace(m, stages=tuple(s for s in m.stages if s.id != "scout")) if name == "notebooklm" else m

    monkeypatch.setattr(pm, "load_manifest", without_scout)
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: CONTENT)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research-gate")
    rel = _write_sizing(
        tmp_path, "--research-class", "corpus", "--research-source", "notebooklm", "--research-target", "notebooklm=A"
    )
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) != cli_module.EXIT_OK
    assert "lacks stage 'scout'" in capsys.readouterr().err
