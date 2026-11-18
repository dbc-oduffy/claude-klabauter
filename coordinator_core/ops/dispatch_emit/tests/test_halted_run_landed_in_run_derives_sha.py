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


def test_clean_run_names_its_own_base_never_an_older_commit(repo):
    """G-EM: the matched sha is the run's own base. A clean write set is unchanged since that base, so
    an older commit touching the paths (5585843534 for ask-20261114T133823-7a3cdc) is not this run's."""
    _commit(repo, "a", "v1")
    _commit(repo, "a", "v2")
    base = _commit(repo, "b")
    _manifest(repo, base, ["a"])
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert out["ok"] and out["sha"] == base and out["written"] is True, out
    assert base[:12] in out["close"]["detail"]


def test_a_dirty_row_write_outside_the_review_set_refuses(repo):
    """The 1a false close: evidence.md sat in a row's `writes` but not in `review_declared_paths`."""
    base = _commit(repo, "a", "v1")
    _commit(repo, "evidence.md", "v1")
    d = repo / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    doc = {"head_sha": base, "review_declared_paths": ["a"], "rows": [{"id": "R1", "writes": ["a", "evidence.md"]}]}
    (d / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    (repo / "evidence.md").write_text("rewritten by the leg", encoding="utf-8")
    out = op._handler({"run_id": RUN, "in_run": True}, repo)
    assert not out["ok"] and "not clean" in out["error"] and "evidence.md" in out["error"], out


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
