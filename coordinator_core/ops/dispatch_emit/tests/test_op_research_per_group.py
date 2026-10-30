"""dispatch.emit per_group: one single-notebook research emit per notebooklm target, the reply is the fan-out plan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
import coordinator_core.sizing_assemble as sa
from coordinator_core.ops import research_close
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_groups as rg
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.session import record_homes

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"
_NAME = "2026-10-08-r.yaml"
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-per-group")
    return tmp_path


def _sizing(repo: Path, letters: str = "ABC", *extra: str) -> str:
    home = Path(record_homes.home_dir(str(repo), "sizings"))
    home.mkdir(parents=True, exist_ok=True)
    (home / _NAME).write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    flags = [f for letter in letters for f in ("--research-target", f"notebooklm={letter}")]
    assert sa.main([
        "--tshirt", "XS", "--interaction-mode", "pm", "--write", rel,
        "--research-class", "corpus", "--research-source", "notebooklm",
        "--research-notebooklm-need", "youtube", *flags, *extra,
    ]) == sa.EXIT_OK
    return rel


def _emit(repo: Path, rel: str, **params):
    return op_module._dispatch_emit({"from_sizing": rel, rg.PER_GROUP_PARAM: True, **params}, repo)


def test_three_notebooks_yield_three_independent_runs(repo):
    rel = _sizing(repo)
    reply = _emit(repo, rel)
    groups = reply[rg.GROUPS_REPLY_KEY]
    assert reply[rg.PER_GROUP_PARAM] is True and reply["ok"]
    assert [g["ref"] for g in groups] == ["A", "B", "C"]
    assert [g["priority"] for g in groups] == [1, 2, 3]
    run_ids = [g["run_id"] for g in groups]
    assert len(set(run_ids)) == 3
    assert len({g["path"] for g in groups}) == 3
    assert reply["path"] == groups[0]["path"] and reply["run_id"] == run_ids[0]
    assert "groups" not in reply
    for g in groups:
        script = Path(g["path"]).read_text(encoding="utf-8")
        assert g["run_id"] in Path(g["path"]).name
        params = g["next_action"]["params"]
        assert g["next_action"]["op"] == "research.close"
        assert params["run_id"] == g["run_id"]
        assert params["topic_slug"] == f"r-{g['group']}"
        assert params[rg.GROUP_RUNS_CLOSE_PARAM] == run_ids
        assert f"const NEXT_ACTION = {json.dumps(g['next_action'], sort_keys=True)};" in script
        assert Path(g["receipt"]).is_file()
        assert "1-entry notebooks roster" in script


def test_each_script_binds_exactly_one_notebook(repo):
    rel = _sizing(repo)
    groups = _emit(repo, rel)[rg.GROUPS_REPLY_KEY]
    for g in groups:
        receipt = json.loads(Path(g["receipt"]).read_text(encoding="utf-8"))
        assert receipt["pipelines"] == ["nlm-preflight", "notebooklm"]
        assert [s["lists"].get("notebooks") for s in receipt["segments"] if s["pipeline"] == "notebooklm"] == [[g["ref"]]]


def test_group_refs_re_emits_only_the_named_group_with_its_full_list_priority(repo):
    rel = _sizing(repo)
    reply = _emit(repo, rel, **{rg.GROUPS_PARAM: ["B"]})
    groups = reply[rg.GROUPS_REPLY_KEY]
    assert [(g["ref"], g["priority"]) for g in groups] == [("B", 2)]
    full = [g["run_id"] for g in _emit(repo, rel)[rg.GROUPS_REPLY_KEY]]
    assert groups[0]["run_id"] == full[1]
    assert groups[0]["next_action"]["params"][rg.GROUP_RUNS_CLOSE_PARAM] == full


def test_group_refs_alone_implies_per_group(repo):
    rel = _sizing(repo)
    reply = op_module._dispatch_emit({"from_sizing": rel, rg.GROUPS_PARAM: ["C"]}, repo)
    assert [g["ref"] for g in reply[rg.GROUPS_REPLY_KEY]] == ["C"]


def test_re_emit_reuses_each_groups_run_id(repo):
    rel = _sizing(repo)
    first = [g["run_id"] for g in _emit(repo, rel)[rg.GROUPS_REPLY_KEY]]
    again = [g["run_id"] for g in _emit(repo, rel, force=True)[rg.GROUPS_REPLY_KEY]]
    assert first == again


def test_refusals_reach_the_caller_together(repo):
    rel = _sizing(repo, "A")
    with pytest.raises(PipelineEmitRefused) as one:
        _emit(repo, rel)
    assert any("at least two" in r for r in one.value.reasons)
    rel = _sizing(repo)
    with pytest.raises(PipelineEmitRefused) as missing:
        _emit(repo, rel, **{rg.GROUPS_PARAM: ["Z"]})
    assert any("Z" in r for r in missing.value.reasons)


def test_per_group_refuses_a_shape_with_other_pipelines(repo):
    rel = _sizing(repo, "ABC", "--research-source", "web")
    with pytest.raises(PipelineEmitRefused) as exc:
        _emit(repo, rel)
    assert any("web" in r for r in exc.value.reasons)


def test_per_group_needs_from_sizing_and_takes_no_output_path(repo):
    with pytest.raises(ValueError, match="from_sizing"):
        op_module._dispatch_emit({rg.PER_GROUP_PARAM: True}, repo)
    rel = _sizing(repo)
    with pytest.raises(ValueError, match="output_path"):
        _emit(repo, rel, output_path=str(repo / "x.workflow.mjs"))
    with pytest.raises(ValueError, match="research"):
        _emit(repo, rel, research=True)


def test_a_plain_emit_of_the_same_sizing_is_one_run_with_every_notebook(repo):
    rel = _sizing(repo)
    reply = op_module._dispatch_emit({"from_sizing": rel}, repo)
    assert rg.GROUPS_REPLY_KEY not in reply and rg.PER_GROUP_PARAM not in reply
    assert rg.GROUP_RUNS_CLOSE_PARAM not in reply["next_action"]["params"]
    assert "3-entry notebooks roster" in Path(reply["path"]).read_text(encoding="utf-8")


def test_group_runs_param_name_matches_what_research_close_reads():
    assert rg.GROUP_RUNS_CLOSE_PARAM == "group_runs"
    assert f'"{rg.GROUP_RUNS_CLOSE_PARAM}"' in Path(research_close.__file__).read_text(encoding="utf-8")


def test_twelve_groups_spawn_nothing_and_stay_under_the_process_bar(repo, monkeypatch):
    import subprocess
    import time

    spawns: list = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (spawns.append(a), real_popen(*a, **k))[1])
    rel = _sizing(repo, "ABCDEFGHIJKL")
    start = time.process_time()
    reply = _emit(repo, rel)
    elapsed_ms = (time.process_time() - start) * 1000
    print(f"per_group 12 groups: {elapsed_ms:.0f}ms process time, {len(spawns)} spawns")
    assert len(reply[rg.GROUPS_REPLY_KEY]) == 12 and spawns == []
    assert elapsed_ms < 500


def test_manifests_are_loaded_once_across_groups(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import pipeline_manifest

    loads: list[str] = []
    real = pipeline_manifest.load_manifest
    monkeypatch.setattr(pipeline_manifest, "load_manifest", lambda root, name: (loads.append(name), real(root, name))[1])
    rel = _sizing(repo, "ABCD")
    _emit(repo, rel)
    assert sorted(loads) == sorted(set(loads))
