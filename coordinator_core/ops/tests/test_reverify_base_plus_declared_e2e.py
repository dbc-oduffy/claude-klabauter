"""Exit criterion end to end: review.reverify and measured_rev judge a run against base plus its declared paths.

A HEAD-red id is pre_existing (base measure CLI), a peer-dirty id is peer_dirt (export-run CLI), and a declared
path the run never rewrote is untouched_declared, never a peer's hunk."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path, record_dir
from coordinator_core.session.machinery_paths import share_dir

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SESSION = "sess-e2e"
PREFIX = "prep-e2e"
RUN = "ask-20261116T000000-bbbbbb"
ENGINE_ROOT = Path(__file__).resolve().parents[3]

HEAD_RED = "tests/test_head_red.py::test_red_at_head"
PEER = "tests/test_peer.py::test_peer_dirty"
DECLARED = "tests/test_declared.py"

HEAD_RED_BASE = "from pkg import VALUE\n\n\ndef test_red_at_head():\n    assert VALUE == 2\n"
HEAD_RED_RUN = "# run-diff: a comment only\n" + HEAD_RED_BASE
PEER_BASE = "from pkg import VALUE\n\n\ndef test_peer_dirty():\n    assert VALUE == 1\n"
PEER_DIRTY = PEER_BASE.replace("VALUE == 1", "VALUE == 3")
DECLARED_HEAD = "def test_declared():\n    assert True\n"
DECLARED_PRE_RUN = "# left in the tree by a halted run\n" + DECLARED_HEAD


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        input=data, capture_output=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.decode().strip()


def _engine(root: Path, module: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ENGINE_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", module, *args], cwd=root, env=env, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=300,
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_bytes(b"VALUE = 1\n")
    for name, text in (("test_head_red.py", HEAD_RED_BASE), ("test_peer.py", PEER_BASE), ("test_declared.py", DECLARED_HEAD)):
        (root / "tests" / name).write_bytes(text.encode("utf-8"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")

    snap = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    data = HEAD_RED_BASE.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    (snap / digest).write_bytes(data)
    (snap / "manifest.json").write_text(json.dumps({
        "base": base, "recorded_at": "2026-11-16T00:00:00Z",
        "paths": ["tests/test_head_red.py"], "files": {"tests/test_head_red.py": digest},
    }), encoding="utf-8")

    (root / "tests" / "test_head_red.py").write_bytes(HEAD_RED_RUN.encode("utf-8"))
    (root / "tests" / "test_peer.py").write_bytes(PEER_DIRTY.encode("utf-8"))
    (root / "tests" / "test_declared.py").write_bytes(DECLARED_PRE_RUN.encode("utf-8"))

    blob = _git(root, "hash-object", "-w", "--stdin", data=DECLARED_PRE_RUN.encode("utf-8"))
    rdir = record_dir(root, RUN)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(json.dumps({
        "rows": [{"id": "C1", "writes": [DECLARED]}], "write_baseline": {DECLARED: blob},
    }), encoding="utf-8")

    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, []))
    return root, base


def _reverify(root: Path, base: str, **extra) -> dict:
    return rr._handler(
        {"base": base, "slice_prefix": PREFIX, "session_id": SESSION, "failing": [HEAD_RED, PEER],
         "sidecars": ["review.md"], **extra},
        root,
    )


def _measure_files(root: Path, base: str) -> tuple[str, str]:
    """The two files the reverify prompt saves: base reds via the base-measure CLI, failing-id reds in the export."""
    done = _engine(root, "coordinator_core.ops.reverify_base_measure", "--sha", base, "--out", "scratch/base.txt", "--", HEAD_RED, PEER)
    assert done.returncode == 0, done.stderr
    measured = mr.measured_rev(RUN, [DECLARED], repo_root=root)
    assert measured["paths"] == [DECLARED]
    ran = _engine(
        root, "coordinator_core.ops.dispatch_emit.clean_overlay_verify", "--export-run", "--run-id", RUN,
        "--repo-root", str(root), "--", sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", HEAD_RED, PEER,
    )
    assert ran.returncode == 1, ran.stdout + ran.stderr
    trailer = json.dumps({"baseline_export": {"export": "ok", "runner_exit": ran.returncode}})
    (root / "scratch" / "export.txt").write_text(ran.stdout + "\n" + trailer + "\n", encoding="utf-8")
    return "scratch/base.txt", "scratch/export.txt"


def test_without_the_measurements_the_reds_are_neither_pre_existing_nor_peer_dirt(world, monkeypatch) -> None:
    root, base = world
    monkeypatch.setattr(rr, "_measure_at_base", lambda *_a, **_k: (set(), set(), None))
    out = _reverify(root, base)
    assert out["pre_existing"] == [] and out["peer_dirt"] == []
    assert out["foreign_red"] == {PEER: ["tests/test_peer.py"]}
    assert [t for e in out["kept"] for t in e["tests"]] == [HEAD_RED]


def test_head_red_is_pre_existing_peer_dirt_is_peer_dirt_and_nothing_is_unattributed(world) -> None:
    root, base = world
    base_file, export_file = _measure_files(root, base)
    out = _reverify(root, base, base_export_path=base_file, export_failing_path=export_file)
    assert out["error"] is None
    assert out["unattributed"] == [] and out["reverted"] == [] and out["unmeasured"] == []
    assert out["pre_existing"] == [HEAD_RED]
    assert out["peer_dirt"] == [PEER]
    assert out["base_unmeasured"] is False and out["export_unmeasured"] is False
    record = json.loads((root / out["record"]).read_text(encoding="utf-8"))
    assert record["pre_existing"] == [HEAD_RED] and record["peer_dirt"] == [PEER]
    assert (root / "tests" / "test_head_red.py").read_text(encoding="utf-8") == HEAD_RED_RUN
    assert (root / "tests" / "test_peer.py").read_text(encoding="utf-8") == PEER_DIRTY


def test_declared_path_the_run_never_rewrote_is_untouched_not_a_peer_hunk(world) -> None:
    root, _base = world
    out = mr.measured_rev(RUN, [DECLARED], repo_root=root)
    assert out["untouched_declared"] == [DECLARED]
    assert "peer_hunks" not in out and "overlay_sha" not in out
    assert out["blobs"][DECLARED] == _git(root, "hash-object", DECLARED) != _git(root, "rev-parse", "HEAD:" + DECLARED)
    record = json.loads(measured_rev_record_path(root, RUN).read_text(encoding="utf-8"))
    assert record["untouched_declared"] == [DECLARED] and "peer_hunks" not in record
