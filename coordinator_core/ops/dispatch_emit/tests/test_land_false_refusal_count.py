"""scripts/land_false_refusal_count.py on a tmp_path fixture: halt-time population, the
record-commit join on sha prefix, and the false / still-refused split."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "land_false_refusal_count.py"
_spec = importlib.util.spec_from_file_location("land_false_refusal_count", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lfrc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lfrc
_spec.loader.exec_module(lfrc)

SHA = "a6233784fe91" + "0" * 28
LOG = (
    "\x01" + "f" * 40 + "\x02dispatch.record_halted_run_landed: ask-20261118T031700-a7bab8 landed at a6233784fe91\n"
    "state/records/x.md\n"
    "\x01" + SHA + "\x02test leg: hand land\n"
    "coordinator_core/ops/test_leg_run.py\n"
    "coordinator_core/ops/tests/test_test_leg_run.py\n"
)


def _run(root: Path, name: str, halted: dict, last_activity: str | None = None) -> None:
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    if last_activity:
        (d / "run-stage.json").write_text(json.dumps({"last_activity_at": last_activity}), encoding="utf-8")


def test_parse_log_joins_record_to_landed_commit_paths() -> None:
    records, commits = lfrc.parse_log(LOG)
    assert records == {"ask-20261118T031700-a7bab8": "a6233784fe91"}
    assert commits[SHA] == {"coordinator_core/ops/test_leg_run.py", "coordinator_core/ops/tests/test_test_leg_run.py"}


def test_false_and_still_refused_split(tmp_path: Path) -> None:
    _run(tmp_path, "ask-20261118T031700-a7bab8", {
        "reason": "verdict-pin-stale: review verdict judged other bytes than the tree carries for 2 file(s); "
                  "re-review only: coordinator_core/ops/test_leg_run.py, coordinator_core/ops/tests/test_test_leg_run.py",
        "run_artifacts": {"withheld": {
            "coordinator_core/ops/test_leg_run.py": "evicted-parked",
            "state/run-evidence/other.md": "evicted-parked",
            "coordinator_core/ops/tests/x.py": "halt-record-only",
        }},
    }, "2026-11-18T04:00:07Z")
    rep = lfrc.collect(tmp_path, log_text=LOG)
    assert rep.runs == 1
    assert rep.per_class() == {"evicted-parked": (2, 1), "verdict-pin-stale": (2, 2)}
    text = lfrc.render(rep)
    assert "TOTAL" in text and "a7bab8" in text and "FALSE (landed a6233784fe91)" in text
    assert "unresolved" in text


def test_run_without_record_is_not_false(tmp_path: Path) -> None:
    _run(tmp_path, "ask-20261118T050000-bbbbbb", {"run_artifacts": {"withheld": {"a/b.py": "peer-claim-indeterminate"}}},
         "2026-11-18T05:10:00Z")
    rep = lfrc.collect(tmp_path, log_text=LOG)
    assert rep.per_class() == {"peer-claim-indeterminate": (1, 0)}


def test_population_uses_halt_time_not_mtime(tmp_path: Path) -> None:
    old = {"run_artifacts": {"withheld": {"a.py": "peer-claim-indeterminate"}}}
    _run(tmp_path, "ask-20261117T230000-aaaaaa", old, "2026-11-17T23:30:00Z")
    _run(tmp_path, "ask-20261117T230000-cccccc", old)  # no run-stage: mint stamp 11-17
    rep = lfrc.collect(tmp_path, log_text=LOG)
    assert rep.runs == 0 and rep.refusals == []


def test_record_joined_by_trailing_hash_of_halted_run(tmp_path: Path) -> None:
    log = LOG.replace("ask-20261118T031700-a7bab8", "warp-2026-11-18-x-a7bab8-20261118T034906-ddeca3")
    _run(tmp_path, "ask-20261118T031700-a7bab8", {"run_artifacts": {"withheld": {
        "coordinator_core/ops/test_leg_run.py": "peer-claim-indeterminate"}}}, "2026-11-18T04:00:07Z")
    assert lfrc.collect(tmp_path, log_text=log).per_class() == {"peer-claim-indeterminate": (1, 1)}


def test_default_since_and_unregistered_reason_ignored(tmp_path: Path) -> None:
    assert lfrc.DEFAULT_SINCE == "2026-11-18T00:00Z"
    _run(tmp_path, "ask-20261118T060000-dddddd", {"reason": "abandoned: nothing", "run_artifacts": {"withheld": {
        "a.py": "halt-record-only"}}}, "2026-11-18T06:00:00Z")
    assert lfrc.collect(tmp_path, log_text=LOG).refusals == []
