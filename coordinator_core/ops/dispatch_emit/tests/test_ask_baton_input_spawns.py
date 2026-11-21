"""--baton/--deliverable-id on --ask --sizing: baton read in-process, refusals, and the unaccepted pm/ceo accept phase."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "state/sizings/2026-10-06-baton.yaml"
BATON = "state/handoffs/2026-10-06-existing.md"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _baton(repo, handoff_id: str = "hnd-x-abc123", dlv: str = "dlv-x-abc123") -> None:
    (repo / BATON).write_text(
        f"---\nhandoff_id: {handoff_id}\ndeliverable_id: {dlv}\n---\nbody\n", encoding="utf-8", newline="\n"
    )


def _sizing(repo, tshirt: str = "M", mode: str = "pm", accepted=None, **over) -> None:
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


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_emit_with_baton_joins_it_and_embeds_accept_phase(repo, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _baton(repo)
    _sizing(repo, "M", mode="pm", route="pm-decision", xl_exit="accept_multi_session")
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
def test_raw_ask_with_baton_threads_id_into_size_and_gate(repo, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_prompt_ask_blitz_call_carries_resolved_args(repo, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import plan_blitz_args

    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    # A raw ask carries no plan-blitz stage; an M+ sizing-backed ask does.
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _sizing(repo, "XL", mode="pm")
    assert cli.main(["--ask", "--sizing", REL, "--repo-root", str(repo)]) == 0
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    text = Path(reply["path"]).read_text(encoding="utf-8")
    call = text.split("await planBlitz(", 1)[1].split("\n", 1)[0]
    assert '"provisionSidecarCli": "/x/provision-sidecar"' in call
    assert '"pluginAgentsAvailable"' in call
    assert "gateReportPath: REPO_ROOT + '/' + _sizingRel" in call
