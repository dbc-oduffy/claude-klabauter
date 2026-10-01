"""A git timeout on a delivery-evidence probe reads as "could not determine",
never as "not delivered". Git is stubbed at the `GitResult` seam: no spawn."""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
import coordinator_core.git.run as git_run
from coordinator_core.git.run import GitResult

_SHA = "a" * 40
_TIMEOUT = GitResult(returncode=-1, stdout="", stderr="", timed_out=True)
_NO = GitResult(returncode=1, stdout="", stderr="", timed_out=False)

_LEDGER_PLAN = (
    "## Dispatch Ledger\n\n"
    "| chunk-id | status |\n"
    "|---|---|\n"
    f"| C1 | committed {_SHA[:9]} |\n"
)

_SPINE_PLAN = (
    "---\ntitle: t\n---\n\n## Tasks\n\n"
    "```yaml plan-tasks\n"
    "- id: C1\n  disposition: coded\n  disposition_ref: " + _SHA + "\n"
    "```\n"
)


def _stub(monkeypatch, script):
    """`script` maps git subcommand -> GitResult; applies to both seams."""

    def fake(args, cwd=None, **_kw):
        return script[args[0]]

    monkeypatch.setattr(git_run, "run_git", fake)
    monkeypatch.setattr(coas, "_run_git", lambda args, cwd: fake(args))


def test_ledger_cat_file_timeout_is_error_not_missing(monkeypatch, tmp_path: Path):
    _stub(monkeypatch, {"cat-file": _TIMEOUT})
    shipped, missing, error = coas._dispatch_ledger_delivered(_LEDGER_PLAN, tmp_path)
    assert shipped is False
    assert missing == []
    assert error is not None and "indeterminate" in error


def test_ledger_rev_list_timeout_is_error_not_missing(monkeypatch, tmp_path: Path):
    found = GitResult(0, f"{_SHA} commit\n", "", False)
    _stub(monkeypatch, {"cat-file": found, "rev-list": _TIMEOUT})
    shipped, missing, error = coas._dispatch_ledger_delivered(_LEDGER_PLAN, tmp_path)
    assert (shipped, missing) == (False, [])
    assert error is not None and "indeterminate" in error


def test_ledger_genuine_rev_list_failure_stays_missing(monkeypatch, tmp_path: Path):
    found = GitResult(0, f"{_SHA} commit\n", "", False)
    _stub(monkeypatch, {"cat-file": found, "rev-list": _NO})
    shipped, missing, error = coas._dispatch_ledger_delivered(_LEDGER_PLAN, tmp_path)
    assert (shipped, missing, error) == (False, ["C1"], None)


@pytest.mark.parametrize("timing_out", ["rev-parse", "merge-base"])
def test_disposition_ref_timeout_is_indeterminate(monkeypatch, tmp_path: Path, timing_out):
    script = {
        "rev-parse": GitResult(0, _SHA + "\n", "", False),
        "merge-base": GitResult(0, "", "", False),
    }
    script[timing_out] = _TIMEOUT
    _stub(monkeypatch, script)
    assert coas._verify_disposition_ref(tmp_path, _SHA) == (
        None,
        coas.DISPOSITION_REF_INDETERMINATE,
    )


def test_disposition_ref_genuine_failures_keep_their_reasons(monkeypatch, tmp_path: Path):
    _stub(monkeypatch, {"rev-parse": _NO})
    assert coas._verify_disposition_ref(tmp_path, _SHA)[1] == coas.DISPOSITION_REF_UNRESOLVABLE
    _stub(
        monkeypatch,
        {"rev-parse": GitResult(0, _SHA + "\n", "", False), "merge-base": _NO},
    )
    assert coas._verify_disposition_ref(tmp_path, _SHA)[1] == coas.DISPOSITION_REF_NOT_ANCESTOR


def test_determine_shipped_spine_timeout_is_error_not_halted(monkeypatch, tmp_path: Path):
    _stub(monkeypatch, {"rev-parse": _TIMEOUT})
    shipped, missing, _backed, error = coas._determine_shipped(_SPINE_PLAN, "plan.md", tmp_path)
    assert shipped is False
    assert missing == []
    assert error is not None and "indeterminate" in error and "C1" in error
