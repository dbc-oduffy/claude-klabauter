"""A halted run keeps its work: halt, then refire through the gate, stage and composed script."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, kept_work, reply_sidecar, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, parse_marker, render_marker
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
NEW_RUN = "ask-20261016T000000-bbbbbb"
PLAN_REL = "docs/plans/2026-10-01-gate.md"
C2_PATH = "some/C2.py"
C2_REPORT = _dispatch_report_path(PLAN_REL, "C2")
REVIEWS = [{"reviewer": "staff-eng", "verdict": "APPROVE"}]
COVERAGE = {"verdict": "pass"}
PREMISE_FILE = "target.txt"

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "seed.txt", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _row(rid: str, disposition: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: code-edit\n  surface: some/{rid}.py\n"
        f"  disposition: {disposition}\n  writes:\n    - some/{rid}.py\n"
    ) + extra


def _plan(root: Path, rows: str, front: str = "") -> None:
    _write(
        root,
        PLAN_REL,
        f"---\nrun_id: fixture\n{front}---\n\n# keep\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
    )


def _marker_script(root: Path, rows: dict) -> None:
    built = []
    for cid, paths in rows.items():
        report = _dispatch_report_path(PLAN_REL, cid)
        _write(root, report, f"DONE: {cid}\n")
        for p in paths:
            _write(root, p, f"{p} = 1\n")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=report))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _halt(root: Path, halted: dict) -> dict:
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask.md\n")
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": REL}) + "\n")
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted}
    out = terminal_commit._handler(params, repo_root=root / ".git")
    assert out["committed"] is True, out
    return out


def _refire(root: Path) -> dict:
    return ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=root / ".git")


def _manifest(root: Path, reply: dict):
    return reply_sidecar.read_reply(root, reply["manifest_handle"])


def _script(monkeypatch, arm: str) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=REL,
        run_id=NEW_RUN,
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def _l_shape(repo):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, _row("C1", "coded") + _row("C2", "open"))
    _marker_script(repo, {"C2": [C2_PATH]})
    halt = {"halted": "commit", "plan": PLAN_REL, "sizing": REL, "refused": "since-base-overlap"}
    return _halt(repo, halt)


def test_a_commit_halt_keeps_the_finished_row_and_the_refire_dispatches_no_agent_for_it(repo, monkeypatch):
    _l_shape(repo)
    report = repo / C2_REPORT
    before = report.read_text(encoding="utf-8")

    reply = _refire(repo)
    assert "manifest_handle" in reply, reply
    manifest = _manifest(repo, reply)
    rows = {r["id"]: r for r in manifest["rows"]}

    assert rows["C2"]["kept_from"] == RUN
    assert report.read_text(encoding="utf-8") == before
    assert not report.with_name("C2.prior.md").exists()

    script = _script(monkeypatch, "s")
    loop = script[script.index("else _rows[r.id] = _runRow(r.id") :]
    thunk = loop[: loop.index("{ title: r.id")]
    assert "async () => r.kept_from ? _keptRun(r) :" in thunk

    request = parse_marker((repo / manifest["marker_path"]).read_text(encoding="utf-8"))
    chunk = next(c for c in request.chunks if c.id == "C2")
    assert C2_PATH in chunk.paths and chunk.report == C2_REPORT


def test_a_row_edited_after_the_halt_is_staged_normally(repo):
    _l_shape(repo)
    _write(repo, C2_PATH, "edited = 1\n")

    reply = _refire(repo)
    manifest = _manifest(repo, reply)
    rows = {r["id"]: r for r in manifest["rows"]}

    assert not rows["C2"].get("kept_from")
    assert (repo / C2_REPORT).with_name("C2.prior.md").is_file()
    assert not (repo / C2_REPORT).exists()


_READY_PLAN = (
    "---\ntitle: keep\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
    "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
    "capabilities: []\n"
    "anchor_premises:\n  - id: P1\n    claim: the target carries the marker\n    check:\n"
    f"      command: grep -c marker {PREMISE_FILE}\n      expect: '1'\n"
    "prime_exit_criterion:\n  statement: done\n"
    "  derived_from: state/sizings/2026-10-01-gate.yaml\n"
    "---\n\n# Keep\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: A1\n  title: Ship it\n  body: Do the named work and pin it with a test.\n"
    "  change_kind: code-edit\n  surface: coordinator_core/a1.py\n  writes: [coordinator_core/a1.py]\n"
    "  queue_scope: project\n  disposition: open\n```\n"
)


def _xl_shape(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import _m_repo

    _m_repo(repo, monkeypatch, plan=PLAN_REL)
    _write(repo, PREMISE_FILE, "old\n")
    (repo / "coordinator_core").mkdir()
    _write(repo, PLAN_REL, _READY_PLAN)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "plan")
    halt = {
        "halted": "refusal",
        "reason": "anchor premise P1 has no verdict",
        "plan": PLAN_REL,
        "sizing": REL,
        "blitz": {"pulled": [{"planPath": PLAN_REL, "reviews": REVIEWS, "coverage": COVERAGE}]},
    }
    _halt(repo, halt)


def test_a_refusal_halt_resumes_ready_once_the_premise_target_is_fixed(repo, monkeypatch):
    _xl_shape(repo, monkeypatch)
    _write(repo, PREMISE_FILE, "marker\n")

    out = _refire(repo)

    assert out["halt"] is None and out["resume_ready"] is True, out
    assert "manifest_handle" in out
    script = _script(monkeypatch, "m_plus")
    ready = script.index("if (_gate.resume_ready && _gate.resume_plan) { _planRel = _gate.resume_plan; }")
    ready_line = script[ready : script.index("\n", ready)]
    assert "agent(" not in ready_line and "planBlitz(" not in ready_line
    assert "label: 'plan" not in ready_line and "reblitz" not in ready_line


def test_a_refusal_halt_whose_premise_target_is_still_unfixed_is_todays_verdict(repo, monkeypatch):
    _xl_shape(repo, monkeypatch)

    out = _refire(repo)

    assert "resume_ready" not in out and "manifest_handle" not in out, out
    assert out["halt"] is None and out["resume_plan"] == PLAN_REL


def test_a_refusal_halt_whose_plan_was_edited_is_todays_verdict(repo, monkeypatch):
    _xl_shape(repo, monkeypatch)
    _write(repo, PREMISE_FILE, "marker\n")
    plan = repo / PLAN_REL
    plan.write_bytes(plan.read_bytes() + b"\nedited\n")

    out = _refire(repo)

    assert "resume_ready" not in out and "manifest_handle" not in out, out
    assert out["halt"] is None and out["resume_plan"] == PLAN_REL
    assert kept_work.read(repo / RECORD_DIR_ROOT / RUN)["plan_gate"]["plan"] == PLAN_REL
