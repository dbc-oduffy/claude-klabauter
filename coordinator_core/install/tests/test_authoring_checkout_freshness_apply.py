"""Apply-path tests for authoring-checkout freshness over tmp_path bare-origin + clone fixtures."""

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import authoring_checkout_freshness as acf
from coordinator_core.install import authoring_checkout_freshness_apply as apply
from coordinator_core.warm import live_runs

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _g(cwd, *args):
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env={**os.environ, **_ENV}
    )
    return out.stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _g(repo, "add", rel)
    _g(repo, "commit", "-q", "-m", f"edit {rel}")
    return _g(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_live_runs(monkeypatch):
    monkeypatch.setattr(live_runs, "find_live_runs", lambda repos=None, **kw: [])


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    _g(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "a.txt", "a1\n")
    _commit(seed, "b.txt", "b1\n")
    _g(seed, "push", "-q", "-u", "origin", "main")
    box = tmp_path / "box"
    _g(tmp_path, "clone", "-q", str(origin), str(box))
    _g(box, "checkout", "-q", "-b", "work/day")
    return {"seed": seed, "box": box}


def _advance_origin(seed: Path, rel: str, text: str) -> str:
    sha = _commit(seed, rel, text)
    _g(seed, "push", "-q", "origin", "main")
    return sha


def _sync_ref(box: Path) -> None:
    _g(box, "fetch", "-q", "origin", "main")


def _one(box: Path, **kw) -> dict:
    return apply.apply_all({"repos.box": str(box)}, **kw)["rows"][0]


def test_behind_clean_fast_forwards(world):
    new = _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    row = _one(world["box"])
    assert (row["action"], row["behind"], row["ahead"], row["moved"]) == ("fast_forward", 1, 0, True)
    assert _g(world["box"], "rev-parse", "HEAD") == new
    assert row["branch"] == "work/day"


def test_current_checkout_is_not_moved_and_silent(world):
    result = apply.apply_all({"repos.box": str(world["box"])})
    assert result["rows"][0]["action"] == "current"
    assert result["advisories"] == []


def test_diverged_clean_reports_merge_clean_and_remediation(world):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _commit(world["box"], "c.txt", "c\n")
    _sync_ref(world["box"])
    head = _g(world["box"], "rev-parse", "HEAD")
    result = apply.apply_all({"repos.box": str(world["box"])})
    row = result["rows"][0]
    assert row["action"] == "diverged" and (row["ahead"], row["behind"]) == (1, 1)
    assert row["merge_clean"] is True and row["conflict_paths"] == []
    assert row["remediation"] == "git merge origin/main"
    assert "git merge origin/main" in result["advisories"][0]
    assert _g(world["box"], "rev-parse", "HEAD") == head


def test_diverged_conflicting_names_paths(world):
    _advance_origin(world["seed"], "a.txt", "theirs\n")
    _commit(world["box"], "a.txt", "mine\n")
    _sync_ref(world["box"])
    row = _one(world["box"])
    assert row["action"] == "diverged"
    assert row["merge_clean"] is False
    assert row["conflict_paths"] == ["a.txt"]
    assert "a.txt" in row["reason"]


def test_merge_tree_failure_is_null_not_raise(world, monkeypatch):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _commit(world["box"], "c.txt", "c\n")
    _sync_ref(world["box"])
    real = apply._git

    def fake(checkout, *args, remote=False):
        if args and args[0] == "merge-tree":
            return type("R", (), {"returncode": 129, "stdout": "", "stderr": "unknown option write-tree"})()
        return real(checkout, *args, remote=remote)

    monkeypatch.setattr(apply, "_git", fake)
    row = _one(world["box"])
    assert row["action"] == "diverged" and row["merge_clean"] is None
    assert "unknown option" in row["reason"]


def test_dirty_overlapping_path_blocks(world):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    (world["box"] / "a.txt").write_text("local edit\n", encoding="utf-8")
    head = _g(world["box"], "rev-parse", "HEAD")
    result = apply.apply_all({"repos.box": str(world["box"])})
    row = result["rows"][0]
    assert row["action"] == "blocked_dirty_overlap" and row["moved"] is False
    assert "a.txt" in row["reason"] and "a.txt" in result["advisories"][0]
    assert _g(world["box"], "rev-parse", "HEAD") == head


def test_dirty_non_overlapping_path_fast_forwards(world):
    new = _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    (world["box"] / "b.txt").write_text("local edit\n", encoding="utf-8")
    row = _one(world["box"])
    assert row["action"] == "fast_forward" and row["moved"] is True
    assert _g(world["box"], "rev-parse", "HEAD") == new
    assert (world["box"] / "b.txt").read_text(encoding="utf-8") == "local edit\n"


def test_live_run_defers_with_run_id(world, monkeypatch):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    seen = {}

    def fake(repos=None, **kw):
        seen.update(repos)
        return [live_runs.LiveRun("repos.box", "run-77", "stage", "now")]

    monkeypatch.setattr(live_runs, "find_live_runs", fake)
    head = _g(world["box"], "rev-parse", "HEAD")
    row = _one(world["box"])
    assert row["action"] == "deferred_live_run" and "run-77" in row["reason"]
    assert seen == {"repos.box": str(world["box"])}
    assert _g(world["box"], "rev-parse", "HEAD") == head


def test_unreadable_run_registry_defers(world, monkeypatch):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])

    def boom(repos=None, **kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(live_runs, "find_live_runs", boom)
    row = _one(world["box"])
    assert row["action"] == "deferred_live_run" and row["moved"] is False


def test_fetch_failure_recorded_and_ff_proceeds(world, tmp_path):
    new = _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    _advance_origin(world["seed"], "b.txt", "b2\n")
    _g(world["box"], "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    row = _one(world["box"], do_fetch=True)
    assert row["fetch_error"].startswith("fetch failed")
    assert row["action"] == "fast_forward" and row["moved"] is True
    assert _g(world["box"], "rev-parse", "HEAD") == new


def test_default_does_not_fetch(world):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    row = _one(world["box"])
    assert row["action"] == "current" and row["behind"] == 0
    assert "fetch_error" not in row


def test_fetch_success_picks_up_new_main(world):
    new = _advance_origin(world["seed"], "a.txt", "a2\n")
    row = _one(world["box"], do_fetch=True)
    assert row["moved"] is True and "fetch_error" not in row
    assert _g(world["box"], "rev-parse", "HEAD") == new


def test_detached_head_is_advisory_never_moved(world):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    _g(world["box"], "checkout", "-q", "--detach")
    head = _g(world["box"], "rev-parse", "HEAD")
    result = apply.apply_all({"repos.box": str(world["box"])})
    assert result["rows"][0]["action"] == "detached"
    assert "detached HEAD" in result["advisories"][0]
    assert _g(world["box"], "rev-parse", "HEAD") == head


def test_missing_origin_main_reports_no_main(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "work/day")
    _commit(repo, "a.txt", "a\n")
    row = apply.apply_all({"repos.solo": str(repo)})["rows"][0]
    assert row["action"] == "no_main" and row["moved"] is False


def test_unreadable_checkout_is_a_row(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = apply.apply_all({"repos.plain": str(plain)})
    assert result["rows"][0]["action"] == "unreadable"
    assert "repos.plain" in result["advisories"][0]


def test_ff_refusal_is_moved_false_with_stderr(world, monkeypatch):
    _advance_origin(world["seed"], "a.txt", "a2\n")
    _sync_ref(world["box"])
    real = apply._git

    def fake(checkout, *args, remote=False):
        if args and args[0] == "merge":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "fatal: Not possible"})()
        return real(checkout, *args, remote=remote)

    monkeypatch.setattr(apply, "_git", fake)
    result = apply.apply_all({"repos.box": str(world["box"])})
    row = result["rows"][0]
    assert row["moved"] is False and "ff-only refused: fatal: Not possible" in row["reason"]
    assert result["advisories"]


def test_klabauter_mirror_excluded(world):
    result = apply.apply_all({"repos.box": str(world["box"]), acf.KLABAUTER_REGISTRY_KEY: str(world["box"])})
    assert [r["key"] for r in result["rows"]] == ["repos.box"]


def test_spawn_count_current_clean_is_two(world, monkeypatch):
    real = apply._git
    calls = []

    def spy(checkout, *args, remote=False):
        calls.append(args[0])
        return real(checkout, *args, remote=remote)

    monkeypatch.setattr(apply, "_git", spy)
    _one(world["box"])
    assert calls == ["status", "rev-list"]
