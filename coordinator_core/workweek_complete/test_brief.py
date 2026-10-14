"""coordinator_core/workweek_complete/test_brief.py — C4a pin: Step 2's
directive resolves through the validate gate CLI, not the standalone
resolver.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C4, Design decision 2 / Problem 3). The repoint itself and CONSUMES_MANIFEST
membership were already landed by a prior session on this branch (verified on
disk: `d_step2_resolve_validation_cmd` already carries
`cli="validate-fast-and-packageability"`, `args=["fast"]`, and
`coordinator-resolve-validation-cmd` is not, and has never needed to be, a
CONSUMES_MANIFEST member -- no other directive ever named it). This file adds
the row's own declared test surface (`coordinator_core/workweek_complete/
test_brief.py`) as a focused, file-local pin, alongside the pre-existing
broader pin in `test_workweek_complete_contract.py::
test_step2_directive_names_the_validate_gate_cli_fast_subcommand`.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from coordinator_core.workweek_complete import brief as wwc_brief


def test_step2_directive_repointed_at_validate_gate_fast_subcommand() -> None:
    directive = next(
        d
        for d in wwc_brief._build_directives()
        if d["id"] == "d_step2_resolve_validation_cmd"
    )
    assert directive["cli"] == "validate-fast-and-packageability"
    assert directive["args"] == ["fast"]


def test_standalone_resolver_cli_is_not_a_manifest_member() -> None:
    assert "coordinator-resolve-validation-cmd" not in wwc_brief.CONSUMES_MANIFEST


def test_validate_gate_cli_is_a_manifest_member() -> None:
    assert "validate-fast-and-packageability" in wwc_brief.CONSUMES_MANIFEST


def test_doc_staleness_evidence_omits_changed_areas() -> None:
    line = wwc_brief._doc_staleness_evidence(
        {
            "path": "README.md",
            "commits_since": 9000,
            "threshold_commits": 8000,
            "days_since": 40,
            "threshold_days": 30,
            "last_touch_sha": "abc1234",
            "last_touch_date": "2026-09-01",
            "changed_areas": [],
        }
    )
    assert "commits_since=9000" in line
    assert "days_since=40" in line
    assert "last_touch=abc1234@2026-09-01" in line
    assert "changed_areas" not in line
    assert line == (
        "README.md: commits_since=9000 (threshold 8000), days_since=40 "
        "(threshold 30), last_touch=abc1234@2026-09-01"
    )


def _load_trampoline_parser() -> argparse.ArgumentParser:
    path = (
        Path(__file__).resolve().parents[2]
        / "coordinator"
        / "bin"
        / "workweek-complete-doc-staleness.py"
    )
    spec = importlib.util.spec_from_file_location("wwc_doc_staleness_trampoline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._build_parser()


def test_no_directive_runs_the_doc_staleness_cli() -> None:
    directives = wwc_brief._build_directives(
        stale_docs=[{"path": "README.md"}, {"path": "docs/guides/alpha.md"}],
        repo_root=None,
    )
    assert not any(d["cli"] == "workweek-complete-doc-staleness" for d in directives)
    assert not any(
        d["id"].startswith("d_step4b_4k_doc_staleness") for d in directives
    )


def test_trampoline_parser_takes_doc_and_has_no_ack_flag() -> None:
    parser = _load_trampoline_parser()
    assert parser.parse_args(["--doc", "README.md"]).doc == "README.md"
    with pytest.raises(SystemExit):
        parser.parse_args(["--doc", "README.md", "--ack"])


def test_doc_staleness_judgment_dispositions_resolve_no_directive() -> None:
    points = wwc_brief._build_judgment_points(
        stale_docs=[{"path": "README.md"}]
    )
    point = next(p for p in points if p["id"] == "jp_doc_staleness_readme_md")
    assert [d["value"] for d in point["dispositions"]] == [
        "reviewed_ok",
        "queued_for_update",
    ]
    assert all(d["resolves"] == [] for d in point["dispositions"])
