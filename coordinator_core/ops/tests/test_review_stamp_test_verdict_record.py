"""The review-stamp mint cites the test-verdict record verb and accepts its record."""
from __future__ import annotations

import pytest

from coordinator_core.completion_receipts.test_verdict import record_test_verdict
from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.tests.test_review_stamp import _mint_success_fixture, _setup_repo, _write_sidecar

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _mint(tmp_path, build_test_fields):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    _write_sidecar(build_test, build_test_fields)
    return repo, build_test


def _run(repo, build_test):
    return m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=str(build_test))


def test_open_sidecar_refusal_names_the_record_verb(tmp_path):
    repo, bt = _mint(tmp_path, {"status": "open", "agent_type": "test-runner"})
    with pytest.raises(m.MintRefusal) as exc:
        _run(repo, bt)
    assert "build/test verdict is 'open'" in str(exc.value)
    assert "test-verdict record" in str(exc.value)


def test_recorded_verdict_mints(tmp_path):
    repo, bt = _mint(tmp_path, {"status": "open", "agent_type": "test-runner"})
    record_test_verdict(
        repo, {"status": "pass", "tests_run": 124, "tests_failed": 0, "sidecar_path": str(bt)}
    )
    result = _run(repo, bt)
    stamp = result.get("review_stamp", result) if isinstance(result, dict) else result
    text = repr(stamp)
    assert "'verdict': 'pass'" in text and "'ran': 124" in text and "'failed': 0" in text


def test_complete_sidecar_without_verdict_gets_the_hint(tmp_path):
    repo, bt = _mint(tmp_path, {"status": "complete", "agent_type": "test-runner"})
    with pytest.raises(m.MintRefusal, match="test-verdict record"):
        _run(repo, bt)


def test_recorded_fail_refusal_names_the_rerun_route(tmp_path):
    repo, bt = _mint(tmp_path, {"status": "open", "agent_type": "test-runner"})
    record_test_verdict(
        repo, {"status": "fail", "tests_run": 10, "tests_failed": 2, "sidecar_path": str(bt)}
    )
    with pytest.raises(m.MintRefusal) as exc:
        _run(repo, bt)
    assert "build/test verdict is 'fail'" in str(exc.value)
    assert "re-run the plan's tests with the test-runner" in str(exc.value)
    assert "--build-test <test-runner sidecar>" in str(exc.value)
