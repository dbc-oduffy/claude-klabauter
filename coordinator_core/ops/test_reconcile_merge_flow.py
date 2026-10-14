"""End-to-end flow of the script-fired reconcile merge: the Step 0 start op
holds a conflicted merge open, plain file writes stand in for the file-only
resolver, and the conclude op refuses while a marker remains then commits.
Also measures each op's own process time and spawn count, git merge child apart.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import reconcile_manifest as rm
from coordinator_core.ops import reconcile_merge_conclude as conclude
from coordinator_core.ops import workday_start_step0_reconcile as step0
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BRANCH = "work/testmachine/2026-01-01"
OWN_PROCESS_BAR_MS = 500.0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _identify(root: Path) -> None:
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _conflicted_clone(tmp_path: Path, files: tuple[str, ...]) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    _identify(clone)
    _git(clone, "checkout", "-q", "-B", "main")
    for name in files:
        (clone / name).write_text("base\n")
    _commit_all(clone, "initial")
    _git(clone, "push", "-q", "-u", "origin", "main")

    _git(clone, "checkout", "-q", "-b", BRANCH)
    for name in files:
        (clone / name).write_text("local edit\n")
    _commit_all(clone, "local conflicting edit")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(other)],
        check=True,
        **no_console_passthrough_kwargs(),
    )
    _identify(other)
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    for name in files:
        (other / name).write_text("upstream edit\n")
    (other / "upstream-only.txt").write_text("new upstream file\n")
    _commit_all(other, "upstream conflicting edit")
    _git(other, "push", "-q", "origin", "main")
    return clone, _git(other, "rev-parse", "HEAD").stdout.strip()


class _SpawnMeter:
    """Wraps subprocess.run, recording per-child wall time by git verb."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.children: list[tuple[str, float]] = []
        self._real = subprocess.run
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, cmd, *args, **kwargs):
        args_ = list(cmd[1:])
        if args_[:1] == ["-C"]:
            args_ = args_[2:]
        verb = next((a for a in args_ if not a.startswith("-")), "git")
        started = time.perf_counter()
        try:
            return self._real(cmd, *args, **kwargs)
        finally:
            self.children.append((verb, (time.perf_counter() - started) * 1000.0))

    def measure(self, op, root: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        self.children.clear()
        monkeypatch.chdir(root)
        cpu_before = time.process_time()
        rc = op([])
        own_ms = (time.process_time() - cpu_before) * 1000.0
        merge_ms = sum(ms for verb, ms in self.children if verb == "merge")
        return {
            "rc": rc,
            "own_process_ms": round(own_ms, 1),
            "spawns": len(self.children),
            "merge_child_ms": round(merge_ms, 1),
            "child_wall_ms": round(sum(ms for _, ms in self.children), 1),
        }


def test_conflict_to_concluded_merge_flow(tmp_path, capsys, monkeypatch):
    files = ("a.txt", "b.txt")
    clone, origin_sha = _conflicted_clone(tmp_path, files)
    meter = _SpawnMeter(monkeypatch)

    start = meter.measure(step0.main, clone, monkeypatch)
    captured = capsys.readouterr()
    assert start["rc"] == 3
    assert captured.out.strip().splitlines()[-1] == f"RECONCILE-CONFLICT-OPEN branch={BRANCH}"
    merge_head_file = clone / ".git" / "MERGE_HEAD"
    assert merge_head_file.exists()
    manifest = rm.read(clone)
    assert manifest is not None
    assert manifest.paths == files
    assert manifest.merge_head == merge_head_file.read_text().strip() == origin_sha

    (clone / "a.txt").write_text("resolved a\n")
    (clone / "b.txt").write_text("<<<<<<< ours\nlocal edit\n=======\nupstream edit\n>>>>>>> theirs\n")

    refused = meter.measure(conclude.main, clone, monkeypatch)
    err = capsys.readouterr().err
    assert refused["rc"] != 0
    assert rm.REFUSAL_MARKERS in err and "b.txt" in err
    assert merge_head_file.exists()
    assert rm.read(clone) is not None
    assert _git(clone, "rev-list", "--count", "HEAD").stdout.strip() == "2"

    (clone / "b.txt").write_text("resolved b\n")
    done = meter.measure(conclude.main, clone, monkeypatch)
    out = capsys.readouterr().out
    assert done["rc"] == 0
    assert out.strip() == f"{rm.OUTCOME_CONCLUDED} branch={BRANCH}"

    parents = _git(clone, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:]
    assert len(parents) == 2 and parents[1] == origin_sha
    assert not merge_head_file.exists()
    assert rm.read(clone) is None
    assert not rm.manifest_path(clone).exists()
    assert _git(clone, "diff", "--name-only", "--diff-filter=U").stdout.strip() == ""
    assert (clone / "a.txt").read_text() == "resolved a\n"

    for label, row in (("start", start), ("conclude_refused", refused), ("conclude", done)):
        assert row["own_process_ms"] < OWN_PROCESS_BAR_MS, (label, row)
    print("RECONCILE-FLOW-MEASURE " + json.dumps(
        {"start": start, "conclude_refused": refused, "conclude": done}, sort_keys=True
    ))
