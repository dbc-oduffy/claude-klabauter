"""land_run halted mode: ``withheld`` names only declared paths the run itself changed."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halted_record_eviction
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import (  # noqa: F401
    RUN, _call, _chunks, _git, _record, repo,
)
import pytest


def _baseline(root, mapping: dict) -> None:
    manifest = root / RECORD_DIR_ROOT / RUN / "manifest.json"
    manifest.write_text(json.dumps({"write_baseline": mapping}), encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_declared_path_the_run_left_untouched_is_not_withheld(repo):
    _record(repo)
    _chunks(repo, {"C1": ["ran.py", "peer.py"]})
    _baseline(repo, {"ran.py": None, "peer.py": fingerprint(repo, "peer.py")})

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert out["withheld"] == {"ran.py": halted_record_eviction.EVICTED_PARKED}
    assert "peer.py" not in _git(repo, "show", "--name-only", "--format=", out["sha"]).split()


def test_declared_path_changed_since_the_baseline_is_still_withheld(repo):
    _record(repo)
    _chunks(repo, {"C1": ["ran.py", "peer.py"]})
    _baseline(repo, {"ran.py": None, "peer.py": "0" * 40})

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert out["withheld"] == {
        "ran.py": halted_record_eviction.EVICTED_PARKED, "peer.py": "halt-record-only",
    }


def test_without_a_baseline_every_declared_path_is_withheld(repo):
    _record(repo)
    _chunks(repo, {"C1": ["ran.py", "peer.py"]})

    out = _call(repo, {"halted": "refusal", "reason": "x"})

    assert set(out["withheld"]) == {"ran.py", "peer.py"}
