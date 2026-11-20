"""grind_bookkeep: close, land and sweep a no-product-edit run in-process; a product path is refused."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit import grind_bookkeep as gb
from coordinator_core.ops.dispatch_emit.grind_commit_contract import check_relay
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261118T120000Z"
PROFILE = "lessons"
ROW = "state/backlog/r1.md"
ARCHIVE = "state/backlog/archive/2026-11/r1.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "state" / "backlog").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / ROW).write_text("row one\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _row(**over) -> dict:
    return {
        "row_id": "r1", "path": ROW, "digest": "d" * 64, "verdict": "flake", "evidence": "passes in isolation",
        "outcome": "deferred", "closed": False, "touchedFiles": [ROW, ARCHIVE], **over,
    }


def _record() -> dict:
    return {"profile": PROFILE, "run_id": RUN, "counts": {}, "spend": {}, "handed_back": [], "halted": False}


@pytest.fixture
def verbs(monkeypatch, repo: Path) -> dict:
    """Fake the two grind-row verbs whose real bodies need a profile tree; the land is real."""
    seen: dict = {"close": [], "record": [], "fail_close": False, "sweep_swept": True}

    def close(argv):
        seen["close"].append(argv)
        if seen["fail_close"]:
            print("grind-row close: digest mismatch", file=sys.stderr)
            return 3
        (repo / ARCHIVE).parent.mkdir(parents=True, exist_ok=True)
        (repo / ROW).replace(repo / ARCHIVE)
        print(json.dumps({"old": str(repo / ROW), "new": str(repo / ARCHIVE)}))
        return 0

    def run_record(argv):
        seen["record"].append(argv)
        print(json.dumps({"path": "p", "commit": None, "swept": seen["sweep_swept"], "sweep_reason": "boom"}))
        return 0

    monkeypatch.setattr(grind_rows, "cmd_close", close)
    monkeypatch.setattr(grind_rows, "cmd_run_record", run_record)
    return seen


def _call(repo: Path, rows, **extra) -> dict:
    return gb.bookkeep(repo, RUN, rows, profile=PROFILE, profile_dir=str(repo / "profiles"), **extra)


def test_closes_lands_and_sweeps_with_the_reply_shape(repo, verbs):
    reply = _call(repo, [_row()], record=_record(), declared=[ROW])
    assert reply["closed"] == ["r1"]
    assert reply["committed"] is True and reply["sha"]
    assert reply["swept"] is True and reply["error"] is None
    assert reply["ms"] < 500  # bounds the real land leg only; close and run-record are faked
    head = _git(["show", "--name-status", "--format=%B", "HEAD"], repo)
    assert "Grind-Row: r1 closed" in head
    assert ARCHIVE in head
    assert not (repo / ROW).exists()
    assert verbs["close"][0][verbs["close"][0].index("--closed-by") + 1] == "refute-close"


def test_run_record_gets_a_relay_checked_record_and_declared_paths(repo, verbs):
    _call(repo, [_row()], record=_record(), declared=[ROW])
    argv = verbs["record"][0]
    assert "--sweep" in argv
    assert argv[argv.index("--queue") + 1] == "state/backlog"
    for flag in ("record", "declared"):
        token = argv[argv.index(f"--{flag}-urlenc") + 1]
        assert check_relay(
            f"{flag}-urlenc", token, argv[argv.index(f"--{flag}-len") + 1], argv[argv.index(f"--{flag}-sha256") + 1],
        ) is None
    assert json.loads(unquote(argv[argv.index("--record-urlenc") + 1]))["run_id"] == RUN


def test_product_path_is_refused_naming_the_path_and_nothing_is_written(repo, verbs):
    before = _git(["rev-parse", "HEAD"], repo)
    reply = _call(repo, [_row(touchedFiles=[ROW, "coordinator_core/x.py"])], record=_record())
    assert reply["refused"] == gb.REFUSED_PRODUCT_PATH
    assert "coordinator_core/x.py" in reply["error"] and reply["path"] == "coordinator_core/x.py"
    assert reply["closed"] == [] and reply["committed"] is False
    assert verbs["close"] == [] and verbs["record"] == []
    assert _git(["rev-parse", "HEAD"], repo) == before
    assert (repo / ROW).exists()


def test_a_refused_close_leaves_the_row_open_and_unlanded_but_still_sweeps(repo, verbs):
    verbs["fail_close"] = True
    before = _git(["rev-parse", "HEAD"], repo)
    reply = _call(repo, [_row()], record=_record())
    assert reply["closed"] == [] and reply["committed"] is False
    assert "r1: grind-row close: digest mismatch" in reply["error"]
    assert reply["swept"] is True
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_a_failed_sweep_is_reported_not_swallowed(repo, verbs):
    verbs["sweep_swept"] = False
    reply = _call(repo, [_row()], record=_record())
    assert reply["committed"] is True and reply["swept"] is False
    assert "sweep: boom" in reply["error"]


def test_no_record_skips_the_sweep(repo, verbs):
    reply = _call(repo, [_row()])
    assert reply["committed"] is True and reply["swept"] is False
    assert reply["sweep_skipped"] and verbs["record"] == []


@pytest.mark.parametrize("rows", [[], [{"row_id": "r1"}]])
def test_malformed_rows_are_refused_before_any_write(repo, verbs, rows):
    reply = _call(repo, rows)
    assert reply["refused"] == gb.REFUSED_PARAMS and verbs["close"] == []


@pytest.mark.parametrize(
    "over",
    [{"row_id": "../../evil"}, {"row_id": "a/b"}, {"path": "../outside.md"}, {"path": "/abs/outside.md"}],
)
def test_a_row_naming_a_path_outside_its_segment_is_refused_before_any_write(repo, verbs, over):
    reply = _call(repo, [_row(**over)], record=_record())
    assert reply["refused"] == gb.REFUSED_PARAMS and verbs["close"] == [] and not (repo / "state" / "queue-grind").exists()


@pytest.mark.parametrize("kwargs", [{"profile": "../x"}, {"profile": "a/b"}])
def test_a_profile_that_is_not_one_segment_is_refused(repo, verbs, kwargs):
    reply = gb.bookkeep(repo, RUN, [_row()], profile_dir=str(repo / "profiles"), **kwargs)
    assert reply["refused"] == gb.REFUSED_PARAMS and verbs["close"] == []


def test_the_op_is_registered_and_runs_bookkeep(repo, verbs):
    from coordinator_core.ipc import _REGISTRY

    assert gb.OP_NAME in _REGISTRY
    reply = _REGISTRY[gb.OP_NAME]({
        "repo_root": str(repo), "run_id": RUN, "rows": [_row()], "record": _record(),
        "run_close_deferred": True, "profile_dir": str(repo / "profiles"),
    })
    assert reply["closed"] == ["r1"] and reply["swept"] is True


def test_concurrent_verb_runs_each_capture_their_own_output_and_restore_the_streams():
    import sys
    import threading
    import time

    before = (sys.stdout, sys.stderr)
    seen: dict[str, tuple] = {}

    def verb(tag):
        def run(_argv):
            print(f"out-{tag}")
            time.sleep(0.05)
            print(f"err-{tag}", file=sys.stderr)
            return 0
        return run

    threads = [threading.Thread(target=lambda t=t: seen.__setitem__(t, gb._run_verb(verb(t), []))) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (sys.stdout, sys.stderr) == before
    assert seen["a"] == (0, "out-a\n", "err-a") and seen["b"] == (0, "out-b\n", "err-b")
