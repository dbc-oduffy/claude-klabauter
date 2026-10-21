"""An intake slice's em_context_paths reach the slice run's EmBrief as its context files.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_ask_intake_slice_context.py -q
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

REL = "state/sizings/2026-10-14-intake.yaml"
CTX = "docs/a.md"
BRIEF = "EM-DIRECTIVE concrete host bug"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / CTX).write_text("context\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, *, brief=None, paths=None):
    doc = contract.intake_record(name="intake", turn={"text": "ship it", "turn": 4}, today="2026-10-14")
    if brief:
        doc["em_analysis"][contract.EM_BRIEF_KEY] = brief
    if paths:
        doc["em_analysis"][contract.EM_CONTEXT_PATHS_KEY] = paths
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _script(repo, *extra):
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo), *extra]) == 0
    (path,) = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return path.read_text(encoding="utf-8")


def test_slice_brief_and_context_paths_declare_the_em_brief(repo):
    _put(repo, brief=BRIEF, paths=CTX)
    script = _script(repo)
    assert EM_BRIEF_VAR in script and BRIEF in script
    assert f"Read each context file first: {CTX}" in script


def test_slice_with_paths_but_no_brief_still_declares_an_em_brief(repo):
    _put(repo, paths=CTX)
    script = _script(repo)
    assert f"const {EM_BRIEF_VAR} =" in script
    assert CTX in script


def test_a_missing_context_path_is_refused(repo, capsys):
    _put(repo, brief=BRIEF, paths="docs/gone.md")
    code = cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)])
    assert code != 0
    assert "docs/gone.md" in capsys.readouterr().err
    assert not list((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))


def test_slice_without_the_keys_emits_no_em_brief(repo):
    _put(repo)
    assert f"const {EM_BRIEF_VAR} =" not in _script(repo)
