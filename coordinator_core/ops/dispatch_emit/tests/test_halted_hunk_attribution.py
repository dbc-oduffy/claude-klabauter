"""halted_hunk_attribution.reader: one case per attribution rule, plus the zero-spawn and CRLF pins."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_state
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import own_checkpoint_paths
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HALTED = "ask-20261024T000000-aaaaaa"
LANDING = "ask-20261024T020000-bbbbbb"
PATH = "src/target.py"
HALT_AT = "2026-10-24T01:00:00Z"
START_AT = "2026-10-24T02:00:00Z"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, check=True, **no_console_creationflags()
    )


def _bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _sha(text: str) -> str:
    return blob_sha(_bytes(text))


@pytest.fixture
def root(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    return repo


def _tree(root: Path, text: str) -> None:
    (root / PATH).write_bytes(_bytes(text))


def _stage_index(root: Path, text: str) -> None:
    _tree(root, text)
    _git(root, "add", PATH)


def _run(root: Path, run_id: str, *, baseline=None, stage=None, started=None, head_sha=None) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}
    if baseline is not None:
        manifest["write_baseline"] = baseline
    if head_sha:
        manifest["head_sha"] = head_sha
    (rdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    record = {"stage": stage or "staged", "started_at": started or START_AT, "last_activity_at": started or START_AT}
    (rdir / "run-stage.json").write_text(json.dumps(record), encoding="utf-8")


def _halted_run(root: Path, bh: str | None, *, halt_at: str = HALT_AT, run_id: str = HALTED) -> None:
    _run(root, run_id, baseline={PATH: bh}, stage="halted", started="2026-10-24T00:00:00Z")
    rdir = root / "state" / "warp" / run_id
    (rdir / "run-stage.json").write_text(
        json.dumps({"stage": "halted", "started_at": "2026-10-24T00:00:00Z", "last_activity_at": halt_at}),
        encoding="utf-8",
    )
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")


def _landing(root: Path, bl: str | None, **kw) -> None:
    _run(root, LANDING, baseline={PATH: bl}, started=START_AT, **kw)


def _held(root: Path, run_id: str = HALTED, landing: str | None = LANDING, **kw) -> set:
    return hha.reader(root, landing, **kw)(run_id, [PATH])


def test_rule1_path_missing_from_the_halted_baseline_holds(root):
    _tree(root, "tree\n")
    _run(root, HALTED, baseline={"other.py": None}, stage="halted")
    _landing(root, _sha("x\n"))
    assert _held(root) == {PATH}


def test_rule1_unreadable_manifest_holds(root):
    _tree(root, "tree\n")
    rdir = root / "state" / "warp" / HALTED
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text("{not json", encoding="utf-8")
    assert _held(root) == {PATH}


def test_rule1_manifest_without_write_baseline_holds(root):
    _tree(root, "tree\n")
    _run(root, HALTED, baseline=None, stage="halted")
    assert _held(root) == {PATH}


def test_rule2_tree_still_equals_the_halted_baseline_releases_even_with_no_landing_run(root):
    _tree(root, "same\n")
    _halted_run(root, _sha("same\n"))
    assert _held(root, landing=None) == set()


def test_rule2_absent_tree_and_none_baseline_releases(root):
    _halted_run(root, None)
    assert _held(root, landing=None) == set()


def test_rule3_no_landing_run_holds_when_the_tree_differs(root):
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"))
    assert _held(root, landing=None) == {PATH}


def test_rule3_path_missing_from_the_landing_baseline_holds(root):
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"))
    _run(root, LANDING, baseline={"other.py": None})
    assert _held(root) == {PATH}


def test_rule3_halt_after_the_landing_start_holds(root):
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"), halt_at="2026-10-24T03:00:00Z")
    _landing(root, _sha("old\n"))
    assert _held(root) == {PATH}


def test_rule3_halt_epoch_falls_back_to_the_halted_json_mtime_and_a_later_mtime_holds(root):
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"))
    rdir = root / "state" / "warp" / HALTED
    (rdir / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    _landing(root, _sha("old\n"))
    os.utime(rdir / "halted.json", (1893456000, 1893456000))  # 2030
    assert _held(root) == {PATH}
    os.utime(rdir / "halted.json", (1577836800, 1577836800))  # 2020
    assert _held(root) == set()


def test_rule3_unknown_landing_start_holds(root):
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"))
    _run(root, "plain-run", baseline={PATH: _sha("old\n")})
    (root / "state" / "warp" / "plain-run" / "run-stage.json").unlink()
    assert _held(root, landing="plain-run") == {PATH}


def test_rule4_landing_baseline_equal_to_the_halted_baseline_releases(root):
    _tree(root, "landing wrote this\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("old\n"))
    assert _held(root) == set()


def test_rule5_index_equal_to_the_landing_baseline_releases(root):
    _stage_index(root, "committed between stages\n")
    _tree(root, "landing wrote this\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("committed between stages\n"))
    assert _held(root) == set()


def test_rule6_checkpoint_landed_path_releases_and_otherwise_holds(root, monkeypatch):
    _stage_index(root, "index\n")
    _tree(root, "tree\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("landing baseline\n"), head_sha="a" * 40)
    calls: list = []

    def fake(root_, plan, base, paths):
        calls.append((plan, base, list(paths)))
        return frozenset({PATH})

    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", fake)
    assert _held(root, landing_plan="docs/plans/p.md") == set()
    assert calls == [("docs/plans/p.md", "a" * 40, [PATH])]
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *a: frozenset())
    assert _held(root, landing_plan="docs/plans/p.md") == {PATH}
    assert _held(root) == {PATH}


def test_rule7_foreign_dirt_present_at_the_landing_stage_holds(root):
    _stage_index(root, "committed\n")
    _tree(root, "halted run left this\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("halted run left this\n"))
    assert _held(root) == {PATH}


def test_reader_answers_every_case_with_run_git_and_popen_forbidden(root, monkeypatch):
    _stage_index(root, "committed\n")
    _tree(root, "landing wrote this\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("committed\n"))

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr("coordinator_core.git.run.run_git", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _held(root) == set()
    assert _held(root, landing=None) == {PATH}
    assert _held(root, run_id="no-such-run") == {PATH}


def test_the_index_is_parsed_once_across_two_halted_runs_on_one_reader(root, monkeypatch):
    _stage_index(root, "committed\n")
    _tree(root, "landing wrote this\n")
    other = "ask-20261024T000500-cccccc"
    _halted_run(root, _sha("old\n"))
    _halted_run(root, _sha("older\n"), run_id=other)
    _landing(root, _sha("committed\n"))
    real = git_state._parse_index_bytes
    parses: list = []
    monkeypatch.setattr(git_state, "_parse_index_bytes", lambda *a, **k: parses.append(1) or real(*a, **k))
    attr = hha.reader(root, LANDING)
    assert attr(HALTED, [PATH]) == set()
    assert attr(other, [PATH]) == set()
    assert len(parses) == 1


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_index_blob_equal_to_the_landing_baseline_releases_for_lf_and_crlf_alike(root, eol):
    _git(root, "config", "core.autocrlf", "true")
    _stage_index(root, "line one\nline two\n")
    _tree(root, f"line one{eol}line two{eol}")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha(f"line one{eol}line two{eol}"))
    assert _held(root) == set()


def test_crlf_converted_landing_baseline_never_releases_through_the_index(root):
    _git(root, "config", "core.autocrlf", "true")
    _stage_index(root, "line one\nline two\n")
    _tree(root, "line one\r\nline two\r\nplus\r\n")
    _halted_run(root, _sha("old\n"))
    _landing(root, _sha("line one\r\nline two\r\n"))
    assert _held(root) == {PATH}
