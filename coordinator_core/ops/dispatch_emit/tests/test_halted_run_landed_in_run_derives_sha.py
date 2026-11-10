"""dispatch.record_halted_run_landed ``in_run`` with no sha: the landing sha is derived from the declared paths."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-10-derive-20261110T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, body: str | None = None) -> str:
    (repo / name).write_text(body if body is not None else name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{name} {body}")
    return _git(repo, "rev-parse", "HEAD")


def _manifest(root: Path, base: str, paths: list) -> None:
    d = root / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"head_sha": base, "review_declared_paths": paths}), encoding="utf-8")


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_clean_run_names_the_newest_commit_that_touched_the_paths(repo):
    _commit(repo, "a", "v1")
    sweep = _commit(repo, "a", "v2")
    base = _commit(repo, "b")
    _manifest(repo, base, ["a"])
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert out["ok"] and out["sha"] == sweep and out["written"] is True, out
    assert sweep[:12] in out["close"]["detail"]


def test_merge_on_top_is_skipped_for_the_first_parent_landing(repo):
    _commit(repo, "a", "v1")
    landing = _commit(repo, "a", "v2")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "a", "side")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-X", "theirs", "-m", "merge", "side")
    merge = _git(repo, "rev-parse", "HEAD")
    _manifest(repo, merge, ["a"])
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert out["ok"] and out["sha"] == landing and out["sha"] != merge, out


def test_no_hit_within_the_bound_refuses_naming_the_run(repo, monkeypatch):
    _commit(repo, "a")
    _commit(repo, "b")
    _commit(repo, "c")
    base = _commit(repo, "d")
    _manifest(repo, base, ["a"])
    monkeypatch.setattr(op, "_DERIVE_WALK_DEPTH", 2)
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert not out["ok"] and RUN in out["error"] and "pass sha" in out["error"], out
    monkeypatch.setattr(op, "_DERIVE_WALK_DEPTH", 4)
    assert op._handler({"run_id": RUN, "in_run": True}, repo)["ok"]


def test_dirty_write_set_still_refuses_without_a_sha(repo):
    base = _commit(repo, "a", "v1")
    _commit(repo, "a", "v2")
    _manifest(repo, base, ["a"])
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert not out["ok"] and "not clean" in out["error"], out


def test_no_sha_without_in_run_still_refuses(repo):
    base = _commit(repo, "a")
    _manifest(repo, base, ["a"])
    out = op._handler({"run_id": RUN}, repo)
    assert not out["ok"] and "sha" in out["error"]
    assert not op._handler({"run_id": RUN, "in_run": False}, repo)["ok"]


def test_explicit_sha_is_not_overridden(repo):
    first = _commit(repo, "a", "v1")
    second = _commit(repo, "a", "v2")
    base = _commit(repo, "b")
    _manifest(repo, base, ["a"])
    out = op._handler({"run_id": RUN, "in_run": True, "sha": first}, repo)
    assert out["ok"] and out["sha"] == first != second


@pytest.mark.parametrize("body", ["{not json", json.dumps({"head_sha": "x"})])
def test_malformed_manifest_refuses_instead_of_raising(repo, body):
    _commit(repo, "a")
    d = repo / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(body, encoding="utf-8")
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert not out["ok"] and RUN in out["error"], out
