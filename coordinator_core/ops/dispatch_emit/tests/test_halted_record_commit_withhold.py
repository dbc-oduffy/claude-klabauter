"""terminal_commit halt route: a path in the run's withhold.jsonl stays out of the halt commit."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_withhold
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_artifacts_only_ledger_halt_with_zero_review_verdicts_commits(repo):
    # A ledger halt with no targeted-test verdict withholds product (halt-record-only) and still
    # commits the record; the review floor never refuses it.
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, inline_review={"integration_stem": "s", "slices": 1, "fixes": 0})

    assert out["committed"] is True, out
    assert out.get("refused") != "review-floor"
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names


def test_withheld_dirty_declared_path_stays_out_of_the_halt_commit(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "peer_owned.py").write_text("o = 1\n", encoding="utf-8")
    _marker_script(
        repo, [("C1", ["done.py"]), ("C2", ["peer_owned.py"])], {"C1": "DONE: C1\n", "C2": "DONE: C2\n"}
    )
    run_withhold.record(
        repo / RECORD_DIR_ROOT / RUN, [{"path": "peer_owned.py", "reason": "owned by peer run d238dc"}], "t"
    )

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"peer_owned.py": "owned by peer run d238dc", "done.py": "halt-record-only"}
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names and "peer_owned.py" not in names
