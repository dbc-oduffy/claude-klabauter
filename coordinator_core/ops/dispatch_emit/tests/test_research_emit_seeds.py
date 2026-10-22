"""research.seeds routing in the research emit: each seed feeds its own pipeline's segment brief; repo seeds are clone-instructed."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _Admitted, _write_sizing
from coordinator_core.session import record_homes

_FIXTURES = Path(__file__).parent / "fixtures"
URL_A = "https://example.com/org/engine-repo.git"
URL_B = "https://example.com/watch?v=abc123"
URL_C = "https://example.com/docs/guide"


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "content"
    for name in ("pipeline_research", "pipeline_research_multi_repo"):
        shutil.copytree(_FIXTURES / name, root, dirs_exist_ok=True)
    return root


@pytest.fixture
def repo(monkeypatch, tmp_path, content_root):
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content_root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-seeds")
    return work


def _seeded_sizing(repo: Path, seeds, *flags: str) -> str:
    rel = _write_sizing(repo, "--research-class", "corpus", *flags)
    path = Path(record_homes.home_dir(str(repo), "sizings")) / Path(rel).name
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["research"]["seeds"] = [{"kind": k, "url": u} for k, u in seeds]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return rel


def _setup(repo, rel):
    ctx, _, _ = op_module._research_route_setup({}, repo, rel, None)
    return ctx


def _brief(repo: Path, ctx: dict, segment: str) -> str:
    inputs = next(i for m, i, _ in ctx["segments"] if op_module.segment_name(m, i) == segment)
    return (repo / inputs.brief).read_text(encoding="utf-8")


def test_seed_kinds_route_to_their_own_pipelines(repo):
    rel = _seeded_sizing(
        repo, [("repo", URL_A), ("video", URL_B), ("doc", URL_C)], "--research-target", "notebooklm=A"
    )
    ctx = _setup(repo, rel)
    assert ctx["shape"]["pipelines"] == ["nlm-preflight", "repo", "notebooklm", "web"]
    names = [op_module.segment_name(m, i) for m, i, _ in ctx["segments"]]
    assert names == ["nlm-preflight", "repo-1-engine-repo-git", "notebooklm", "web"]
    repo_brief = _brief(repo, ctx, "repo-1-engine-repo-git")
    assert URL_A in repo_brief and "/repo-1-engine-repo-git/clone" in repo_brief
    assert URL_B not in repo_brief and URL_C not in repo_brief
    nlm = _brief(repo, ctx, "notebooklm")
    assert URL_B in nlm and URL_A not in nlm and URL_C not in nlm
    web = _brief(repo, ctx, "web")
    assert URL_C in web and URL_A not in web and URL_B not in web


def test_one_repo_seed_gets_one_keyed_segment_and_no_compare(repo):
    ctx = _setup(repo, _seeded_sizing(repo, [("repo", URL_A)], "--research-source", "repo"))
    names = [op_module.segment_name(m, i) for m, i, _ in ctx["segments"]]
    assert names == ["repo-1-engine-repo-git"]
    assert "require_outputs" not in ctx["next_action"]["params"]


def test_repo_target_plus_repo_seed_get_two_keyed_segments_and_a_compare(repo, tmp_path):
    target = (tmp_path / "targets" / "alpha").as_posix()
    rel = _seeded_sizing(
        repo, [("repo", URL_A)], "--research-source", "repo", "--research-target", f"repo={target}"
    )
    ctx = _setup(repo, rel)
    names = [op_module.segment_name(m, i) for m, i, _ in ctx["segments"]]
    assert names == ["repo-1-alpha", "repo-2-engine-repo-git", "repo-compare"]
    assert "clone" not in _brief(repo, ctx, "repo-1-alpha")
    assert "/repo-2-engine-repo-git/clone" in _brief(repo, ctx, "repo-2-engine-repo-git")


def test_a_notebooklm_segment_without_notebooks_or_video_seeds_still_refuses(repo):
    rel = _seeded_sizing(repo, [("doc", URL_C)], "--research-source", "notebooklm")
    with pytest.raises(PipelineEmitRefused) as exc:
        _setup(repo, rel)
    assert any(
        "pipeline 'notebooklm' needs its notebooks: record them on the sizing with "
        "sizing-assemble --research-target notebooklm=<letter or id>, or pass --list notebooks=A,B" in r
        for r in exc.value.reasons
    )


def test_video_seed_without_notebooks_refuses_naming_the_seed_and_the_clearing_flag(repo):
    rel = _seeded_sizing(repo, [("video", URL_B)])
    with pytest.raises(PipelineEmitRefused) as exc:
        _setup(repo, rel)
    text = " ".join(exc.value.reasons)
    assert "needs its notebooks" in text and f"video={URL_B}" in text and "--clear-research-seeds" in text


def test_the_emit_spawns_no_subprocess(repo, monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("emit spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _fail)
    rel = _seeded_sizing(
        repo, [("repo", URL_A), ("video", URL_B), ("doc", URL_C)], "--research-target", "notebooklm=A"
    )
    assert _setup(repo, rel)["segments"]


def test_bind_seed_briefs_refuses_an_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        re_.bind_seed_briefs(tmp_path, "scratch/run", "scratch/run/ask.md", [{"kind": "podcast", "url": "x"}])


def test_bind_seed_briefs_skips_repo_seeds(tmp_path):
    assert re_.bind_seed_briefs(tmp_path, "scratch/run", "b.md", [{"kind": "repo", "url": URL_A}]) == {}
    assert not any(tmp_path.rglob("*"))
