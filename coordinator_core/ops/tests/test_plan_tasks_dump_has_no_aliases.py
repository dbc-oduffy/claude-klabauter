from coordinator_core.ops.plan_tasks_mutate import _dump_rows


def test_rows_sharing_a_list_dump_without_anchors():
    shared = [{"chunk": "C0", "gate_kind": "independent"}]
    out = _dump_rows([{"id": "C1", "depends_on": shared}, {"id": "C2", "depends_on": shared}])
    assert "&id" not in out and "*id" not in out
    assert out.count("chunk: C0") == 2
