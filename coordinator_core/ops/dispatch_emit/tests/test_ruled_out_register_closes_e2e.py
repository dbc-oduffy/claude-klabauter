"""An ask run whose every register row a recorded revise ruling took out of scope commits through
`dispatch.land_run` and its close job pays every close leg, whichever run recorded the ruling."""

from __future__ import annotations

import copy

import pytest
import yaml

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_met_judge_reaches_close_legs_e2e import _run_close_job
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER_RUN = "ask-20990101T000000-aaaaaa"
OPEN_TRAILER = "Requirement-Open: R1 withheld by out-of-scope ruling"


def _fixture(tmp_path, monkeypatch, ruling_run, *, source_anchor=None, ask_text="ship a.py") -> tuple[Path, str]:
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_ask_fixture(parent, ask_text=ask_text)
    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    assert row[rr.ROW_ID] == "R1", row
    if source_anchor:
        row[rr.ROW_ANCHOR] = source_anchor
    row[rr.ROW_STATUS] = rr.STATUS_DEFERRED
    row[rr.ROW_RULING] = {
        rr.RULING_SOURCE: "pm",
        rr.RULING_QUOTE: "R1 is out of scope",
        rr.RULING_REF: f"workflow.revise:{ruling_run}:out_of_scope",
    }
    sizing.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    plan = repo / driver.ASK_PLAN_REL
    text = plan.read_text(encoding="utf-8")
    claim = f'register_claims:\n  sizing: "{driver.ASK_SIZING_REL}"\n  rows: ["R1"]\nprime_exit_criterion:'
    plan.write_text(text.replace("prime_exit_criterion:", claim, 1), encoding="utf-8")

    outbox = tmp_path / "lessons-outbox"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None, classes=None: ([("handoff", "b.md")], [])
    )
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    return repo, script


def _drive(repo, script, verdict):
    return land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": copy.deepcopy(driver.INLINE_REVIEW), "ledger_verdict": verdict},
        repo_root=repo / ".git",
    )


def _assert_closed(repo, out) -> None:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    assert out.get("committed") is True, out
    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", out["sha"]], cwd=str(repo), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout
    assert OPEN_TRAILER in message, message
    legs = {leg: status for leg, (status, _detail) in _run_close_job(repo, out).items()}
    owed = ("review-stamp", "plan-reconciliation", "completion-entry", "baton-ship", "lessons", "close-commit")
    assert {leg: legs.get(leg) for leg in owed if legs.get(leg) != "ran"} == {}, legs


@pytest.mark.parametrize("ruling_run", [OTHER_RUN, driver.ASK_RUN], ids=["re-fire-other-run", "same-run"])
def test_all_ruled_out_register_with_empty_verdict_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ruling_run) -> None:
    repo, script = _fixture(tmp_path, monkeypatch, ruling_run, source_anchor="plan-row:C3")
    _assert_closed(repo, _drive(repo, script, {"items": []}))


def test_verdict_judging_the_ruled_out_row_met_still_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, script = _fixture(tmp_path, monkeypatch, OTHER_RUN, source_anchor="plan-row:C3")
    _assert_closed(repo, _drive(repo, script, {"items": [{"id": "R1", "status": "met"}]}))

@pytest.mark.parametrize("reason", [
    "blocked: no owner",
    "Sparse alignment passed on the retained sample.",
    "Await calibration from the external map repository, then replay dense grading.",
])
@pytest.mark.parametrize("ruling_run", [OTHER_RUN, driver.ASK_RUN])
def test_pm_row_still_owed_after_unverified_disposition(tmp_path, monkeypatch, reason, ruling_run):
    import subprocess

    repo, script = _fixture(tmp_path, monkeypatch, ruling_run)
    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    row[rr.ROW_TEXT] = "Use dense measurements for grading."
    row[rr.ROW_CLAIMED_BY] = []
    row[rr.ROW_RULING][rr.RULING_QUOTE] = reason
    sizing.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo)
    result = _drive(repo, script, {"items": [{"id": "R1", "status": "met", "observed": reason}]})
    assert result["refused"] == "ledger-unmet"
    assert result["ledger_failures"] == [{"id": "R1", "reason": rr.PM_DISPOSITION_REASON}]
    assert result["accountable_run"] == driver.ASK_RUN
    assert result["sha"] is None
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo) == before


def test_external_original_stays_blocked_at_landing(tmp_path, monkeypatch):
    repo, script = _fixture(tmp_path, monkeypatch, driver.ASK_RUN)
    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    row.pop(rr.ROW_RULING)
    row[rr.ROW_STATUS] = rr.STATUS_OPEN
    original = "https://claude.ai/artifact/calibration-map"
    row[rr.ROW_TEXT] = f"Integrate the calibration repository's map {original} and replay grading."
    sizing.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = _drive(repo, script, {"items": [{"id": "R1", "status": "met", "observed": "Sparse alignment passed."}]})
    assert result["sha"] is None
    failure = next(f for f in result["ledger_failures"] if f.get("status") == "BLOCKED")
    assert failure["id"] == "R1" and original in failure["reason"]
    assert result["accountable_run"] == driver.ASK_RUN


def test_successor_rebind_keeps_missing_owner_obligation_actionable(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import requirements_ledger as ledger

    repo, _ = _fixture(tmp_path, monkeypatch, driver.ASK_RUN)
    successor = ledger.start(repo, OTHER_RUN, resumed_from=driver.ASK_RUN)
    new_sizing = "state/sizings/successor.yaml"
    (repo / new_sizing).write_text(driver._ASK_SIZING, encoding="utf-8")
    rebound = ledger.bind(repo, OTHER_RUN, new_sizing)
    assert ledger.read_run(successor)["resumed_from"] == driver.ASK_RUN
    assert [(item.text, item.origin) for item in rebound.items] == [("ship a.py", "ask")]
    assert not ledger.judge(rebound, {"items": []})["pass"]
    row, = rr.read_register((repo / new_sizing).read_text(encoding="utf-8")).rows
    assert row["status"] == "open" and row["claimed_by"] == []


def test_permitted_sparse_alignment_lands_without_waiving_a_row(tmp_path, monkeypatch):
    import subprocess
    import sys

    repo, script = _fixture(tmp_path, monkeypatch, driver.ASK_RUN)
    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    row.pop(rr.ROW_RULING)
    row.update(status="open", source_text="Use the sparse alignment caller on the retained sample.")
    sizing.write_text(yaml.safe_dump(data), encoding="utf-8")
    (repo / "a.py").write_text('print("Sparse alignment passed on the retained sample.")\n', encoding="utf-8")
    observed = subprocess.check_output([sys.executable, "a.py"], cwd=repo, text=True).strip()
    result = _drive(
        repo,
        script,
        {
            "items": [
                {
                    "id": "R1",
                    "status": "met",
                    "wired": True,
                    "command": "python a.py",
                    "observed": observed,
                    "evidence": observed,
                }
            ]
        },
    )
    assert result["committed"] and result["sha"]
    product = subprocess.check_output(
        ["git", "show", result["sha"] + ":a.py"], cwd=repo
    )
    assert (
        subprocess.check_output([sys.executable, "-"], input=product).decode().strip()
        == observed
    )


@pytest.mark.parametrize(
    ("amendment", "field", "expected_land"),
    [
        ("No owner for the adapter; use sparse grading.", "sparse", False),
        ("PM permits sparse alignment only.", "sparse", False),
        ("PM says waive dense grading.", "sparse", False),
        (None, "sparse", False),
        (None, "dense", True),
        (None, "stale", False),
        (None, "claimed-only", False),
        (None, "wrong-claim", False),
        (None, "typed-sparse", False),
        (None, "typed-dense", True),
    ],
)
def test_registered_revision_and_landing_compare_public_caller(
    tmp_path, monkeypatch, amendment, field, expected_land
):
    import asyncio
    import json
    import subprocess
    import sys

    from coordinator_core import ipc

    ask = "Use dense measurements for grading. With dense [2,5,8] and sparse [2,8], grade must return 15."
    if field.startswith("typed-"):
        ask = "Use dense measurements at the grading entry; expected grade is fifteen on the retained input."
    repo, script = _fixture(tmp_path, monkeypatch, driver.ASK_RUN, ask_text=ask)

    def invoke(method, params):
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "_origin_worktree": str(repo),
            "method": method,
            "params": params,
        }
        reply = asyncio.run(ipc.dispatch_message(envelope, caller="offline-replay"))
        assert "result" in reply, reply
        return reply["result"]

    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text())
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    row.pop("ruling")
    assert row["source_text"] == ask
    row.update(status="open", claimed_by=[])
    sizing.write_text(yaml.safe_dump(data, sort_keys=False))
    if amendment:
        prior = sizing.read_bytes()
        revised = invoke(
            "workflow.revise",
            {"run_id": driver.ASK_RUN, "verbatim": amendment, "out_of_scope": ["R1"]},
        )
        assert rr.PM_DISPOSITION_REASON in revised["error"], revised
        assert sizing.read_bytes() == prior
    sample = json.dumps({"dense": [2, 5, 8], "sparse": [2, 8]})

    def product(field_name):
        (repo / "a.py").write_text(
            "import sys,json\nprint(sum(json.load(sys.stdin)["
            + repr(field_name)
            + "]))\n"
        )

    def grade(directory):
        return subprocess.check_output(
            [sys.executable, "a.py"], cwd=directory, input=sample, text=True
        ).strip()

    product("sparse")
    verifier = repo / "grade_check.py"
    verifier.write_text(
        "import subprocess,json,sys\n"
        f"v=int(subprocess.check_output([sys.executable,'a.py'],input={sample!r},text=True))\n"
        "ok=v==15\n"
        "print(json.dumps(dict(axis='R1', kind='measure', command='python a.py', comparator='==',"
        "now=v, threshold=15, **{'pass':ok})))\n"
        "print(json.dumps({'summary':True,'pass':ok,'failing':[] if ok else ['R1']}))\n"
        "sys.exit(0 if ok else 1)\n"
    )
    subprocess.run(["git", "add", "--", "grade_check.py"], cwd=repo, check=True)
    claim = (
        "Sparse alignment must return 15."
        if field == "wrong-claim"
        else row["source_text"]
    )
    written = invoke(
        "dispatch.objective_write",
        {
            "run_id": driver.ASK_RUN,
            "verbatim": row["source_text"],
            "axes": [{"id": "R1", "threshold": 15, "claim": claim}],
            "falsifier_script": "grade_check.py",
        },
    )
    assert written["written"], written
    calibrated = invoke(
        "dispatch.objective_calibrate", {"run_id": driver.ASK_RUN, "credited": []}
    )
    assert calibrated["calibrated"], calibrated
    product("dense" if field in ("dense", "stale", "wrong-claim", "typed-dense") else "sparse")
    measured = invoke(
        "dispatch.measured_rev", {"run_id": driver.ASK_RUN, "paths": ["a.py"]}
    )
    assert measured.get("sha"), measured
    checked = invoke("dispatch.objective_check", {"run_id": driver.ASK_RUN})
    if expected_land:
        assert checked["status"] == "met", checked
    if field == "stale":
        product("sparse")
    observed = "15" if field in ("stale", "claimed-only", "typed-sparse") else grade(repo)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo)
    result = invoke(
        "dispatch.land_run",
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            "ledger_verdict": {
                "items": [
                    {
                        "id": "R1",
                        "status": "met",
                        "wired": True,
                        "command": "python a.py",
                        "observed": observed,
                    }
                ]
            },
        },
    )
    assert result["committed"] is expected_land, result
    print(
        json.dumps(
            {
                "case": amendment or field,
                "actual": grade(repo),
                "claimed": observed,
                "committed": result["committed"],
                "sha": result.get("sha"),
                "refused": result.get("refused"),
            }
        )
    )
    if expected_land:
        checkout = tmp_path / "detached"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(repo), str(checkout)], check=True
        )
        subprocess.run(
            ["git", "checkout", "-q", "--detach", result["sha"]],
            cwd=checkout,
            check=True,
        )
        assert grade(checkout) == "15"
    else:
        assert (
            result["refused"] == "ledger-unmet"
            and result["accountable_run"] == driver.ASK_RUN
        )
        assert not result["sha"] and before == subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo
        )
