"""dispatch.terminal_commit refuses, before commit_v2, a commit whose carried test verdict names a
run-caused failure."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire as _fire_base,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _reviewed(repo) -> dict:
    sidecar = repo.parent / "rev-sidecar.md"
    sidecar.write_text("review\n", encoding="utf-8")
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"verdict": "PASS", "sidecar_path": str(sidecar)}],
    }


def _fire(repo, path: str, **extra) -> dict:
    extra.setdefault("inline_review", _reviewed(repo))
    return _fire_base(repo, path, **extra)


def _write(repo) -> str:
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    return _git(["rev-parse", "HEAD"], repo)


def test_caused_failure_is_refused_naming_the_test_file(repo):
    head = _write(repo)
    verdict = {
        "status": "fail", "tests_failed": 2,
        "baseline": {"caused": ["pkg/tests/test_a.py::test_x"], "pre_existing": [], "unverified": []},
    }

    out = _fire(repo, "ok.py", targeted_tests=verdict)

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert out["integrity"][0]["path"] == "pkg/tests/test_a.py"
    assert out["targeted_tests_check"] == "failed"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_pass_verdict_commits(repo):
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests={"status": "pass", "tests_failed": 0, "baseline": None})

    assert out["committed"] is True, out
    assert out["targeted_tests_check"] == "pass"


def test_inline_review_fail_without_targeted_tests_is_refused_unattributed(repo):
    head = _write(repo)
    inline = {**_reviewed(repo), "tests": {"status": "fail", "failed": 3}}

    out = _fire(repo, "ok.py", inline_review=inline)

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert out["integrity"][0]["path"] == ""
    assert _git(["rev-parse", "HEAD"], repo) == head


def _run_deleted_target(repo, rel: str = "test_gone.py", dropped=None) -> dict:
    """Commit a deletion of ``rel``, write a no-tests retest record dropping ``dropped``; return the base params."""
    import json

    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("def test_g():\n    pass\n", encoding="utf-8")
    _git(["add", rel], repo)
    _git(["commit", "-q", "-m", "add"], repo)
    base = _git(["rev-parse", "HEAD"], repo)
    _git(["rm", "-q", rel], repo)
    _git(["commit", "-q", "-m", "delete"], repo)
    record = {"run_id": "r1", "head": _git(["rev-parse", "HEAD"], repo), "status": "no-tests", "tests": [],
              "tests_run": 0, "dropped": dropped or [rel], "covered": {}}
    path = landing_retest.fresh_verdict_path(repo, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return {"run_id": "r1", "run_base_sha": base}


@pytest.mark.parametrize("verdict", [
    None,
    {"status": "fail", "tests_run": 0, "tests_failed": 0, "baseline": None},
    {"status": "error", "tests_run": 0, "tests_failed": 0, "baseline": None},
])
def test_reply_names_run_deleted_targets_on_both_routes(repo, verdict):
    _write(repo)
    params = _run_deleted_target(repo)
    if verdict is not None:
        params["targeted_tests"] = verdict

    out = _fire(repo, "ok.py", **params)

    assert out["committed"] is True, out
    assert out["targeted_tests_check"] == ("no-verdict" if verdict is None else "not-run")
    assert "test_gone.py" in out["targeted_tests_note"]


@pytest.mark.parametrize("target", ["pkg/sub/test_n.py", "test_gone.py::test_g"])
def test_nested_directory_and_node_targets_are_named(repo, target):
    _write(repo)
    rel = target.split("::")[0]
    params = _run_deleted_target(repo, rel, dropped=[target])

    out = _fire(repo, "ok.py", **params)

    assert out["committed"] is True, out
    assert target in out["targeted_tests_note"]


def test_a_typo_target_beside_the_deleted_one_refuses_and_carries_no_note(repo):
    _write(repo)
    params = _run_deleted_target(repo, dropped=["test_gone.py", "test_typo.py"])
    params["targeted_tests"] = {"status": "error", "tests_run": 0, "tests_failed": 0, "baseline": None}

    out = _fire(repo, "ok.py", **params)

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert "targeted_tests_note" not in out


def test_absent_verdict_commits_and_reports_no_verdict(repo):
    _write(repo)

    out = _fire(repo, "ok.py")

    assert out["committed"] is True, out
    assert out["targeted_tests_check"] == "no-verdict"
