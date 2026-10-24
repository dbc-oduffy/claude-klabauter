"""dispatch.land_run: a halted holder with no hunks in a declared path releases it; one with hunks refuses the landing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_peer_hunk_gate import (
    PATH,
    SIZING,
    _aged,
    _holder,
    _sizing,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

B0 = "x = 0\n"
B1 = "x = 1  # halted holder's hunk\n"
B2 = "x = 2\n"
HALT_AT = "2026-10-24T01:00:00Z"
START_AT = "2026-10-24T02:00:00Z"
STATUS_READ = 1  # the one shared dirty-tree read a halted holder declaring a candidate path costs; attribution itself spawns nothing


def _sha(text: str) -> str:
    return blob_sha(text.encode("utf-8"))


def _fresh(root: Path) -> Path:
    root.mkdir(parents=True)
    prod._git(["init", "-q"], root)
    prod._git(["config", "user.email", "t@example.com"], root)
    prod._git(["config", "user.name", "t"], root)
    prod._git(["config", "core.autocrlf", "false"], root)
    prod._write(root, "README.md", "seed\n")
    prod._write(root, prod.PLAN, "---\ntitle: p\n---\nbody\n")
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "seed"], root)
    session_core.init(prod.SELF, cwd=str(root))
    return root


def _landing(root: Path, *, b_landing: str, holder: str | None, b_holder: str, live: bool = False) -> str | None:
    """PATH committed at B0; the landing run L stages against `b_landing`, writes B2 over the tree,
    and a peer `holder` (halted, or live when `live`) declares PATH against `b_holder`."""
    prod._write(root, PATH, B0)
    prod._git(["add", PATH], root)
    prod._git(["commit", "-q", "-m", "base"], root)
    holder_id = None
    if holder:
        holder_id = _aged(60, holder)
        _sizing(root, SIZING)
        if live:
            _holder(root, holder_id, None, token=True, manifest=False)
        else:
            rdir = _holder(root, holder_id, "halted", plan="docs/plans/other.md")
            (rdir / "manifest.json").write_text(
                json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [],
                            "write_baseline": {PATH: _sha(b_holder)}}),
                encoding="utf-8",
            )
            (rdir / "run-stage.json").write_text(
                json.dumps({"stage": "halted", "started_at": "2026-10-24T00:00:00Z", "last_activity_at": HALT_AT}),
                encoding="utf-8",
            )
        if b_holder != B0 and not live:
            prod._write(root, PATH, b_holder)
    rec = f"{RECORD_DIR_ROOT}/{prod.RUN}"
    prod._write(root, f"{rec}/manifest.json", json.dumps({"rows": [], "write_baseline": {PATH: b_landing}}))
    prod._write(root, f"{rec}/run-stage.json", json.dumps(
        {"stage": "staged", "started_at": START_AT, "last_activity_at": START_AT}))
    prod._author(root, PATH, B2)
    prod._declare(root, PATH)
    prod._record_dir(root)
    return holder_id


def _spawns(monkeypatch) -> dict:
    count = {"n": 0}
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            count["n"] += 1
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return count


def _count_landing(monkeypatch, root: Path, **kw) -> tuple:
    count = _spawns(monkeypatch)
    holder_id = _landing(root, **kw)
    count["n"] = 0
    out = prod._land(root)
    return out, count["n"], holder_id


def test_halted_holder_with_no_hunks_in_the_path_lands(repo, monkeypatch, tmp_path):
    out, n, holder = _count_landing(monkeypatch, repo, b_landing=_sha(B0), holder="aaaaaa", b_holder=B0)

    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert peer_hunk_gate.PEER_HUNKS_CO_DECLARED not in json.dumps(out)
    assert prod._git(["show", "HEAD:" + PATH], repo) == B2
    assert PATH in prod._tree(repo)
    assert json.loads((repo / "state" / "warp" / holder / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"

    _, control_n, _ = _count_landing(monkeypatch, _fresh(tmp_path / "control"), b_landing=_sha(B0), holder=None, b_holder=B0)
    assert n == control_n + STATUS_READ


def test_halted_holder_with_hunks_in_the_path_refuses_the_landing(repo, monkeypatch, tmp_path):
    out, n, holder = _count_landing(monkeypatch, repo, b_landing=_sha(B1), holder="aaaaaa", b_holder=B0)

    assert out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    assert out.get("committed") is not True
    assert holder in str(out), out
    assert prod._message(repo).strip() == "base"
    assert (repo / PATH).read_text(encoding="utf-8") == B2
    assert json.loads((repo / "state" / "warp" / holder / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"

    _, none_n, _ = _count_landing(monkeypatch, _fresh(tmp_path / "control"), b_landing=_sha(B1), holder=None, b_holder=B0)
    live_out, live_n, _ = _count_landing(
        monkeypatch, _fresh(tmp_path / "live"), b_landing=_sha(B1), holder="cccccc", b_holder=B0, live=True
    )
    assert live_out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, live_out
    assert n == none_n + STATUS_READ
    assert n <= live_n + STATUS_READ
