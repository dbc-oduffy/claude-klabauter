"""A baton minted by `mint_baton_from_sizing` is cascade-eligible: the library cascade
(`deliverable.cascade_terminal`, handoff then sizing) advances both with no leg-(d) refusal.

Run:
    pytest coordinator/bin/tests/test_from_sizing_baton_cascades.py -v
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_SIZING_REL = "state/sizings/2026-10-03-cascade-fixture.yaml"
_SESSION = "11111111-2222-3333-4444-555555555555"
_PLAN_REL = "docs/plans/2026-10-03-cascade-fixture.md"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cdn_cascade_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_cascade_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()

_SIZING = """\
schema: sizing-object
name: "cascade fixture"
intent: "Mint a baton that the cascade can close"
estimate:
  tshirt: M
  provisional: true
route: plan
detents: []
fork: null
xl_exit: null
status: sized
premise:
  provenance: read
  evidence: "fixture evidence"
deliverable_id: null
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=_GIT_ENV, check=True,
                   timeout=15, stdin=subprocess.DEVNULL, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(_SIZING, encoding="utf-8", newline="\n")
    (tmp_path / _PLAN_REL).write_text("---\nstatus: implemented\n---\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SESSION)
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _cascade(repo: Path, deliverable_id: str, target_kind: str, source_kind: str = "plan") -> dict:
    return asyncio.run(cascade_mod._handler(
        {
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": _PLAN_REL,
            "target_kind": target_kind,
        },
        repo_root=repo,
    ))


def test_minted_baton_is_a_session_handoff(repo):
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    text = (repo / result["path"]).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])

    assert fm["kind"] == "session-handoff"
    assert "What travels with this spinoff" not in text
    assert "<!-- spinoff:" not in text


def test_library_cascade_advances_the_minted_baton_and_its_sizing(repo):
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    sizing = yaml.safe_load((repo / _SIZING_REL).read_text(encoding="utf-8"))
    dlv = sizing["deliverable_id"]
    assert dlv
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add baton and plan\n\nSession-Id: {_SESSION}")

    handoff_run = _cascade(repo, dlv, "handoff", source_kind="handoff")
    assert [a["handoff_path"] for a in handoff_run["advanced"]], handoff_run
    assert not handoff_run["refused"], handoff_run

    sizing_run = _cascade(repo, dlv, "sizing")
    assert sizing_run["advanced"], sizing_run
    assert not sizing_run["refused"], sizing_run
    assert yaml.safe_load((repo / _SIZING_REL).read_text(encoding="utf-8"))["status"] == "shipped"
    baton = (repo / result["path"]).read_text(encoding="utf-8")
    assert "deployment_state: shipped" in baton
