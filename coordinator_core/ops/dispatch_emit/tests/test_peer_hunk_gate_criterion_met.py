"""co_declared_peer_holds releases a halted holder whose landing is at HEAD, and holds the rest."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_criterion_met, peer_hunk_gate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/shared.py"
PATH2 = "pkg/other.py"


def _aged(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


H1 = _aged(120, "aaaaaa")
H2 = _aged(90, "bbbbbb")
OWN = _aged(10, "cccccc")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _holder(root: Path, run_id: str, status: str = "halted", path: str = PATH) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [path]}], "doc_writes": []}), encoding="utf-8"
    )
    marker = {"status": status, "run_artifacts": {"withheld": {path: "peer-held"}}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return rdir


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _land(root: Path, run_id: str) -> str:
    _git(root, "commit", "--allow-empty", "-q", "-m", f"land\n\nInline-Review: {run_id}")
    return _git(root, "rev-parse", "HEAD")


def _holds(root: Path) -> list:
    both = [PATH, PATH2]
    return peer_hunk_gate.co_declared_peer_holds(root, OWN, both, both)


def test_met_holder_is_released_recorded_and_never_re_read_while_unmet_holder_holds(root, monkeypatch):
    r1 = _holder(root, H1)
    r2 = _holder(root, H2, path=PATH2)
    sha = _land(root, H1)
    h2_before = (r2 / "halted.json").read_bytes()
    calls = []
    real = halted_criterion_met.met_at_head
    monkeypatch.setattr(
        halted_criterion_met, "met_at_head", lambda *a, **k: calls.append(1) or real(*a, **k)
    )

    holds = _holds(root)

    assert [s for _p, s in holds] == [H2]
    assert json.loads((r1 / "halted.json").read_text(encoding="utf-8"))["released"] == {
        "reason": "criterion-met-at-head", "sha": sha,
    }
    assert (r2 / "halted.json").read_bytes() == h2_before

    calls.clear()
    assert [s for _p, s in _holds(root)] == [H2]
    assert calls == [1]  # only H2 is still halted-and-held: H1 is freed by release_reason before the gate


def test_halted_holder_whose_withheld_path_is_still_dirty_holds_despite_a_landing_at_head(root):
    rdir = _holder(root, H1)
    _land(root, H1)
    (root / "pkg").mkdir()
    (root / PATH).write_text("hunk\n", encoding="utf-8")
    before = (rdir / "halted.json").read_bytes()

    assert [s for _p, s in peer_hunk_gate.co_declared_peer_holds(root, OWN, [PATH], [PATH])] == [H1]
    assert (rdir / "halted.json").read_bytes() == before


def test_resumed_holder_without_a_recorded_release_still_holds(root):
    rdir = _holder(root, H1, status="resumed")
    _land(root, H1)
    before = (rdir / "halted.json").read_bytes()

    assert peer_hunk_gate._release_criterion_met(root, [(PATH, H1)]) == [(PATH, H1)]
    assert (rdir / "halted.json").read_bytes() == before


def test_halted_holder_with_a_baseline_named_held_path_never_reaches_met_at_head(root, monkeypatch):
    rdir = root / "state" / "warp" / H1
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: "0" * 40}}),
        encoding="utf-8",
    )
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    _land(root, H1)
    before = (rdir / "halted.json").read_bytes()
    calls = []
    monkeypatch.setattr(halted_criterion_met, "met_at_head", lambda *a, **k: calls.append(1) or {})

    assert peer_hunk_gate._release_criterion_met(root, [(PATH, H1)]) == [(PATH, H1)]
    assert calls == []
    assert (rdir / "halted.json").read_bytes() == before
