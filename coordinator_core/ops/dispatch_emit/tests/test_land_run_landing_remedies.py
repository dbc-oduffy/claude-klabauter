"""dispatch.land_run landing remedies: runnable release_held line, ledger-aware blob-drift remedy,
direct withhold_paths param, review-only refresh of the expected blob."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c2"
RUN = "inv-t1-p1-land.workflow"
SCRIPT = "run-a.mjs"
def _reviewed(root: Path) -> dict:
    sidecar = root.parent / "review-sidecar.md"
    sidecar.write_text("verdict: PASS\n", encoding="utf-8")
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"verdict": "PASS", "sidecar_path": str(sidecar)}],
    }


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _declare(root: Path, chunks, review_only: bool = False) -> None:
    request = CommitRequest(
        chunks=tuple(ChunkCommit(id=i, title=f"t{i}", paths=tuple(ps)) for i, ps in chunks),
        plan_path=None, review_only=review_only,
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")


def _land(root: Path, **extra) -> dict:
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": _reviewed(root),
        "session_id": SELF, "run_id": RUN, **extra,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _printed_params(error: str) -> dict:
    match = re.search(r"coordinator-invoke dispatch\.land_run --from-task-output \S+ '(\{.*?\})'", error)
    assert match is not None, error
    return json.loads(match.group(1))


def _tree(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def test_overlap_refusal_prints_a_release_line_that_lands(repo):
    _author(repo, "a.py", "a\n")
    _author(repo, "b.py", "b\n")
    _declare(repo, [("C1", ["a.py"]), ("C2", ["b.py", "a.py"])])
    held = [{"chunk": "C2", "reason": "halted"}]

    refused = _land(repo, held=held)

    assert refused["refused"] == "held-landed-overlap"
    assert "params.release_held" not in refused["error"]
    printed = _printed_params(refused["error"])
    assert printed == {"release_held": ["C2"]}
    out = _land(repo, held=held, **printed)
    assert out["committed"] is True, out
    assert "a.py" in _tree(repo)


def test_ledgerless_blob_drift_prints_no_revise_target_and_a_withhold_line_that_lands(repo):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "b.py", "b = 1\n")
    _declare(repo, [("C1", ["a.py"]), ("C2", ["b.py"])])
    _write(repo, "a.py", "a = 2  # edited after the last recorded write\n")

    refused = _land(repo)

    assert refused["refused"] == "blob-drift", refused
    assert "workflow.revise" not in refused["error"]
    assert "--review-only" in refused["error"]
    printed = _printed_params(refused["error"])
    assert [e["path"] for e in printed["withhold_paths"]] == ["a.py"]
    out = _land(repo, **printed)
    assert out["committed"] is True, out
    assert "b.py" in _tree(repo) and "a.py" not in _tree(repo)
    assert "a.py" in _dirty(repo)


def test_a_ledger_bearing_blob_drift_keeps_the_revise_remedy(repo):
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    _write(repo, "a.py", "a = 2  # edited after the last recorded write\n")

    refused = _land(repo)

    assert refused["refused"] == "blob-drift", refused
    assert f"workflow.revise run_id={RUN}" in refused["error"]


def test_direct_withhold_paths_param_keeps_the_path_out_without_a_record_dir(repo):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "b.py", "b = 1\n")
    _declare(repo, [("C1", ["a.py"]), ("C2", ["b.py"])])

    out = _land(repo, withhold_paths=[{"path": "a.py", "reason": "peer owns it"}])

    assert out["committed"] is True, out
    assert "a.py" not in _tree(repo) and "b.py" in _tree(repo)
    assert "a.py" in _dirty(repo)


def test_a_malformed_withhold_paths_refuses_before_any_write(repo):
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo, withhold_paths=[{"path": 3}])

    assert out["committed"] is False and "withhold_paths" in out["error"], out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_a_landing_trailer_and_reply_name_the_run_deleted_targets(repo):
    from coordinator_core.ops.dispatch_emit import landing_integrity
    from coordinator_core.ops.dispatch_emit import landing_retest

    _write(repo, "test_gone.py", "def test_g():\n    pass\n")
    _git(["add", "test_gone.py"], repo)
    _git(["commit", "-q", "-m", "add"], repo)
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _git(["rm", "-q", "test_gone.py"], repo)
    _git(["commit", "-q", "-m", "delete"], repo)
    record = {"run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo).strip(), "status": "no-tests", "tests": [],
              "tests_run": 0, "dropped": ["test_gone.py"], "covered": {}}
    path = landing_retest.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])

    out = _land(repo, run_base_sha=base)

    assert out["committed"] is True, out
    assert "test_gone.py" in out["targeted_tests_note"]
    assert "Targeted-Tests-Note: not-run: run deleted test_gone.py" in _git(["log", "-1", "--format=%B"], repo)


def _review_only_fixture(repo: Path, *, edited_after_review: bool) -> dict:
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])], review_only=True)
    now = time.time()
    _write(repo, "a.py", "a = 2  # edited after the executor's recorded write\n")
    _write(repo, "prep.md", "test-runner prep: 186 passed, 0 failed\n")
    os.utime(repo / "a.py", (now - 10, now - 10))
    os.utime(repo / "prep.md", (now + 100, now + 100))
    if edited_after_review:
        _write(repo, "a.py", "a = 3  # peer rewrite after the review\n")
        os.utime(repo / "a.py", (now + 200, now + 200))
    review = {"integration_stem": "rev-stem", "slices": 0, "fixes": 0, "prep_sidecar": "prep.md", "wave_reviews": _reviewed(repo)["wave_reviews"]}
    return _land(repo, inline_review=review)


def test_review_only_landing_refreshes_the_expected_blob(repo):
    out = _review_only_fixture(repo, edited_after_review=False)

    assert out["committed"] is True, out
    assert "a.py" in _tree(repo)


def test_review_only_landing_still_refuses_a_rewrite_after_the_review(repo):
    out = _review_only_fixture(repo, edited_after_review=True)

    assert out["committed"] is False and out["refused"] == "blob-drift", out


from coordinator_core.ops.dispatch_emit import clean_overlay_verify as _cov  # noqa: E402
from coordinator_core.ops.dispatch_emit import measured_rev as _mr  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed as _reviewed_fixture  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2 as _RUN2, halted as _halted  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as _product_repo  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (  # noqa: E402
    FIXTURE as _FIXTURE,
    LANDING as _LANDING,
    _VERDICT as _LEDGER_VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage  # noqa: E402

_PASS = {"status_after": "pass"}


class TestTimedOutOverlay:
    """A timed-out overlay lands with an advisory only when the run's own reverify passed.

    Class-local ``repo``/``halted`` shadow this module's plain-git ``repo`` with the product-commit one."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        return _product_repo.__wrapped__(tmp_path, monkeypatch)

    @pytest.fixture
    def halted(self, repo, monkeypatch):
        return _halted.__wrapped__(repo, monkeypatch)

    def _land(self, halted, monkeypatch, **review):
        def boom(*a, **k):
            raise AssertionError("land_run must not run the overlay verify")

        monkeypatch.setattr(_cov, "verify", boom)
        _track_fixture(halted)
        resume, token = _stage(halted)
        measured = _mr.measured_rev(_RUN2, _LANDING, repo_root=halted)
        _cov.write_verdict(
            halted, _RUN2, measured, _cov.OverlayVerdict("inconclusive", "pytest timed out", ("t/x.py",), timed_out=True),
        )
        _write(halted, _FIXTURE, '{"a": 9, "dirty": true}\n')
        return land_run._handler(
            {
                "script_path": "run2.mjs", "run_id": _RUN2, "run_token": token, "incomplete_chunks": [],
                "inline_review": _reviewed_fixture(**review),
                "session_id": "11111111-2222-3333-4444-555555555555", "ledger_verdict": _LEDGER_VERDICT,
                "resume_ledger": resume,
            },
            repo_root=halted / ".git",
        )

    def test_lands_with_an_advisory_naming_the_timeout_when_the_reverify_passed(self, halted, monkeypatch):
        result = self._land(halted, monkeypatch, reverify=_PASS)

        assert result["committed"] is True, result
        assert "timed out" in result["clean_overlay_verify"]["advisory"], result
        assert _git(["log", "-1", "--format=%(trailers:key=Overlay,valueonly)"], halted).strip().startswith("inconclusive")

    @pytest.mark.parametrize(
        "review",
        [{}, {"reverify": {"status_after": "fail"}}, {"reverify": {"status_after": "pass", "failing": ["t"]}}],
    )
    def test_still_refuses_when_the_reverify_did_not_pass(self, halted, monkeypatch, review):
        result = self._land(halted, monkeypatch, **review)

        assert result["committed"] is False and result["refused"] == "verify-overlay-unverified", result
