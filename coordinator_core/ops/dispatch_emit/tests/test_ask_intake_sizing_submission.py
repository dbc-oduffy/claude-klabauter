"""engine-ask over a minted intake sizing: the run sizes into that file and carries its em_brief.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_ask_intake_sizing_submission.py -q
"""

from __future__ import annotations

import re

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

REL = "state/sizings/2026-10-14-intake.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
BRIEF = "EM-DIRECTIVE keep the change small"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, *, text="ship it", brief=None, route=None):
    doc = contract.intake_record(name="intake", turn={"text": text, "turn": 4}, today="2026-10-14")
    if brief:
        doc["em_analysis"][contract.EM_BRIEF_KEY] = brief
    if route:
        doc["route"] = route
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _script(repo, *extra):
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo), *extra]) == 0
    (path,) = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return path.read_text(encoding="utf-8")


def test_intake_sizing_with_brief_emits_the_brief_clause_and_sizes_in_run(repo, capsys):
    _put(repo, brief=BRIEF)
    script = _script(repo)
    assert EM_BRIEF_VAR in script and BRIEF in script
    assert "phase('size')" in script
    # `_sizingPath` derives from `_sizingPathFor`, which for an intake sizing is the submitted path verbatim.
    assert re.search(rf"const _sizingPathFor = \(r\) => REPO_ROOT \+ '/' \+ ['\"]{re.escape(REL)}['\"];", script)
    assert (
        "const _sizingPath = _boundRelOf(_scouted.sizing_path) ? REPO_ROOT + '/' + _scouted.sizing_path"
        " : _sizingPathFor(_scouted);"
    ) in script


def test_intake_sizing_without_brief_emits_no_clause(repo, capsys):
    _put(repo)
    script = _script(repo)
    assert "phase('size')" in script
    assert f"const {EM_BRIEF_VAR} =" not in script


def test_explicit_brief_arg_wins_over_the_sizings_brief(repo, capsys):
    _put(repo, brief=BRIEF)
    script = _script(repo, "--brief", "ARG-DIRECTIVE wins")
    assert "ARG-DIRECTIVE wins" in script and BRIEF not in script


def test_a_placeholder_roadmap_route_never_picks_the_arm_or_refuses_the_brief(repo, capsys):
    _put(repo, brief=BRIEF, route="roadmap")
    script = _script(repo)
    assert BRIEF in script and "phase('size')" in script


def test_a_sized_sizing_still_skips_the_size_stage(repo, capsys):
    _put(repo)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["status"] = "routed"
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    assert contract.is_intake(doc) is False
    assert ask_compose._intake_sizing(str(repo), REL) is None


def test_the_size_stage_keeps_a_two_word_turns_minted_verbatim(repo, capsys):
    import coordinator_core.sizing_assemble as sizing_assemble

    _put(repo, text="ship it")
    code = sizing_assemble.main([
        "--tshirt", "M", "--intent", "Ship the planned change", "--premise-provenance", "read",
        "--premise-evidence", "read it", "--no-plan-hand-off", "--write", str(repo / REL),
    ])
    capsys.readouterr()
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    assert code == 0
    assert [v["text"] for v in doc["pm_verbatims"]] == ["ship it"]
    assert re.search(r"[MS]", doc["estimate"]["tshirt"]) and doc["status"] == "routed"


def _mark_relayed(repo):
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["em_analysis"]["ask_source"] = "relayed"
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return doc["intent"] if "intent" in doc else None


def test_a_relayed_intake_slice_writes_relayed_ask_and_no_ask_md(repo):
    _put(repo, text="relay me")
    intent = _mark_relayed(repo)
    script = _script(repo)
    (relayed_file,) = list((repo / "state").rglob("relayed-ask.md"))
    assert relayed_file.read_text(encoding="utf-8") == intent
    assert not list((repo / "state").rglob("ask.md"))
    assert "--relayed-ask-file" in script
    assert "Relayed ask" in script


def test_a_pm_intake_slice_is_unchanged(repo):
    _put(repo)
    script = _script(repo)
    assert "--relayed-ask-file" not in script
    assert not list((repo / "state").rglob("relayed-ask.md"))


def test_em_passed_relayed_beside_a_sizing_path_still_refuses(repo, capsys):
    _put(repo)
    code = cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo), "--ask-source", "relayed"])
    assert code != 0
    assert "sizing_path carries none" in capsys.readouterr().err


def test_compose_refuses_relayed_on_a_planned_sizing(repo):
    _put(repo)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["status"] = "routed"
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    with pytest.raises(ask_compose.AskComposeRefused, match="planned sizing"):
        ask_compose.compose_ask_script(
            repo_root=str(repo), prompt=None, sizing_rel=REL, run_id="r1", session_id=None,
            ask_source="relayed",
        )
