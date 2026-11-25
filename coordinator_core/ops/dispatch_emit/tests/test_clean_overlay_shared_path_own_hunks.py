"""A path the run and a peer both touched (git status `MM`) reaches the clean mirror at HEAD plus only the run's own hunk."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, review_edits, run_write_prints
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runA"
SHARED = "f.txt"
HEAD_LINES = [f"line {i:02d}\n" for i in range(1, 21)]
OWN_LINES = [*HEAD_LINES[:1], "line 02 run edit\n", *HEAD_LINES[2:]]
BOTH_LINES = [*OWN_LINES[:17], "line 18 peer edit\n", *OWN_LINES[18:]]
PEER_ONLY_LINES = [*HEAD_LINES[:17], "line 18 peer edit\n", *HEAD_LINES[18:]]


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, input=None if stdin is None else stdin.encode("utf-8"),
        **no_console_creationflags(),
    ).stdout.decode("utf-8")


def _write(root, lines):
    (root / SHARED).write_text("".join(lines), encoding="utf-8", newline="")


@pytest.fixture
def mm_repo(tmp_path, monkeypatch):
    """HEAD holds the file; the run edited line 2 (write-time print recorded), then a peer edited line 18 and its
    hunk alone is staged: the path is `MM`, the worktree carries both hunks."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, HEAD_LINES)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    record = root / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True)
    head_blob = _git(root, "rev-parse", f"HEAD:{SHARED}").strip()
    (record / "manifest.json").write_text(
        json.dumps({"write_baseline": {SHARED: head_blob}, "review_declared_paths": [SHARED]}), encoding="utf-8",
    )
    _write(root, OWN_LINES)
    run_write_prints.record(root, RUN, [SHARED])
    _write(root, BOTH_LINES)
    staged = _git(root, "hash-object", "-w", "--stdin", stdin="".join(PEER_ONLY_LINES)).strip()
    _git(root, "update-index", "--cacheinfo", f"100644,{staged},{SHARED}")
    assert _git(root, "status", "--porcelain", "--", SHARED).startswith("MM"), "the path must carry staged and unstaged hunks"
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {SHARED: "peer-run"})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    return root


def test_own_hunk_overrides_split_an_mm_path_to_head_plus_the_run_s_hunk_only(mm_repo):
    got = cov.own_hunk_overrides(mm_repo, RUN, [SHARED])

    assert got == {SHARED: "".join(OWN_LINES).encode("utf-8")}


def test_own_delta_keeps_the_shared_declared_path_and_overrides_it_with_the_own_hunk(mm_repo):
    paths, overrides = cov.own_delta(mm_repo, RUN, [SHARED])

    assert paths == [SHARED]
    assert overrides == {SHARED: "".join(OWN_LINES).encode("utf-8")}


def test_the_clean_mirror_holds_the_mm_path_at_head_plus_the_own_hunk_never_head_alone_or_the_peer_hunk(mm_repo):
    paths, overrides = cov.own_delta(mm_repo, RUN, [SHARED])
    probe = (
        "import sys; t = open('f.txt', 'rb').read().decode();"
        "sys.exit(0 if 'line 02 run edit' in t and 'line 18 peer edit' not in t and 'line 18\\n' in t else 3)"
    )

    ran = cov.run_in_export(mm_repo, RUN, paths, [sys.executable, "-c", probe], overrides)

    assert ran.returncode == 0, ran.stderr


def test_a_shared_path_the_run_cannot_split_carries_no_override(mm_repo, monkeypatch):
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", lambda *_a, **_k: ({}, [SHARED]))

    assert cov.own_hunk_overrides(mm_repo, RUN, [SHARED]) == {}


def test_an_unsplittable_shared_path_the_run_declares_stays_in_the_delta_at_worktree_bytes(mm_repo, monkeypatch):
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {})

    paths, overrides = cov.own_delta(mm_repo, RUN, [])

    assert paths == [SHARED] and overrides == {}
