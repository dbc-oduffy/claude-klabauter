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
