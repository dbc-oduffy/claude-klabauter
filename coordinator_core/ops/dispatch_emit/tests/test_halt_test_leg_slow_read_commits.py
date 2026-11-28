"""A halted dispatch.land_run whose test-leg.json check-in is slow still commits its halt record in one call."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope, commit
from coordinator_core.ops.dispatch_emit import halted_run, land_run, run_token, test_leg_freshness
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
LEDGER = {"halted": "ledger-check", "reason": "row R1 open"}
SLOW_SECS = 0.8


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _write(root: Path, rel: str, data: bytes) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, ".gitattributes", b"*.json text=auto\n")
    _write(tmp_path, "docs/plans/peer.md", b"peer\n")
    # The run's leg is already tracked (a refire): the halt scan hashes it against HEAD's blob.
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/test-leg.json", b'{"status": "fail",\r\n "tests_run": 1}\r\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", b"run\n")
    # A CR-bearing leg under text=auto: its check-in form reads the whole git index.
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/test-leg.json", b'{"status": "pass",\r\n "tests_run": 3}\r\n')
    _write(tmp_path, "run.mjs", b"// script\n")
    return tmp_path


def _op(root: Path, token: str) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: dict(LEDGER), "run_token": token}
    return land_run._handler(params, repo_root=root / ".git")


def test_a_slow_index_read_under_the_leg_check_in_still_commits_the_halt_record(repo, monkeypatch):
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(repo, "rev-parse", "HEAD")
    real = commit._index_entry_sha
    slow: list = []

    def slow_index(*args, **kwargs):
        slow.append(1)
        time.sleep(SLOW_SECS)
        return real(*args, **kwargs)

    monkeypatch.setattr(commit, "_index_entry_sha", slow_index)
    out = _op(repo, token)

    assert slow, "the leg's check-in never read the index; the fixture does not reach the slow read"
    assert out.get("refused") != "halt-read-indeterminate" and not out.get("parked"), out
    assert out["committed"] is True
    assert _git(repo, "rev-parse", "HEAD") != head
    assert f"{RECORD_DIR_ROOT}/{RUN}/test-leg.json" in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()


def test_index_entry_sha_matches_the_parsed_index_without_parsing_it(repo, monkeypatch):
    from coordinator_core.git.git_state import read_index

    _write(repo, f"x/{RECORD_DIR_ROOT}/{RUN}/test-leg.json", b"{}\n")
    _git(repo, "add", "-A")
    parsed = dict(read_index(repo, fresh=True).items())
    monkeypatch.setattr(commit, "read_index", lambda *a, **k: pytest.fail("whole-index parse"))
    for path, entry in parsed.items():
        assert commit._index_entry_sha(repo, path) == entry.sha
    # A tracked path that is only the suffix of a longer tracked path is untracked itself.
    assert commit._index_entry_sha(repo, "peer.md") is None
    assert commit._index_entry_sha(repo, f"{RECORD_DIR_ROOT}/{RUN}/missing.json") is None


def test_a_stall_on_another_halt_read_still_parks(repo, monkeypatch):
    from coordinator_core import bounded_io

    token = run_token.mint(repo, RUN, "run.mjs")
    real = bounded_io._read_registered

    def wedged(path):
        if path.endswith(("run.json", ".mjs")):
            time.sleep(SLOW_SECS)
        return real(path)

    monkeypatch.setattr(bounded_io, "_read_registered", wedged)
    out = _op(repo, token)

    assert out["committed"] is False and out["parked"] is True
    assert out["refused"] == "halt-read-indeterminate"


def test_halt_marker_reads_the_leg_once(tmp_path, monkeypatch):
    rec = tmp_path / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "test-leg.json").write_text(json.dumps({"status": "fail", "flagged_fixture_edits": [{"file": "a"}]}), encoding="utf-8")
    reads: list = []
    real = call_io_scope.read_bytes

    def counting(path, **kwargs):
        if Path(path).name == "test-leg.json":
            reads.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(call_io_scope, "read_bytes", counting)
    halted_run.write_halt_marker(rec, halted="criterion_not_met", sizing_rel=None)

    assert len(reads) == 1
    written = json.loads((rec / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert written[halted_run.HALT_FIXTURE_EDITS_FIELD] == [{"file": "a"}]


def test_halt_stamp_given_no_leg_record_reads_no_leg(tmp_path):
    rec = tmp_path / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "test-leg.json").write_text(json.dumps({"measured_at": "2026-11-24T20:00:00+00:00", "head_sha": "a" * 40}), encoding="utf-8")
    out = test_leg_freshness.halt_stamp(rec, record=None)
    assert out["measured_at"] is None and out["stale"] is True


def test_a_single_stalled_test_leg_read_is_retried_and_the_halt_record_commits(repo, monkeypatch):
    from coordinator_core import bounded_io

    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(repo, "rev-parse", "HEAD")
    real = bounded_io._read_registered
    stalls: list = []

    def wedged_once(path):
        if path.endswith("test-leg.json") and not stalls:
            stalls.append(path)
            time.sleep(SLOW_SECS)
        return real(path)

    monkeypatch.setattr(bounded_io, "_read_registered", wedged_once)
    warm, warms = land_run._warm_test_leg_checkin, []

    def cold_first_warm(root, params):
        warms.append(1)
        if len(warms) > 1:
            warm(root, params)

    monkeypatch.setattr(land_run, "_warm_test_leg_checkin", cold_first_warm)
    out = _op(repo, token)

    assert stalls, "the bounded read of test-leg.json never stalled; the retry branch was not reached"
    assert out.get("refused") != "halt-read-indeterminate" and not out.get("parked"), out
    assert out["committed"] is True
    assert _git(repo, "rev-parse", "HEAD") != head
    assert f"{RECORD_DIR_ROOT}/{RUN}/test-leg.json" in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()


def test_halt_stamp_without_a_manifest_applies_the_own_diff_mtime_check(repo):
    rec = repo / RECORD_DIR_ROOT / RUN
    head = _git(repo, "rev-parse", "HEAD")
    (rec / "test-leg.json").write_text(
        json.dumps({"measured_at": "2020-01-01T00:00:00+00:00", "head_sha": head}), encoding="utf-8"
    )
    leg = {"measured_at": "2020-01-01T00:00:00+00:00", "head_sha": head}

    (rec / "manifest.json").write_text(json.dumps({"own_diff_paths": ["run.mjs"]}), encoding="utf-8")
    assert test_leg_freshness.halt_stamp(rec, record=leg)["stale"] is True

    (rec / "manifest.json").write_text(json.dumps({"own_diff_paths": []}), encoding="utf-8")
    assert test_leg_freshness.halt_stamp(rec, record=leg)["stale"] is False
