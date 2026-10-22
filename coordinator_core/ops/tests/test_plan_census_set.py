"""
coordinator_core/ops/tests/test_plan_census_set.py — the set-level census op.

Subject: `plan_mise_prep_probe._census_set_handler`. The revalidator is stubbed for the param
and deadline cases; one case drives the real `revalidate_set` in a `git init` tmp repo. Every
case points the store at `tmp_path`.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from coordinator_core import ipc
from coordinator_core.ops import plan_mise_prep_probe as mod
from coordinator_core.ops.tests.test_plan_mise_prep_probe import _SPINE, _plan, _repo
from coordinator_core.roadmap import census_store
from coordinator_core.session import machinery_paths

# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "docs/plans/2026-09-07-a.md"
B = "docs/plans/2026-09-07-b.md"

_PROSE_FM = """census:
  - question: a prose entry
    command: echo hello
    result: hello there
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_COUNT_FM = """census:
  - question: a counted entry
    command: grep -c . data.txt
    result: "3"
    count: 3
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""


@pytest.fixture(autouse=True)
def _store_in_tmp(tmp_path, monkeypatch):
    cache = tmp_path / "cache-dir"
    cache.mkdir()
    monkeypatch.setattr(machinery_paths, "cache_dir", lambda *a, **k: str(cache))
    monkeypatch.setattr(census_store, "cache_dir", lambda *a, **k: str(cache))
    return cache


class Stub:
    def __init__(self):
        self.seen: dict = {}

    def revalidate_set(self, plans, root, timeout, **kw):
        self.seen = {"plans": plans, "root": root, "timeout": timeout, **kw}
        return {"plans": {}, "spent_ms": 7, "covered": 0, "census_plans": 0, "pending": []}


def _two_plans(tmp_path, fm=_PROSE_FM):
    common = _repo(tmp_path)
    _plan(tmp_path, frontmatter=fm, spine=_SPINE, name=A)
    _plan(tmp_path, frontmatter=fm, spine=_SPINE, name=B)
    return common


def _call(params, common):
    return mod._census_set_handler(params, common)


def test_the_op_resolves_through_the_real_dispatch_path():
    handler = ipc._REGISTRY.get("plan.census_set") or ipc._lazy_import_and_lookup("plan.census_set")
    assert callable(handler)


def test_the_op_passes_the_pinned_deadline_and_the_head(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 8.0)
    stub = Stub()
    monkeypatch.setattr(mod, "_revalidator", lambda: stub)
    before = time.monotonic()
    reply = _call({"plans": [A, B], "census_budget_ms": 1000, "census_timeout": 9}, common)
    guard_share = 8.0 * mod._CENSUS_DEADLINE_SHARE
    assert stub.seen["deadline"] - before == pytest.approx(guard_share, abs=0.5)
    assert stub.seen["process_budget_ms"] == 1000
    assert stub.seen["head"] == "a" * 40
    assert stub.seen["timeout"] == 9
    assert [p.name for p in stub.seen["plans"]] == [A.rsplit("/", 1)[1], B.rsplit("/", 1)[1]]
    assert isinstance(stub.seen["store"], census_store.CensusStore)
    assert reply["spent_ms"] == 7


def test_a_zero_budget_passes_the_ops_own_500ms_process_budget(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 8.0)
    stub = Stub()
    monkeypatch.setattr(mod, "_revalidator", lambda: stub)
    before = time.monotonic()
    _call({"plans": [A], "census_budget_ms": 0}, common)
    assert stub.seen["deadline"] - before == pytest.approx(
        8.0 * mod._CENSUS_DEADLINE_SHARE, abs=0.5
    )
    assert stub.seen["process_budget_ms"] == 500


def test_process_ms_adds_the_children_process_ms(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    stub = Stub()
    stub.revalidate_set = lambda *a, **k: {"plans": {}, "pending": [], "children_process_ms": 250}
    monkeypatch.setattr(mod, "_revalidator", lambda: stub)
    reply = _call({"plans": [A]}, common)
    assert reply["children_process_ms"] == 250
    assert 250 <= reply["process_ms"] < 250 + 5000


def test_a_reply_without_children_process_ms_sums_the_op_thread_alone(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    monkeypatch.setattr(mod, "_revalidator", lambda: Stub())
    reply = _call({"plans": [A]}, common)
    assert 0 <= reply["process_ms"] < 5000


@pytest.mark.parametrize("bad", [None, [], "a", [""], [7], [A, None]])
def test_bad_plans_raise(tmp_path, bad):
    common = _two_plans(tmp_path)
    with pytest.raises(ValueError, match="plans must be a non-empty list"):
        _call({"plans": bad}, common)


@pytest.mark.parametrize("bad", [0, -1, "30", True, 1.5])
def test_bad_census_timeout_raises(tmp_path, bad):
    common = _two_plans(tmp_path)
    with pytest.raises(ValueError, match="census_timeout must be a positive integer"):
        _call({"plans": [A], "census_timeout": bad}, common)


@pytest.mark.parametrize("bad", [-1, "10", True, 1.5])
def test_bad_census_budget_ms_raises(tmp_path, bad):
    common = _two_plans(tmp_path)
    with pytest.raises(ValueError, match="census_budget_ms must be a non-negative integer"):
        _call({"plans": [A], "census_budget_ms": bad}, common)


def test_a_plan_escaping_the_root_raises(tmp_path):
    common = _two_plans(tmp_path)
    with pytest.raises(ValueError, match="escapes the resolved worktree"):
        _call({"plans": ["../outside.md"]}, common)


def test_an_absent_repo_root_refuses():
    with pytest.raises(ValueError, match="requires a resolved repo_root"):
        _call({"plans": [A]}, None)


def test_a_prose_entry_is_not_spawned(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    calls: list = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    reply = _call({"plans": [A]}, common)
    entry = reply["plans"][A]["entries"][0]
    assert entry["state"] == "UNDECIDABLE" and "cannot drift" in entry["basis"]
    assert calls == []


def test_process_ms_is_a_number_on_every_reply(tmp_path):
    common = _two_plans(tmp_path)
    reply = _call({"plans": [A, B]}, common)
    assert isinstance(reply["process_ms"], float) and reply["process_ms"] >= 0


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_a_second_call_over_an_unchanged_plan_spawns_nothing(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "data.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (tmp_path / "coordinator_core").mkdir()
    _plan(tmp_path, frontmatter=_COUNT_FM, spine=_SPINE, name=A)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fixture")

    from coordinator_core.roadmap import census_inproc

    monkeypatch.setattr(census_inproc, "evaluate", lambda command, files, store: None)
    calls: list = []
    real = subprocess.Popen

    def counting_popen(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    first = _call({"plans": [A]}, tmp_path)
    first_spawns = len(calls)
    second = _call({"plans": [A]}, tmp_path)
    assert first["plans"][A]["entries"][0]["state"] == "MATCH", first["plans"][A]["entries"]
    assert first_spawns >= 1
    assert len(calls) == first_spawns
    assert second["memo_hits"] == 1
    assert [e["state"] for e in second["plans"][A]["entries"]] == [
        e["state"] for e in first["plans"][A]["entries"]
    ]
