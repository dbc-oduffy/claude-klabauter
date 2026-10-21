"""
coordinator_core/ops/tests/test_plan_mise_prep_probe_census_set.py — the probe's set-level
census wiring: `census_run` memo, `decidable_only` skip and `census_spent_ms`.

Subject: `plan_mise_prep_probe._probe_handler`. Fixtures are imported from
test_plan_mise_prep_probe.py. Spawns are counted by wrapping `subprocess.run`.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from coordinator_core import ipc
from coordinator_core.ops import plan_mise_prep_probe as mod
from coordinator_core.ops.tests.test_plan_mise_prep_probe import (
    _SPINE,
    _plan,
    _probe,
    _repo,
)

_FM = """census:
  - question: a counted entry
    command: echo 1
    result: "1"
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_PROSE_FM = """census:
  - question: a prose entry
    command: echo hello
    result: hello there
prime_exit_criterion:
  statement: the op reports per class
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

A = "docs/plans/2026-09-07-a.md"
B = "docs/plans/2026-09-07-b.md"


@pytest.fixture(autouse=True)
def _fresh():
    mod._CORPUS_CACHE.clear()
    mod._CENSUS_MEMO.clear()
    yield
    mod._CORPUS_CACHE.clear()
    mod._CENSUS_MEMO.clear()


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "run", counting)
    return calls


def _two_plans(tmp_path, fm=_FM):
    common = _repo(tmp_path)
    _plan(tmp_path, frontmatter=fm, spine=_SPINE, name=A)
    _plan(tmp_path, frontmatter=fm, spine=_SPINE, name=B)
    return common


def _p(plan, **extra):
    return {"plan": plan, "check_census": True, **extra}


def test_one_run_token_spawns_a_shared_command_once(tmp_path, spawns):
    common = _two_plans(tmp_path)
    _probe(_p(A, census_run="r1"), common)
    _probe(_p(B, census_run="r1"), common)
    assert len(spawns) == 1


def test_no_token_means_no_memo(tmp_path, spawns):
    common = _two_plans(tmp_path)
    _probe(_p(A), common)
    _probe(_p(B), common)
    assert len(spawns) == 2


def test_a_new_run_token_spawns_again(tmp_path, spawns):
    common = _two_plans(tmp_path)
    _probe(_p(A, census_run="r1"), common)
    _probe(_p(B, census_run="r2"), common)
    assert len(spawns) == 2


def test_a_changed_head_with_the_same_token_spawns_again(tmp_path, spawns):
    common = _two_plans(tmp_path)
    _probe(_p(A, census_run="r1"), common)
    (common / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    _probe(_p(B, census_run="r1"), common)
    assert len(spawns) == 2


def test_a_prose_entry_is_not_spawned(tmp_path, spawns):
    common = _two_plans(tmp_path, _PROSE_FM)
    reply = _probe(_p(A, census_run="r1"), common)
    assert spawns == []
    assert reply["census"]["entries"][0]["state"] == "UNDECIDABLE"
    assert "cannot drift" in reply["census"]["entries"][0]["basis"]


@pytest.mark.parametrize("bad", ["", "x" * 65, 7, ["a"], True])
def test_a_bad_census_run_raises(tmp_path, bad):
    common = _two_plans(tmp_path)
    with pytest.raises(ValueError, match="census_run"):
        _probe(_p(A, census_run=bad), common)


@pytest.mark.parametrize("census", [True, False])
def test_census_spent_ms_is_an_int_on_every_reply(tmp_path, census):
    common = _two_plans(tmp_path)
    reply = _probe({"plan": A, "check_census": census}, common)
    assert isinstance(reply["census_spent_ms"], int) and not isinstance(
        reply["census_spent_ms"], bool
    )
    assert reply["census_spent_ms"] >= 0
    if not census:
        assert reply["census_spent_ms"] == 0


def test_the_deadline_passed_is_the_ops_own(tmp_path, monkeypatch):
    common = _two_plans(tmp_path)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 8.0)
    seen: dict = {}

    class Stub:
        def revalidate(self, plan, root, timeout, **kw):
            seen.update(kw)
            return {"state": "MATCH", "entries": [], "spent_ms": 7}

    monkeypatch.setattr(mod, "_revalidator", lambda: Stub())
    before = time.monotonic()
    reply = _probe(_p(A, census_run="r1"), common)
    assert seen["deadline"] - before == pytest.approx(
        8.0 * mod._CENSUS_DEADLINE_SHARE, abs=0.5
    )
    assert seen["decidable_only"] is True and isinstance(seen["memo"], dict)
    assert reply["census_spent_ms"] == 7
