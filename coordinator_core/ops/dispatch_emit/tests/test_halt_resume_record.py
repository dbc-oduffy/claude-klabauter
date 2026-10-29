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


def test_hint_round_trips_per_sizing_and_refuses_an_unsafe_id(tmp_path):
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    hrr.write_hint(tmp_path, "state/a.yaml", "warp-a-20261029T010101-abc123")
    assert hrr.read_hint(tmp_path, "state/a.yaml") == "warp-a-20261029T010101-abc123"
    assert hrr.read_hint(tmp_path, "state/b.yaml") is None
    hrr.write_hint(tmp_path, "state/c.yaml", "../escape")
    assert hrr.read_hint(tmp_path, "state/c.yaml") is None
