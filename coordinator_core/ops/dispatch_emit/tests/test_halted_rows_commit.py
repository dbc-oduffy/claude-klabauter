"""dispatch.commit_halted_rows: per-row commits of a halted run, hunk-scoped where write sets overlap."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_rows_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED_SEED = "".join(f"line {i}\n" for i in range(1, 31))
ROW_HUNK = SHARED_SEED.replace("line 2\n", "line 2 by r1\n")
BOTH_HUNKS = ROW_HUNK.replace("line 28\n", "line 28 by r3\n")
_DIFFS: dict = {}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for name, body in (("a.py", "a = 0\n"), ("b.py", "b = 0\n"), ("c.py", "c = 0\n"), ("shared.py", SHARED_SEED)):
        (root / name).write_text(body, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    chunks = (
        ChunkCommit(id="r1", title="first", paths=("a.py", "shared.py")),
        ChunkCommit(id="r2", title="second", paths=("b.py",)),
        ChunkCommit(id="r3", title="withheld", paths=("c.py", "shared.py")),
    )
    marker = render_marker(CommitRequest(chunks=chunks))
    (root / "run.mjs").write_text(marker + "\n", encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "b.py").write_text("b = 1\n", encoding="utf-8")
    (root / "c.py").write_text("c = 1\n", encoding="utf-8")
    (root / "shared.py").write_text(ROW_HUNK, encoding="utf-8")
    r1_diff = _git(["diff", "--", "shared.py"], root)
    (root / "shared.py").write_text(BOTH_HUNKS, encoding="utf-8")
    _DIFFS[str(root)] = r1_diff
    return root


def _call(root: Path, delivered, with_diff=True, **extra) -> dict:
    params = {
        "script_path": "run.mjs",
        "delivered": delivered,
        "open_rows": [{"id": "r3", "reason": "gated on DoE field"}],
        **extra,
    }
    if with_diff:
        params.setdefault("row_diffs", {"shared.py": _DIFFS[str(root)]})
    return halted_rows_commit._handler(params, repo_root=root / ".git")


def _show(root: Path, rev: str, path: str) -> str:
    return _git(["show", f"{rev}:{path}"], root)


def test_halted_run_lands_delivered_rows_and_leaves_withheld_open(repo):
    out = _call(repo, ["r1", "r2"])

    assert out["ok"] is True, out
    assert [x["row"] for x in out["landed"]] == ["r1", "r2"]
    status = _git(["status", "--porcelain"], repo)
    assert "c.py" in status
    assert "b.py" not in status and " a.py" not in status
    message = _git(["log", "--format=%B", "-n", "2"], repo)
    assert "Requirement-Open: r3 gated on DoE field" in message
    assert "Halted-Run-Row: r1" in message


def test_overlapping_file_carries_only_the_rows_own_hunks(repo):
    out = _call(repo, ["r1"])

    assert out["ok"] is True, out
    assert _show(repo, "HEAD", "shared.py") == ROW_HUNK
    assert (repo / "shared.py").read_text(encoding="utf-8") == BOTH_HUNKS
    assert _git(["diff", "--cached", "--name-only"], repo).strip() == ""


def test_overlapping_file_without_a_recorded_diff_refuses_the_row(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, ["r1"], with_diff=False)

    assert out["ok"] is False
    assert out["refused"][0]["row"] == "r1"
    assert "shared.py" in out["refused"][0]["reason"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_diff_naming_another_file_is_refused(repo):
    out = _call(repo, ["r1"], row_diffs={"shared.py": _DIFFS[str(repo)].replace("shared.py", "a.py")})

    assert out["refused"] and "other than" in out["refused"][0]["reason"]


def test_second_call_is_a_noop(repo):
    _call(repo, ["r1", "r2"])
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, ["r1", "r2"])

    assert out["ok"] is True, out
    assert out["landed"] == []
    assert sorted(out["noop"]) == ["r1", "r2"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_unknown_row_is_rejected(repo):
    out = _call(repo, ["r9"])

    assert out["ok"] is False and "r9" in out["error"]


RUN = "ask-run-20261016T000000-aaaaaa"
SUCC = "ask-run-20261016T000001-bbbbbb"
PARENT = "ask-parent-20261016T000000-cccccc"


def _halt_fixture(root: Path) -> None:
    """Every row clean, an ask script under scratch/warp, RUN at running with SUCC waiting after it."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "rows landed"], root)
    script = root / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text((root / "run.mjs").read_text(encoding="utf-8"), encoding="utf-8")
    runs = (
        c.SiblingRun(RUN, RUN, (), "state/sizings/a.yaml", c.STATUS_READY),
        c.SiblingRun(SUCC, SUCC, (RUN,), "state/sizings/b.yaml", c.STATUS_AFTER),
    )
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, runs).to_json()), encoding="utf-8")
    st = root / c.status_rel(RUN)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"status": c.STATUS_RUNNING, "parent_id": PARENT}), encoding="utf-8")


def _status(root: Path, rid: str = RUN) -> str:
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

    return json.loads((root / c.status_rel(rid)).read_text(encoding="utf-8"))["status"]


def _halt_call(root: Path, **extra) -> dict:
    params = {"script_path": f"scratch/warp/{RUN}.workflow.mjs", "delivered": ["r1", "r2"], **extra}
    return halted_rows_commit._handler(params, repo_root=root / ".git")


def test_all_noop_rows_still_settle_the_run_halted(repo):
    _halt_fixture(repo)

    out = _halt_call(repo)

    assert out["noop"] == ["r1", "r2"] and out["landed"] == []
    assert _status(repo) == "halted"


def test_successor_is_told_the_predecessor_halted(repo):
    from coordinator_core.hooks.submit_engine_ask import _slice_deny

    _halt_fixture(repo)
    _halt_call(repo, open_rows=[{"id": "r3", "reason": "gated on DoE field"}])

    deny = _slice_deny(repo, (PARENT, SUCC))

    assert deny is not None and "halted" in deny and "has not landed" not in deny


def test_successor_is_released_when_every_row_is_committed(repo):
    from coordinator_core.hooks.submit_engine_ask import _slice_deny

    _halt_fixture(repo)
    out = _halt_call(repo, delivered=["r1", "r2", "r3"])

    assert out["siblings"]["blocked"] == []
    assert _slice_deny(repo, (PARENT, SUCC)) is None


def test_an_undelivered_row_keeps_the_successor_held(repo):
    from coordinator_core.hooks.submit_engine_ask import _slice_deny

    _halt_fixture(repo)
    _halt_call(repo)

    assert _slice_deny(repo, (PARENT, SUCC)) is not None


def test_explicit_run_id_wins_over_script_stem(repo):
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

    _halt_fixture(repo)
    other = repo / c.status_rel("other-run")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps({"status": c.STATUS_RUNNING}), encoding="utf-8")

    _halt_call(repo, run_id="other-run")

    assert _status(repo, "other-run") == "halted"
    assert _status(repo) == "running"


def test_parameter_failure_writes_no_status(repo):
    _halt_fixture(repo)

    out = _halt_call(repo, delivered=[])

    assert out["ok"] is False
    assert _status(repo) == "running"
