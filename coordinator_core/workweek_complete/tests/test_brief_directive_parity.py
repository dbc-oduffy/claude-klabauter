"""Parity oracle: the workweek-complete brief's directive and judgment-point set, pinned.

The golden baseline (`data/brief_parity_baseline.json`) was captured from the
brief at HEAD with its inputs stubbed to fixed values, so the set is
deterministic. A rebuild of the brief must reproduce it field for field; the
only sanctioned difference is the `args` of `ARGS_EXCLUDED_IDS`.

The inputs are injected by patching `compute_brief_inputs` on `coordinator_core.workweek_complete.brief`.
Regenerate the baseline with `python <this file> --print`, only for a deliberate set change.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.workweek_complete import brief as brief_mod
from coordinator_core.workweek_complete.brief_inputs import BriefInputs

BASELINE_PATH = Path(__file__).parent / "data" / "brief_parity_baseline.json"

#: The three directives whose args the rebuild fixed, with the exact args each now carries.
FIXED_ARGS: dict[str, list[str]] = {
    "d_step3_5_advisories": ["tripwire-summary", "/fixed/repo-root/state/runtime-tripwire-fire-log.tsv"],
    "d_step4_counts_query_records": ["--type", "improvement", "--format", "json"],
    "d_step4_counts_coordinator_initiative": ["list-unattached"],
}

#: Their args differ from the HEAD baseline by design; asserted exactly via FIXED_ARGS.
ARGS_EXCLUDED_IDS: frozenset[str] = frozenset(FIXED_ARGS)

_FIXED_ROOT = "/fixed/repo-root"

_VERIFY_FINDINGS = [
    {
        "doc": "docs/guides/beta.md",
        "line": 12,
        "token": "coordinator_core/ghost.py",
        "reason": "path does not resolve",
    },
    {
        "doc": "docs/guides/beta.md",
        "line": 40,
        "token": "bin/missing-cli",
        "reason": "cli not found",
    },
]

_VERSION = {"disposition": "minor", "rationale": "fixed-input minor recommendation"}

_DIRECTIVE_FIELDS = (
    "id",
    "cli",
    "args",
    "depends_on",
    "already_satisfied",
    "hard_block",
    "best_effort",
)


def _directive_row(directive: dict[str, Any]) -> dict[str, Any]:
    return {key: directive.get(key) for key in _DIRECTIVE_FIELDS}


def _point_row(point: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": point["id"],
        "dispositions": point["dispositions"],
        "reason": point.get("reason"),
        "has_recommendation": point.get("recommendation") is not None,
        "round_trip": point.get("round_trip"),
        "reportable": point.get("reportable"),
    }


def snapshot(envelope: dict[str, Any]) -> dict[str, Any]:
    """The parity-relevant projection of one brief envelope."""
    return {
        "directives": [_directive_row(d) for d in envelope["directives"]],
        "judgment_points": [_point_row(p) for p in envelope["judgment_points"]],
        "narration": envelope["narration"],
    }


def capture(patch: Any, env: dict[str, str], *, populated: bool) -> dict[str, Any]:
    """Run brief() with the computes stubbed to fixed inputs (or all empty)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in _VERIFY_FINDINGS:
        grouped.setdefault(finding["doc"], []).append(finding)
    inputs = BriefInputs(
        repo_root=_FIXED_ROOT,
        doc_verify_findings_by_doc=grouped if populated else {},
        version_recommendation=dict(_VERSION) if populated else None,
    )
    patch.setattr(brief_mod, "compute_brief_inputs", lambda *a, **k: inputs)
    exit_code, envelope = brief_mod.brief(env=env)
    assert exit_code == 0, envelope
    return snapshot(envelope)


def _strip_excluded_args(snap: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {k: v for k, v in row.items() if not (k == "args" and row["id"] in ARGS_EXCLUDED_IDS)}
        for row in snap["directives"]
    ]
    return {**snap, "directives": rows}


@pytest.fixture
def operator_env(tmp_path):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (settings_home / "machine-local" / ".coordinator-content-root").write_text(str(root))
    return {"COORDINATOR_SETTINGS_HOME": str(settings_home), "HOME": str(tmp_path)}


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario,populated", [("populated", True), ("empty", False)])
def test_brief_matches_golden_baseline(scenario, populated, operator_env, monkeypatch):
    actual = _strip_excluded_args(capture(monkeypatch, operator_env, populated=populated))
    expected = _strip_excluded_args(_baseline()[scenario])
    assert actual == expected


@pytest.mark.parametrize("populated", [True, False])
def test_fixed_directives_carry_exactly_the_new_args(populated, operator_env, monkeypatch):
    snap = capture(monkeypatch, operator_env, populated=populated)
    actual = {row["id"]: row["args"] for row in snap["directives"] if row["id"] in FIXED_ARGS}
    assert actual == FIXED_ARGS


def test_excluded_args_are_exactly_the_three_usage_error_directives():
    assert ARGS_EXCLUDED_IDS == {
        "d_step3_5_advisories",
        "d_step4_counts_query_records",
        "d_step4_counts_coordinator_initiative",
    }
    ids = {row["id"] for row in _baseline()["populated"]["directives"]}
    assert ARGS_EXCLUDED_IDS <= ids


def test_populated_baseline_carries_per_doc_points_and_acks():
    populated = _baseline()["populated"]
    jp_ids = {p["id"] for p in populated["judgment_points"]}
    d_ids = {d["id"] for d in populated["directives"]}
    assert not any(i.startswith("jp_doc_staleness_") for i in jp_ids)
    assert "jp_doc_verify_docs_guides_beta_md" in jp_ids
    assert "d_doc_staleness_ack_readme_md" not in d_ids
    assert "d_doc_verify_ack_docs_guides_beta_md" in d_ids
    assert "jp_requirement_register_stall" not in populated["narration"]


if __name__ == "__main__":
    if "--print" not in sys.argv:
        raise SystemExit("usage: test_brief_directive_parity.py --print")
    import os
    import tempfile

    from _pytest.monkeypatch import MonkeyPatch

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings_home = tmp_path / "settings-home"
        (settings_home / "machine-local").mkdir(parents=True)
        content_root = tmp_path / "root"
        content_root.mkdir()
        (settings_home / "machine-local" / ".coordinator-content-root").write_text(str(content_root))
        env = {"COORDINATOR_SETTINGS_HOME": str(settings_home), "HOME": str(tmp_path)}
        out: dict[str, Any] = {}
        for name, populated in (("populated", True), ("empty", False)):
            mp = MonkeyPatch()
            try:
                out[name] = capture(mp, env, populated=populated)
            finally:
                mp.undo()
        sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + os.linesep)
