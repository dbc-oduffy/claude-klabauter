"""The submit hook's three `state/warp` lookups find their runs by key, so their directory work does not
grow with the number of run dirs `state/warp` retains."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import live_run_index, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

SIZING = "state/sizings/2026-11-13-scan-scaling.yaml"
OTHER = "state/sizings/2026-10-01-other.yaml"
SESSION = "sess-scan-scaling"
MINTED_RUN = "warp-2026-11-13-scan-scaling-20261113T010101-aaaaaa"
TOKEN_RUN = "warp-2026-11-13-scan-scaling-20261113T020202-bbbbbb"
INDEXED_RUN = "warp-2026-11-13-scan-scaling-20261113T030303-cccccc"
SIZES = (50, 5000)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _retained(root: Path, count: int) -> None:
    """`count` landed run dirs of another sizing: retained history the lookups must not walk."""
    base = root / RECORD_DIR_ROOT
    base.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        d = base / f"warp-2026-10-01-other-20261001T{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}"
        d.mkdir()
        (d / RUN_STAGE_FILE).write_text('{"stage": "landed"}', encoding="utf-8")
        (d / "run.json").write_text(json.dumps({"sizing_rel": OTHER}), encoding="utf-8")


def _stage(root: Path, run_id: str, stage: str) -> None:
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(json.dumps({"stage": stage}), encoding="utf-8")
    (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")


def _mint_token(root: Path, run_id: str) -> None:
    run_token.mint(root, run_id, f"scratch/warp/{run_id}.workflow.mjs", session_id=SESSION, sizing_rel=SIZING)


def _forbid_listing(monkeypatch, base: Path) -> list:
    """Records every listing of `base` through scandir, listdir or iterdir; the caller asserts it stayed empty."""
    seen: list = []
    real_scandir, real_listdir, real_iterdir = os.scandir, os.listdir, Path.iterdir

    def named(path) -> bool:
        return isinstance(path, (str, os.PathLike)) and Path(path) == base

    def scandir(path=".", *a, **k):
        if named(path):
            seen.append("scandir")
        return real_scandir(path, *a, **k)

    def listdir(path=".", *a, **k):
        if named(path):
            seen.append("listdir")
        return real_listdir(path, *a, **k)

    def iterdir(self):
        if self == base:
            seen.append("iterdir")
        return real_iterdir(self)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    return seen


@pytest.mark.parametrize("count", SIZES)
def test_minted_since_start_names_the_sessions_run_without_listing_state_warp(repo, monkeypatch, count):
    _retained(repo, count)
    _stage(repo, MINTED_RUN, "minted")
    _mint_token(repo, MINTED_RUN)
    peer = "warp-2026-11-13-scan-scaling-20261113T040404-dddddd"
    _stage(repo, peer, "minted")
    run_token.mint(repo, peer, f"scratch/warp/{peer}.workflow.mjs", session_id="sess-peer", sizing_rel=SIZING)
    seen = _forbid_listing(monkeypatch, repo / RECORD_DIR_ROOT)
    call = {"t0": time.monotonic(), "wall0": time.time() - 5, "roots": [repo], "minted": [], "session": SESSION}

    assert mod._minted_since_start(call) == [MINTED_RUN]  # the peer's run is not this call's
    assert seen == []


@pytest.mark.parametrize("count", SIZES)
def test_a_run_staged_before_the_call_began_is_not_minted_since_start(repo, count):
    _retained(repo, count)
    _stage(repo, MINTED_RUN, "minted")
    _mint_token(repo, MINTED_RUN)
    call = {"t0": time.monotonic(), "wall0": time.time() + 60, "roots": [repo], "minted": [], "session": SESSION}

    assert mod._minted_since_start(call) == []


@pytest.mark.parametrize("count", SIZES)
def test_run_dirs_stats_the_named_ids_without_listing_state_warp(repo, monkeypatch, count):
    _retained(repo, count)
    _stage(repo, MINTED_RUN, "minted")
    seen = _forbid_listing(monkeypatch, repo / RECORD_DIR_ROOT)

    assert mod._run_dirs(repo, [MINTED_RUN, TOKEN_RUN]) == {MINTED_RUN}  # TOKEN_RUN has no run dir
    assert seen == []


@pytest.mark.parametrize("count", SIZES)
def test_unmarked_emitted_run_is_found_by_its_token_without_listing_state_warp(repo, monkeypatch, count):
    _retained(repo, count)
    _stage(repo, TOKEN_RUN, "staged")
    _mint_token(repo, TOKEN_RUN)
    seen = _forbid_listing(monkeypatch, repo / RECORD_DIR_ROOT)

    assert mod._unmarked_emitted_run(repo, SIZING) == TOKEN_RUN
    assert seen == []


@pytest.mark.parametrize("count", SIZES)
def test_unmarked_emitted_run_is_found_by_the_live_run_index_without_listing_state_warp(repo, monkeypatch, count):
    _retained(repo, count)
    _stage(repo, INDEXED_RUN, "staged")
    assert live_run_index.add(repo, INDEXED_RUN, time.time())
    seen = _forbid_listing(monkeypatch, repo / RECORD_DIR_ROOT)

    assert mod._unmarked_emitted_run(repo, SIZING) == INDEXED_RUN
    assert seen == []


@pytest.mark.parametrize("count", SIZES)
def test_unmarked_emitted_run_refuses_what_is_marked_landed_or_another_sizings(repo, count):
    _retained(repo, count)
    _stage(repo, TOKEN_RUN, "landed")
    _mint_token(repo, TOKEN_RUN)
    assert mod._unmarked_emitted_run(repo, SIZING) is None  # landed
    _stage(repo, TOKEN_RUN, "staged")
    (repo / RECORD_DIR_ROOT / TOKEN_RUN / "halted.json").write_text("{}", encoding="utf-8")
    assert mod._unmarked_emitted_run(repo, SIZING) is None  # marked
    assert mod._unmarked_emitted_run(repo, OTHER) is None  # a sizing with no run of its own


def test_unmarked_emitted_run_in_neither_store_is_not_found(repo):
    _stage(repo, TOKEN_RUN, "staged")  # on disk, but no token record and no live-run-index entry

    assert mod._unmarked_emitted_run(repo, SIZING) is None


def test_the_answer_does_not_depend_on_how_many_run_dirs_state_warp_retains(tmp_path):
    answers = []
    for count in SIZES:
        root = tmp_path / f"repo-{count}"
        for rel in (".git/objects", ".git/refs"):
            (root / rel).mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        _retained(root, count)
        _stage(root, MINTED_RUN, "minted")
        _mint_token(root, MINTED_RUN)
        _stage(root, TOKEN_RUN, "staged")
        _mint_token(root, TOKEN_RUN)
        call = {"t0": time.monotonic(), "wall0": time.time() - 5, "roots": [root], "minted": [], "session": SESSION}
        answers.append((
            mod._minted_since_start(call),
            sorted(mod._run_dirs(root, [MINTED_RUN, TOKEN_RUN])),
            mod._unmarked_emitted_run(root, SIZING),
        ))
    assert answers[0] == answers[1]
    assert answers[0] == ([MINTED_RUN, TOKEN_RUN], [MINTED_RUN, TOKEN_RUN], TOKEN_RUN)
