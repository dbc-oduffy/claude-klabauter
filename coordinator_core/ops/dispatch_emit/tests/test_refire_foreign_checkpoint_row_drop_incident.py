"""example-retrieval-repo d5aca3: another plan's checkpoint and another run's halt record touched fix rows' shared
writes, so a refire credited them as landed and staged only the test rows."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REFIRE_RUN = "warp-2026-10-24-d5aca3-20261024T170000-aaaaaa"
OTHER_PLAN = "docs/plans/2026-10-24-other.md"

_ROWS = [
    ("C0", "coded", ["some/c0.py"], None),
    ("T1", "open", ["tests/t1.py"], None),
    ("T2", "open", ["tests/t2.py"], None),
    ("T3", "open", ["tests/t3.py"], None),
    ("FA", "open", ["some/x.py", "some/y.py"], "T1"),
    ("F3", "open", ["some/x.py", "some/z.py"], "T2"),
    ("F4", "open", ["some/x.py"], "T3"),
]


def _spine() -> str:
    body = ""
    for rid, disp, writes, dep in _ROWS:
        body += f"- id: {rid}\n  title: T{rid}\n  disposition: {disp}\n  writes:\n"
        body += "".join(f"    - {w}\n" for w in writes)
        if dep:
            body += f"  depends_on:\n    - chunk: {dep}\n      gate_kind: output-consumption-runtime\n      note: n\n"
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _commit_all(repo, message: str, *extra: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, *extra)


def _incident(repo) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(_spine(), encoding="utf-8", newline="\n")
    _put(repo, tshirt="S", route="spec-dispatch")
    for rel in ("some/c0.py", "some/x.py", "some/y.py", "some/z.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("v = 0\n", encoding="utf-8")
    _commit_all(repo, "plan")
    (repo / "some/x.py").write_text("v = 1\n", encoding="utf-8")
    (repo / "some/y.py").write_text("v = 1\n", encoding="utf-8")
    _commit_all(repo, "checkpoint(wave 1): 2 rows — A, B", "-m", f"Checkpoint-Plan: {OTHER_PLAN}\nCheckpoint-Base: abc1234")
    (repo / "some/x.py").write_text("v = 2\n", encoding="utf-8")
    _commit_all(repo, halted_record_message("warp-other-run", {"halted": "ledger-check", "plan": OTHER_PLAN}))


def test_refire_stages_every_fix_row_a_foreign_run_touched(repo):  # noqa: F811
    _incident(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    staged = {r["id"] for r in read_reply(repo, reply["manifest_handle"])["rows"]}
    assert {"FA", "F3", "F4"} <= staged
    assert {"FA", "F4"} <= set(reply["resume_rows"])
