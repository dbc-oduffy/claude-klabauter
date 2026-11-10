import json
import threading
import time

import pytest

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr


def _setup(tmp_path):
    (tmp_path / "plan.md").write_bytes(b"plan v1")
    (tmp_path / "sizing.yaml").write_bytes(b"sizing v1")
    return hrr.fingerprint_of(tmp_path, "plan.md", "sizing.yaml", "abc123")


def _record(fp):
    return hrr.build_record(
        composed_script_rel="s.py", hoisted_fragments=["f"], gate_verdict={"ok": True}, landed_state_sha="deadbeef", fingerprint=fp
    )


def test_round_trip(tmp_path):
    fp = _setup(tmp_path)
    rd = tmp_path / "run"
    hrr.write(rd, _record(fp))
    rec = hrr.read(rd, 1000)
    assert rec["composed_script_rel"] == "s.py" and rec["landed_state_sha"] == "deadbeef"
    assert hrr.matches(rec, fp)


@pytest.mark.parametrize("field", ["plan", "sizing", "head"])
def test_fingerprint_mismatch_on_each_input(tmp_path, field):
    fp = _setup(tmp_path)
    rec = _record(fp)
    if field == "plan":
        (tmp_path / "plan.md").write_bytes(b"plan v2")
    elif field == "sizing":
        (tmp_path / "sizing.yaml").write_bytes(b"sizing v2")
    now = hrr.fingerprint_of(tmp_path, "plan.md", "sizing.yaml", "other" if field == "head" else "abc123")
    assert not hrr.matches(rec, now)
    assert hrr.matches(rec, fp)


def test_none_halt_head_never_matches(tmp_path):
    fp = hrr.fingerprint_of(tmp_path, None, None, None)
    assert not hrr.matches(_record(fp), fp)


def test_record_built_before_a_revise_is_not_served_after_it(tmp_path):
    import json

    from coordinator_core.ops.dispatch_emit import halted_run

    (tmp_path / "plan.md").write_bytes(b"p")
    (tmp_path / "sizing.yaml").write_bytes(b"s")
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": None}))
    stored = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md", "land1")
    rec = hrr.build_record(
        composed_script_rel="s", hoisted_fragments={}, gate_verdict={}, landed_state_sha="land1",
        fingerprint=stored, resume_plan_rel="plan.md",
    )
    assert hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))
    assert halted_run.mark_revised(rd, "2026-10-29T10:00:00Z")
    after = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md")
    assert after["revised_at"] == "2026-10-29T10:00:00Z"
    assert not hrr.matches(rec, after)
    rebuilt = hrr.build_record(
        composed_script_rel="s", hoisted_fragments={}, gate_verdict={}, landed_state_sha="land1",
        fingerprint=hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md", "land1"), resume_plan_rel="plan.md",
    )
    assert hrr.matches(rebuilt, after)


def test_changed_resume_plan_resolution_with_same_bytes_mismatches(tmp_path):
    import json

    (tmp_path / "plan.md").write_bytes(b"same")
    (tmp_path / "other.md").write_bytes(b"same")
    (tmp_path / "sizing.yaml").write_bytes(b"s")
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": None}))
    stored = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md", "land1")
    rec = hrr.build_record(
        composed_script_rel="s", hoisted_fragments={}, gate_verdict={}, landed_state_sha="land1",
        fingerprint=stored, resume_plan_rel="plan.md",
    )
    assert hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))
    assert not hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "other.md"))
    assert not hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md", "land2"))
    assert hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md", "land1"))
    rec["landed_state_sha"] = "tampered"
    assert not hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))


def test_resolver_follows_sizing_edge_and_a_changed_resolution_is_not_served(tmp_path):
    import json

    (tmp_path / "a.md").write_bytes(b"a")
    (tmp_path / "b.md").write_bytes(b"b")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = tmp_path / "state" / "sizings" / "z.yaml"
    sizing.write_text("plan: a.md\n", encoding="utf-8")
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": "m.md"}))
    plan = hrr.resolve_fingerprint_plan(tmp_path, rd, "state/sizings/z.yaml")
    assert plan == "a.md"
    rec = _record(hrr.fingerprint_for_run(tmp_path, rd, "state/sizings/z.yaml", plan))
    rec["resume_plan_rel"] = plan
    assert hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "state/sizings/z.yaml", hrr.resolve_fingerprint_plan(tmp_path, rd, "state/sizings/z.yaml")))
    sizing.write_text("plan: b.md\n", encoding="utf-8")
    now_plan = hrr.resolve_fingerprint_plan(tmp_path, rd, "state/sizings/z.yaml")
    assert now_plan == "b.md"
    assert not hrr.matches(rec, hrr.fingerprint_for_run(tmp_path, rd, "state/sizings/z.yaml", now_plan))


def test_in_backoff_honours_the_window():
    assert hrr.in_backoff({"retry_not_before": 100.0}, 99.0)
    assert not hrr.in_backoff({"retry_not_before": 100.0}, 100.0)
    assert not hrr.in_backoff({"retry_not_before": None}, 0.0)
    assert not hrr.in_backoff({}, 0.0)


def test_missing_and_corrupt(tmp_path):
    assert hrr.read(tmp_path, 1000) is hrr.MISSING
    (tmp_path / hrr.RESUME_FILE).write_text("{not json")
    assert hrr.read(tmp_path, 1000) is hrr.MISSING
    (tmp_path / hrr.RESUME_FILE).write_text('{"schema": "x"}')
    assert hrr.read(tmp_path, 1000) is hrr.MISSING


def test_stalled_read_returns_sentinel_without_raising_and_one_in_flight(tmp_path, monkeypatch):
    (tmp_path / "f.bin").write_bytes(b"x")
    gate = threading.Event()
    monkeypatch.setattr(hrr, "_read_file", lambda p: (gate.wait(5), b"x")[1])
    t0 = time.perf_counter()
    assert hrr.bounded_read(tmp_path / "f.bin", 50) is hrr.STALLED
    assert time.perf_counter() - t0 < 1
    t1 = time.perf_counter()
    assert hrr.bounded_read(tmp_path / "f.bin", 5000) is hrr.STALLED  # outstanding read: immediate
    assert time.perf_counter() - t1 < 0.5
    gate.set()
    for _ in range(100):
        if hrr.bounded_read(tmp_path / "f.bin", 1000) == b"x":
            break
        time.sleep(0.02)
    else:
        pytest.fail("in-flight slot never freed")


def test_stalled_record_read(tmp_path, monkeypatch):
    fp = _setup(tmp_path)
    hrr.write(tmp_path / "run", _record(fp))
    gate = threading.Event()
    monkeypatch.setattr(hrr, "_read_file", lambda p: (gate.wait(5), b"")[1])
    assert hrr.read(tmp_path / "run", 30) is hrr.STALLED
    gate.set()
    time.sleep(0.1)


def test_fingerprint_for_run_is_stable_when_head_advances(tmp_path):
    import json

    (tmp_path / "plan.md").write_bytes(b"plan v1")
    (tmp_path / "sizing.yaml").write_bytes(b"sizing v1")
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": None}))
    producer = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md")
    record = hrr.build_record(
        composed_script_rel=None, hoisted_fragments={}, gate_verdict={}, landed_state_sha=None,
        fingerprint=producer, resume_plan_rel="plan.md",
    )
    consumer = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", record["resume_plan_rel"])
    assert producer == consumer and producer["halt_head"] == "a" * 40
    assert hrr.matches(record, consumer)


def _lineage_run(root, run_id, halted="criterion", **links):
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN, record_dir, write_stop_after_marker

    rd = record_dir(root, run_id)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps(links))
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "halted": halted, "run_base_sha": "a" * 40}))
    return rd, lambda: write_stop_after_marker(rd, STOP_AFTER_PLAN)


def test_inherited_stop_after_follows_the_ancestor_chain_and_ends_at_a_discharged_park(tmp_path):
    parent, mark = _lineage_run(tmp_path, "ask-parent")
    mark()
    child, _ = _lineage_run(tmp_path, "warp-child", chain_parent="ask-parent")
    assert hrr.inherited_stop_after(tmp_path, child) == "plan"
    parked, _ = _lineage_run(tmp_path, "warp-parked", halted="parked", chain_parent="ask-parent")
    assert hrr.inherited_stop_after(tmp_path, parked) is None
    free, _ = _lineage_run(tmp_path, "warp-free")
    assert hrr.inherited_stop_after(tmp_path, free) is None


def test_a_record_built_before_the_lineage_gained_a_park_is_not_served_after_it(tmp_path):
    (tmp_path / "plan.md").write_bytes(b"plan v1")
    (tmp_path / "sizing.yaml").write_bytes(b"sizing v1")
    parent, mark = _lineage_run(tmp_path, "ask-parent")
    child, _ = _lineage_run(tmp_path, "warp-child", chain_parent="ask-parent")
    before = hrr.fingerprint_for_run(tmp_path, child, "sizing.yaml", "plan.md")
    record = hrr.build_record(
        composed_script_rel=None, hoisted_fragments={}, gate_verdict={}, landed_state_sha=None,
        fingerprint=before, resume_plan_rel="plan.md", stop_after=before["stop_after"],
    )
    assert record["stop_after"] is None
    assert hrr.matches(record, hrr.fingerprint_for_run(tmp_path, child, "sizing.yaml", "plan.md"))
    mark()
    after = hrr.fingerprint_for_run(tmp_path, child, "sizing.yaml", "plan.md")
    assert after["stop_after"] == "plan" and not hrr.matches(record, after)


def test_a_record_composed_by_another_engine_is_not_served(tmp_path, monkeypatch):
    import json

    (tmp_path / "plan.md").write_bytes(b"plan v1")
    (tmp_path / "sizing.yaml").write_bytes(b"sizing v1")
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": "a" * 40, "plan": None}))
    monkeypatch.setattr(hrr, "engine_sha", lambda: "e" * 40)
    old = hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md")
    record = {**_record(old), "resume_plan_rel": "plan.md"}
    assert old["engine_sha"] == "e" * 40 and hrr.matches(record, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))
    monkeypatch.setattr(hrr, "engine_sha", lambda: "f" * 40)
    assert not hrr.matches(record, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))
    legacy = {k: v for k, v in old.items() if k != "engine_sha"}
    assert not hrr.matches({**_record(legacy), "resume_plan_rel": "plan.md"}, hrr.fingerprint_for_run(tmp_path, rd, "sizing.yaml", "plan.md"))


def test_hint_round_trips_per_sizing_and_refuses_an_unsafe_id(tmp_path):
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    hrr.write_hint(tmp_path, "state/a.yaml", "warp-a-20261029T010101-abc123")
    assert hrr.read_hint(tmp_path, "state/a.yaml") == "warp-a-20261029T010101-abc123"
    assert hrr.read_hint(tmp_path, "state/b.yaml") is None
    hrr.write_hint(tmp_path, "state/c.yaml", "../escape")
    assert hrr.read_hint(tmp_path, "state/c.yaml") is None


def test_refire_record_names_script_sha_engine_and_time(tmp_path):
    import hashlib

    script = tmp_path / "s.mjs"
    script.write_bytes(b"// body")
    rd = tmp_path / "run"
    hrr.write_refire_record(rd, script, "stored", "c" * 40, 123.5)
    hrr.write_refire_record(rd, script, "launcher", None, 124.5)
    lines = [json.loads(x) for x in (rd / hrr.REFIRE_LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["script_path"] == script.as_posix()
    assert lines[0]["script_sha256"] == hashlib.sha256(b"// body").hexdigest()
    assert lines[0]["composed_by_engine_sha"] == "c" * 40
    assert lines[0]["engine_sha"] == hrr.engine_sha()
    assert lines[0]["launched_at"] == 123.5
    assert lines[1]["kind"] == "launcher" and lines[1]["composed_by_engine_sha"] is None


def test_bounded_reads_reuse_one_worker_thread(tmp_path):
    f = tmp_path / "a.json"
    f.write_bytes(b"{}")
    hrr.bounded_read(f, 1000)
    before = threading.active_count()
    for _ in range(20):
        assert hrr.bounded_read(f, 1000) == b"{}"
    assert threading.active_count() <= before


def test_stalled_read_does_not_block_the_next_read(tmp_path, monkeypatch):
    slow, fast = tmp_path / "slow.json", tmp_path / "fast.json"
    slow.write_bytes(b"s")
    fast.write_bytes(b"f")
    release = threading.Event()
    real = hrr._read_file

    def gated(path):
        if path.name == "slow.json":
            release.wait(5)
        return real(path)

    monkeypatch.setattr(hrr, "_read_file", gated)
    try:
        assert hrr.bounded_read(slow, 20) is hrr.STALLED
        assert hrr.bounded_read(fast, 1000) == b"f"
    finally:
        release.set()
