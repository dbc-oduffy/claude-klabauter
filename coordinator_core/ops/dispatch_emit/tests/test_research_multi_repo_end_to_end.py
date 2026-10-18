"""A corpus sizing with N repo targets emits one keyed repo segment per target plus a repo-compare segment, and research.close lands the comparison.

Assertions read the script text and next_action only; the composed script is never run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap import _Admitted
from coordinator_core.ops.dispatch_emit.tests.test_research_gate_end_to_end import _git, close_repo  # noqa: F401
from coordinator_core.ops.research_close import _handler
import coordinator_core.ops.dispatch_emit as _pkg

FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research_multi_repo"
_REFS = ("alpha", "beta", "gamma")
_KEYS = ["repo-1-alpha", "repo-2-beta", "repo-3-gamma"]


def _emit(monkeypatch, tmp_path, capsys, content_root, refs=_REFS):
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content_root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-multi-repo")
    flags = ["--research-class", "corpus", "--research-source", "repo"]
    for ref in refs:
        flags += ["--research-target", f"repo={(tmp_path / 'targets' / ref).as_posix()}"]
    rel = _write_sizing(tmp_path, *flags)
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    return Path(reply["path"]).read_text(encoding="utf-8"), reply


def test_one_script_holds_three_keyed_repo_blocks_then_the_compare_block(monkeypatch, tmp_path, capsys):
    script, reply = _emit(monkeypatch, tmp_path, capsys, FIXTURE)
    assert "parts" not in reply
    scratch = reply["scratch_dir"]
    at = [script.index(f"{scratch}/{key}/synthesis.md") for key in _KEYS]
    at.append(script.index(f"{scratch}/repo-compare/comparison.md"))
    assert at == sorted(at)
    assert [s["segment"] for s in reply["segments"]] == [*_KEYS, "repo-compare"]
    assert [s["pipeline"] for s in reply["segments"]] == ["repo"] * 3 + ["repo-compare"]
    for key in _KEYS:
        assert f"segment: '{key}'" in script


def test_no_repo_segment_writes_to_the_scratch_root(monkeypatch, tmp_path, capsys):
    script, reply = _emit(monkeypatch, tmp_path, capsys, FIXTURE)
    scratch = reply["scratch_dir"]
    for name in ("scope.md", "synthesis.md", "comparison.md"):
        assert f"{scratch}/{name}" not in script
    for key in _KEYS:
        assert f"{scratch}/{key}/scope.md" in script


def test_next_action_names_every_segment_dir_and_the_compare_output(monkeypatch, tmp_path, capsys):
    _, reply = _emit(monkeypatch, tmp_path, capsys, FIXTURE)
    params = reply["next_action"]["params"]
    assert params["segment_dirs"] == [*_KEYS, "repo-compare"]
    assert params["require_outputs"] == ["repo-compare/comparison.md"]
    assert "multi_repo" not in reply


def _seed(scratch: Path, comparison: bool) -> None:
    for key in _KEYS:
        (scratch / key).mkdir(parents=True)
        (scratch / key / "synthesis.md").write_text(f"{key} synthesis\n", encoding="utf-8")
    (scratch / "repo-compare").mkdir()
    if comparison:
        (scratch / "repo-compare" / "comparison.md").write_text("ranking\n", encoding="utf-8")


def _close(repo: Path, reply: dict, scratch: Path) -> dict:
    params = {**reply["next_action"]["params"], "scratch_dir": str(scratch)}
    params.pop("sizing", None)
    return asyncio.run(_handler(params, repo / ".git"))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_research_close_lands_the_comparison(monkeypatch, tmp_path, capsys, close_repo):  # noqa: F811
    _, reply = _emit(monkeypatch, tmp_path / "run", capsys, FIXTURE)
    scratch = tmp_path / "scratch"
    _seed(scratch, comparison=True)
    out = _close(close_repo, reply, scratch)
    assert out["committed"] is True and out["deliverable"] is True, out
    tracked = _git(close_repo, "ls-files").splitlines()
    assert any(p.endswith("repo-compare/comparison.md") for p in tracked)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_research_close_refuses_without_the_comparison(monkeypatch, tmp_path, capsys, close_repo):  # noqa: F811
    _, reply = _emit(monkeypatch, tmp_path / "run", capsys, FIXTURE)
    scratch = tmp_path / "scratch"
    _seed(scratch, comparison=False)
    before = _git(close_repo, "rev-list", "--count", "HEAD")
    out = _close(close_repo, reply, scratch)
    assert out["committed"] is False and out["deliverable"] is False, out
    assert _git(close_repo, "rev-list", "--count", "HEAD") == before


def test_one_repo_target_emits_today_s_unkeyed_script(monkeypatch, tmp_path, capsys):
    script, reply = _emit(monkeypatch, tmp_path, capsys, FIXTURE, refs=("alpha",))
    assert reply["pipelines"] == ["repo"]
    assert [s["segment"] for s in reply["segments"]] == ["repo"]
    assert "segment: '" not in script
    params = reply["next_action"]["params"]
    assert "require_outputs" not in params and "segment_dirs" not in params
    assert "multi_repo" not in reply


def test_an_over_cap_split_names_part_files_by_segment_key(monkeypatch, tmp_path, capsys):
    whole, _ = _emit(monkeypatch, tmp_path / "whole", capsys, FIXTURE)
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", len(whole.encode("utf-8")) - 1)
    _, reply = _emit(monkeypatch, tmp_path / "split", capsys, FIXTURE)
    parts = reply["parts"]
    assert len(parts) > 1
    paths = [p["path"] for p in parts]
    assert len(set(paths)) == len(paths)
    for part in parts:
        assert Path(part["path"]).name.endswith(f"-{part['pipeline']}.workflow.mjs")
    labels = "+".join(p["pipeline"] for p in parts).split("+")
    assert labels == [*_KEYS, "repo-compare"]


def test_a_content_root_without_repo_compare_emits_today_s_single_repo_segment(monkeypatch, tmp_path, capsys):
    bare = tmp_path / "bare"
    shutil.copytree(FIXTURE, bare)
    (bare / "pipelines" / "deep-research" / "repo-compare.manifest.yaml").unlink()
    script, reply = _emit(monkeypatch, tmp_path / "run", capsys, bare)
    assert reply["pipelines"] == ["repo"]
    assert "segment: '" not in script
    assert "require_outputs" not in reply["next_action"]["params"]
    assert "repo-compare" in reply["multi_repo"] and reply["multi_repo"].startswith("unavailable")
