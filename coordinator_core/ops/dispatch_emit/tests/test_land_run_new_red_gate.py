"""Falsifier: every landing runs its touched python paths' tests (plus direct importers) in the shared-clone mirror
at the post-land tree and at the pre-land tip, refusing on a node id red post-land and green at the tip, and
landing when that node was already red at the tip; driven through the default `land_run._handler` with no peer
dirt and no measured run."""

from __future__ import annotations

import pytest

from coordinator_core.ops import test_leg_run
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import LANE_TEST, _VERDICT
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

NODE = f"{LANE_TEST}::test_lane"
_GREEN = "def test_lane():\n    pass\n"
_RED_1 = "def test_lane():\n    assert 1 == 2\n"
_RED_2 = "def test_lane():\n    assert 2 == 3\n"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    """The persistent mirror lives under the system Temp's coordinator root; every test gets its own."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _commit_at_head(root, body: str) -> None:
    _write(root, LANE_TEST, body)
    _git(root, "add", LANE_TEST)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "test at tip", "--", LANE_TEST)


def _land(root, body: str) -> dict:
    resume, token = _stage(root)
    _write(root, LANE_TEST, body)
    return _terminal_with(root, token, resume, _VERDICT)


def test_a_landing_whose_test_is_red_post_land_and_green_at_the_tip_refuses_naming_the_node(halted):  # noqa: F811
    _commit_at_head(halted, _GREEN)
    tip = _git(halted, "rev-parse", "HEAD").strip()

    out = _land(halted, _RED_1)

    assert out["committed"] is False, out
    assert out["refused"] == "new-red", out
    assert out["new_red"] == [NODE], out
    assert NODE in out["error"] and "re-land" in out["error"], out
    assert _git(halted, "rev-parse", "HEAD").strip() == tip


def test_a_landing_whose_test_is_already_red_at_the_tip_lands(halted):  # noqa: F811
    _commit_at_head(halted, _RED_1)

    out = _land(halted, _RED_2)

    assert out["committed"] is True, out
    assert out["new_red_gate"]["new_red"] == [], out
    assert out["new_red_gate"]["pre_existing"] == [NODE], out


def test_a_landing_whose_tests_are_green_lands_and_reports_the_gate(halted):  # noqa: F811
    _commit_at_head(halted, _RED_1)

    out = _land(halted, _GREEN)

    assert out["committed"] is True, out
    assert out["new_red_gate"]["new_red"] == [] and out["new_red_gate"]["pre_existing"] == [], out


def test_an_importer_test_red_post_land_and_green_at_the_tip_refuses(halted):  # noqa: F811
    _write(halted, "a.py", "value = 1\n")
    _write(halted, "y/test_uses_a.py", "import a\n\n\ndef test_value():\n    assert a.value == 1\n")
    _git(halted, "add", "a.py", "y/test_uses_a.py")
    _git(halted, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "importer pair", "--", "a.py", "y/test_uses_a.py")
    resume, token = _stage(halted)
    _write(halted, "a.py", "value = 2\n")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False, out
    assert out["refused"] == "new-red", out
    assert "y/test_uses_a.py::test_value" in out["new_red"], out


def test_an_unmeasurable_gate_lands_with_an_advisory_never_a_refusal(halted, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise OSError("no clone today")

    monkeypatch.setattr(test_leg_run, "run", boom)
    _commit_at_head(halted, _GREEN)

    out = _land(halted, _RED_1)

    assert out["committed"] is True, out
    assert "no clone today" in out["new_red_gate"]["advisory"], out


def test_on_the_warm_request_path_the_gate_never_runs_and_the_landing_proceeds(halted, monkeypatch):  # noqa: F811
    """The warm caller abandons a land at its op budget; a clone test leg inside it halted every run at commit."""
    def never(*a, **k):
        raise AssertionError("clone test leg ran on the warm land path")

    monkeypatch.setattr(test_leg_run, "run", never)
    monkeypatch.setenv("COORDINATOR_EXECUTION_ROUTE", "warm_server")
    _commit_at_head(halted, _GREEN)

    out = _land(halted, _RED_1)

    assert out["committed"] is True, out
    assert "warm land budget" in out["new_red_gate"]["advisory"], out
