"""--sizing emit: gate and footprint refused before any write; M+ receipt carries batons/uncommitted and planBlitz args."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-02-emit.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_ROUTE = {"XS": "dispatch", "S": "spec-dispatch", "M": "plan"}
_ACCEPTED = {"pm_quote": "yes", "on": "2026-10-02", "mode": "pm"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, tshirt="M", **over):
    doc = {
        "schema": "sizing-object",
        "name": "emit fixture",
        "intent": "exercise the emit",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": _ROUTE[tshirt],
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-emit-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": _ACCEPTED},
    }
    doc.update(over)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _emit(repo, *argv):
    return cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo), *argv])


def _mjs(repo):
    return list(repo.rglob("*.workflow.mjs")) + list(repo.rglob("*.emitted.json"))


def test_draft_default_sizing_refused_naming_every_field_and_writes_nothing(repo, capsys):
    _put(
        repo,
        "XS",
        status="draft",
        intent="PLACEHOLDER intent",
        premise={"provenance": "unrecorded", "evidence": "PLACEHOLDER"},
    )
    rc = _emit(repo, "--writes", "a.py")
    err = capsys.readouterr().err
    assert rc != 0
    for field in ("status", "intent", "premise.evidence", "premise.provenance"):
        assert field in err
    assert _mjs(repo) == []


def test_writes_outside_repo_root_refused_and_nothing_written(repo, capsys):
    _put(repo, "XS")
    outside = (repo.parent / "other-repo" / "a.py").as_posix()
    rc = _emit(repo, "--writes", outside)
    assert rc != 0 and "outside repo root" in capsys.readouterr().err
    assert _mjs(repo) == []


def test_xs_without_writes_still_emits(repo):
    _put(repo, "XS")
    assert _emit(repo) == 0


def test_writes_without_ask_is_a_usage_error(repo, capsys):
    assert cli.main(["--plan", "x.md", "--writes", "a.py", "--repo-root", str(repo)]) != 0
    assert "--writes" in capsys.readouterr().err


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_accepted_m_receipt_carries_batons_and_uncommitted_and_planblitz_args(repo, capsys):
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, "M")
    assert _emit(repo) == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    receipt = json.loads(open(reply["receipt"], encoding="utf-8").read())
    baton = receipt["batons"][0]
    assert baton.startswith("state/handoffs/")
    assert receipt["uncommitted"] == [baton, REL]
    assert reply["batons"] == [baton] and reply["uncommitted"] == [baton, REL]
    text = open(reply["path"], encoding="utf-8").read()
    for key in ("provisionSidecarCli", "gateReportPath", "pluginAgentsAvailable"):
        assert key in text

    assert _emit(repo, "--force") == 0
    out = capsys.readouterr().out
    again = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert again["batons"] == [baton] and again["uncommitted"] == []


def test_gate_halt_raises_refusal_naming_kind(repo):
    from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit

    _put(repo, "M", exit_criterion={"statement": "done", "accepted": None})
    with pytest.raises(SizingFireRefused) as exc:
        _gate_sizing_at_emit(repo, REL, [])
    assert "touchpoint" in str(exc.value) and "sizing-accept-exit-criterion" in str(exc.value)
