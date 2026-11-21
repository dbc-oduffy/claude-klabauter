"""End to end through the default entry points: a bug-blitz submission carrying `rulings` (real `dispatch.emit`
queue route and real mint, fake parse), then the `workflow.revise` op on the returned run stamp.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec, blitz_revisions_rel
from coordinator_core.hooks import submit_bug_blitz as mod
from coordinator_core.ops.dispatch_emit import pipeline_run, requirements_ledger

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN_ID = "20261121T120000Z"
RULING = "measure, do not bump the RSS bound"
VERBATIM = "also delete dead tests under b/"
_FIXTURE_PROFILE = (
    Path(__file__).resolve().parents[2] / "ops" / "dispatch_emit" / "tests" / "fixtures" / "queue-profiles" / "fixture.yaml"
)
_TESTS = (
    FailingTest(file="a/test_x.py", name="TestX::test_one", message="boom"),
    FailingTest(file="b/test_y.py", name="TestY::test_one", message="boom"),
    FailingTest(file="b/test_y.py", name="TestY::test_two", message="boom"),
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.fixture
def real_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID", "QUEUE_APPEND_OUTPUT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    root = tmp_path / "real"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/box/2026-11-21")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "state" / "bug-backlog").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    profiles = tmp_path / "queue-profiles"
    profiles.mkdir()
    (profiles / "bug.yaml").write_text(_FIXTURE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "_profile_dir", lambda: str(profiles))
    return root


def _submit(root: Path, args: dict) -> dict:
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": "coordinator:bug-blitz", "args": args},
        "session_id": "sess-blitz",
        "cwd": str(root),
    }
    return mod.evaluate(
        payload,
        resolve=lambda raw, r: TargetSpec(kind="dir", path=raw),
        parse=lambda spec: ParseResult(tests=_TESTS),
        run_id=RUN_ID,
    )


def _committed_row(root: Path, rel: str) -> dict:
    return yaml.safe_load(_git(root, "show", f"HEAD:{rel}"))


def test_rulings_land_in_the_commit_then_revise_on_the_stamp_reaches_the_script(real_repo: Path) -> None:
    env = _submit(real_repo, {"targets": "red-set", "rulings": [{"match": "b/test_y.py", "ruling": RULING}]})
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    updated = hso["updatedInput"]
    stamp = updated["args"]["run_stamp"]
    assert stamp == RUN_ID
    script = Path(updated["scriptPath"])

    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    rows = {rel: _committed_row(real_repo, rel) for rel in decisions["minted"]}
    assert len(rows) == 3
    ruled = {r["title"] for r in rows.values() if r.get("pm_ruling") == RULING}
    assert ruled == {"TF: b/test_y.py::TestY::test_one", "TF: b/test_y.py::TestY::test_two"}
    assert [r["title"] for r in rows.values() if "pm_ruling" in r and r["title"] not in ruled] == []

    text = script.read_text(encoding="utf-8")
    revisions_rel = blitz_revisions_rel(stamp)
    assert pipeline_run.reads_revisions(text)
    assert revisions_rel in text

    reply = requirements_ledger._handler({"run_id": stamp, "verbatim": VERBATIM}, repo_root=real_repo)
    assert "error" not in reply, reply
    assert reply["delivered"] is True
    assert reply["revisions"] == revisions_rel
    assert VERBATIM in (real_repo / revisions_rel).read_text(encoding="utf-8")


def test_revise_on_a_mistyped_stamp_is_still_refused(real_repo: Path) -> None:
    env = _submit(real_repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    reply = requirements_ledger._handler({"run_id": "20261121T125959Z", "verbatim": VERBATIM}, repo_root=real_repo)
    assert "unknown run" in reply.get("error", ""), reply
