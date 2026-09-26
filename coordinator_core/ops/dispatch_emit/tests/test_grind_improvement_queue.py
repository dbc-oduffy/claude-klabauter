"""
IBMDT-C22 item 18: re-verify what `queue_select`/`grind_compose` already do
for `--queue state/improvement-queue`, over the SAME generic mechanisms
every other queue grinds through -- no improvement-queue-specific code
path. Covers batching by `change_kind` and the profile's own
`hand_back_types` channel carrying the four queue-terminus outcome classes
(docs/wiki/queue-terminus-doctrine.md) plus `route-to-learn-lessons`.

Item 18's third leg -- "one post-batch index regeneration" -- is NOT
exercised here: the only index-regenerate hook this repo's composer carries
(`grind_stages.compose_commit_call`'s `regenerate_op`) fires per ROW, on the
commit stage, not once per batch, and `grind_stages.py` sits outside this
row's footprint. Wiring a batch-level regeneration would mean either
threading a new parameter through a file this row may not touch, or adding
a new batch-end call site -- exactly the "new stage" the row's own body
names as the spin-out trigger. Left unimplemented; reported, not silently
dropped.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

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
# Item 17.6: `_commit_row` (and `_close_batch`'s own close-confirm commit)
# route a schema-refused close to `needs-judgment`, not `commit-failed`.
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

_SCHEMA_REFUSED_REASON = (
    "grind-row close: schema validation failed: {'ok': False, 'errors': [...]}"
)


def _run_close_confirm(commit_result: dict):
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return [{"row": "r0", "verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""}]
        if stage_kind == "refute-close":
            return {"confirmed": [{"row": "r0", "new_path": "archive/2026-09/r0.yaml"}], "refuted": []}
        if stage_kind == "commit":
            return commit_result
        return None

    return gc.run_admission(
        [("b0", ["r0"])], _CLOSE_CONFIRM_ROUTING, "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent_fn,
    )


def test_close_batch_routes_a_schema_refused_close_commit_to_needs_judgment():
    result = _run_close_confirm({"outcome": "commit-failed", "reason": _SCHEMA_REFUSED_REASON})
    types_by_row = {(h["row"], h["type"]) for h in result["handed_back"]}
    assert ("r0", "solo-baton") in types_by_row
    assert ("r0", "needs-judgment") in types_by_row
    assert ("r0", "commit-failed") not in types_by_row


def test_close_batch_still_routes_an_ordinary_commit_failure_to_commit_failed():
    result = _run_close_confirm({"outcome": "commit-failed", "reason": "git push rejected: non-fast-forward"})
    types_by_row = {(h["row"], h["type"]) for h in result["handed_back"]}
    assert ("r0", "commit-failed") in types_by_row
    assert not any(t == "needs-judgment" for _, t in types_by_row)


_GENERIC_COMMIT_ROUTING = {
    "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix"}, "on_fail": None},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "commit": {"kind": "commit", "edges": {}, "on_fail": None},
}


def _run_generic_commit(commit_result: dict):
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
            return commit_result
        return None

    return gc.run_admission(
        [("b0", ["r0"])], _GENERIC_COMMIT_ROUTING, "triage",
        reserve=gc.batch_reserve(1), budget=budget, agent=agent_fn,
    )


def test_commit_row_routes_a_schema_refused_close_to_needs_judgment():
    result = _run_generic_commit({"outcome": "commit-failed", "reason": _SCHEMA_REFUSED_REASON})
    assert any(h["row"] == "r0" and h["type"] == "needs-judgment" for h in result["handed_back"])
    assert not any(h["type"] == "commit-failed" for h in result["handed_back"])


def test_commit_row_still_routes_an_ordinary_commit_failure_to_commit_failed():
    result = _run_generic_commit(
        {"outcome": "commit-failed", "reason": "git push rejected: non-fast-forward"}
    )
    assert any(h["row"] == "r0" and h["type"] == "commit-failed" for h in result["handed_back"])
    assert not any(h["type"] == "needs-judgment" for h in result["handed_back"])


def test_commit_result_is_schema_refused_helper():
    assert gc._commit_result_is_schema_refused({"reason": _SCHEMA_REFUSED_REASON})
    assert not gc._commit_result_is_schema_refused({"reason": "git push rejected"})
    assert not gc._commit_result_is_schema_refused({})
