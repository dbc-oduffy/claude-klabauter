"""
IBMDT-C22 item 18: re-verify what `queue_select`/`grind_compose` already do
for `--queue state/improvement-queue`, over the SAME generic mechanisms
every other queue grinds through -- no improvement-queue-specific code
path. Covers batching by `change_kind` and the profile's own
`hand_back_types` channel carrying the four queue-terminus outcome classes
(coordinator-content-repo coordinator/docs/wiki/ceremony-calibration/queue-terminus-doctrine.md) plus `route-to-learn-lessons`.

Item 18's third leg -- "one post-batch index regeneration" -- is NOT
exercised here: the grind commits once per run, through
`dispatch.land_run`, with no per-row commit stage left to carry an
index-regenerate hook, and no batch-end call site exists for one. Left
unimplemented; reported, not silently dropped.

The commit expectations are the terminal shape: a finished row queues, one
`commit` call at the end lands the queue, and a refused or failed commit hands
every queued row back `commit-failed` naming the runnable `settle-run` recovery.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

from .conftest import REVIEW_KW

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"

#: The four queue-terminus outcome classes (docs/wiki/queue-terminus-
#: doctrine.md) plus the improvement-queue's own `route-to-learn-lessons`
#: hand-back, all on the profile's existing generic `hand_back_types`
#: channel -- never a new stage.
_TERMINUS_HAND_BACK_TYPES = (
    "solo-baton",
    "themed-baton",
    "immediate-dispatch",
    "close-or-park",
    "route-to-learn-lessons",
)


def _load_fixture_doc() -> dict:
    text = (_FIXTURE_DIR / "fixture.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _profile_from_doc(tmp_path: Path, name: str, doc: dict) -> gp.Profile:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return gp.load_profile(name, tmp_path)


def test_improvement_queue_batches_by_change_kind(tmp_path: Path) -> None:
    """`batch_key: [change_kind]` groups the manifest by `change_kind`
    through `grind_compose._group_into_batches` -- the same generic
    coalesce every other queue profile uses, no improvement-queue branch."""
    doc = _load_fixture_doc()
    doc["batch_key"] = ["change_kind"]
    profile = _profile_from_doc(tmp_path, "improvement-queue-fixture-batch", doc)
    assert profile.batch_key == ("change_kind",)

    manifest = Manifest(
        entries=(
            ManifestEntry(row_id="r0", path="state/improvement-queue/r0.yaml", digest="d0", batch_key="code-edit"),
            ManifestEntry(row_id="r1", path="state/improvement-queue/r1.yaml", digest="d1", batch_key="code-edit"),
            ManifestEntry(row_id="r2", path="state/improvement-queue/r2.yaml", digest="d2", batch_key="doc-edit"),
        ),
        batch_sizes={},
        source=None,
        digest="m",
    )
    grouped = gc._group_into_batches(manifest, {"batch_size": 4})
    by_key: dict[str, list[str]] = {}
    for batch_id, entries in grouped:
        key = batch_id.rsplit("-b", 1)[0]
        by_key.setdefault(key, []).extend(e.row_id for e in entries)
    assert by_key == {"code-edit": ["r0", "r1"], "doc-edit": ["r2"]}


def test_hand_back_types_carry_the_four_terminus_classes_plus_route_to_learn_lessons(
    tmp_path: Path,
) -> None:
    """The profile's `hand_back_types` list -- the existing hand-back
    channel `route_after_triage`/`follow_edge` already hand back through --
    accepts every terminus class plus `route-to-learn-lessons` with no code
    change: they are ordinary declared values, disjoint from graph node
    ids."""
    doc = _load_fixture_doc()
    doc["hand_back_types"] = list(_TERMINUS_HAND_BACK_TYPES)
    profile = _profile_from_doc(tmp_path, "improvement-queue-fixture-handback", doc)
    assert set(profile.hand_back_types) == set(_TERMINUS_HAND_BACK_TYPES)


def test_hand_back_types_still_refuse_a_name_colliding_with_a_graph_node(
    tmp_path: Path,
) -> None:
    """Sanity check on the channel itself, not improvement-queue-specific:
    a hand-back type may never shadow a graph node id (DR-404 § 3)."""
    doc = _load_fixture_doc()
    doc["hand_back_types"] = ["fix"]
    with pytest.raises(gp.ProfileError) as exc_info:
        _profile_from_doc(tmp_path, "improvement-queue-fixture-collision", doc)
    assert exc_info.value.rule == "hand_back_type_collision"


# ---------------------------------------------------------------------------
# One terminal commit for every profile: the improvement profile composes the
# same single terminal stage as the fixture profile, with no `@commit` lock and
# no branch on the profile name.
# ---------------------------------------------------------------------------


def _compose_for(profile: gp.Profile) -> str:
    knobs = gp.resolve_appetite(profile, "standard")
    manifest = Manifest(
        entries=(ManifestEntry(row_id="r0", path="state/improvement-queue/r0.yaml", digest="d0", batch_key="code-edit"),),
        batch_sizes={},
        source=None,
        digest="m",
    )
    return gc.compose_grind_script(
        manifest, profile, knobs,
        run_dir=Path("state/queue-grind/improvement/run-1"),
        agent_type_host=None,
        queue_dirs=["state/improvement-queue"],
        **REVIEW_KW,
    )


def test_fixture_and_improvement_profiles_each_compose_one_terminal_commit_and_no_commit_lock(
    tmp_path: Path,
) -> None:
    doc = _load_fixture_doc()
    doc["batch_key"] = ["change_kind"]
    doc["hand_back_types"] = list(_TERMINUS_HAND_BACK_TYPES)
    improvement = _profile_from_doc(tmp_path, "improvement-queue-fixture-terminal", doc)
    fixture = gp.load_profile("fixture", _FIXTURE_DIR)
    for profile in (fixture, improvement):
        script = _compose_for(profile)
        assert "@commit" not in script
        assert script.count("coordinator-invoke dispatch.land_run") == 1
        assert script.count("async function _terminalCommit()") == 1
        assert f"const PROFILE_NAME = '{profile.name}';" in script


# ---------------------------------------------------------------------------
# Terminal commit outcomes through the pure-Python twin of the composed script.
# ---------------------------------------------------------------------------


class _StubBudgetSpender:
    def __init__(self, total=None):
        self._spent = 0
        self.total = total

    def spend(self, amount):
        self._spent += amount

    def spent(self):
        return self._spent

    def remaining(self):
        return None if self.total is None else self.total - self._spent


_CLOSE_CONFIRM_ROUTING = {
    "triage": {"kind": "triage", "edges": {"not-reproduced": "refute_close"}, "on_fail": None},
    "refute_close": {
        "kind": "refute-close",
        "edges": {"confirmed": "solo-baton", "refuted": "needs-judgment"},
        "on_fail": None,
    },
}

_REFUSAL = "block-subagent-commit: no readable pathspec"


def _run_close_confirm(commit_reply: dict):
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return [{"row": "r0", "verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""}]
        if stage_kind == "refute-close":
            return {"confirmed": [{"row": "r0", "new_path": "archive/2026-09/r0.yaml"}], "refuted": []}
        if stage_kind == "commit":
            return commit_reply
        return None

    return gc.run_admission(
        [("b0", ["r0"])], _CLOSE_CONFIRM_ROUTING, "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent_fn,
    )


def test_close_batch_queues_the_archive_move_and_lands_it_with_the_terminal_commit():
    result = _run_close_confirm({"committed": True, "sha": "c0ffee"})
    types_by_row = {(h["row"], h["type"]) for h in result["handed_back"]}
    assert ("r0", "solo-baton") in types_by_row
    assert ("r0", "commit-failed") not in types_by_row
    assert {"row": "r0", "outcome": "committed", "sha": "c0ffee"} in result["settled"]
    queued = result["terminal_commit"]["rows"]
    assert [q["row_id"] for q in queued] == ["r0"]
    assert queued[0]["deleted_paths"] == ["r0"]
    assert "archive/2026-09/r0.yaml" in queued[0]["paths"]


def test_close_batch_refused_terminal_commit_hands_the_row_back_commit_failed_naming_settle_run():
    result = _run_close_confirm({"committed": False, "refused": _REFUSAL})
    hand_backs = {(h["row"], h["type"]): h["reason"] for h in result["handed_back"]}
    reason = hand_backs[("r0", "commit-failed")]
    assert _REFUSAL in reason
    assert "settle-run" in reason
    assert not any(t == "needs-judgment" for _, t in hand_backs)
    assert not result["settled"]


_GENERIC_COMMIT_ROUTING = {
    "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix"}, "on_fail": None},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "commit": {"kind": "commit", "edges": {}, "on_fail": None},
}


def _run_generic_commit(commit_reply):
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
        if stage_kind == "fix":
            return {"outcome": "done"}
        if stage_kind == "verify":
            return {"outcome": "pass"}
        if stage_kind == "commit":
            return commit_reply
        if stage_kind == "ledger-sweep":
            return {"outcome": "recorded", "swept": True}
        return None

    return gc.run_admission(
        [("b0", ["r0"])], _GENERIC_COMMIT_ROUTING, "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent_fn,
    )


def test_commit_node_queues_the_row_and_one_terminal_commit_lands_it():
    result = _run_generic_commit({"committed": True, "sha": "abc123"})
    assert [c for c in result["call_log"] if c[0] == "commit"] == [("commit", "run")]
    assert {"row": "r0", "outcome": "committed", "sha": "abc123"} in result["settled"]
    assert not result["handed_back"]


def test_commit_node_ordinary_failure_hands_back_commit_failed_with_the_error():
    result = _run_generic_commit({"committed": False, "error": "git push rejected: non-fast-forward"})
    reasons = [h["reason"] for h in result["handed_back"] if h["row"] == "r0" and h["type"] == "commit-failed"]
    assert len(reasons) == 1
    assert "git push rejected: non-fast-forward" in reasons[0]
    assert not any(h["type"] == "needs-judgment" for h in result["handed_back"])


def test_a_null_terminal_reply_hands_back_commit_failed():
    result = _run_generic_commit(None)
    assert any(h["row"] == "r0" and h["type"] == "commit-failed" for h in result["handed_back"])
    assert not result["settled"]
