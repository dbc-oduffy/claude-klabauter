"""Tests for coordinator_core.ops.repo_byproduct_prune."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import repo_byproduct_policy as policy
from coordinator_core.ops import repo_byproduct_prune as prune

NOW = time.time()


def _age(path: Path, days: float) -> None:
    t = NOW - days * 86400
    paths = [path] + (list(path.rglob("*")) if path.is_dir() else [])
    for p in paths:
        os.utime(p, (t, t))


def _unit(root: Path, rel: str, days: float, nbytes: int = 1000, files: int = 1) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"f{i}.bin").write_bytes(b"x" * (nbytes // files))
    _age(d, days)
    return d


def _link(link: Path, target: Path) -> None:
    """Symlink, or a junction where Windows refuses unprivileged symlinks."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if made.returncode == 0:
            return
    pytest.skip("no link kind available")


def _make_repo(tmp_path: Path, tracked: "tuple[str, ...]" = ()) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    # The runner only substring-matches the index, so a fabricated one is enough.
    (root / ".git" / "index").write_bytes(b"DIRC\0" + b"\0".join(t.encode() for t in tracked))
    lines = sorted({c.gitignore_line for c in policy.CATEGORIES})
    (root / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _small_policy(monkeypatch, keep: int = 2, cap: int = 2500) -> None:
    monkeypatch.setattr(
        policy, "CATEGORIES",
        tuple(dataclasses.replace(c, keep_newest=keep, max_total_bytes=cap) for c in policy.CATEGORIES),
    )


class _Clock:
    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _stamp(root: Path) -> Path:
    return root / ".coordinator-local" / prune.STAMP_NAME


def _warp(root: Path, name: str, days: float, stage: str = "landed") -> Path:
    d = _unit(root, f"state/warp/{name}", days)
    (d / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    _age(d, days)
    return d


def test_end_to_end(tmp_path, monkeypatch):
    _small_policy(monkeypatch)
    root = _make_repo(tmp_path, tracked=(".coordinator-local/subagent-share/tracked-old/f0.bin", "docs/tracked.md"))
    tracked_md = root / "docs" / "tracked.md"
    tracked_md.parent.mkdir(parents=True)
    tracked_md.write_bytes(b"tracked bytes\n")
    sess = root / ".coordinator-local" / "coordinator-sessions" / "sess" / "pm_turns.jsonl"
    sess.parent.mkdir(parents=True)
    sess.write_bytes(b'{"turn": 1}\n')
    _age(root / ".coordinator-local" / "coordinator-sessions", 40)
    sess_before = sess.read_bytes()

    for cat in policy.CATEGORIES:
        if cat.name in ("scratch-top", "scratch-pytest-basetemp", "scratch-pytest-current", "warp-runs"):
            continue
        for name, days in (("a0", 0), ("a1", 1), ("a2", 2), ("a3", 40)):
            if cat.unit_glob.endswith(".log"):
                f = root / cat.relpath / cat.unit_glob.replace("*", name)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x" * 1000)
                _age(f, days)
            else:
                _unit(root, f"{cat.relpath}/{name}", days)
    _unit(root, ".coordinator-local/subagent-share/tracked-old", 40)

    for i, days in enumerate((0, 1, 2, 40)):
        _unit(root, f"scratch/s{i}/pytest-of-u", days)
        _age(root / "scratch" / f"s{i}", days)
    for name, days in (("w0", 0), ("w1", 1), ("w2", 2), ("done", 40)):
        _warp(root, name, days)
    _warp(root, "live", 40, stage="running")
    _unit(root, "scratch/warp/done", 40)
    _unit(root, "scratch/warp/live", 40)

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess spawned during run_prune")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    prune._deps()
    p0 = time.process_time()
    result = prune.run_prune(root, now=NOW)
    spent = time.process_time() - p0

    assert result.get("error") is None, result
    assert result["ran"] is True
    assert result["process_ms"] / 1000 < prune.BUDGET_SECONDS
    assert spent < prune.BUDGET_SECONDS * 4
    assert _stamp(root).exists()

    for cat in policy.CATEGORIES:
        if cat.name in ("scratch-top", "scratch-pytest-basetemp", "scratch-pytest-current", "warp-runs"):
            continue
        base = root / cat.relpath
        for name, expect in (("a0", True), ("a1", True), ("a2", False), ("a3", False)):
            p = base / (cat.unit_glob.replace("*", name) if cat.unit_glob.endswith(".log") else name)
            assert p.exists() is expect, (cat.name, name)
    assert (root / ".coordinator-local/subagent-share/tracked-old/f0.bin").exists()
    assert (root / "scratch/s0").exists() and (root / "scratch/s1").exists()
    assert not (root / "scratch/s2").exists() and not (root / "scratch/s3").exists()
    for name, expect in (("w0", True), ("w1", True), ("w2", False), ("done", False), ("live", True)):
        assert (root / "state/warp" / name).exists() is expect, name
    assert not (root / "scratch/warp/done").exists()
    assert (root / "scratch/warp/live").exists()
    assert tracked_md.read_bytes() == b"tracked bytes\n"
    assert sess.read_bytes() == sess_before
    assert not (root / ".coordinator-local" / prune.INFLIGHT_NAME).exists()


def test_quiet_repo_keeps_newest_when_everything_is_old(tmp_path, monkeypatch):
    _small_policy(monkeypatch)
    root = _make_repo(tmp_path)
    for i in range(5):
        _unit(root, f".coordinator-local/subagent-share/q{i}", 100 + i)
    assert prune.run_prune(root, now=NOW)["ran"] is True
    left = sorted(p.name for p in (root / ".coordinator-local/subagent-share").iterdir())
    assert left == ["q0", "q1"]


def test_no_index_keeps_everything(tmp_path, monkeypatch):
    _small_policy(monkeypatch)
    root = _make_repo(tmp_path)
    (root / ".git" / "index").unlink()
    _unit(root, ".coordinator-local/subagent-share/old", 100)
    result = prune.run_prune(root, now=NOW)
    assert (root / ".coordinator-local/subagent-share/old").exists()
    assert result["skipped"]["subagent-share"] == "no_index"


def test_category_without_gitignore_line_is_skipped(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    (root / ".gitignore").write_text("state/warp/\n", encoding="utf-8")
    _unit(root, "scratch/old", 100)
    _warp(root, "old", 100)
    result = prune.run_prune(root, now=NOW)
    assert (root / "scratch/old").exists()
    assert result["skipped"]["scratch-top"] == "not_ignored"
    assert not (root / "state/warp/old").exists()


def test_links_are_unlinked_never_descended_and_linked_root_is_skipped(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    (root / "scratch").mkdir()
    _link(root / "scratch" / "lnk", outside)
    result = prune.run_prune(root, now=NOW + 200 * 86400)
    assert not os.path.lexists(root / "scratch" / "lnk")
    assert (outside / "keep.txt").read_text() == "precious"
    assert result["ran"] is True


def test_linked_category_root_is_skipped(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    _unit(outside, "old", 100)
    _link(root / "scratch", outside)
    result = prune.run_prune(root, now=NOW + 200 * 86400)
    assert (outside / "old").exists()
    assert result["skipped"]["scratch-top"] == "link"


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    _unit(root, "scratch/old", 100)
    result = prune.run_prune(root, now=NOW, apply=False)
    assert result["ran"] is True and result["deleted"]["scratch-top"] == 1
    assert (root / "scratch/old").exists()
    assert not _stamp(root).exists()


def test_fresh_stamp_skips_and_force_overrides(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    _unit(root, "scratch/old", 100)
    _stamp(root).parent.mkdir(parents=True)
    _stamp(root).touch()
    assert prune.run_prune(root) == {"ran": False}
    assert (root / "scratch/old").exists()
    assert prune.run_prune(root, force=True)["ran"] is True
    assert not (root / "scratch/old").exists()


def test_lock_held_blocks_and_stale_lock_is_broken(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    _unit(root, "scratch/old", 100)
    lock = root / ".coordinator-local" / prune.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.touch()
    assert prune.run_prune(root) == {"ran": False}
    assert (root / "scratch/old").exists()
    stale = time.time() - prune.LOCK_STALE_SECONDS - 10
    os.utime(lock, (stale, stale))
    assert prune.run_prune(root)["ran"] is True
    assert not lock.exists()


def test_spawn_if_due(tmp_path):
    calls: list = []
    root = _make_repo(tmp_path)
    _stamp(root).parent.mkdir(parents=True)
    _stamp(root).touch()
    assert prune.spawn_if_due(str(root), spawn=lambda *a: calls.append(a) or True) is False
    assert calls == []
    stale = time.time() - prune.THROTTLE_SECONDS - 10
    os.utime(_stamp(root), (stale, stale))
    assert prune.spawn_if_due(str(root), spawn=lambda *a: calls.append(a) or True) is True
    assert calls == [(str(root), "coordinator_core/ops/repo_byproduct_prune.py", [])]
    assert prune.spawn_if_due(str(root), spawn=lambda *a: (_ for _ in ()).throw(RuntimeError())) is False


def test_budget_exhaustion_leaves_stamp_unwritten_and_runs_no_subprocess(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    for i in range(10):
        _unit(root, f"scratch/old{i}", 100)
    monkeypatch.setattr(prune, "_clock", _Clock(0.01))

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    result = prune.run_prune(root, now=NOW, budget=0.05)
    assert result == {"ran": False, "budget_exhausted": True}
    assert not _stamp(root).exists()


def test_oversized_unit_is_removed_across_budget_exhausted_runs(tmp_path, monkeypatch):
    big_cat = policy.Category("big", "scratch", "*", "transient", 7, 1, 100, "scratch/")
    monkeypatch.setattr(policy, "CATEGORIES", (big_cat,))
    monkeypatch.setattr(prune, "_CHECK_EVERY", 1)
    root = _make_repo(tmp_path)
    _unit(root, "scratch/new", 0, nbytes=10)
    big = root / "scratch" / "big"
    for d in range(3):
        (big / f"d{d}").mkdir(parents=True)
        for i in range(100):
            (big / f"d{d}" / f"z{i}").write_bytes(b"")
    _age(big, 1)
    monkeypatch.setattr(prune, "_clock", _Clock(0.001))

    def _count() -> int:
        return sum(1 for _ in big.rglob("*")) if big.exists() else 0

    counts = [_count()]
    for _ in range(12):
        result = prune.run_prune(root, now=NOW, budget=0.2)
        counts.append(_count())
        if result.get("ran"):
            break
        assert result == {"ran": False, "budget_exhausted": True}
        assert counts[-1] < counts[-2], counts
    assert result["ran"] is True, counts
    assert not big.exists()
    assert (root / "scratch/new/f0.bin").exists()
    assert _stamp(root).exists()
    assert not (root / ".coordinator-local" / prune.INFLIGHT_NAME).exists()


def test_main_runs_in_cwd_repo(tmp_path, monkeypatch):
    _small_policy(monkeypatch, keep=0)
    root = _make_repo(tmp_path)
    _unit(root, "scratch/old", 100)
    monkeypatch.chdir(root)
    assert prune.main(["repo_byproduct_prune.py", "--force"]) == 0
    assert not (root / "scratch/old").exists()


def test_module_has_no_shell_out():
    src = Path(prune.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src and "os.system" not in src


def test_pytest_basetemp_prunes_stale_runs_inside_newest_dir(tmp_path):
    root = _make_repo(tmp_path)
    base = root / "scratch" / "bt" / "pytest-of-u"
    for name, days in (("pytest-0", 40), ("pytest-1", 30), ("pytest-2", 0)):
        _unit(root, f"scratch/bt/pytest-of-u/{name}", days)
    (root / "scratch" / "bt" / "keep.txt").write_bytes(b"x")

    result = prune.run_prune(root, now=NOW, force=True)

    assert result.get("error") is None, result
    assert (base / "pytest-2").exists()
    assert not (base / "pytest-0").exists() and not (base / "pytest-1").exists()
    assert (root / "scratch" / "bt" / "keep.txt").exists()


def test_stale_dangling_pytest_current_link_beyond_newest_is_removed(tmp_path):
    root = _make_repo(tmp_path)
    t = NOW - 40 * 86400
    links = []
    for name in ("old", "new"):
        base = root / "scratch" / name / "pytest-of-u"
        base.mkdir(parents=True)
        gone = tmp_path / f"gone-{name}"
        gone.mkdir()
        _link(base / "current", gone)
        gone.rmdir()
        links.append(base / "current")
    try:
        os.utime(links[0], (t, t), follow_symlinks=False)
    except (OSError, NotImplementedError):
        pytest.skip("cannot age a link")
    assert os.stat(links[0], follow_symlinks=False).st_mtime < NOW - 30 * 86400

    result = prune.run_prune(root, now=NOW, force=True)

    assert result.get("error") is None, result
    assert not os.path.lexists(links[0])
    assert os.path.lexists(links[1])
