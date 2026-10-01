"""Pins the five-bucket comparison in commit_ledger.join_divergence and its
sparse-coverage basis. Pure: no tmp repo, no spawn."""

import ast
import inspect

from coordinator_core.commit_ledger import join_divergence as jd
from coordinator_core.commit_ledger.join_divergence import (
    DISAGREEMENT_CAP,
    OUTCOMES,
    compare,
)


def test_one_sha_per_outcome():
    ledger = {"a": "dlv-1", "b": "dlv-1", "c": "dlv-3"}
    trailer = {"a": "dlv-1", "b": "dlv-2", "d": "dlv-4"}
    report = compare(["a", "b", "c", "d", "e"], ledger, trailer)
    assert report.counts == {
        "agree": 1,
        "disagree": 1,
        "ledger_only": 1,
        "trailer_only": 1,
        "neither": 1,
    }
    assert report.commits_examined == 5
    assert [r.sha for r in report.disagreements] == ["b"]
    row = report.disagreements[0]
    assert (row.ledger_deliverable, row.trailer_deliverable) == ("dlv-1", "dlv-2")


def test_counts_carry_every_key_when_zero():
    report = compare(["a"], {}, {})
    assert set(report.counts) == set(OUTCOMES)
    assert report.counts["neither"] == 1
    assert sum(v for k, v in report.counts.items() if k != "neither") == 0


def test_disagreements_only_disagree_in_order_and_capped():
    n = DISAGREEMENT_CAP + 5
    shas = []
    ledger = {}
    trailer = {}
    for i in range(n):
        sha = f"d{i:03d}"
        shas.append(sha)
        ledger[sha] = "dlv-x"
        trailer[sha] = "dlv-y"
        agree = f"a{i:03d}"
        shas.append(agree)
        ledger[agree] = trailer[agree] = "dlv-z"
    report = compare(shas, ledger, trailer)
    assert report.counts["disagree"] == n
    assert len(report.disagreements) == DISAGREEMENT_CAP
    assert all(r.outcome == "disagree" for r in report.disagreements)
    assert [r.sha for r in report.disagreements] == [
        f"d{i:03d}" for i in range(DISAGREEMENT_CAP)
    ]


def test_zero_overlap_basis_says_nothing_known():
    report = compare(["a", "b"], {"a": "dlv-1"}, {"b": "dlv-2"})
    assert "nothing about agreement is known" in report.basis
    assert "2 commits examined" in report.basis


def test_overlap_basis_states_split():
    report = compare(
        ["a", "b", "c"],
        {"a": "dlv-1", "b": "dlv-1", "c": "dlv-1"},
        {"a": "dlv-1", "b": "dlv-2"},
    )
    assert "nothing about agreement is known" not in report.basis
    assert "1 agree and 1 disagree" in report.basis


def test_report_is_not_a_gate():
    report = compare(["a"], {}, {})
    for name in ("exit_code", "ok", "refused", "verdict"):
        assert not hasattr(report, name)


def test_module_imports_no_io():
    tree = ast.parse(inspect.getsource(jd))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    for banned in ("subprocess", "pathlib", "coordinator_core.git"):
        assert not any(
            m == banned or m.startswith(banned + ".") for m in imported
        ), banned
