"""Pins the shared post-fire predicate: fired(), is_product_path(), and the floor-guard deny."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.hooks import _em_post_fire as pf
from coordinator_core.hooks import flag_em_poll_in_flight

ROOT = str(Path(__file__).resolve().parents[3])


def _runs(monkeypatch, value):
    def fake(sid):
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(flag_em_poll_in_flight, "session_workflow_run_ids", fake)


def test_guard_name_is_a_floor_guard():
    assert pf.GUARD_NAME == "block-em-post-fire-product-write"
    assert pf.GUARD_NAME in mp.FLOOR_GUARDS


def test_fired_true_with_a_run(monkeypatch):
    _runs(monkeypatch, ["run-1"])
    assert pf.fired("s1") is True


def test_fired_false_without_a_run_or_session(monkeypatch):
    _runs(monkeypatch, [])
    assert pf.fired("s1") is False
    _runs(monkeypatch, ["run-1"])
    assert pf.fired("") is False


def test_fired_fails_open_on_exception(monkeypatch):
    _runs(monkeypatch, OSError("boom"))
    assert pf.fired("s1") is False


def test_product_path_inside_root():
    assert pf.is_product_path(ROOT + "/coordinator_core/ops/x.py", ROOT, "s1") is True


def test_bare_root_is_product():
    assert pf.is_product_path(ROOT, ROOT, "s1") is True


@pytest.mark.parametrize(
    "rel",
    ["state/handoffs/x.md", ".coordinator-local/a/b.md", "scratch/x.py"],
)
def test_non_product_paths_inside_root(rel):
    assert pf.is_product_path(ROOT + "/" + rel, ROOT, "s1") is False


def test_path_outside_root_is_not_product(tmp_path):
    assert pf.is_product_path(str(tmp_path / "x.py"), ROOT, "s1") is False
    assert pf.is_product_path(ROOT + "-sibling/x.py", ROOT, "s1") is False


def test_empty_inputs_are_not_product():
    assert pf.is_product_path("", ROOT, "s1") is False
    assert pf.is_product_path(ROOT + "/a.py", "", "s1") is False


@pytest.mark.parametrize("level", ["warn", "off", "strict"])
def test_deny_envelope_denies_at_every_level(monkeypatch, tmp_path, level):
    reg = tmp_path / "registry.toml"
    reg.write_text('"coordinator.guard_level" = "%s"\n' % level)
    monkeypatch.setattr(mp, "guard_level", lambda name: level)
    env = pf.deny_envelope()
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "workflow.revise" in env["hookSpecificOutput"]["permissionDecisionReason"]


_PLAN = """\
```yaml plan-tasks
- id: S1
  title: peer row
  writes:
    - src/peer.py
- id: S2
  title: em row
  performer: em
  writes:
    - docs/verdict.md
  depends_on:
    - chunk: S1
  disposition: open
- id: S3
  performer: em
  disposition: coded
  writes:
    - docs/closed.md
```
"""


@pytest.fixture
def em_repo(tmp_path, monkeypatch):
    from coordinator_core.hooks import _em_write_budget as wb

    monkeypatch.setattr(wb, "_is_bootstrap_or_out_of_repo", lambda p: False)
    monkeypatch.setattr(wb, "_is_scratch_or_tmp", lambda p: False)
    run = tmp_path / "state" / "warp" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"sizing_rel": "state/sizings/s.yaml"}')
    (tmp_path / "state" / "sizings").mkdir()
    (tmp_path / "state" / "sizings" / "s.yaml").write_text('plan: "docs/plans/p.md"\n')
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(_PLAN)
    _runs(monkeypatch, ["r1"])
    return tmp_path


def test_open_em_row_surface_is_not_product(em_repo):
    assert pf.is_product_path(str(em_repo / "docs" / "verdict.md"), str(em_repo), "s1") is False


@pytest.mark.parametrize("rel", ["src/peer.py", "docs/closed.md", "docs/other.md"])
def test_other_product_paths_stay_product(em_repo, rel):
    assert pf.is_product_path(str(em_repo / rel), str(em_repo), "s1") is True


def test_surface_of_an_unfired_run_stays_product(em_repo, monkeypatch):
    _runs(monkeypatch, [])
    assert pf.is_product_path(str(em_repo / "docs" / "verdict.md"), str(em_repo), "s1") is True


def test_discharged_row_surface_is_product_again(em_repo):
    (em_repo / "docs" / "plans" / "p.evidence.yaml").write_text("em_discharged:\n  S2: {text: done}\n")
    assert pf.is_product_path(str(em_repo / "docs" / "verdict.md"), str(em_repo), "s1") is True


def test_unreadable_plan_fails_toward_product(em_repo):
    (em_repo / "docs" / "plans" / "p.md").unlink()
    assert pf.is_product_path(str(em_repo / "docs" / "verdict.md"), str(em_repo), "s1") is True


def test_row_surface_lookup_is_inside_the_bar(em_repo):
    import time

    t = time.perf_counter()
    pf.is_product_path(str(em_repo / "docs" / "verdict.md"), str(em_repo), "s1")
    assert time.perf_counter() - t < 0.5
