"""Both close ops stamp the Checkpoint-Base of every run-open checkpoint of the run's plan, a base that is another checkpoint included."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_run_superseded, kept_work
from coordinator_core.ops.dispatch_emit import halted_run_landed as landed_op
from coordinator_core.percolate.source_preflight import _open_checkpoints
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-x-20261026T000000-abcdef"
PLAN = "docs/plans/2026-11/2026-11-16-chain-aaaaaa.md"
OTHER_PLAN = "docs/plans/2026-11/2026-11-16-other-bbbbbb.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _checkpoint(repo: Path, plan: str, base: str, withheld: str, tag: str) -> str:
    message = (
        f"checkpoint(wave 0): 1 rows — C{tag}\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {base}\n"
        f"Checkpoint-Not-Landed: run-open\nCheckpoint-Withheld: {withheld}\n"
    )
    return _commit(repo, {plan: f"plan {tag}"}, message)


def _chain(root: Path, withheld: tuple, b0: str) -> tuple:
    """CP1 (base B0) -> CP2 (base CP1, same plan) -> CP3 (base CP2, other plan); returns ``(b0, cp1, cp2, cp3)``."""
    cp1 = _checkpoint(root, PLAN, b0, withheld[0], "1")
    cp2 = _checkpoint(root, PLAN, cp1, withheld[1], "2")
    cp3 = _checkpoint(root, OTHER_PLAN, cp2, "c.txt", "3")
    return b0, cp1, cp2, cp3


def _closed_in_head(root: Path) -> set:
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases

    return set(run_closed_bases(_git(root, "log", "-1", "--format=%B")))


def _halted_repo(tmp_path: Path, withheld: tuple, commit_record: bool = True):
    _git(tmp_path, "init", "-q")
    old = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    with pytest.MonkeyPatch.context() as mp:
        for key, value in old.items():
            mp.setenv(key, value)
        _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0", "c.txt": "c0"}, "seed")
    b0 = _git(tmp_path, "rev-parse", "HEAD")
    record = ask_contract.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, plan_rel=PLAN, run_base_sha=b0)
    record.joinpath("manifest.json").write_text(f'{{"head_sha": "{b0}"}}', encoding="utf-8")
    ask_contract.write_run_stage_at(record, "halted", now="2026-01-10T00:00:00Z")
    if commit_record:
        _git(tmp_path, "add", "--", record.relative_to(tmp_path).as_posix())
        _git(tmp_path, "commit", "-q", "-m", "halt record")
    return record, _chain(tmp_path, withheld, b0)


def test_record_landed_closes_checkpoint_based_on_another_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    _, (b0, cp1, cp2, cp3) = _halted_repo(tmp_path, ("a.txt", "b.txt"))
    landing = _commit(tmp_path, {"a.txt": "a1", "b.txt": "b1"}, "landing")
    assert len(_open_checkpoints(tmp_path, b0)) == 3

    out = landed_op._handler({"run_id": RUN, "sha": landing}, tmp_path)

    assert out["ok"], out
    assert _closed_in_head(tmp_path) == {b0, cp1}
    assert [plan for _, plan in _open_checkpoints(tmp_path, b0)] == [OTHER_PLAN]


def test_record_landed_leaves_same_plan_checkpoint_whose_paths_never_landed(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    _, (b0, cp1, cp2, cp3) = _halted_repo(tmp_path, ("a.txt", "b.txt"))
    landing = _commit(tmp_path, {"a.txt": "a1"}, "landing carries only a.txt")

    out = landed_op._handler({"run_id": RUN, "sha": landing}, tmp_path)

    assert out["ok"], out
    assert _closed_in_head(tmp_path) == {b0}
    assert [(sha, plan) for sha, plan in _open_checkpoints(tmp_path, b0) if plan == PLAN] == [(cp2, PLAN)]


def test_close_checkpointed_run_without_halt_record_closes_chained_same_plan_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0", "c.txt": "c0"}, "seed")
    record = ask_contract.record_dir(tmp_path, RUN)
    b0, cp1, cp2, cp3 = _chain(tmp_path, ("a.txt", "b.txt"), _git(tmp_path, "rev-parse", "HEAD"))
    record.mkdir(parents=True, exist_ok=True)
    record.joinpath("manifest.json").write_text(f'{{"head_sha": "{b0}"}}', encoding="utf-8")
    record.joinpath("resume.json").write_text(f'{{"resume_plan_rel": "{PLAN}"}}', encoding="utf-8")
    landing = _commit(tmp_path, {PLAN: "landed plan", "a.txt": "a1", "b.txt": "b1"}, "landing")

    out = landed_op._handler({"run_id": RUN, "sha": landing}, tmp_path)

    assert out["ok"], out
    assert _closed_in_head(tmp_path) == {b0, cp1}
    assert [plan for _, plan in _open_checkpoints(tmp_path, b0)] == [OTHER_PLAN]


def test_close_superseded_closes_chained_same_plan_checkpoints_and_not_other_plans(tmp_path):
    record, (b0, cp1, cp2, cp3) = _halted_repo(tmp_path, ("a.txt", "b.txt"), commit_record=False)
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept"})
    kept_work.write(record, {"c": {"paths": {"a.txt": _hash(tmp_path, "a-kept"), "b.txt": _hash(tmp_path, "b-kept")}}}, None)
    _commit(tmp_path, {"a.txt": "a-later", "b.txt": "b-later"}, "later commit supersedes the withheld paths")
    assert len(_open_checkpoints(tmp_path, b0)) == 3

    reply = halted_run_superseded._handler({"run_id": RUN}, tmp_path)

    assert reply["ok"] is True, reply
    assert _closed_in_head(tmp_path) == {b0, cp1}
    assert [plan for _, plan in _open_checkpoints(tmp_path, b0)] == [OTHER_PLAN]


def _hash(repo: Path, text: str) -> str:
    done = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=text, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()
