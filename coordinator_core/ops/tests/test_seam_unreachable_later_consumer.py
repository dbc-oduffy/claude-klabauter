"""plan.seam_record / plan.seam_check at wave-boundary: ``unreachable`` is computed in-process, is
non-blocking while a not-yet-landed row of the SAME plan declares the path, and the written sidecar
is cut from the same reply. Real git repos under tmp_path; reachability is never faked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_seam_check as op

pytestmark = [pytest.mark.spawns_process]

API = "lib/api.py"
API_SRC = "def save_photo_arrays():\n    return 1\n"

PLAN = (
    "---\ntitle: t\nstatus: approved\ncapabilities: []\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
)
ROW = "- id: {id}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: [{writes}]\n{extra}"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _plan(root: Path, name: str, rows: list) -> str:
    rel = f"docs/plans/2026-10-27-{name}.md"
    (root / "docs/plans").mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(PLAN.format(rows="".join(rows)), encoding="utf-8")
    return rel


def _row(row_id: str, writes: str, extra: str = "") -> str:
    return ROW.format(id=row_id, writes=writes, extra=extra)


def _land(root: Path, files: dict) -> str:
    """Commit the plans as the base, then the wave's files; return the landed range."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "wave")
    base, head = _git(root, "rev-parse", "HEAD~1", "HEAD").split()
    return f"{base}..{head}"


def _params(plans: list, rng: str, landed: list, wave: int = 1) -> dict:
    return {"plans": plans, "phase": "wave-boundary", "named_set": True, "landed_range": rng,
            "landed_rows": [{"plan": p, "row": r} for p, r in landed], "wave": wave}


def _unreachable(reply: dict) -> list:
    return [f for f in reply["findings"] if f["class"] == "unreachable"]


@pytest.mark.parametrize("key", ["consumes", "reads_at_head"])
def test_intermediate_wave_later_consumer_is_non_blocking(tmp_path, key):
    a = _plan(tmp_path, "a", [_row("A1", API), _row("A2", "docs/out.md", f"  {key}: [{API}]\n")])
    rng = _land(tmp_path, {API: API_SRC})
    reply = op._check_handler(_params([a], rng, [(a, "A1")]), tmp_path)
    found = _unreachable(reply)
    assert [(f["path"], f["blocking"], f["plan"]) for f in found] == [(API, False, a)], reply["findings"]
    assert reply["verdict"] == "CLEAN" and reply["per_plan"][a] == "CLEAN"


def test_final_wave_unreachable_still_blocks(tmp_path):
    a = _plan(tmp_path, "a", [_row("A1", API), _row("A2", "notes.txt", f"  consumes: [{API}]\n")])
    rng = _land(tmp_path, {API: API_SRC, "notes.txt": "n\n"})
    reply = op._check_handler(_params([a], rng, [(a, "A1"), (a, "A2")]), tmp_path)
    found = _unreachable(reply)
    assert [(f["path"], f["blocking"], f["plan"]) for f in found] == [(API, True, a)], reply["findings"]
    assert reply["verdict"] == "DRIFT" and reply["per_plan"][a] == "DRIFT"


def test_consumer_in_a_different_plan_does_not_exempt(tmp_path):
    a = _plan(tmp_path, "a", [_row("A1", API)])
    b = _plan(tmp_path, "b", [_row("B1", "docs/out.md", f"  consumes: [{API}]\n")])
    rng = _land(tmp_path, {API: API_SRC})
    reply = op._check_handler(_params([a, b], rng, [(a, "A1")]), tmp_path)
    assert [(f["plan"], f["blocking"]) for f in _unreachable(reply)] == [(a, True)], reply["findings"]
    assert reply["per_plan"][a] == "DRIFT"


def test_a_coded_consumer_row_counts_as_landed(tmp_path):
    a = _plan(tmp_path, "a", [
        _row("A1", API),
        _row("A2", "docs/out.md", f"  consumes: [{API}]\n  disposition: coded\n"),
    ])
    rng = _land(tmp_path, {API: API_SRC})
    reply = op._check_handler(_params([a], rng, [(a, "A1")]), tmp_path)
    assert [f["blocking"] for f in _unreachable(reply)] == [True], reply["findings"]


@pytest.mark.parametrize("final", [False, True])
def test_sidecar_verdict_and_findings_equal_the_reply(tmp_path, final):
    rows = [_row("A1", API), _row("A2", "notes.txt", f"  consumes: [{API}]\n")]
    a = _plan(tmp_path, "a", rows)
    b = _plan(tmp_path, "b", [_row("B1", "docs/b.md")])
    rng = _land(tmp_path, {API: API_SRC, **({"notes.txt": "n\n"} if final else {})})
    landed = [(a, "A1"), (a, "A2")] if final else [(a, "A1")]
    reply = op._record_handler(_params([a, b], rng, landed, wave=2), tmp_path)
    assert _unreachable(reply), reply["findings"]
    keys = ("class", "blocking", "counterpart_plan", "capability", "missing_consumer", "path", "row", "detail")
    for rel in (a, b):
        doc = yaml.safe_load((tmp_path / rel).with_name(Path(rel).stem + ".wave-2.seam.yaml").read_text())
        assert doc["verdict"] == reply["per_plan"][rel]
        assert doc["findings"] == [{k: f[k] for k in keys} for f in reply["findings"] if f["plan"] == rel]
    assert reply["per_plan"][a] == ("DRIFT" if final else "CLEAN")


def test_a_reachability_op_error_fails_the_check_closed(tmp_path, monkeypatch):
    from coordinator_core.ops import review_reachability

    a = _plan(tmp_path, "a", [_row("A1", API)])
    rng = _land(tmp_path, {API: API_SRC})
    monkeypatch.setattr(review_reachability, "_handler", lambda params, repo_root=None: {"error": "boom"})
    with pytest.raises(ValueError, match="review.reachability: boom"):
        op._check_handler(_params([a], rng, [(a, "A1")]), tmp_path)


def test_a_broken_click_path_is_a_blocking_unreachable_finding(tmp_path, monkeypatch):
    from coordinator_core.ops import review_reachability

    a = _plan(tmp_path, "a", [_row("A1", API)])
    rng = _land(tmp_path, {API: API_SRC})
    broken = {"entries": [], "click_paths": [{"role": "admin", "verdict": "broken", "broken_at": "step 2"}]}
    monkeypatch.setattr(review_reachability, "_handler", lambda params, repo_root=None: broken)
    reply = op._check_handler(_params([a], rng, [(a, "A1")]), tmp_path)
    assert [(f["plan"], f["blocking"]) for f in _unreachable(reply)] == [(a, True)], reply["findings"]
    assert reply["verdict"] == "DRIFT"
