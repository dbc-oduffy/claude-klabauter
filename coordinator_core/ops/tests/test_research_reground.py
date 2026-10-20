"""research.reground: moves a stale grounding.md aside and emits a grounding-only script."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import _research_grounding as rg
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.research_close import _handler as close_handler
from coordinator_core.ops.research_reground import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FIXTURE = Path(op_module.__file__).parent / "tests" / "fixtures" / "pipeline_research"
_STAGE = """  - id: grounding
    phase: Ground
    agent_type: general-purpose
    model: sonnet
    template: grounding-template.md
    depends_on: [scout]
    web_caller: true
    output: "{{scratch_dir}}/grounding.md"
"""
_GOOD = (
    "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **grounded** b holds\n"
    "## Contradictions\n- **real** sources disagree\n"
)
_BAD = _GOOD + "- **contradicted** a claim its source denies\n"
RUN = "run1"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    root = tmp_path / "content"
    shutil.copytree(_FIXTURE, root)
    pdir = root / "pipelines" / "deep-research"
    manifest = pdir / "scouts.manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").rstrip("\n") + "\n" + _STAGE, encoding="utf-8")
    (pdir / "grounding-template.md").write_text(
        "Your brief is the file `{{brief}}`. Ground the digests under `{{scratch_dir}}`.\n", encoding="utf-8"
    )
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-reground")
    return r


@pytest.fixture
def scratch(repo: Path) -> Path:
    s = repo / "scratch" / "warp" / RUN
    s.mkdir(parents=True)
    (s / "digest-a.md").write_text("alpha\n")
    (s / "digest-b.md").write_text("beta\n")
    (s / "ask.md").write_text("What is X?\n")
    (s / "grounding.md").write_text(_BAD, encoding="utf-8")
    return s


def _reground(repo: Path, **extra) -> dict:
    params = {"run_id": RUN}
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def _snapshot(repo: Path) -> dict:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in (repo / "scratch").rglob("*") if p.is_file()
    }


def test_happy_path_moves_report_aside_and_emits_script(repo, scratch):
    out = _reground(repo)
    assert out["exit_code"] == 0, out
    assert out["generation"] == 1 and out["stale"] == "grounding.stale-1.md"
    assert not (scratch / "grounding.md").exists()
    assert (scratch / "grounding.stale-1.md").read_text(encoding="utf-8") == _BAD
    brief = (scratch / "reground-1.md").read_text(encoding="utf-8")
    assert brief.startswith("What is X?\n") and "generation 1" in brief and "grounding.stale-1.md" in brief
    script = repo / "scratch" / "warp" / f"{RUN}.reground-1.workflow.mjs"
    assert out["script"] == script.as_posix() and "\\" not in out["script"]
    assert out["next_action"] == {"tool": "Workflow", "scriptPath": out["script"]}
    assert out["then"]["op"] == "research.close"
    assert out["brief"] == f"scratch/warp/{RUN}/reground-1.md"
    text = script.read_text(encoding="utf-8")
    assert text.count("agent(") == 1 and out["brief"] in text
    receipt = json.loads(Path(str(script) + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["session_id"] == "sess-reground" and receipt["sha256"]


def test_second_generation_leaves_the_first_untouched(repo, scratch):
    first = _reground(repo)
    gen1 = {k: v for k, v in _snapshot(repo).items() if "reground-1" in k or "stale-1" in k}
    (scratch / "grounding.md").write_text(_BAD + "\n", encoding="utf-8")
    second = _reground(repo)
    assert second["generation"] == 2 and second["stale"] == "grounding.stale-2.md"
    after = _snapshot(repo)
    assert all(after[k] == v for k, v in gen1.items())
    assert first["script"] != second["script"]


def test_absent_grounding_report_still_emits_with_null_stale(repo, scratch):
    (scratch / "grounding.md").unlink()
    out = _reground(repo)
    assert out["exit_code"] == 0 and out["stale"] is None and out["generation"] == 1
    assert not list(scratch.glob("grounding.stale-*"))


def test_unfired_generation_still_advances(repo, scratch):
    assert _reground(repo)["generation"] == 1
    assert _reground(repo)["generation"] == 2


def test_prompt_differs_per_generation(repo, scratch):
    one = _reground(repo)
    two = _reground(repo)
    s1 = Path(one["script"]).read_text(encoding="utf-8")
    s2 = Path(two["script"]).read_text(encoding="utf-8")
    assert one["brief"] in s1 and two["brief"] in s2
    assert one["brief"] not in s2 and two["brief"] not in s1
    assert f"{RUN}/ask.md" not in s1


def test_refusals_write_nothing(repo, scratch, tmp_path):
    def refused(**params):
        before = _snapshot(repo)
        out = asyncio.run(_handler({"run_id": RUN, **params}, repo / ".git"))
        assert out["exit_code"] == 1 and out["error"], out
        assert _snapshot(repo) == before
        assert not list((repo / "scratch" / "warp").glob("*.mjs*"))
        return out

    refused(run_id="../x")
    refused(scratch_dir=str(tmp_path / "missing"))
    refused(brief=str(tmp_path / "nope.md"))
    refused(scratch_dir="relative/dir")
    (scratch / "ask.md").unlink()
    refused()
    (scratch / "ask.md").write_text("What is X?\n")
    for digest in scratch.glob("digest-*.md"):
        digest.unlink()
    assert "digest" in refused()["error"]


def test_manifest_without_grounding_stage_refuses_naming_manifest(repo, scratch, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    before = _snapshot(repo)
    out = _reground(repo)
    assert out["exit_code"] == 1 and "grounding" in out["error"]
    assert _snapshot(repo) == before


def test_contradicted_then_reground_then_fresh_grounding_closes(repo, scratch):
    def close() -> dict:
        params = {
            "scratch_dir": scratch.as_posix(), "tier": "scouts", "run_id": RUN,
            "topic_slug": "topic", "require_grounding": True,
        }
        return asyncio.run(close_handler(params, repo / ".git"))

    refused = close()
    assert refused["exit_code"] == 1 and refused["deliverable"] is False
    assert refused["grounding"]["contradicted"] == 1

    out = _reground(repo, scratch_dir=scratch.as_posix())
    assert out["exit_code"] == 0 and out["stale"] == rg.stale_report_name(1)
    absent = close()
    assert absent["exit_code"] == 1 and "grounding.md" in absent["error"]

    (scratch / "grounding.md").write_text(_GOOD, encoding="utf-8")
    closed = close()
    assert closed["exit_code"] == 0 and closed["deliverable"] is True, closed
