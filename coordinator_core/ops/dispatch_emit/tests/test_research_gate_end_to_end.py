"""The research route's gate, end to end: the emitted script halts ahead of the workers, workers fan over the strategy, and research.close refuses an empty run."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pipeline_compose as compose_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import PIPELINE_RELAY_CLAUSE, WORKFLOW_SCRIPT_CAP_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap import _emit
from coordinator_core.ops.research_close import _handler

VENDORED = Path(__file__).parent / "fixtures" / "pipeline_research"
_NOTEBOOKS = 8
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture
def emitted(monkeypatch, tmp_path, capsys):
    """(script, reply) of the vendored web + notebooklm research emit over eight notebooklm targets."""
    return _emit(monkeypatch, tmp_path, capsys, VENDORED, ["web", "notebooklm"], _NOTEBOOKS)


def _workers_agent_site(script: str) -> int:
    """Offset of the first `agent(` call after the notebooklm Workers phase opens."""
    return script.index("agent(", script.index("phase('Workers')"))


def test_the_scope_halt_compares_against_the_eight_entry_roster(emitted):
    script, _ = emitted
    scope = script.index("halted: 'notebooklm.scope'")
    block = script[script.rindex("if (", 0, scope):scope]
    assert f"!== {_NOTEBOOKS}" in block
    assert "worker_count" in block and "notebooks" in block
    assert f"{_NOTEBOOKS}-entry notebooks roster" in script


def test_the_scout_halt_rejects_a_null_non_object_or_empty_return(emitted):
    script, _ = emitted
    scout = script.index("halted: 'notebooklm.scout'")
    block = script[script.rindex("if (", 0, scout):scout]
    assert "typeof" in block and "Array.isArray" in block and ".length === 0" in block
    assert "usable_sources" in block


def test_both_halts_return_before_the_workers_agent_call(emitted):
    script, _ = emitted
    workers = _workers_agent_site(script)
    for stage in ("scope", "scout"):
        assert script.index(f"halted: 'notebooklm.{stage}'") < workers, stage
    assert script.index("halted: 'notebooklm.scope'") < script.index("halted: 'notebooklm.scout'")


def test_workers_fan_over_the_scope_return_not_a_baked_roster(emitted):
    script, _ = emitted
    fan = script[script.index("phase('Workers')") :]
    assert "pre['scope']['notebooks']" in fan.split("agent(", 1)[0]
    assert not any(f"'T{i}'" in fan or f'"T{i}"' in fan for i in range(_NOTEBOOKS))


def test_the_relay_clause_is_declared_once(emitted):
    script, _ = emitted
    assert script.count("const RELAY = ") == 1
    assert script.count(compose_module._RELAY_DECL) == 1
    assert PIPELINE_RELAY_CLAUSE.startswith("This prompt")
    assert "genuine instruction to stop" not in script


def test_the_close_params_require_the_notebooklm_claims(emitted):
    _, reply = emitted
    assert reply["next_action"]["op"] == "research.close"
    assert reply["next_action"]["params"]["require_claims"] == ["notebooklm/merged-claims.json"]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return out.stdout.strip()


@pytest.fixture
def close_repo(tmp_path):
    repo = tmp_path / "closerepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n")
    sizing = repo / "state" / "sizings" / "2026-10-08-r.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("sizing: stub\n")
    _git(repo, "add", "seed.txt", "state/sizings/2026-10-08-r.yaml")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _close(repo: Path, emitted_reply: dict, scratch: Path) -> dict:
    params = {**emitted_reply["next_action"]["params"], "scratch_dir": str(scratch)}
    return asyncio.run(_handler(params, repo / ".git"))


def _scratch(root: Path, claims: str | None) -> Path:
    scratch = root / "scratch"
    (scratch / "notebooklm").mkdir(parents=True)
    (scratch / "web").mkdir()
    (scratch / "notebooklm" / "report.md").write_text("report\n")
    if claims is not None:
        (scratch / "notebooklm" / "merged-claims.json").write_text(claims, encoding="utf-8")
    return scratch


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("claims", ["[]", None, "{}", "not json"])
def test_close_refuses_a_run_with_no_claims_and_commits_nothing(emitted, close_repo, tmp_path, claims):
    _, reply = emitted
    before = _git(close_repo, "rev-list", "--count", "HEAD")
    out = _close(close_repo, reply, _scratch(tmp_path, claims))
    assert out["committed"] is False and out["deliverable"] is False and out["exit_code"] == 1, out
    assert _git(close_repo, "rev-list", "--count", "HEAD") == before
    assert _git(close_repo, "status", "--porcelain") == ""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_close_accepts_a_run_holding_a_claim_as_deliverable(emitted, close_repo, tmp_path):
    _, reply = emitted
    claims = json.dumps([{"claim": "x"}])
    out = _close(close_repo, reply, _scratch(tmp_path, claims))
    assert out["committed"] is True and out["deliverable"] is True, out


def test_an_oversize_template_is_refused_at_emit(monkeypatch, tmp_path, capsys):
    fat_root = tmp_path / "fat"
    shutil.copytree(VENDORED, fat_root)
    template = next((fat_root / "pipelines" / "deep-research").rglob("notebooklm-ask-template.md"))
    template.write_text(template.read_text(encoding="utf-8") + "x" * WORKFLOW_SCRIPT_CAP_BYTES, encoding="utf-8")
    with pytest.raises(AssertionError):
        _emit(monkeypatch, tmp_path / "run", capsys, fat_root, ["web", "notebooklm"], 1)
    out = capsys.readouterr()
    assert str(WORKFLOW_SCRIPT_CAP_BYTES) in out.out + out.err
