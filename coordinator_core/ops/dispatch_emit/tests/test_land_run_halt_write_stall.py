"""A halted dispatch.land_run whose receipt write stalls answers typed and retry-safe, inside the caller bar."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import warm_write_stall_probe as probe
from coordinator_core.ops.dispatch_emit import halt_resume_record, halted_withheld_index, land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
RUN2 = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-dddddd"
LEDGER = {"halted": "ledger-check", "reason": "row R1 open"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


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
    for run in (RUN, RUN2):
        _write(tmp_path, f"{RECORD_DIR_ROOT}/{run}/run.json", "run\n")
    _write(tmp_path, "run.mjs", "// script\n")
    return tmp_path


def _op(root: Path, run: str, token: str) -> dict:
    params = {"script_path": "run.mjs", "run_id": run, HALTED_RECORD_PARAM: dict(LEDGER), "run_token": token}
    return land_run._handler(params, repo_root=root / ".git")


@pytest.fixture
def blocked_write(monkeypatch):
    """`halt_resume_record`'s atomic write blocks for the halted-landing receipt of RUN only."""
    release = threading.Event()
    real = halt_resume_record.atomic_write_bytes
    hit: list = []

    def blocking(target, data, **kwargs):
        if RUN in str(target):
            hit.append(str(target))
            release.wait(10)
        return real(target, data, **kwargs)

    monkeypatch.setattr(halt_resume_record, "atomic_write_bytes", blocking)
    yield hit
    release.set()


def test_a_stalled_receipt_write_returns_typed_inside_the_caller_bar(repo, blocked_write):
    token = run_token.mint(repo, RUN, "run.mjs")
    started = time.monotonic()
    out = _op(repo, RUN, token)
    waited_ms = (time.monotonic() - started) * 1000.0

    assert blocked_write, "the receipt write never went through bounded_write"
    assert out["refused"] == "halt-write-indeterminate" and out["retry_safe"] is True
    assert out["path"] == blocked_write[0]
    assert out["committed"] is True and out["sha"] == _git(repo, "rev-parse", "HEAD")
    assert waited_ms < 5_000


def test_a_second_landing_completes_while_the_first_write_is_stalled(repo, blocked_write):
    first = run_token.mint(repo, RUN, "run.mjs")
    second = run_token.mint(repo, RUN2, "run.mjs")
    stalled = _op(repo, RUN, first)
    other = _op(repo, RUN2, second)

    assert stalled["refused"] == "halt-write-indeterminate"
    assert other["committed"] is True and other.get("refused") is None


def test_a_resend_after_the_write_lands_answers_already_landed(repo, monkeypatch):
    token = run_token.mint(repo, RUN, "run.mjs")
    release = threading.Event()
    real = halt_resume_record.atomic_write_bytes

    def slow(target, data, **kwargs):
        release.wait(5)
        return real(target, data, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(halt_resume_record, "atomic_write_bytes", slow)
        first = _op(repo, RUN, token)
        assert first["refused"] == "halt-write-indeterminate"
        release.set()
        deadline = time.monotonic() + 5
        while halt_resume_record.write_outstanding(first["path"]) and time.monotonic() < deadline:
            time.sleep(0.01)

    again = _op(repo, RUN, token)
    assert again["committed"] is True and again.get("refused") is None, again
    assert run_token.halt_landing(repo, RUN, token, "run.mjs")["sha"] == again["sha"]


def test_a_stalled_withheld_cache_write_is_dropped_not_raised(tmp_path, monkeypatch):
    release = threading.Event()
    hit: list = []

    def blocking(target, data, **kw):
        hit.append(str(target))
        release.wait(10)

    monkeypatch.setattr(halt_resume_record, "atomic_write_bytes", blocking)
    started = time.monotonic()
    try:
        halted_withheld_index._persist(str(tmp_path), str(tmp_path / halted_withheld_index.INDEX_DIR))
    finally:
        release.set()
    assert hit, "the cache write never went through bounded_write"
    assert (time.monotonic() - started) * 1000.0 < 5_000
    assert not os.path.exists(halted_withheld_index._cache_path(str(tmp_path)))


def test_both_writes_of_one_landing_share_one_caller_bar(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(halt_resume_record, "atomic_write_bytes", lambda target, data, **kw: release.wait(10))
    started = time.monotonic()
    try:
        with halt_resume_record.write_budget(run_token.HALT_WRITE_DEADLINE_MS):
            first = halt_resume_record.bounded_write(tmp_path / "a", b"x", run_token.HALT_WRITE_DEADLINE_MS)
            halted_withheld_index._persist(str(tmp_path), str(tmp_path / halted_withheld_index.INDEX_DIR))
    finally:
        release.set()
    assert first is halt_resume_record.STALLED
    assert (time.monotonic() - started) * 1000.0 <= probe.CALLER_WAIT_BAR_MS


def test_every_halt_write_deadline_is_within_the_caller_bar():
    assert run_token.HALT_WRITE_DEADLINE_MS <= probe.CALLER_WAIT_BAR_MS
    assert halted_withheld_index.PERSIST_DEADLINE_MS <= probe.CALLER_WAIT_BAR_MS


def test_a_write_refused_unstarted_is_not_reported_as_outliving_its_deadline():
    landed = {"sha": "abc1234", "paths": []}
    started = land_run._halt_write_reply(run_token.HaltWriteIndeterminate("p", landed))
    unstarted = land_run._halt_write_reply(run_token.HaltWriteIndeterminate("p", landed, started=False))

    assert "outlived its deadline" in started["error"]
    assert "not started" in unstarted["error"] and "outlived" not in unstarted["error"]
    assert unstarted["retry_safe"] is True and unstarted["refused"] == "halt-write-indeterminate"
