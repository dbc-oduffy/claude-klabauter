"""mint given the terminal tree sha skips its `log -1 --format=%T` read and writes the same stamp."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.tests.test_review_stamp_superseding import _git, _plan, _record, _repo, _test_runner

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _mint_counting(monkeypatch, repo, head, **kwargs):
    calls = []
    real = m.run_git

    def counting(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(m, "run_git", counting)
    stamp = m.mint(_plan(repo), repo, build_test_path=str(_test_runner(repo)), **kwargs)
    return stamp, calls


def _landed(repo, head):
    record_path = _record(repo, head)
    return (head, record_path, {**m._load_sidecar(record_path), "delivery": {"verdict": "PASS", "unbacked": []}})


def test_known_tree_spawns_one_fewer_git_child_and_writes_an_identical_stamp(tmp_path, monkeypatch):
    repo, head = _repo(tmp_path)
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}")

    baseline, base_calls = _mint_counting(monkeypatch, repo, head, resolved=_landed(repo, head))
    keyed, keyed_calls = _mint_counting(
        monkeypatch, repo, head, resolved=_landed(repo, head), terminal_tree_sha=tree
    )

    assert len(keyed_calls) == len(base_calls) - 1
    assert any("--format=%T" in c for c in base_calls)
    assert not any("--format=%T" in c for c in keyed_calls)
    assert keyed == baseline
    assert keyed["terminal_tree_sha"] == tree
    assert m.check(_plan(repo), repo) is None


def test_a_superseding_record_reads_its_own_tree_and_ignores_the_callers(tmp_path, monkeypatch):
    repo, head = _repo(tmp_path)
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}")

    stamp, calls = _mint_counting(
        monkeypatch, repo, head, superseding_record=_record(repo, head), terminal_tree_sha="0" * 40
    )

    assert any("--format=%T" in c for c in calls)
    assert stamp["terminal_tree_sha"] == tree
