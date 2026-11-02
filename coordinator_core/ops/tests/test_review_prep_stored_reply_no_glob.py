"""stored_reply_for_run finds a run's reply by a run-keyed pointer: no directory walk, any share-dir size."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-nowalk"
UNRELATED = 20_000


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _store(repo: Path, run: str, stem: str = "p", plan: str = "docs/plans/p.md") -> dict:
    (repo / "a").mkdir(exist_ok=True)
    (repo / "a" / "one.py").write_text("v = 1\n" * 3, encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD").strip()
    return rp.review_prep(
        repo, base=base, paths=["a/one.py"], slice_prefix=f"{stem}-{run}-{base[:12]}-prep",
        plan_path=plan, session_id=SESSION,
    )


@contextlib.contextmanager
def _no_walks(monkeypatch):
    """Scoped to the lookup: a fixture-teardown scandir must not see the patch."""

    def refuse(*_a, **_k):
        raise AssertionError("directory walk")

    with monkeypatch.context() as m:
        m.setattr(Path, "glob", refuse)
        m.setattr(Path, "rglob", refuse)
        m.setattr(Path, "iterdir", refuse)
        m.setattr(os, "scandir", refuse)
        m.setattr(os, "listdir", refuse)
        yield


def test_lookup_walks_nothing_and_stays_under_the_bar_on_a_20k_file_share_dir(repo, monkeypatch):
    made = _store(repo, "run-one")
    share = Path(share_dir(str(repo), SESSION))
    for i in range(UNRELATED):
        (share / f"unrelated-{i}.md").touch()
    started = time.process_time()
    with _no_walks(monkeypatch):
        got = rp.stored_reply_for_run(
            repo, SESSION, made["run_base_sha"], run_key="run-one", within=["a/one.py"], covering=True
        )
    cpu_s = time.process_time() - started
    assert got is not None and got["sidecar_path"] == made["sidecar_path"]
    assert cpu_s < 0.5


def test_a_run_with_no_pointer_is_not_found(repo, monkeypatch):
    made = _store(repo, "run-one")
    with _no_walks(monkeypatch):
        assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run-two") is None
        assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run") is None


def test_run_id_that_prefixes_another_runs_id_finds_its_own_reply(repo, monkeypatch):
    made = _store(repo, "run1")
    _store(repo, "run10")
    with _no_walks(monkeypatch):
        got = rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1")
    assert got is not None
    assert got["sidecar_path"].endswith("p-run1-" + made["run_base_sha"][:12] + "-prep.review-prep.md")


def test_two_bases_for_one_run_stay_ambiguous_without_a_base(repo, monkeypatch):
    _store(repo, "run1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    _store(repo, "run1")
    with _no_walks(monkeypatch):
        assert rp.stored_reply_for_run(repo, SESSION, None, run_key="run1") is None


def test_the_ask_route_canonical_sidecar_is_found_without_a_pointer(repo, monkeypatch):
    made = _store(repo, "run1", stem="manifest", plan="manifest.md")
    share = Path(share_dir(str(repo), SESSION))
    for ptr in list(share.glob("ptr-*.review-prep-ptr.json")):
        ptr.unlink()
    with _no_walks(monkeypatch):
        got = rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1")
    assert got is not None and got["sidecar_path"] == made["sidecar_path"]
