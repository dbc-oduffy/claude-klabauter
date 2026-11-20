"""warp_run_deliverable._manifest_declared answers repeat calls from run_dir_stat_cache outside a scope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope
from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_dir_stat_cache

RUN = "ask-20261118T100000-aaaaaa"
JOB = "tools/job.py"
OTHER = "tools/other.py"


@pytest.fixture(autouse=True)
def _fresh_cache():
    for ns in (wrd.MANIFEST_PARSE_CACHE_NAMESPACE, wrd.REQUEST_PARSE_CACHE_NAMESPACE):
        run_dir_stat_cache.clear(ns)
    yield
    for ns in (wrd.MANIFEST_PARSE_CACHE_NAMESPACE, wrd.REQUEST_PARSE_CACHE_NAMESPACE):
        run_dir_stat_cache.clear(ns)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def reads(monkeypatch):
    seen: list = []
    real = call_io_scope.read_bytes

    def spy(path):
        seen.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(call_io_scope, "read_bytes", spy)
    return seen


def _manifest(repo: Path, text: str) -> Path:
    rec = repo / "state/warp" / RUN
    rec.mkdir(parents=True, exist_ok=True)
    f = rec / "manifest.json"
    f.write_text(text, encoding="utf-8")
    return f


def _bump(f: Path) -> None:
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))


def test_second_call_reads_nothing(repo, reads) -> None:
    _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}], "doc_writes": []}))
    first = wrd._manifest_declared(RUN, repo)
    n = len(reads)
    second = wrd._manifest_declared(RUN, repo)
    assert first == second == ((JOB,), (), False)
    assert n == 1 and len(reads) == n


def test_absent_files_read_nothing(repo, reads) -> None:
    assert wrd._manifest_declared(RUN, repo) == ((), (), False)
    assert wrd._manifest_declared(RUN, repo) == ((), (), False)
    assert reads == []


def test_editing_manifest_rereads(repo, reads) -> None:
    f = _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}]}))
    assert wrd._manifest_declared(RUN, repo)[0] == (JOB,)
    f.write_text(json.dumps({"rows": [{"writes": [JOB, OTHER]}]}), encoding="utf-8")
    _bump(f)
    assert wrd._manifest_declared(RUN, repo)[0] == (JOB, OTHER)
    assert len(reads) == 2


def test_editing_commit_request_rereads(repo, reads) -> None:
    _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}]}))
    req = repo / wrd.RUN_DIR_ROOT / RUN / wrd.COMMIT_REQUEST_FILE
    req.parent.mkdir(parents=True)
    req.write_text("not a marker", encoding="utf-8")
    first = wrd._manifest_declared(RUN, repo)
    n = len(reads)
    assert wrd._manifest_declared(RUN, repo) == first
    assert len(reads) == n
    req.write_text("not a marker, edited", encoding="utf-8")
    _bump(req)
    wrd._manifest_declared(RUN, repo)
    assert reads[n:] == [wrd.COMMIT_REQUEST_FILE]


def test_unreadable_manifest_still_reports_unreadable(repo, reads) -> None:
    _manifest(repo, "{not json")
    assert wrd._manifest_declared(RUN, repo)[2] is True
    assert wrd._manifest_declared(RUN, repo)[2] is True
    assert len(reads) == 1


def test_partial_manifest_keeps_paths_before_failure(repo) -> None:
    _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}, {"nope": 1}]}))
    assert wrd._manifest_declared(RUN, repo) == ((JOB,), (), True)


def test_manifest_created_after_absent_is_seen(repo) -> None:
    assert wrd._manifest_declared(RUN, repo) == ((), (), False)
    _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}]}))
    assert wrd._manifest_declared(RUN, repo)[0] == (JOB,)


def test_removed_manifest_is_seen(repo) -> None:
    f = _manifest(repo, json.dumps({"rows": [{"writes": [JOB]}]}))
    assert wrd._manifest_declared(RUN, repo)[0] == (JOB,)
    f.unlink()
    assert wrd._manifest_declared(RUN, repo) == ((), (), False)
