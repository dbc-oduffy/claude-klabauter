
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.engine_version import (
    MIN_KNOWN_GOOD_SHA,
    engine_build,
    resolve_engine_dirty,
    resolve_engine_sha,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_resolve_engine_sha_returns_40_char_lowercase_hex_in_real_repo():
    sha = resolve_engine_sha()
    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def _fake_git(monkeypatch, *, returncode=0, stdout="", timed_out=False, capture=None):

    def fake_run_git(args, **kwargs):
        if capture is not None:
            capture["args"] = list(args)
            capture["kwargs"] = kwargs
        return GitResult(
            returncode=returncode, stdout=stdout, stderr="", timed_out=timed_out
        )

    monkeypatch.setattr("coordinator_core.engine_version.run_git", fake_run_git)


def test_resolve_engine_sha_returns_none_when_git_missing(monkeypatch):
    _fake_git(monkeypatch, returncode=127)
    assert resolve_engine_sha() is None


def test_resolve_engine_sha_strips_trailing_newline_from_git_stdout(monkeypatch):
    _fake_git(monkeypatch, stdout="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" + chr(10))
    assert resolve_engine_sha() == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_resolve_engine_sha_returns_none_when_git_returns_nonzero(monkeypatch):
    _fake_git(monkeypatch, returncode=1)
    assert resolve_engine_sha() is None


def test_resolve_engine_sha_returns_none_when_timeout(monkeypatch):
    _fake_git(monkeypatch, returncode=-1, timed_out=True)
    assert resolve_engine_sha() is None


def test_resolve_engine_dirty_returns_bool_in_real_repo():
    dirty = resolve_engine_dirty()
    assert dirty is None or isinstance(dirty, bool)


def test_resolve_engine_dirty_false_when_porcelain_output_empty(monkeypatch):
    _fake_git(monkeypatch, stdout="")
    assert resolve_engine_dirty() is False


def test_resolve_engine_dirty_true_when_porcelain_output_nonempty(monkeypatch):
    _fake_git(monkeypatch, stdout=" M engine_version.py" + chr(10))
    assert resolve_engine_dirty() is True


def test_resolve_engine_dirty_scopes_status_to_engine_dir(monkeypatch):
    captured: dict = {}
    _fake_git(monkeypatch, stdout="", capture=captured)
    resolve_engine_dirty()
    engine_dir = str(Path(__file__).resolve().parent.parent)
    assert captured["args"][:2] == ["-C", engine_dir]
    assert captured["args"][-2:] == ["--", "."]


def test_resolve_engine_dirty_returns_none_when_git_missing(monkeypatch):
    _fake_git(monkeypatch, returncode=127)
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_git_returns_nonzero(monkeypatch):
    _fake_git(monkeypatch, returncode=1)
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_timeout(monkeypatch):
    _fake_git(monkeypatch, returncode=-1, timed_out=True)
    assert resolve_engine_dirty() is None


def test_min_known_good_sha_is_40_char_hex():
    assert len(MIN_KNOWN_GOOD_SHA) == 40
    assert all(c in "0123456789abcdef" for c in MIN_KNOWN_GOOD_SHA)


def test_min_known_good_sha_is_a_real_commit_in_this_repo():
    engine_dir = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(engine_dir), "cat-file", "-e",
             f"{MIN_KNOWN_GOOD_SHA}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=5,
            **no_console_creationflags(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable in this test environment")

    assert result.returncode == 0, (
        f"MIN_KNOWN_GOOD_SHA {MIN_KNOWN_GOOD_SHA!r} is not a resolvable "
        f"commit in this repo's history: {result.stderr}"
    )
    anc = subprocess.run(
        ["git", "-C", str(engine_dir), "merge-base", "--is-ancestor",
         MIN_KNOWN_GOOD_SHA, "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        **no_console_creationflags(),
    )
    assert anc.returncode == 0, "MIN_KNOWN_GOOD_SHA is not an ancestor of HEAD"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _repo_with_commit(path: Path, message: str, *, marker: bool = False) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    if marker:
        (path / "PUBLISH_MIRROR").write_text("x", encoding="utf-8")
    (path / "f.txt").write_text("x", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


_SRC = "a" * 40


def test_marker_constant_matches_memo_resolver():
    from coordinator_core import engine_version
    from coordinator_core.ops.fleet._memo_resolver import PUBLISH_MIRROR_MARKER

    assert engine_version._PUBLISH_MIRROR_MARKER == PUBLISH_MIRROR_MARKER


def test_source_head_source_tree(tmp_path):
    from coordinator_core.engine_version import resolve_engine_source_head

    head = _repo_with_commit(tmp_path / "src", "plain")
    got = resolve_engine_source_head(tmp_path / "src")
    assert (got.sha, got.basis, got.engine_head) == (head, "source-tree", head)


def test_source_head_mirror_trailer(tmp_path):
    from coordinator_core.engine_version import resolve_engine_source_head

    head = _repo_with_commit(
        tmp_path / "m",
        f"publish\n\nPercolate-Round: r1\nPercolate-Source-Head: {_SRC}",
        marker=True,
    )
    got = resolve_engine_source_head(tmp_path / "m")
    assert (got.sha, got.basis, got.engine_head) == (_SRC, "mirror-stamp", head)


def test_source_head_mirror_subject_stamp(tmp_path):
    from coordinator_core.engine_version import resolve_engine_source_head

    head = _repo_with_commit(tmp_path / "m", f"publish [source-head {_SRC[:12]}]", marker=True)
    got = resolve_engine_source_head(tmp_path / "m")
    assert (got.sha, got.basis, got.engine_head) == (_SRC[:12], "mirror-stamp", head)


def test_source_head_mirror_unstamped(tmp_path):
    from coordinator_core.engine_version import resolve_engine_source_head

    head = _repo_with_commit(tmp_path / "m", "publish without stamp", marker=True)
    got = resolve_engine_source_head(tmp_path / "m")
    assert (got.sha, got.basis, got.engine_head) == (None, "mirror-stamp", head)


def test_source_head_outside_a_repo_never_raises(monkeypatch, tmp_path):
    from coordinator_core.engine_version import resolve_engine_source_head

    monkeypatch.setattr("coordinator_core.git.repo_root._walk_for_repo", lambda start: None)
    assert resolve_engine_source_head(tmp_path).sha is None


def test_engine_build_reports_the_running_copys_head_in_a_real_repo():
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    build = engine_version.engine_build()
    assert set(build) == {"engine_sha", "engine_dirty"}
    assert build["engine_sha"] == resolve_engine_sha(), (
        "the spawn-free read must agree with `git rev-parse HEAD` on a real repo; "
        "if it does not, the cheap path is reporting a different build from the one "
        "every other provenance consumer records"
    )
    engine_version._BUILD_MEMO = None


def test_engine_build_never_spawns_a_process(monkeypatch):
    import subprocess

    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    engine_version.engine_build()
    engine_version._BUILD_MEMO = None

    def _boom(*args, **kwargs):
        raise AssertionError("engine_build must not create a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert engine_build()["engine_sha"] is not None
    engine_version._BUILD_MEMO = None


def test_engine_build_leaves_dirty_unanswered_rather_than_asserting_clean():
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    assert engine_build()["engine_dirty"] is None
    engine_version._BUILD_MEMO = None


def test_engine_build_resolves_once_per_process(monkeypatch):
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    calls = {"n": 0}

    def fake_head():
        calls["n"] += 1
        return "a" * 40

    monkeypatch.setattr(engine_version, "_engine_head_sha", fake_head)

    first = engine_version.engine_build()
    second = engine_version.engine_build()

    assert calls["n"] == 1
    assert first == second == {"engine_sha": "a" * 40, "engine_dirty": None}
    engine_version._BUILD_MEMO = None


def test_engine_build_returns_a_copy_callers_cannot_poison():
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    engine_version.engine_build()["engine_sha"] = "tampered"
    assert engine_version.engine_build()["engine_sha"] != "tampered"
    engine_version._BUILD_MEMO = None


def test_engine_build_carries_an_unresolvable_head_as_none(monkeypatch):
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    monkeypatch.setattr(engine_version, "_engine_head_sha", lambda: None)
    assert engine_version.engine_build() == {"engine_sha": None, "engine_dirty": None}
    engine_version._BUILD_MEMO = None


def test_engine_head_sha_is_none_outside_a_repo(monkeypatch, tmp_path):
    from coordinator_core import engine_version

    monkeypatch.setattr(
        "coordinator_core.git.repo_root._walk_for_repo", lambda start: None
    )
    assert engine_version._engine_head_sha() is None
