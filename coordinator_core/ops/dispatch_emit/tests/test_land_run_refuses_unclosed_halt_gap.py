"""A ledger resume's met blocking row owes a gaps_closed entry per halt gap line; both commit-side ledger gates refuse one without."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_run as lr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals
from coordinator_core.ops.dispatch_emit.ask_contract import (
    GAPS_CLOSED_FIELD, HALT_GAP_UNCLOSED_REASON, RECORD_DIR_ROOT,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261017T000000-abc123"
HALTED = "ask-20261024T004523-638410"
REL = "state/sizings/2026-10-17-resume.yaml"
GAP = "verify-stage prompts do not carry the corpus-probe clause"
CLOSED = {"gap": GAP, "how": "verify prompt now carries the clause"}


def _git(root, *a):
    return subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _verdict(*closed, status="met"):
    item = {"id": "R1", "status": status, "wired": True}
    if closed:
        item[GAPS_CLOSED_FIELD] = list(closed)
    return {"items": [item]}


def _resume(halt, remaining=True):
    out = {"run_id": HALTED, "halt_sha": halt, "blocking": ["R1"]}
    if remaining:
        out["remaining"] = {"R1": {"remaining": [GAP], "evidence": "e"}}
    return out


def test_pure_met_without_gaps_closed_fails_naming_row():
    gaps = {"R1": {"remaining": [f"  {GAP}  "]}}
    out = lr._unclosed_halt_gaps(_resume("x"), _verdict(), gaps)
    assert out == [{"id": "R1", "reason": f"{HALT_GAP_UNCLOSED_REASON}: {GAP}"}]


def test_pure_matching_entry_passes_and_post_commit_is_judged():
    gaps = {"R1": {"remaining": [GAP]}}
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(CLOSED), gaps) == []
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(status="post_commit"), gaps)[0]["id"] == "R1"


def test_pure_empty_how_wrong_gap_and_unmet_rows():
    gaps = {"R1": {"remaining": [GAP]}}
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict({"gap": GAP, "how": " "}), gaps)
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict({"gap": "other", "how": "h"}), gaps)
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(status="not_met"), gaps) == []
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(), {"R2": {"remaining": [GAP]}}) == []
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(), {}) == []


def test_pure_commit_line_and_post_commit_acts_are_discharged_by_the_commit():
    gaps = {"R1": {"remaining": ["Fix X", "commit"]}}
    fix = {"gap": "Fix X", "how": "fixed"}
    verdict = _verdict(fix, status="post_commit")
    verdict["items"][0]["post_commit_acts"] = ["commit"]
    assert lr._unclosed_halt_gaps(_resume("x"), verdict, gaps) == []
    assert lr._unclosed_halt_gaps(_resume("x"), _verdict(fix), gaps) == []
    other = {"R1": {"remaining": ["Fix X", "Fix Y"]}}
    assert lr._unclosed_halt_gaps(_resume("x"), verdict, other)[0]["id"] == "R1"
    acts = {"R1": {"remaining": ["Fix X", "sha_reply"]}}
    assert lr._unclosed_halt_gaps(_resume("x"), verdict, acts)[0]["reason"].endswith("sha_reply")
    verdict["items"][0]["post_commit_acts"] = ["commit", "sha_reply"]
    assert lr._unclosed_halt_gaps(_resume("x"), verdict, acts) == []


def test_terminal_commit_passes_post_commit_row_with_commit_remaining(run):
    resume = _resume(run[3])
    resume["remaining"] = {"R1": {"remaining": ["Fix X", "commit"], "evidence": "e"}}
    verdict = _verdict({"gap": "Fix X", "how": "fixed"}, status="post_commit")
    verdict["items"][0]["post_commit_acts"] = ["commit"]
    refusal = _gate(run, verdict, resume)
    assert refusal is None or HALT_GAP_UNCLOSED_REASON not in json.dumps(refusal)


def _halt_record(root):
    d = root / RECORD_DIR_ROOT / HALTED
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({
        "halted": "ledger-check", "sizing_rel": REL, "blocking": ["R1"],
        "blocking_items": [{"id": "R1", "remaining": [GAP], "evidence": "e"}],
    }), encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1"}]}), encoding="utf-8")


def test_gap_lines_from_payload_then_record_without_subprocess(tmp_path, monkeypatch):
    _halt_record(tmp_path)

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert lr._halt_gap_lines(tmp_path, _resume("x"))["R1"]["remaining"] == [GAP]
    from_record = lr._halt_gap_lines(tmp_path, _resume("x", remaining=False))
    assert from_record["R1"]["remaining"] == [GAP]
    assert lr._unclosed_halt_gaps(_resume("x", remaining=False), _verdict(), from_record)


def test_no_payload_and_no_record_means_no_failures(tmp_path):
    resume = _resume("x", remaining=False)
    gaps = lr._halt_gap_lines(tmp_path, resume)
    assert gaps == {}
    assert lr._unclosed_halt_gaps(resume, _verdict(), gaps) == []


def test_halted_commit_params_keeps_gaps_closed():
    digest = {
        "halted_by": {"halted": "commit"}, "run_id": "r", "arm": "xs",
        "ledger": {"items": [{"id": "R1", "status": "met", GAPS_CLOSED_FIELD: [CLOSED]}]},
        "review": {"wave": []},
    }
    params = terminal_commit_refusals._halted_commit_params(digest)
    assert params["ledger_verdict"]["items"][0][GAPS_CLOSED_FIELD] == [CLOSED]


@pytest.fixture
def run(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "deliver the owed work"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    manifest = run_dir / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    (tmp_path / "prod.py").write_text("x = 2\n", encoding="utf-8")
    return tmp_path, run_dir, manifest, halt


def _gate(run, verdict, resume):
    root, run_dir, mpath, _ = run
    return terminal_commit_ledger._ledger_gate(root, run_dir, verdict, mpath, resume_ledger=resume)[0]


def test_terminal_commit_refuses_unclosed_gap_and_passes_closed(run):
    refusal = _gate(run, _verdict(), _resume(run[3]))
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["ledger_failures"] == [{"id": "R1", "reason": f"{HALT_GAP_UNCLOSED_REASON}: {GAP}"}]
    assert _gate(run, _verdict(CLOSED), _resume(run[3])) is None


def _land_gate(run, verdict, resume):
    root, run_dir, mpath, _ = run
    return lr._ledger_gate(root, run_dir, verdict, mpath, (), {}, None, {}, resume, REL)[0]


def test_land_run_refuses_unclosed_gap_and_passes_closed(run):
    refusal = _land_gate(run, _verdict(), _resume(run[3]))
    assert refusal is not None
    assert {"id": "R1", "reason": f"{HALT_GAP_UNCLOSED_REASON}: {GAP}"} in refusal["ledger_failures"]
    closed = _land_gate(run, _verdict(CLOSED), _resume(run[3]))
    assert HALT_GAP_UNCLOSED_REASON not in json.dumps(closed)


def test_terminal_commit_non_resume_is_unaffected(run):
    assert _gate(run, _verdict(), None) is None
