"""Delivery-verdict and tests-reverify records store `tests.sidecar` repo-relative."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.reverify_delivery import record_delivery_verdict
from coordinator_core.ops.dispatch_emit.verdict_supersession import record_tests_supersession

_REL = ".coordinator-local/subagent-share/s/t.md"


def _fm(repo_root: Path, rel: str) -> dict:
    text = (repo_root / rel).read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def _delivery(repo_root: Path, sidecar: str) -> dict:
    rel = record_delivery_verdict(
        repo_root=repo_root, supersedes="state/runs/r.md", plan_id="p", head_sha="a" * 40,
        verdict="PASS", unbacked=[], tests={"status": "pass", "run": 1, "failed": 0, "sidecar": sidecar},
    )
    return _fm(repo_root, rel)


def _tests_rec(repo_root: Path, sidecar: str) -> dict:
    rel = record_tests_supersession(
        repo_root=repo_root, plan_id="p", tests={"status": "pass", "run": 1, "failed": 0, "sidecar": sidecar},
    )
    return _fm(repo_root, rel)


def test_delivery_verdict_sidecar_repo_relative(tmp_path):
    assert _delivery(tmp_path, str(tmp_path / _REL))["tests"]["sidecar"] == _REL


def test_tests_reverify_sidecar_repo_relative(tmp_path):
    assert _tests_rec(tmp_path, str(tmp_path / _REL))["tests"]["sidecar"] == _REL


@pytest.mark.skipif(sys.platform != "win32", reason="backslash is a filename character on POSIX")
def test_backslash_spelling_same_value(tmp_path):
    back = str(tmp_path / _REL).replace("/", "\\")
    assert _delivery(tmp_path, back)["tests"]["sidecar"] == _REL
    assert _tests_rec(tmp_path, back)["tests"]["sidecar"] == _REL


def test_outside_sidecar_kept_as_given(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = (tmp_path / "elsewhere" / "t.md").as_posix()
    assert _delivery(root, outside)["tests"]["sidecar"] == outside
    assert _tests_rec(root, outside)["tests"]["sidecar"] == outside


def test_none_sidecar_untouched(tmp_path):
    rel = record_delivery_verdict(
        repo_root=tmp_path, supersedes="x", plan_id="p", head_sha="a" * 40, verdict="PASS", unbacked=[],
        tests={"status": "pass", "run": 1, "failed": 0, "sidecar": None},
    )
    assert _fm(tmp_path, rel)["tests"]["sidecar"] is None


def test_supersedes_stays_repo_relative(tmp_path):
    rec = _delivery(tmp_path, str(tmp_path / _REL))
    assert rec["supersedes"] == "state/runs/r.md"
