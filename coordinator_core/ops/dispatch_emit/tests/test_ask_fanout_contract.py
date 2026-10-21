"""Pins the multi-ask contract: partition, after edges, budget, admit, release, paths, reply shape."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c


def _slice(label, rows=(), writes=None):
    return c.AskSlice(label=label, ask=label, rows=tuple(rows), writes=writes)


def test_partition_names_unclaimed_and_doubled_in_one_refusal():
    msg = c.ledger_partition(
        ["R1", "R2", "R3"], [_slice("a", ["R1", "R2"]), _slice("b", ["R2"])]
    )
    assert msg is not None
    assert "unclaimed rows: R3" in msg
    assert "doubly-claimed rows: R2" in msg


def test_partition_exact_is_none_and_no_row_exempt():
    assert c.ledger_partition(["R1", "R2"], [_slice("a", ["R1"]), _slice("b", ["R2"])]) is None
    assert "R2" in c.ledger_partition(["R1", "R2"], [_slice("a", ["R1"])])


def test_partition_names_row_unknown_to_parent():
    msg = c.ledger_partition(["R1"], [_slice("a", ["R1", "R9"])])
    assert msg is not None and "R9" in msg


def test_no_hint_pair_unsequenced_both_ways_of_order():
    edges = c.after_edges([_slice("a", writes=("x.py",)), _slice("b", writes=None)])
    assert edges == {0: (), 1: ()}
    edges = c.after_edges([_slice("a", writes=None), _slice("b", writes=("x.py",))])
    assert edges == {0: (), 1: ()}


def test_undeclared_slices_get_empty_edges_for_every_index():
    edges = c.after_edges([_slice(n) for n in "abc"])
    assert edges == {0: (), 1: (), 2: ()}


def test_undeclared_slice_still_follows_lower_wave():
    later = c.AskSlice(label="b", ask="b", wave=1)
    earlier = c.AskSlice(label="a", ask="a", wave=0)
    assert c.after_edges([earlier, later])[1] == (0,)


def test_disjoint_pair_unsequenced():
    edges = c.after_edges([_slice("a", writes=("a/x.py",)), _slice("b", writes=("b/y.py",))])
    assert edges == {0: (), 1: ()}


def test_overlap_by_path_and_directory_prefix():
    assert c.after_edges([_slice("a", writes=("a/x.py",)), _slice("b", writes=("a/x.py",))])[1] == (0,)
    assert c.after_edges([_slice("a", writes=("a",)), _slice("b", writes=("a/x.py",))])[1] == (0,)
    assert c.after_edges([_slice("a", writes=("ab/x.py",)), _slice("b", writes=("a/x.py",))])[1] == ()


def test_empty_writes_hint_overlaps_nothing():
    assert c.after_edges([_slice("a", writes=()), _slice("b", writes=None)])[1] == ()
    assert c.after_edges([_slice("a", writes=()), _slice("b", writes=("x",))])[1] == ()


def _waved(label, wave, writes=()):
    return c.AskSlice(label=label, ask=label, writes=writes, wave=wave)


def test_cross_wave_pair_with_disjoint_writes_gets_edge():
    edges = c.after_edges([_waved("a", 0, ("a.py",)), _waved("b", 1, ("b.py",))])
    assert edges == {0: (), 1: (0,)}


def test_same_wave_pair_with_disjoint_writes_gets_no_edge():
    edges = c.after_edges([_waved("a", 1, ("a.py",)), _waved("b", 1, ("b.py",))])
    assert edges == {0: (), 1: ()}


def test_no_wave_slices_keep_overlap_rule():
    assert c.after_edges([_slice("a", writes=("a.py",)), _slice("b", writes=("b.py",))])[1] == ()
    assert c.after_edges([_waved("a", 0, ("a.py",)), _slice("b", writes=("b.py",))])[1] == ()


@pytest.mark.parametrize("cores,expected", [(1, 1), (3, 1), (4, 1), (24, 6)])
def test_budget_from_cores(cores, expected):
    assert c.concurrent_run_budget(cores) == expected


def test_budget_reads_box_when_none(monkeypatch):
    from coordinator_core.benchmarks import concurrency_probe

    monkeypatch.setattr(concurrency_probe, "default_physical_cores", lambda: 16)
    assert c.concurrent_run_budget() == 4


def test_admit_queues_overflow_in_order_and_drops_none():
    budget = 2
    runs = tuple(c.SiblingRun(run_id=f"r{i}", label=f"l{i}") for i in range(budget + 2))
    out = c.admit(runs, budget)
    assert [r.run_id for r in out] == [r.run_id for r in runs]
    assert [r.status for r in out] == ["ready", "ready", "queued", "queued"]


def test_admit_leaves_after_runs_and_counts_running():
    runs = (
        c.SiblingRun("r0", "a", status=c.STATUS_RUNNING),
        c.SiblingRun("r1", "b", after=("r0",), status=c.STATUS_AFTER),
        c.SiblingRun("r2", "c"),
    )
    out = c.admit(runs, 1)
    assert [r.status for r in out] == ["running", "after", "queued"]


def test_release_after_landed_and_queue_slot():
    parent = c.ParentRecord(
        "p",
        (
            c.SiblingRun("r0", "a"),
            c.SiblingRun("r1", "b", after=("r0",), status=c.STATUS_AFTER),
            c.SiblingRun("r2", "c", status=c.STATUS_QUEUED),
        ),
    )
    assert c.release(parent, {"r0": "landed", "r1": "after", "r2": "queued"}, 2) == ("r1", "r2")
    assert c.release(parent, {"r0": "running", "r1": "after", "r2": "queued"}, 1) == ()
    assert c.release(parent, {"r0": "running", "r1": "after", "r2": "queued"}, 2) == ("r2",)


def test_halted_predecessor_frees_slot_but_not_successor():
    parent = c.ParentRecord(
        "p",
        (
            c.SiblingRun("r0", "a"),
            c.SiblingRun("r1", "b", after=("r0",), status=c.STATUS_AFTER),
            c.SiblingRun("r2", "c", status=c.STATUS_QUEUED),
        ),
    )
    assert c.release(parent, {"r0": "halted", "r1": "after", "r2": "queued"}, 1) == ("r2",)


def test_budget_plus_two_asks_yield_two_queued_no_refusal():
    budget = c.concurrent_run_budget(8)
    slices = [_slice(f"s{i}", writes=(f"d{i}/f",)) for i in range(budget + 2)]
    edges = c.after_edges(slices)
    assert all(not e for e in edges.values())
    runs = tuple(c.SiblingRun(f"r{i}", s.label) for i, s in enumerate(slices))
    out = c.admit(runs, budget)
    assert sum(r.status == "queued" for r in out) == 2
    assert len(out) == len(slices)


def test_reply_json_omits_empty_after():
    reply = c.FanoutReply(
        "ask-parent-1",
        (c.SiblingRun("r0", "a"), c.SiblingRun("r1", "b", after=("r0",))),
    )
    assert reply.to_json() == {
        "parent_id": "ask-parent-1",
        "runs": [{"run_id": "r0", "label": "a"}, {"run_id": "r1", "label": "b", "after": ["r0"]}],
    }


def test_paths_and_marker_roundtrip():
    assert c.parent_record_rel("P") == "scratch/warp/P/parent.json"
    assert c.run_parent_copy_rel("R") == "state/warp/R/parent.json"
    assert c.status_rel("R") == "scratch/warp/R/sibling-status.json"
    assert c.parse_ask_parent(c.ask_parent_marker("P", "R")) == ("P", "R")
    assert c.parse_ask_parent("only-one") is None
    assert c.parse_ask_parent(None) is None
    assert c.FANOUT_OP == "dispatch.ask_fanout"


def test_parent_record_roundtrip_and_schema_guard():
    rec = c.ParentRecord("P", (c.SiblingRun("r0", "a", ("r9",), "s.yaml", "after"),), True)
    data = rec.to_json()
    assert data["schema"] == "ask-parent" and data["version"] == 1
    assert c.ParentRecord.from_json(data) == rec
    with pytest.raises(ValueError):
        c.ParentRecord.from_json({"schema": "other", "version": 1})


def test_ask_slice_source_defaults_none():
    assert c.AskSlice(label="a", ask="a").source is None
    assert c.AskSlice(label="a", ask="a", source="relayed").source == "relayed"


def test_slice_source_key_and_reader():
    assert c.SLICE_SOURCE_KEY == "ask_source"
    assert c.slice_ask_source({"em_analysis": {"ask_source": "relayed"}}) == "relayed"


@pytest.mark.parametrize(
    "sizing",
    [{}, {"em_analysis": None}, {"em_analysis": "x"}, {"em_analysis": {}},
     {"em_analysis": {"ask_source": 3}}, None, []],
)
def test_slice_ask_source_none_for_absent_or_malformed(sizing):
    assert c.slice_ask_source(sizing) is None


def test_release_skip_frees_nothing_skipped_and_leaves_slot_free():
    parent = c.ParentRecord(
        "p",
        (
            c.SiblingRun("r1", "b", status=c.STATUS_QUEUED),
            c.SiblingRun("r2", "c", status=c.STATUS_QUEUED),
        ),
    )
    statuses = {"r1": "queued", "r2": "queued"}
    assert c.release(parent, statuses, 1, frozenset({"r1"})) == ("r2",)
    assert c.release(parent, statuses, 1) == ("r1",)


def _hq_batch():
    ids = [["hq-03"], ["hq-05"], ["hq-04"], ["hq-02"], ["hq-06"], ["hq-08"]]
    blocked = [[], [], ["hq-03"], ["hq-01"], ["hq-01"], ["hq-01", "hq-03", "hq-05", "hq-06"]]
    writes = [[f"f{i}.py"] for i in range(6)]
    return ids, blocked, writes


def test_blocked_by_edges_hq_batch_edges_and_holds():
    edges, holds = c.blocked_by_edges(*_hq_batch())
    assert edges[0] == () and edges[1] == ()
    assert edges[2] == (0,)
    assert set(holds) == {3, 4, 5}
    assert "hq-01" in holds[3] and "not in this fan-out" in holds[3]
    assert "hq-01" in holds[4]
    assert holds[5] == "blocked_by hq-01, which is not in this fan-out"
    assert edges[3] == edges[4] == edges[5] == ()


def test_blocked_by_edges_held_blocker_listed_first_reason_names_held():
    edges, holds = c.blocked_by_edges(
        [["a"], ["b"]], [["gone"], ["a"]], [["x"], ["y"]]
    )
    assert holds == {
        0: "blocked_by gone, which is not in this fan-out",
        1: "blocked_by a, which is held",
    }
    edges, holds = c.blocked_by_edges(
        [["a"], ["b"], ["c"]], [["gone"], ["a"], ["a", "gone"]], [["x"], ["y"], ["z"]]
    )
    assert holds[2] == "blocked_by a, which is held"


def test_blocked_by_edges_follows_later_submitted_blocker_and_wave_free():
    edges, holds = c.blocked_by_edges([["a"], ["b"]], [["b"], []], [["x"], ["y"]])
    assert edges == {0: (1,), 1: ()} and holds == {}


def test_blocked_by_edges_writes_overlap_oriented_along_topological_order():
    edges, _ = c.blocked_by_edges([["a"], ["b"]], [["b"], []], [["x"], ["x"]])
    assert edges == {0: (1,), 1: ()}
    edges, _ = c.blocked_by_edges([["a"], ["b"], ["c"]], [[], [], []], [["d"], ["d/e"], ["z"]])
    assert edges == {0: (), 1: (0,), 2: ()}


def test_blocked_by_edges_no_hint_overlaps_and_ordered_pair_adds_no_redundant_edge():
    edges, _ = c.blocked_by_edges([["a"], ["b"]], [[], []], [None, ["y"]])
    assert edges == {0: (), 1: (0,)}
    edges, _ = c.blocked_by_edges([["a"], ["b"]], [[], ["a"]], [["x"], ["x"]])
    assert edges == {0: (), 1: (0,)}


def test_blocked_by_edges_overlap_with_held_item_adds_no_edge():
    edges, holds = c.blocked_by_edges([["a"], ["b"]], [["gone"], []], [["x"], ["x"]])
    assert edges == {0: (), 1: ()} and set(holds) == {0}


@pytest.mark.parametrize(
    "ids,blocked",
    [
        ([["a"], ["a"]], [[], []]),
        ([["a"], ["b", "a"]], [[], []]),
        ([["a"], ["b"]], [["a"], []]),
        ([["a", "a2"], ["b"]], [["a2"], []]),
        ([["a"], ["b"]], [["b"], ["a"]]),
        ([["a"], ["b"], ["c"]], [["c"], ["a"], ["b"]]),
    ],
)
def test_blocked_by_edges_bad_input_raises_value_error(ids, blocked):
    with pytest.raises(ValueError):
        c.blocked_by_edges(ids, blocked, [["x"]] * len(ids))


def test_held_run_parent_record_round_trips_and_non_held_json_is_unchanged():
    held = c.SiblingRun("r1", "a", (), "s1", c.STATUS_HELD, "blocked_by q, which is not in this fan-out")
    plain = c.SiblingRun("r2", "b", ("r1",), "s2", c.STATUS_AFTER)
    rec = c.ParentRecord("p", (held, plain))
    data = rec.to_json()
    assert data["runs"][0]["hold_reason"] == held.hold_reason
    assert data["runs"][1] == {
        "run_id": "r2", "label": "b", "after": ["r1"], "slice_sizing": "s2", "status": "after",
    }
    assert c.ParentRecord.from_json(data) == rec
    assert c.SiblingRun("r", "l").hold_reason == ""
    assert c.SiblingRun("r", "l", (), "s", "ready").hold_reason == ""


def test_held_run_is_never_released_and_keeps_reason_through_admit():
    held = c.SiblingRun("r1", "a", (), "s1", c.STATUS_HELD, "why")
    parent = c.ParentRecord("p", (held,))
    assert c.release(parent, {"r1": "held"}, 4) == ()
    assert c.admit((held,), 4) == (held,)
    assert c.STATUS_HELD in c.RUN_STATUSES


def test_held_edge_mode_names():
    assert (c.EDGES_FROM_PARAM, c.EDGES_FROM_BLOCKED_BY, c.ITEM_IDS_KEY, c.ITEM_BLOCKED_BY_KEY, c.HELD_KEY) == (
        "edges_from", "blocked_by", "ids", "blocked_by", "held",
    )


def test_not_fired_names_and_vocabulary():
    assert (c.INTERACTION_MODE_PARAM, c.PARENT_SIZING_KEY, c.NOT_FIRED_KEY) == (
        "interaction_mode",
        "parent_sizing",
        "not_fired",
    )
    assert len(c.NOT_FIRED_REASONS) == 15 and len(set(c.NOT_FIRED_REASONS)) == 15
    assert "held" in c.NOT_FIRED_REASONS
    assert [k for k, _ in c.PLAN_BLITZ_UNFIRED_BUCKETS] == [
        "pulled", "replan", "errored", "surfacedToPm", "adjudicated", "xsRuns", "routedElsewhere",
    ]
    assert all(reason in c.NOT_FIRED_REASONS for _, reason in c.PLAN_BLITZ_UNFIRED_BUCKETS)


def test_not_fired_entry_drops_none_and_refuses_unknown_reason():
    assert c.not_fired_entry(reason="after", plan="p.md", detail="waits on r1") == {
        "plan": "p.md",
        "reason": "after",
        "detail": "waits on r1",
    }
    assert c.not_fired_entry(reason="wave-not-run", baton="b.md", wave=2) == {
        "reason": "wave-not-run",
        "wave": 2,
        "baton": "b.md",
    }
    with pytest.raises(ValueError):
        c.not_fired_entry(reason="bogus", plan="p.md")


def test_not_fired_refusal_accepts_valid_and_names_first_bad_index():
    good = [c.not_fired_entry(reason="queued", plan="p.md"), c.not_fired_entry(reason="pulled", baton="b")]
    assert c.not_fired_refusal(good) is None
    assert c.not_fired_refusal([]) is None
    assert "[1]" in c.not_fired_refusal([good[0], "x"])
    assert "[1]" in c.not_fired_refusal([good[0], {"reason": "nope", "plan": "p"}])
    assert "[0]" in c.not_fired_refusal([{"reason": "queued"}])
    assert "[0]" in c.not_fired_refusal([{"reason": "queued", "plan": ""}])
    assert c.not_fired_refusal("x") is not None


def test_asks_item_keys_pinned():
    assert c.ASKS_ITEM_KEYS == (
        "ask", "source", "sender", "relayed_text", "route_preference",
        "em_rationale", "em_context", "context", "label", "writes",
    )


def test_unknown_keys_refusal_names_key_and_every_accepted_key():
    msg = c.unknown_keys_refusal("asks[1]", ["ask", "contxt"], c.ASKS_ITEM_KEYS)
    assert msg is not None and msg.startswith("asks[1]: unknown key 'contxt'")
    assert all(k in msg for k in c.ASKS_ITEM_KEYS)
    assert msg.endswith(", ".join(c.ASKS_ITEM_KEYS))


def test_unknown_keys_refusal_none_for_subset_and_names_first_unknown():
    assert c.unknown_keys_refusal("asks[0]", ["ask", "context"], c.ASKS_ITEM_KEYS) is None
    assert c.unknown_keys_refusal("asks[0]", [], c.ASKS_ITEM_KEYS) is None
    msg = c.unknown_keys_refusal("args", iter(["zz", "yy"]), ("a",))
    assert "'zz'" in msg and "'yy'" not in msg


def test_superseded_marker_path_and_reader(tmp_path):
    assert c.superseded_rel("r") == "scratch/warp/r/superseded.json"
    assert c.read_superseded_by(tmp_path, "r") is None
    path = tmp_path / c.superseded_rel("r")
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    assert c.read_superseded_by(tmp_path, "r") is None
    path.write_text('{"superseded_by": "n"}', encoding="utf-8")
    assert c.read_superseded_by(tmp_path, "r") == "n"
    assert (c.CLOSED_BY_KEY, c.SUPERSEDED_BY_KEY) == ("closed_by", "superseded_by")
