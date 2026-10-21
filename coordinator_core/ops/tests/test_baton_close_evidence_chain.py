"""Tests for baton_close_evidence_chain: successor edges and the batched merged-branch read."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.baton_close_evidence_chain as chain
from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_spawns = pytest.mark.spawns_process
_cadence = pytest.mark.cadence


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path, *, main: str | None = "main") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", main or "scratch-root")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _baton(root: Path, rel: str, **fields: str) -> dict:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    path.write_text(f"---\n{body}---\n\nbody\n", encoding="utf-8")
    return dict(fields)


def _archived(root: Path, name: str, **fields: str) -> None:
    _baton(root, f"archive/handoffs/2026-10/{name}", **fields)


def _kinds(result: dict, rel: str) -> list[str]:
    return [e["kind"] for e in result.get(rel, [])]


def test_claimed_baton_with_archived_successor(tmp_path: Path) -> None:
    rel = "state/handoffs/a.md"
    batons = {rel: _baton(tmp_path, rel, status="claimed", deployment_state="in_flight", predecessor="none")}
    _archived(tmp_path, "b.md", predecessor="state/handoffs/a.md", deployment_state="shipped")
    out = chain.successor_evidence(tmp_path, batons)
    assert _kinds(out, rel) == ["SUCCESSOR_CLAIMED"]
    ev = out[rel][0]
    assert (ev["reads"], ev["strength"], ev["proposes"]) == ("terminal", "strong", "continued")
    assert ev["source"] == "archive/handoffs/2026-10/b.md"


def test_unclaimed_baton_with_terminal_live_successor(tmp_path: Path) -> None:
    a, b = "state/handoffs/a.md", "state/handoffs/b.md"
    batons = {
        a: _baton(tmp_path, a, deployment_state="awaiting_gate", predecessor="none"),
        b: _baton(tmp_path, b, deployment_state="shipped", predecessor="state/handoffs/a.md"),
    }
    out = chain.successor_evidence(tmp_path, batons)
    assert _kinds(out, a) == ["SUCCESSOR_UNCLAIMED"]
    ev = out[a][0]
    assert (ev["reads"], ev["strength"], ev["proposes"]) == ("terminal", "weak", "abandoned")
    assert b not in out


def test_live_successor(tmp_path: Path) -> None:
    a, b = "state/handoffs/a.md", "state/handoffs/b.md"
    batons = {
        a: _baton(tmp_path, a, deployment_state="in_flight", predecessor="none"),
        b: _baton(tmp_path, b, deployment_state="in_flight", predecessor="state/handoffs/a.md"),
    }
    out = chain.successor_evidence(tmp_path, batons)
    assert _kinds(out, a) == ["SUCCESSOR_LIVE"]
    assert out[a][0]["reads"] == "live" and out[a][0]["proposes"] is None


def test_archived_successor_spelled_with_backslash_and_quotes(tmp_path: Path) -> None:
    rel = "state/handoffs/a.md"
    batons = {rel: _baton(tmp_path, rel, status="claimed", deployment_state="in_flight")}
    _archived(tmp_path, "b.md", predecessor='"state\\handoffs\\a.md"  # old', deployment_state="continued")
    assert _kinds(chain.successor_evidence(tmp_path, batons), rel) == ["SUCCESSOR_CLAIMED"]


def test_archived_successor_matches_by_basename(tmp_path: Path) -> None:
    rel = "state/handoffs/sub/a.md"
    batons = {rel: _baton(tmp_path, rel, status="claimed", deployment_state="in_flight")}
    _archived(tmp_path, "b.md", predecessor="state/handoffs/a.md", deployment_state="shipped")
    assert _kinds(chain.successor_evidence(tmp_path, batons), rel) == ["SUCCESSOR_CLAIMED"]


def test_no_predecessor_edge_gives_nothing(tmp_path: Path) -> None:
    rel = "state/handoffs/a.md"
    batons = {rel: _baton(tmp_path, rel, deployment_state="in_flight", predecessor="none")}
    _archived(tmp_path, "b.md", predecessor="none", deployment_state="shipped")
    _archived(tmp_path, "c.md", predecessor="state/handoffs/other.md", deployment_state="shipped")
    assert chain.successor_evidence(tmp_path, batons) == {}


def test_missing_archive_dir_is_not_an_error(tmp_path: Path) -> None:
    rel = "state/handoffs/a.md"
    batons = {rel: _baton(tmp_path, rel, deployment_state="in_flight")}
    assert chain.successor_evidence(tmp_path, batons) == {}


def test_header_read_ignores_body_predecessor(tmp_path: Path) -> None:
    rel = "state/handoffs/a.md"
    batons = {rel: _baton(tmp_path, rel, status="claimed", deployment_state="in_flight")}
    path = tmp_path / "archive/handoffs/2026-10/b.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: x\n---\npredecessor: state/handoffs/a.md\n", encoding="utf-8")
    assert chain.successor_evidence(tmp_path, batons) == {}


@_spawns
@_cadence
def test_no_main_ref_is_skipped_with_zero_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path, main=None)

    def _boom(*_a, **_k):
        raise AssertionError("run_git must not be called without a main ref")

    monkeypatch.setattr(chain, "run_git", _boom)
    assert chain.merged_branches(tmp_path) == (None, "skipped: no main ref")


def test_not_a_repo_is_skipped(tmp_path: Path) -> None:
    assert chain.merged_branches(tmp_path) == (None, "skipped: no main ref")


@_spawns
@_cadence
def test_merged_branches_one_spawn_and_prefix_strip(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "branch", "feature/done")
    _git(tmp_path, "checkout", "-b", "feature/ahead")
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "x.txt")
    _git(tmp_path, "commit", "-m", "ahead")
    _git(tmp_path, "checkout", "main")
    before = spawn_counter.spawn_count()
    merged, status = chain.merged_branches(tmp_path)
    assert spawn_counter.spawn_count() - before == 1
    assert status == "ran"
    assert merged is not None
    assert {"main", "feature/done"} <= merged
    assert "feature/ahead" not in merged


@_spawns
@_cadence
def test_origin_prefix_stripped_and_bare_origin_dropped(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/feature/remote-done", head)
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head)
    _git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    merged, status = chain.merged_branches(tmp_path)
    assert status == "ran"
    assert merged is not None
    assert "feature/remote-done" in merged and "origin/feature/remote-done" in merged
    assert "origin" not in merged


@_spawns
@_cadence
def test_git_failure_is_skipped_with_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)

    class _Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad object\nsecond line\n"

    monkeypatch.setattr(chain, "run_git", lambda *a, **k: _Result())
    assert chain.merged_branches(tmp_path) == (None, "skipped: fatal: bad object")


def test_merged_branch_evidence_in_flight_only() -> None:
    merged = frozenset({"feature/done", "candidate", "main"})
    batons = {
        "state/handoffs/a.md": {"branch": '"feature/done"', "deployment_state": "in_flight"},
        "state/handoffs/b.md": {"branch": "feature/done", "deployment_state": "ready_to_fire"},
        "state/handoffs/c.md": {"branch": "candidate", "deployment_state": "in_flight"},
        "state/handoffs/d.md": {"branch": "feature/other", "deployment_state": "in_flight"},
        "state/handoffs/e.md": {"deployment_state": "in_flight"},
    }
    out = chain.merged_branch_evidence(batons, merged)
    assert list(out) == ["state/handoffs/a.md"]
    ev = out["state/handoffs/a.md"][0]
    assert (ev["kind"], ev["reads"], ev["strength"], ev["proposes"]) == (
        "MERGED_BRANCH", "terminal", "weak", "shipped",
    )


def test_merged_branch_evidence_skipped_leg_gives_nothing() -> None:
    batons = {"state/handoffs/a.md": {"branch": "x", "deployment_state": "in_flight"}}
    assert chain.merged_branch_evidence(batons, None) == {}


def test_kind_literals_match_pinned_shapes() -> None:
    from coordinator_core.ops import baton_close_predicate as pred

    sample = chain._evidence("X", "terminal", "weak", None, "s", "d")
    assert set(sample) == {"kind", "reads", "strength", "proposes", "source", "detail"}
    for kind in ("SUCCESSOR_CLAIMED", "SUCCESSOR_UNCLAIMED", "SUCCESSOR_LIVE", "MERGED_BRANCH"):
        assert kind in pred.KIND_SHAPES
