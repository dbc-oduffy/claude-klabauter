"""--baton/--deliverable-id on --ask --sizing: baton read in-process, refusals, and the unaccepted pm/ceo accept phase."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit, _read_baton_ids
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-06-baton.yaml"
BATON = "state/handoffs/2026-10-06-existing.md"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _baton(repo, handoff_id="hnd-x-abc123", dlv="dlv-x-abc123"):
    (repo / BATON).write_text(
        f"---\nhandoff_id: {handoff_id}\ndeliverable_id: {dlv}\n---\nbody\n", encoding="utf-8", newline="\n"
    )


def _sizing(repo, tshirt="M", mode="pm", accepted=None, **over):
    doc = {
        "schema": "sizing-object",
        "name": "baton fixture",
        "intent": "exercise the baton input",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": {"XS": "dispatch", "S": "spec-dispatch", "M": "plan", "XL": "plan"}[tshirt],
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": mode,
        "exit_criterion": {"statement": "done", "accepted": accepted},
    }
    doc.update(over)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_read_baton_ids_returns_path_and_id(repo):
    _baton(repo)
    assert _read_baton_ids(repo, BATON, None) == {"path": BATON, "deliverable_id": "dlv-x-abc123"}
    assert _read_baton_ids(repo, BATON, "dlv-x-abc123")["deliverable_id"] == "dlv-x-abc123"
    assert _read_baton_ids(repo, None, None) is None


def test_read_baton_ids_refuses_missing_null_and_mismatch(repo):
    with pytest.raises(SizingFireRefused, match="not found"):
        _read_baton_ids(repo, BATON, None)
    _baton(repo, dlv="null")
    with pytest.raises(SizingFireRefused, match="null deliverable_id"):
        _read_baton_ids(repo, BATON, None)
    _baton(repo)
    with pytest.raises(SizingFireRefused, match="differs"):
        _read_baton_ids(repo, BATON, "dlv-other-000000")
    with pytest.raises(SizingFireRefused, match="needs --baton"):
        _read_baton_ids(repo, None, "dlv-x-abc123")


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_gate_refuses_baton_at_arm_that_mints_none(repo, tshirt):
    _sizing(repo, tshirt, accepted={"pm_quote": "y", "on": "2026-10-06", "mode": "pm"})
    v = gate(repo, REL, writes=["a.py"], baton=BATON)
    assert v.halt["kind"] == "refusal" and "mints no baton" in v.halt["reason"]


def test_unaccepted_pm_sizing_emit_gate_sets_accept_pending(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    _baton(repo)
    _sizing(repo, "XL", mode="pm")
    out = _gate_sizing_at_emit(repo, REL, [], baton={"path": BATON, "deliverable_id": "dlv-x-abc123"})
    assert out["accept_pending"] is True and out["batons"] == [BATON]


def test_unaccepted_hands_on_still_refuses(repo):
    _sizing(repo, "XL", mode="hands-on")
    with pytest.raises(SizingFireRefused, match="touchpoint"):
        _gate_sizing_at_emit(repo, REL, [])


@pytest.mark.parametrize("flag", ["--baton", "--deliverable-id"])
def test_flags_refused_off_the_ask_route(repo, capsys, flag):
    rc = cli.main(["--plan", "docs/plans/x.md", flag, "x", "--repo-root", str(repo)])
    assert rc != 0 and "--baton/--deliverable-id" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["state/handoffs/../../etc/x.md", "docs/x.md", "/abs/state/handoffs/x.md"])
def test_read_baton_ids_refuses_path_outside_handoffs(repo, bad):
    with pytest.raises(SizingFireRefused, match="under state/handoffs/"):
        _read_baton_ids(repo, bad, None)


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_emit_with_baton_joins_it_and_embeds_accept_phase(repo, capsys, monkeypatch):
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _baton(repo)
    _sizing(repo, "XL", mode="pm")
    rc = cli.main(["--ask", "--sizing", REL, "--baton", BATON, "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    text = Path(reply["path"]).read_text(encoding="utf-8")
    assert "phase('accept')" in text and BATON in text
    assert reply["run_id"].startswith("warp-2026-10-06-baton-")
    assert "Plan, stage, execute, review from accepted sizing 2026-10-06-baton." in text


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_raw_ask_with_baton_threads_id_into_size_and_gate(repo, capsys, monkeypatch):
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _baton(repo)
    rc = cli.main(["--ask", "do a thing", "--baton", BATON, "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    text = Path(reply["path"]).read_text(encoding="utf-8")
    assert "--deliverable-id dlv-x-abc123" in text and f'baton: "{BATON}"' in text


_FILLED = (
    "## Specification\nDo it.\n\n## Reference materials (read first)\n- a.py\n\n"
    "## Acceptance criteria\n- AC-1\n"
)
_SKELETON = "## Specification\n\n## Reference materials (read first)\n  \n## Acceptance criteria\n\n"


@pytest.mark.parametrize("body,empty", [
    (_SKELETON, ["## Specification", "## Reference materials (read first)", "## Acceptance criteria"]),
    (_FILLED, []),
])
def test_empty_baton_sections_named(tmp_path, body, empty):
    from coordinator_core.ops.dispatch_emit.op import _empty_baton_sections

    p = tmp_path / "b.md"
    p.write_text(f"---\nhandoff_id: h\n---\n{body}", encoding="utf-8", newline="\n")
    assert _empty_baton_sections(p) == empty


def test_gate_refuses_skeleton_baton_then_passes_once_filled(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    _sizing(repo, "XL", mode="pm")
    b = {"path": BATON, "deliverable_id": "dlv-x-abc123"}
    (repo / BATON).write_text(
        f"---\nhandoff_id: hnd-x-abc123\ndeliverable_id: dlv-x-abc123\n---\n{_SKELETON}", encoding="utf-8", newline="\n"
    )
    with pytest.raises(SizingFireRefused, match="## Specification"):
        _gate_sizing_at_emit(repo, REL, [], baton=b)
    (repo / BATON).write_text(
        f"---\nhandoff_id: hnd-x-abc123\ndeliverable_id: dlv-x-abc123\n---\n{_FILLED}", encoding="utf-8", newline="\n"
    )
    assert _gate_sizing_at_emit(repo, REL, [], baton=b)["batons"] == [BATON]


def test_prompt_ask_blitz_call_carries_resolved_args(repo, capsys, monkeypatch):
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    assert cli.main(["--ask", "do a thing", "--repo-root", str(repo)]) == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    text = Path(reply["path"]).read_text(encoding="utf-8")
    call = text.split("await planBlitz(", 1)[1].split("\n", 1)[0]
    assert '"provisionSidecarCli": "/x/provision-sidecar"' in call
    assert '"pluginAgentsAvailable"' in call
    assert "gateReportPath: REPO_ROOT + '/' + _sizingRel" in call
