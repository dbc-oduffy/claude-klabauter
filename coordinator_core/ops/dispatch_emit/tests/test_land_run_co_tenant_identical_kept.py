"""Exit test: two runs keeping byte-identical blobs for one path in the verified reach never refuse each other
(`verify-overlay-unverified` or `cohold-declared-path`) through `dispatch.land_run` at default params, whichever
run orders first: the landing run lands crediting its co-holder, and once the path is at HEAD the landing run lands
without it. Differing blobs keep the overlay refusal."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, cohold_refusal, halted_withheld_index
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.tests import test_land_run_foreign_dirt_in_reach_e2e as reach
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_false_refusals_e2e import _write_overlay
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED_TEXT = '{"a": 2, "shared": "work"}\n'
OTHER_TEXT = '{"a": 2, "other": "work"}\n'
SIZING = "state/sizings/co-tenant-peer.yaml"
_STAMP = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - 120))
# RUN2 orders by lexical run id when neither run has a token or receipt: "ask-..." < "run2" < "warp-...".
HOLDER_BEFORE = f"ask-{_STAMP}-aaaaaa"
HOLDER_AFTER = f"warp-2026-11-20-peer-bbbbbb-{_STAMP}-cccccc"
UNVERIFIED = "verify-overlay-unverified"
NEVER_REFUSED = (UNVERIFIED, cohold_refusal.REFUSED_COHOLD)


def _blob(root, text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text, capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()


def _halt(root, record, run_id: str, blob: str) -> None:
    """A halted run withholding the fixture and keeping `blob` for it."""
    record.mkdir(parents=True, exist_ok=True)
    peer = run_id != RUN2  # the landing run keeps its own sizing; only the peer sits on a sizing of its own
    (record / "halted.json").write_text(
        json.dumps({
            "status": "halted", "halted": "refusal", **({"sizing_rel": SIZING} if peer else {}),
            "run_artifacts": {"withheld": {reach.FIXTURE: "halt-record-only"}},
        }),
        encoding="utf-8",
    )
    (record / "kept.json").write_text(
        json.dumps({"chunks": {"X1": {"paths": {reach.FIXTURE: blob}}}, "plan_gate": None}), encoding="utf-8"
    )
    if peer:
        (record / "run.json").write_text(json.dumps({"run_id": run_id, "sizing_rel": SIZING}), encoding="utf-8")
        _write(root, SIZING, "status: open\n")
    halted_withheld_index.add_run(root, run_id, [reach.FIXTURE])
    (halted_withheld_index._dir(root) / halted_withheld_index.SENTINEL).write_bytes(b"")


def _two_runs(root, holder: str, holder_text: str):
    """RUN2 (the landing run) and `holder` both halted-keep the fixture; the tree holds RUN2's bytes dirty and the
    clean-overlay verdict is inconclusive, the exact shape that refused both runs."""
    reach._track_fixture(root)
    resume, token = _stage(root)
    _write(root, reach.FIXTURE, SHARED_TEXT)
    mr.measured_rev(RUN2, reach.LANDING, repo_root=root)
    _write_overlay(root, "inconclusive")
    _halt(root, ask_contract.record_dir(root, RUN2), RUN2, _blob(root, SHARED_TEXT))
    _halt(root, ask_contract.record_dir(root, holder), holder, _blob(root, holder_text))
    return resume, token


def _land(root, token, resume) -> dict:
    return _terminal_with(root, token, resume, reach._VERDICT)


@pytest.mark.parametrize("holder", [HOLDER_BEFORE, HOLDER_AFTER], ids=["holder-orders-first", "holder-orders-last"])
def test_either_order_the_first_land_credits_the_co_holder_and_the_second_lands_with_the_path_at_head(
    halted, holder,  # noqa: F811
) -> None:
    resume, token = _two_runs(halted, holder, SHARED_TEXT)
    head = _git(halted, "rev-parse", "HEAD")

    first = _land(halted, token, resume)

    assert first["committed"] is True, first
    assert first.get("refused") not in NEVER_REFUSED, first
    assert first["co_kept"] == {reach.FIXTURE: holder}, first
    assert _git(halted, "rev-parse", "HEAD") != head
    assert f"Co-kept: {holder}" in _git(halted, "log", "-1", "--format=%B")
    assert reach.FIXTURE not in _git(halted, "show", "--name-only", "--format=", first["sha"]).split()

    _git(halted, "add", reach.FIXTURE)
    _git(halted, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "co-holder lands the shared path")
    mr.measured_rev(RUN2, reach.LANDING, repo_root=halted)
    _write_overlay(halted, "inconclusive")

    second = _land(halted, token, resume)

    assert second["committed"] is True, second
    assert second.get("refused") not in NEVER_REFUSED, second
    assert reach.FIXTURE not in (second.get("reach_dirt_foreign") or {}), second
    assert _git(halted, "show", f"HEAD:{reach.FIXTURE}") == SHARED_TEXT


@pytest.mark.parametrize("holder", [HOLDER_BEFORE, HOLDER_AFTER], ids=["holder-orders-first", "holder-orders-last"])
def test_the_co_holder_landing_the_path_first_leaves_the_landing_run_unrefused(halted, holder) -> None:  # noqa: F811
    resume, token = _two_runs(halted, holder, SHARED_TEXT)
    _git(halted, "add", reach.FIXTURE)
    _git(halted, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "co-holder lands the shared path")
    mr.measured_rev(RUN2, reach.LANDING, repo_root=halted)
    _write_overlay(halted, "inconclusive")

    out = _land(halted, token, resume)

    assert out["committed"] is True, out
    assert out.get("refused") not in NEVER_REFUSED, out
    assert reach.FIXTURE not in (out.get("reach_dirt_foreign") or {}), out


@pytest.mark.parametrize("holder", [HOLDER_BEFORE, HOLDER_AFTER], ids=["holder-orders-first", "holder-orders-last"])
def test_differing_kept_blobs_keep_the_overlay_refusal(halted, holder) -> None:  # noqa: F811
    resume, token = _two_runs(halted, holder, OTHER_TEXT)
    head = _git(halted, "rev-parse", "HEAD")

    out = _land(halted, token, resume)

    assert out["committed"] is False and out["refused"] == UNVERIFIED, out
    assert "co_kept" not in out
    assert _git(halted, "rev-parse", "HEAD") == head
