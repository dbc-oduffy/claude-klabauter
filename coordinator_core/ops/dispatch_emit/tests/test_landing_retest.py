"""The retest-side own-sizing exemption: only a sizing under the sizings home is exempt from freshness."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_retest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-own"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _repo(tmp_path: Path, sizing_rel: str) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    sizing = root / sizing_rel
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text("status: open\n", encoding="utf-8")
    run_dir = root / "state" / "warp" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    path = landing_retest.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "run_id": RUN_ID, "status": "pass", "head": _git(root, "rev-parse", "HEAD"),
            "covered": landing_retest.landing_fingerprint(root, [sizing_rel]),
        }),
        encoding="utf-8",
    )
    return root


def test_own_sizing_under_sizings_home_is_exempt(tmp_path):
    root = _repo(tmp_path, "state/sizings/s.yaml")
    assert landing_retest.own_sizing_rel(root, root / "state" / "warp" / RUN_ID) == "state/sizings/s.yaml"
    (root / "state" / "sizings" / "s.yaml").write_text("status: stamped\n", encoding="utf-8")
    record = landing_retest.fresh_targeted_verdict(root, RUN_ID)
    assert record is not None and record["status"] == "pass"


def test_sizing_outside_sizings_home_is_not_exempt(tmp_path):
    root = _repo(tmp_path, "src/other.yaml")
    assert landing_retest.own_sizing_rel(root, root / "state" / "warp" / RUN_ID) == ""
    (root / "src" / "other.yaml").write_text("status: edited\n", encoding="utf-8")
    assert landing_retest.fresh_targeted_verdict(root, RUN_ID) is None
