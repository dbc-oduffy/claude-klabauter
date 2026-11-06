"""A wave entry counts as applied only the findings whose files the run diff holds.

A reviewer claims `applied: 3`; the recorded run diff is non-empty but holds none of the files its findings
name. The entry reaches the blocked-halt check with `applied 0`, `applied_claimed 3` and a reason naming the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.review_mint.tests.test_wave_bookkeeping import _findings_sidecar
from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave
from coordinator_core.win_portability import no_console_creationflags

_MEMOS = (
    "2026-11-04-example-retrieval-repo-ue-addon-em-review-wave-applied-count-overstates.md, "
    "2026-11-05-example-retrieval-repo-ue-addon-em-engine-review-applied-count-not-in-tree.md, "
    "2026-11-05-example-retrieval-repo-ue-addon-em-review-applied-not-in-diff.md"
)
EDITED = "a.py"
NAMED = "b.py"


def _reviewer_sidecar(tmp_path: Path) -> Path:
    """A reviewer sidecar claiming three applied findings, every one naming ``b.py``."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / NAMED).write_text("y = 2\n", encoding="utf-8")
    sid = tmp_path / ".coordinator-local" / "subagent-share" / "s"
    path = _findings_sidecar(
        sid,
        "reviewer.md",
        "".join(f"### Finding {i}: t{i}\nBody.\n\n" for i in (1, 2, 3)),
        [{"id": f"f{i}", "file": NAMED, "line": i, "before": "y = 1", "after": "y = 2"} for i in (1, 2, 3)],
    )
    path.write_text(path.read_text(encoding="utf-8").replace("agent_type:", "applied: 3\nagent_type:", 1), encoding="utf-8")
    return path


def _reconcile(wave: list, reverify: dict) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"let _reviewReused = false; let _reviewWave = {json.dumps(wave)}; "
        f"let _reviewReverify = {json.dumps(reverify)}; "
        f"let _reviewPrep = {json.dumps({'slices': [{'id': 's1', 'files': [NAMED]}]})};\n"
        + ask_compose._WAVE_DIFF_RECONCILE_JS
        + "\nconsole.log(JSON.stringify(_reviewWave));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _entry(sidecar: Path) -> dict:
    return {
        "slice_id": "s1",
        "verdict": "BLOCKED",
        "findings": 3,
        "applied": 3,
        "sidecar_path": sidecar.as_posix(),
    }


def test_bookkeeping_already_judges_each_finding_against_the_run_diff(tmp_path):
    record = bookkeep_wave(
        [_reviewer_sidecar(tmp_path)], repo_root=tmp_path, session_id="s", plan_id="pln-x-aaaaaa",
        prep_sidecar=None, record_stem="rec", run_diff=[EDITED],
    )

    assert record["review_firings"][0]["applied"] == 0
    assert [e["finding"] for e in record["unapplied_findings"]] == ["f1", "f2", "f3"]
    assert {e["reason"] for e in record["unapplied_findings"]} == {"not in run diff"}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_non_empty_run_diff_without_the_reviewers_files_demotes_the_entry(tmp_path):
    wave = _reconcile([_entry(_reviewer_sidecar(tmp_path))], {"edited": [EDITED], "created": []})

    assert wave[0]["applied"] == 0
    assert wave[0]["applied_claimed"] == 3
    assert NAMED in wave[0]["unapplied_reason"]
    assert wave[0]["findings"] > wave[0]["applied"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_run_diff_holding_the_reviewers_file_leaves_the_count_standing(tmp_path):
    wave = _reconcile([_entry(_reviewer_sidecar(tmp_path))], {"edited": [NAMED], "created": []})

    assert wave[0]["applied"] == 3
    assert "applied_claimed" not in wave[0]
