"""Replay e2e for unmet-row recovery: the composed ask script under node with recorded agent replies, its
commit params handed to `dispatch.land_run` on a tmp_path git repo.

Shapes: the r43 R5 halt fixed in one attempt, exhausted at the cap, and escaping the fix footprint; the
432570 T5 halt, whose remaining work sits in chunks stage never ran. No live agent runs."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, land_run
from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_ledger_recovery_loop as loop
from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as base
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    readers_cfg,
    stage_root,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

if shutil.which("node") is None:
    pytest.skip("node not on PATH", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RUN_432570 = "warp-2026-10-23-xl-xxl-fan-out-sizing-seam-pass-432570-20261023T155009-3d4d54"
_STATE_432570 = _REPO_ROOT / "state" / "warp" / _RUN_432570

_TEST_FILE = "tests/test_producer_failure_disclosure.py"
_FOOTPRINT = ["pkg/producer.py", _TEST_FILE]
_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": _FOOTPRINT, "wave": 0, "deps": [], "register_rows": ["R5"]},
    {"id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["pkg/live.py"], "wave": 0, "deps": [], "register_rows": ["R5"]},
    {"id": "C3", "brief_path": "b/C3.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["a.py"], "wave": 0, "deps": [], "register_rows": ["R5"]},
]
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": _ROWS, "review_declared_paths": [p for r in _ROWS for p in r["writes"]],
    "test_targets": [], "marker_path": "m", "head_sha": "c" * 40,
}
_R5_MET = {"id": "R5", "status": "met", "wired": True}
_R5_OPEN = loop._unmet(
    [f"Fix the 2 failing tests in {_TEST_FILE}", "Prove bare-name or production-entry run from a foreign cwd"],
    "the foreign-cwd recogniser test fails", rid="R5",
)
_FIXED = {"status": "DONE", "paths": ["pkg/producer.py"], "reason": "fixed"}
_ESCAPED = {"status": "DONE", "paths": ["pkg/producer.py", "pkg/_producer_substrate.py"], "reason": "needs the substrate"}
_PARAMS_RE = re.compile(r"-----BEGIN TERMINAL-COMMIT PARAMS-----\n(.*?)\n-----END TERMINAL-COMMIT PARAMS-----", re.S)


def _fix_calls(out: dict) -> list[str]:
    return loop._fix_labels(out)


def _agent_labels_outside(out: dict, allowed: set[str]) -> list[str]:
    """Labels of executor-type recovery calls naming a chunk outside `allowed`."""
    return [
        lab for lab in loop._labels(out)
        if lab and lab.startswith(lr.LEDGER_FIX_LABEL) and lab[len(lr.LEDGER_FIX_LABEL):] not in allowed
    ]


def _commit_params(out: dict) -> dict:
    return json.loads(_PARAMS_RE.search(loop._commit_prompt(out)).group(1))


def _write_record(repo: Path, out: dict) -> None:
    """What the recovery-record agent does: write its prompted JSON into the run dir."""
    if "ledger-recovery-record" in loop._labels(out):
        base._write(repo, f"{RUN_DIR_ROOT}/{base.RUN}/{lr.RECOVERY_RECORD_FILE}", json.dumps(loop._record(out)))


def _request(repo: Path, rows: dict) -> None:
    request = CommitRequest(
        chunks=tuple(ChunkCommit(id=cid, title=cid, paths=tuple(paths)) for cid, paths in rows.items()),
        deliverable_id=base.DLV, plan_path=base.PLAN,
    )
    base._write(repo, f"{RUN_DIR_ROOT}/{base.RUN}/commit-request.txt", render_marker(request) + "\n")


def _land(repo: Path, out: dict) -> dict:
    """Run `dispatch.land_run` on the params the script handed the commit leg (run-identity fields aside)."""
    params = _commit_params(out)
    for key in ("run_id", "run_token", "resume_ledger"):
        params.pop(key, None)
    params.update(script_path="run.mjs", session_id=base._SID)
    return land_run._handler(params, repo_root=repo / ".git")


def _message(repo: Path) -> str:
    return base._git(repo, "log", "-1", "--format=%B")


def _lands(repo: Path) -> set[str]:
    line = next(ln for ln in _message(repo).splitlines() if ln.startswith("Lands-Chunks:"))
    return {c.strip() for c in line.split(":", 1)[1].split(",")}


def _register(repo: Path, row_id: str) -> None:
    """Rebind the run's sizing to a register holding the one row the replayed halt names."""
    base._write(
        repo, base.SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\n"
        "requirement_register:\n  sources:\n  - doc: " + base.SIZING + "\n    ref: ask\n  rows:\n"
        f"  - id: {row_id}\n    source_text: replayed halt row\n    anchor: relayed\n    surface: internal\n"
        "    status: open\n    claimed_by: []\n",
    )


def _tree(repo: Path, *, substrate: bool = False) -> None:
    """Three finished chunks' bytes on disk, uncommitted; `substrate` adds r43's stray fix file and its importer."""
    base._write(repo, "pkg/__init__.py", "")
    base._git(repo, "add", "pkg/__init__.py")
    base._git(repo, "commit", "-q", "-m", "package")
    base._write(repo, "pkg/producer.py", "P = 1\n")
    base._write(repo, _TEST_FILE, "def test_x():\n    assert True\n")
    base._write(repo, "pkg/live.py", "from pkg._producer_substrate import S\n" if substrate else "L = 1\n")
    if substrate:
        base._write(repo, "pkg/_producer_substrate.py", "S = 1\n")
    _request(repo, {"C1": _FOOTPRINT, "C2": ["pkg/live.py"], "C3": ["a.py"]})


def _drive(monkeypatch, replies: dict, *, items=None, manifest=None) -> dict:
    return loop._run(loop._script(monkeypatch), items=items or [_R5_OPEN], replies=replies, manifest=manifest or _DISK)


def test_r43_fixed_in_one_attempt_lands_all_three_chunks(monkeypatch, repo):  # noqa: F811
    out = _drive(monkeypatch, {"ledger-fix:C1": _FIXED, lr.LEDGER_REVERIFY_LABEL: {"items": [_R5_MET]}})

    assert _fix_calls(out) == ["ledger-fix:C1"]
    assert not _agent_labels_outside(out, {"C1"})
    assert out["result"]["halted_by"] is None
    assert loop._record(out)["chunks"]["C1"]["status"] == "fixed"
    assert lr.RECOVERY_WITHHELD_PARAM not in _commit_params(out)
    _register(repo, "R5")
    _tree(repo)
    _write_record(repo, out)

    landed = _land(repo, out)

    assert landed["committed"] is True, landed
    assert _lands(repo) == {"C1", "C2", "C3"}
    assert "Requirement-Open" not in _message(repo)
    assert {"pkg/producer.py", _TEST_FILE, "pkg/live.py", "a.py"} <= base._names(repo, landed["sha"])


def test_r43_exhausted_at_the_cap_withholds_the_chunk_and_lands_the_rest(monkeypatch, repo):  # noqa: F811
    out = _drive(monkeypatch, {"ledger-fix:C1": _FIXED, lr.LEDGER_REVERIFY_LABEL: {"items": [_R5_OPEN]}})

    assert _fix_calls(out) == ["ledger-fix:C1"] * lr.RECOVERY_ATTEMPT_CAP
    assert not _agent_labels_outside(out, {"C1"})
    assert out["result"]["halted_by"] is None
    assert loop._record(out)["chunks"]["C1"]["status"] == "exhausted"
    assert _commit_params(out)[lr.RECOVERY_WITHHELD_PARAM] == ["C1"]
    _register(repo, "R5")
    _tree(repo)
    _write_record(repo, out)

    landed = _land(repo, out)

    assert landed["committed"] is True, landed
    assert _lands(repo) == {"C2", "C3"}
    assert "Requirement-Open: R5 recovery-cap-exhausted" in _message(repo)
    names = base._names(repo, landed["sha"])
    assert {"pkg/live.py", "a.py"} <= names and not names & {"pkg/producer.py", _TEST_FILE}
    assert landed["recovery"]["open_rows"] == ["R5"]


def test_r43_footprint_escape_blocks_the_chunk_withholds_its_importer_and_still_lands(monkeypatch, repo):  # noqa: F811
    out = _drive(monkeypatch, {"ledger-fix:C1": [_FIXED, _ESCAPED], lr.LEDGER_REVERIFY_LABEL: {"items": [_R5_OPEN]}})

    assert _fix_calls(out) == ["ledger-fix:C1"] * 2
    assert not _agent_labels_outside(out, {"C1"})
    rec = loop._record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked" and rec["paths"] == ["pkg/_producer_substrate.py"]
    _register(repo, "R5")
    _tree(repo, substrate=True)
    _write_record(repo, out)

    landed = _land(repo, out)

    assert landed["committed"] is True, landed
    assert "halt-record-only" not in json.dumps(landed)
    assert _lands(repo) == {"C3"}
    assert "Requirement-Open: R5 recovery-fix-blocked" in _message(repo)
    names = base._names(repo, landed["sha"])
    assert "a.py" in names and not names & {"pkg/live.py", "pkg/producer.py", "pkg/_producer_substrate.py"}
    assert landed["recovery"]["withheld"]["C2"] == lr.IMPORT_CLOSURE_OPEN
    assert landed["withheld"]["pkg/_producer_substrate.py"].startswith(lr.FIX_BLOCKED)


def _manifest_432570() -> dict:
    if not (_STATE_432570 / "manifest.json").is_file():
        pytest.skip("432570 run record is not in this tree")
    return json.loads((_STATE_432570 / "manifest.json").read_text(encoding="utf-8"))


def _replay_432570(monkeypatch, repo: Path, tmp_path: Path, **stage_emits) -> dict:
    """The 432570 T5 halt replayed over a tmp_path copy of its stage manifest, `stage_emits` overriding fields."""
    halted = json.loads((_STATE_432570 / "halted.json").read_text(encoding="utf-8"))
    copy = tmp_path / "432570-manifest.json"
    copy.write_text(json.dumps({**_manifest_432570(), **stage_emits}), encoding="utf-8")
    manifest = json.loads(copy.read_text(encoding="utf-8"))
    item = {**halted["blocking_items"][0], "status": "not_met", "essential": True}

    out = _drive(monkeypatch, {}, items=[item], manifest=manifest)

    assert _fix_calls(out) == []
    assert (out["result"]["halted_by"] or {}).get("halted") != "ledger-check", out["result"]["halted_by"]
    held = loop._record(out)["chunks"]
    slot_held = {g["id"] for g in manifest["slot_withheld"]}
    assert set(held) == {"C7", "C8", "C9", "C10"} | slot_held and not set(held) & {r["id"] for r in manifest["rows"]}
    assert all(e["status"] == "held" and e["attempts"] == 0 for e in held.values())
    _register(repo, "T5")
    base._write(repo, "a.py", "a\n")
    base._git(repo, "add", "a.py")
    base._git(repo, "commit", "-q", "-m", "finished chunks already landed")
    _request(repo, {"C0": ["a.py"]})
    _write_record(repo, out)
    return _land(repo, out)


def _assert_t5_reported_open(landed: dict) -> None:
    assert landed.get("halted") != "ledger-check" and landed.get("refused") != "ledger-unmet", landed
    report = json.dumps(landed.get("recovery") or {})
    assert "T5" in report and all(c in report for c in ("C7", "C8", "C9", "C10")), landed
    assert all(r.startswith(lr.HELD) for r in landed["recovery"]["withheld"].values())


def test_432570_held_chunks_run_no_fix_leg_and_t5_is_reported_open(monkeypatch, repo, tmp_path):  # noqa: F811
    """Stage as it must emit the shape: T5 not ruled by the EM, its unrun chunks listed as held ids."""
    landed = _replay_432570(
        monkeypatch, repo, tmp_path, rulable_ids=[], held_ids=["C7", "C8", "C9", "C10"],
    )

    _assert_t5_reported_open(landed)


def test_432570_as_recorded_ends_without_a_ledger_check_halt(monkeypatch, repo, tmp_path):  # noqa: F811
    """The manifest exactly as run 432570 staged it carries no `held_ids`; the ledger-resume stage derives them
    from the recorded manifest and the plan spine, with no hand edit."""
    plan = _REPO_ROOT / "docs" / "plans" / "2026-10-23-xl-xxl-fan-out-sizing-seam-pass.md"
    if not plan.is_file():
        pytest.skip("432570 plan is not in this tree")
    raw_by_id = ask_stage._parse_plan(_REPO_ROOT, plan, plan.read_text(encoding="utf-8")).raw_by_id
    held = list(ask_stage.derive_held_ids(raw_by_id, _manifest_432570(), staged_ids=()))
    assert held == ["C7", "C8", "C9", "C10"]

    rulable = ask_stage.without_held_rows(_manifest_432570()["rulable_ids"], held, raw_by_id)
    landed = _replay_432570(monkeypatch, repo, tmp_path, rulable_ids=list(rulable), held_ids=held)

    _assert_t5_reported_open(landed)
