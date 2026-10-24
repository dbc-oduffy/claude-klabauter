"""land_run halted mode: one commit of the record dir and the finished chunks' product."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import halted_run, land_run_halt, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
PEER_RUN = "ask-20261015T000001-bbbbbb"
LEDGER = {"halted": "ledger-check", "reason": "row R1 open"}
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "docs/plans/peer.md", "peer\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _record(root: Path, run_id: str = RUN, names=("ask.md", "run.json")) -> None:
    for name in names:
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/{name}", f"{name}\n")


def _chunks(root: Path, chunks: dict) -> None:
    built = []
    for cid, paths in chunks.items():
        _write(root, f"reports/{cid}.md", f"DONE: {cid}\n")
        for p in paths:
            _write(root, p, f"{p} = 1\n")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=f"reports/{cid}.md"))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _call(root: Path, halted: dict | None = None, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted or LEDGER, **extra}
    return land_run_halt.land(root, root, params)


def _names(root: Path, sha: str) -> list:
    return _git(root, "show", "--name-only", "--format=", sha).split()


def _message(root: Path) -> str:
    return _git(root, "log", "-1", "--format=%B")


def test_record_dir_lands_with_the_halt_in_the_message(repo):
    _record(repo)

    out = _call(repo, {"halted": "refusal", "reason": "estimate L needs a PM touchpoint"})

    assert out["committed"] is True, out
    assert sorted(_names(repo, out["sha"])) == sorted(
        f"{RECORD_DIR_ROOT}/{RUN}/{n}" for n in ("ask.md", "run.json", "halted.json", "run-stage.json")
    )
    message = _message(repo)
    assert "Halted-By: refusal" in message and "Halt-Reason: estimate L needs a PM touchpoint" in message
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_peer_held_path_is_withheld_whole(repo, monkeypatch):
    _record(repo)
    _chunks(repo, {"C1": ["done.py"], "C2": ["other.py"]})

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        holds.extend((p, "peer-sid") for p in paths if p == "other.py")
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"other.py": "peer-held"}
    assert out["peer_held_by"] == [{"session": "peer-sid", "paths": ["other.py"]}]
    names = _names(repo, out["sha"])
    assert "done.py" in names and "other.py" not in names
    assert "Lands-Chunks: C1" in _message(repo)


def test_token_is_released_not_spent_and_a_repeat_answers_already_landed(repo):
    _record(repo)
    _write(repo, "run.mjs", "// script\n")
    token = run_token.mint(repo, RUN, "run.mjs")

    first = _call(repo, run_token=token)
    assert first["committed"] is True, first
    assert run_token.admits(repo, RUN, token, "run.mjs")

    again = _call(repo, run_token=token)
    assert again["committed"] is True and again["already_landed"] is True
    assert again["sha"] == first["sha"]
    assert run_token.admits(repo, RUN, token, "run.mjs")


def test_a_wrong_token_is_refused_before_any_write(repo):
    _record(repo)
    head = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, run_token="not-a-token")

    assert out["refused"] == "run-token" and out["committed"] is False
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / RECORD_DIR_ROOT / RUN / "halted.json").exists()


def test_a_not_run_verdict_withholds_record_only_and_records_not_run(repo):
    _record(repo)
    _chunks(repo, {"C1": ["done.py"]})

    out = _call(repo, targeted_tests={"status": "error", "tests_run": 0, "tests_failed": 0, "baseline": None})

    assert out["withheld"] == {"done.py": land_run_halt.HALT_RECORD_ONLY}
    assert out["targeted_tests"]["check"] == "not-run"
    assert "targeted-tests-failed" not in out["withheld"].values()
    assert "done.py" not in _names(repo, out["sha"])


def test_a_halt_other_than_ledger_check_lands_the_record_only(repo):
    _record(repo)
    _chunks(repo, {"C1": ["done.py"]})

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert out["withheld"] == {"done.py": land_run_halt.HALT_RECORD_ONLY}
    assert "done.py" not in _names(repo, out["sha"])
    assert halted_run.halted_withheld(repo / RECORD_DIR_ROOT / RUN) == {"done.py": land_run_halt.HALT_RECORD_ONLY}


def test_a_live_peer_runs_undeclared_dirt_is_neither_landed_nor_listed(repo):
    """Closed membership: the peer's dirty plan and its record dir are not this run's paths."""
    _record(repo)
    _record(repo, PEER_RUN, ("ask.md",))
    _write(repo, "docs/plans/peer.md", "peer, edited by its own live run\n")
    _chunks(repo, {"C1": ["done.py"]})

    out = _call(repo, LEDGER, **PASSING)

    names = _names(repo, out["sha"])
    assert "docs/plans/peer.md" not in names
    assert not any(n.startswith(f"{RECORD_DIR_ROOT}/{PEER_RUN}/") for n in names)
    assert "done.py" in names
    listed = str(out.get("withheld", "")) + str(out.get("run_artifacts")) + str(out.get("run_artifact_residue", ""))
    assert "peer.md" not in listed and PEER_RUN not in listed
    status = _git(repo, "status", "--porcelain", "-uall")
    assert "docs/plans/peer.md" in status and PEER_RUN in status


def test_a_tracked_record_file_deleted_from_disk_lands_as_a_removal(repo):
    _record(repo, names=("ask.md", "run.json", "notes.md"))
    _git(repo, "add", RECORD_DIR_ROOT)
    _git(repo, "commit", "-q", "-m", "record")
    (repo / RECORD_DIR_ROOT / RUN / "notes.md").unlink()

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert out["committed"] is True, out
    assert f"Removes: {RECORD_DIR_ROOT}/{RUN}/notes.md" in _message(repo)
    assert f"{RECORD_DIR_ROOT}/{RUN}/notes.md" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_nothing_changed_commits_nothing(repo):
    _record(repo)
    _call(repo, {"halted": "refusal", "reason": "x"})
    head = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert out["committed"] is False and out["nothing_to_commit"] is True
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize(
    "halted, expect",
    [
        ({"halted": "criterion_not_met", "blocker": "failing_test"}, True),
        ({"halted": "review_blocked", "blocker": "failing_test"}, True),
        ({"halted": "criterion_not_met"}, False),
        ({"halted": "criterion_not_met", "blocker": "other"}, False),
        ({"halted": "refusal", "blocker": "failing_test"}, False),
    ],
)
def test_criterion_not_met_test_blocker_is_a_test_blocker_halt(halted, expect):
    assert land_run_halt.is_test_blocker_halt(halted) is expect
    assert land_run_halt.marker_kind(halted) == ("ledger-check" if expect else halted["halted"])
