"""Tests for envelope.main's migration onto classify_shas_on_origin_main (C32).

Purpose: pin that the ``check-shipped-on-main.sh`` CLI port classifies its whole argv sha
set with ONE ``classify_shas_on_origin_main`` call, not one ``sha_on_origin_main`` spawn per
ref — the many-commits-against-ONE-ref shape that DOES batch (one ``git rev-list <ref>`` plus
in-memory membership), distinct from the many-independent-RANGES shape, which never batches.

Also pins the explicit requested-vs-returned reconciliation: a sha absent from the
classified map must degrade to the same NOT_ON_MAIN/None branch as an indeterminate
classification, never be silently read as ON_MAIN.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md § C32
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit import envelope
from coordinator_core.ops.emit.envelope import main

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="module")
def _shared_repo(tmp_path_factory):
    """Built ONCE per module (~11 spawns total) and reused via `repo_with_origin`
    below, rather than once per test (~11 spawns * 5 tests) -- Windows process-spawn
    cost, not wall-clock, is the metric (see this repo's CLAUDE.md § Load norm). Every
    consuming test only reads via `main()`'s git calls; none mutate this repo.

    Layout:
      - bare_origin/         — bare repo acting as "origin"
      - work/                — clone; origin/main tracks bare_origin's main
        - first commit  -> pushed to origin/main (ON_MAIN)
        - second commit -> local-only, NOT pushed (NOT_ON_MAIN)
        - third commit  -> local-only, NOT pushed (NOT_ON_MAIN)
    """
    tmp_path = tmp_path_factory.mktemp("check_shipped_on_main_batching")
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "a.txt").write_text("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "first")
    on_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "origin", "main")

    (work / "b.txt").write_text("two\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "second (unpushed)")
    off_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    (work / "c.txt").write_text("three\n")
    _git(work, "add", "c.txt")
    _git(work, "commit", "-m", "third (unpushed)")
    off_main_sha_2 = _git(work, "rev-parse", "HEAD").stdout.strip()

    _git(work, "fetch", "origin")

    return {"work": work, "on_main": on_main_sha, "off_main": off_main_sha, "off_main_2": off_main_sha_2}


@pytest.fixture
def repo_with_origin(_shared_repo, monkeypatch):
    monkeypatch.chdir(_shared_repo["work"])
    return _shared_repo


def test_main_calls_classify_shas_on_origin_main_exactly_once(repo_with_origin, monkeypatch, capsys):
    """Many refs against origin/main -> ONE batched classification call, not one per ref."""
    calls: list[list[str]] = []
    real_classify = envelope.classify_shas_on_origin_main

    def _counting_classify(repo_root, shas):
        calls.append(list(shas))
        return real_classify(repo_root, shas)

    monkeypatch.setattr(envelope, "classify_shas_on_origin_main", _counting_classify)

    rc = main([
        "--verbose",
        repo_with_origin["on_main"],
        repo_with_origin["off_main"],
        repo_with_origin["off_main_2"],
    ])

    assert rc == 1
    assert len(calls) == 1, f"expected exactly one batched classify call, got {len(calls)}: {calls}"
    assert set(calls[0]) == {
        repo_with_origin["on_main"],
        repo_with_origin["off_main"],
        repo_with_origin["off_main_2"],
    }


def test_main_never_calls_sha_on_origin_main(repo_with_origin, monkeypatch, capsys):
    """The per-SHA oracle main() used to call per-ref must not be invoked anymore."""

    def _fail(*args, **kwargs):
        raise AssertionError("main() must not call sha_on_origin_main per ref anymore")

    monkeypatch.setattr(envelope, "sha_on_origin_main", _fail)

    rc = main([
        "--verbose",
        repo_with_origin["on_main"],
        repo_with_origin["off_main"],
    ])

    assert rc == 1


def test_main_still_reports_on_main_and_not_on_main_correctly(repo_with_origin, capsys):
    """Behavioral parity: batching must not change which refs report ON_MAIN/NOT_ON_MAIN."""
    rc = main([
        "--verbose",
        repo_with_origin["on_main"],
        repo_with_origin["off_main"],
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"{repo_with_origin['on_main'][:8]}: ON_MAIN" in out
    assert f"{repo_with_origin['off_main'][:8]}: NOT_ON_MAIN" in out


def test_sha_absent_from_classified_map_degrades_to_not_on_main(repo_with_origin, monkeypatch, capsys):
    """Explicit reconciliation (§ Anti-scope 25): a sha the batched call omits from its
    returned map must NEVER be silently read as a resolved ON_MAIN classification —
    it must degrade the same way an indeterminate (None) result does."""
    on_main_sha = repo_with_origin["on_main"]

    def _classify_dropping_entry(repo_root, shas):
        # Simulate a classifier that returns a map missing the requested sha entirely
        # (rather than an explicit False/None) -- the absence case this chunk pins.
        return {}

    monkeypatch.setattr(envelope, "classify_shas_on_origin_main", _classify_dropping_entry)

    rc = main(["--verbose", on_main_sha])
    out = capsys.readouterr().out

    assert rc == 1, "absence from the classified map must not be read as ON_MAIN"
    assert f"{on_main_sha[:8]}: NOT_ON_MAIN" in out
    assert "ON_MAIN" not in out.replace("NOT_ON_MAIN", "")


def test_unresolvable_ref_never_reaches_classify_call(repo_with_origin, monkeypatch, capsys):
    """A ref that fails to resolve must be excluded from the batched sha set entirely, not
    passed through as None/'' and misclassified."""
    calls: list[list[str]] = []
    real_classify = envelope.classify_shas_on_origin_main

    def _counting_classify(repo_root, shas):
        calls.append(list(shas))
        return real_classify(repo_root, shas)

    monkeypatch.setattr(envelope, "classify_shas_on_origin_main", _counting_classify)

    rc = main(["--verbose", "not-a-real-ref", repo_with_origin["on_main"]])
    err = capsys.readouterr().err

    assert rc == 1
    assert "cannot resolve 'not-a-real-ref'" in err
    assert len(calls) == 1
    assert calls[0] == [repo_with_origin["on_main"]]
