"""The inline_review `reverify` projection carries `foreign` and `pre_existing` to the landing drop."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import wake_digest
from coordinator_core.ops.dispatch_emit.landing_integrity import REVERIFY_KEY, reverify_red_findings
from coordinator_core.ops.dispatch_emit.self_correction import FOREIGN_FIELD

_BASE = {
    "prep": "_reviewPrep",
    "wave": "_reviewWave",
    "delivery": "_deliveryVerdict",
    "bookkeeping_stem": "'stem'",
    "plan_id_literal": "null",
    "reverify": "_rv",
}

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _project(record: dict) -> dict:
    params = wake_digest.next_action_parts(
        has_commit_request=True, review_vars=dict(_BASE), test_var=None, falsifier_var=None,
        verification_var="_verifications", test_absent_status="not_run",
    )[2]
    start = params.index("(_rv ? {")
    end = params.index(" : null)", start) + len(" : null)")
    expr = params[start:end]
    js = f"const _rv = {json.dumps(record)}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_projection_carries_all_three_lists_and_the_drop_names_only_the_owned_red(tmp_path):
    projected = _project({
        "status_after": "fail", "failing": ["tests/test_a.py::t", "tests/test_b.py::t"],
        FOREIGN_FIELD: ["tests/test_b.py::t"], "pre_existing": ["tests/test_c.py::t"],
    })
    assert projected["failing"] == ["tests/test_a.py::t", "tests/test_b.py::t"]
    assert projected[FOREIGN_FIELD] == ["tests/test_b.py::t"]
    assert projected["pre_existing"] == ["tests/test_c.py::t"]

    _write(tmp_path, "pkg/mod.py", "X = 1\n")
    _write(tmp_path, "tests/test_a.py", "import pkg.mod\n")
    _write(tmp_path, "tests/test_b.py", "import pkg.mod\n")
    findings = reverify_red_findings(tmp_path, {"inline_review": {REVERIFY_KEY: projected}}, ["pkg/mod.py"])
    text = " ".join(str(f) for f in findings)
    assert findings
    assert "test_a.py" in text
    assert "test_b.py" not in text


def test_record_without_foreign_projects_empty_lists():
    projected = _project({"status_after": "fail", "failing": ["tests/test_a.py::t"]})
    assert projected[FOREIGN_FIELD] == []
    assert projected["pre_existing"] == []
    assert projected["failing"] == ["tests/test_a.py::t"]
