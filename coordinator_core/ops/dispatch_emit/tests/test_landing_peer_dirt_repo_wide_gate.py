"""A fresh red the run's retest could not attribute is checked in an export of HEAD plus only the run's
landing paths: red caused by a peer's uncommitted file lands, red the run's own bytes cause still refuses.
A real git repo and real exports; only the pytest runner is a stub that reads the cwd's files."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-peer-dirt"
GATE = "tests/test_gate.py"
GATE_NODE = f"{GATE}::test_the_live_gate_is_green"
OWN = "src/own.py"
PEER = "src/peer.py"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """``(root, base_sha)``: the run's landing commit rewrites OWN; PEER is clean at HEAD."""
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, GATE, "GATE\n")
    _write(root, OWN, "v = 1\n")
    _write(root, PEER, "v = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, OWN, "v = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "landing")
    _write(root, f"state/warp/{RUN_ID}/manifest.json", json.dumps({"review_declared_paths": [OWN]}))
    return root, base


@pytest.fixture
def runs(monkeypatch):
    """Stub runner: the gate is red when the cwd's PEER holds DIRT or its OWN holds BAD. Records each cwd."""
    cwds: list[Path] = []

    def stub(argv, cwd, *, log=None, timeout=None, append=False):
        cwd = Path(cwd)
        cwds.append(cwd)
        nodes = [a for a in argv if "::" in a]
        red = "DIRT" in (cwd / PEER).read_text(encoding="utf-8") or "BAD" in (cwd / OWN).read_text(encoding="utf-8")
        out = "".join(f"FAILED {n} - AssertionError\n" for n in nodes) if red else ""
        out += f"{0 if red else len(nodes)} passed, {len(nodes) if red else 0} failed in 0.1s\n"
        with open(log, "ab" if append else "wb") as sink:
            sink.write(out.encode())
        return 1 if red else 0

    monkeypatch.setattr(retest_targeted, "_run_argv", stub)
    return cwds


def _fresh_red(root: Path, base: str) -> dict:
    """A fresh engine-written fail record whose gate red has no baseline evidence, as the live pass left it."""
    covered = li.landing_fingerprint(root, [OWN, GATE])
    li.fresh_verdict_path(root, RUN_ID).parent.mkdir(parents=True, exist_ok=True)
    li.fresh_verdict_path(root, RUN_ID).write_text(json.dumps({
        "run_id": RUN_ID, "head": _git(root, "rev-parse", "HEAD"), "covered": covered,
        "status": "fail", "tests": [GATE], "failed_tests": [GATE_NODE],
        "baseline": {"base_sha": "c" * 40, "caused": [], "foreign": [GATE_NODE], "evidence": []},
    }), encoding="utf-8")
    return {"run_base_sha": base}


def test_a_gate_red_by_a_peers_uncommitted_file_lands(repo, runs):
    root, base = repo
    params = _fresh_red(root, base)
    _write(root, PEER, "DIRT\n")

    findings, check = li.landing_test_gate(root, params, RUN_ID)

    assert not [f for f in findings if f.reason == li.TARGETED_TESTS_FAILED]
    assert check == "pre-existing-only"
    assert len(runs) == 1 and runs[0] != root


def test_a_gate_red_by_the_runs_own_bytes_still_refuses(repo, runs):
    root, base = repo
    _write(root, OWN, "BAD\n")
    params = _fresh_red(root, base)

    findings, check = li.landing_test_gate(root, params, RUN_ID)

    assert check == "failed"
    assert [f.reason for f in findings] == [li.TARGETED_TESTS_FAILED]
    assert GATE_NODE in findings[0].detail


def test_a_red_with_no_landing_set_is_not_cleared(repo, runs):
    root, base = repo
    params = _fresh_red(root, base)
    (root / "state" / "warp" / RUN_ID / "manifest.json").unlink()

    findings, check = li.landing_test_gate(root, params, RUN_ID)

    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED
    assert runs == []
