"""Cost pin: origin_stub_staleness.survey spawns nothing and stays cheap."""

import subprocess
import time

from coordinator_core.ops.origin_stub_staleness import survey

_RECORDS = 600
_STUB_EVERY = 20


def _build(root):
    handoffs = root / "state" / "handoffs"
    plans = root / "docs" / "plans"
    handoffs.mkdir(parents=True)
    plans.mkdir(parents=True)
    for i in range(_RECORDS):
        if i % _STUB_EVERY == 0:
            (handoffs / f"stub-{i}.md").write_text(
                f"---\nkind: spinoff\nroadmap_id: r\nstub_id: s{i}\n"
                "deployment_state: ready_to_fire\n---\nbody\n",
                encoding="utf-8",
            )
        elif i % 2:
            (handoffs / f"h-{i}.md").write_text(
                "---\nkind: handoff\ndeployment_state: shipped\n---\n" + "text\n" * 50,
                encoding="utf-8",
            )
        else:
            (plans / f"p-{i}.md").write_text(
                "---\nstatus: draft\n---\n" + "text\n" * 50, encoding="utf-8"
            )
    (plans / "done.md").write_text(
        "---\nstatus: shipped\nroadmap_id: r\nstub_id: s0\n---\n", encoding="utf-8"
    )


def test_survey_spawns_nothing_and_is_under_budget(tmp_path, monkeypatch):
    _build(tmp_path)
    spawned = []
    real_init = subprocess.Popen.__init__

    def counting_init(self, *a, **kw):
        spawned.append(a)
        real_init(self, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting_init)
    start = time.process_time()
    res = survey(tmp_path)
    elapsed = time.process_time() - start

    assert res.live_with_pair == _RECORDS // _STUB_EVERY
    assert [s.pair for s in res.stale] == [("r", "s0")]
    assert len(spawned) == 0
    assert elapsed < 0.2
