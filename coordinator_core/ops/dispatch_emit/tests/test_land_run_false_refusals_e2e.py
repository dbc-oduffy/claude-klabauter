"""Exit test: a finished run lands through `dispatch.land_run` at default params when (a) peer dirt sits in
the verified reach under a green clean-overlay verdict, or (b) a sibling commit since base added lines to a
shared file the run only adds to. A red, inconclusive or missing verdict, and a landing that drops a sibling's line,
still refuse."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_land_run_foreign_dirt_in_reach_e2e as reach
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git as _rgit, _write as _rwrite, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
RUN = "plan-20261021T100000-aaaaaa"
SCRIPT = "run-a.mjs"
SHARED = "src/shared.py"
BASE_TEXT = "a = 1\nb = 2\nc = 3\n"
SIBLING_TEXT = "a = 1\nsibling = 9\nb = 2\nc = 3\n"


def _write_overlay(root, verdict: str) -> None:
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    measured = land_run._measured_record(root, RUN2)
    assert measured and not measured.get("unreadable"), measured
    cov.write_verdict(root, RUN2, measured, cov.OverlayVerdict(verdict, "pytest tail", ("t.py",)))


def _refused(root, head, out, code) -> None:
    assert out["committed"] is False and out["refused"] == code, out
    assert reach.FIXTURE in out["paths"]
    assert reach._git(root, "rev-parse", "HEAD") == head
    assert reach.FIXTURE in reach._git(root, "status", "--porcelain", "--", reach.FIXTURE)


def _landed_over_dirt(root, out, verdict: str = "green") -> None:
    assert out["committed"] is True, out
    assert out["clean_overlay_verify"]["verdict"] == verdict
    assert reach.FIXTURE not in reach._git(root, "show", "--name-only", "--format=", out["sha"]).split()
    assert reach.FIXTURE in reach._git(root, "status", "--porcelain", "--", reach.FIXTURE)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def stale_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _write(root, SHARED, BASE_TEXT)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _stale_run(root: Path, run_text: str) -> str:
    """Run base, a sibling commit adding a line to SHARED, then the run's own SHARED bytes `run_text`."""
    base = _git(["rev-parse", "HEAD"], root).strip()
    _write(root, SHARED, SIBLING_TEXT)
    _git(["add", SHARED], root)
    _git(["commit", "-q", "-m", "sibling adds a line"], root)
    _write(root, SHARED, run_text)
    session_scope.touch(SELF, SHARED, cwd=str(root))
    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=SHARED,
        content_hash=touch_record.compute_content_hash(root / SHARED), kind="w",
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="row one", paths=(SHARED,)),), plan_path=None)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return base


def _land(root: Path, base: str) -> dict:
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF, "run_id": RUN, "run_base_sha": base},
        repo_root=root / ".git",
    )


def test_a_sibling_commit_since_base_on_a_shared_file_the_run_only_adds_to_lands(stale_repo):
    base = _stale_run(stale_repo, SIBLING_TEXT + "run = 4\n")

    out = _land(stale_repo, base)

    assert out["committed"] is True, out
    assert out.get("refused") is None
    assert SHARED in _git(["show", "--name-only", "--format=", out["sha"]], stale_repo).split()


def test_a_landing_that_drops_the_siblings_line_still_refuses(stale_repo):
    base = _stale_run(stale_repo, "a = 1\nb = 2\nrun = 4\n")
    head = _git(["rev-parse", "HEAD"], stale_repo)

    out = _land(stale_repo, base)

    assert out["committed"] is False and out["refused"] == "since-base-overlap", out
    assert _git(["rev-parse", "HEAD"], stale_repo) == head


def _dirty_reach(root, verdict):
    reach._track_fixture(root)
    resume, token = _stage(root)
    _rwrite(root, reach.FIXTURE, '{"a": 2}\n')
    mr.measured_rev(RUN2, reach.LANDING, repo_root=root)
    if verdict:
        _write_overlay(root, verdict)
    return resume, token


def test_peer_dirt_in_the_verified_reach_lands_on_a_green_overlay_verdict(halted):  # noqa: F811
    resume, token = _dirty_reach(halted, "green")

    out = _terminal_with(halted, token, resume, reach._VERDICT)

    _landed_over_dirt(halted, out)


def test_peer_dirt_in_the_verified_reach_refuses_on_a_red_verdict(halted):  # noqa: F811
    resume, token = _dirty_reach(halted, "red")
    head = _rgit(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, reach._VERDICT)

    _refused(halted, head, out, land_run.VERIFY_OVERLAY_RED)


def test_peer_dirt_in_the_verified_reach_refuses_as_unverified_when_no_verdict_was_written(halted):  # noqa: F811
    """A missing verdict is unverified: the landing refuses and commits nothing."""
    resume, token = _dirty_reach(halted, None)
    head = _rgit(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, reach._VERDICT)

    _refused(halted, head, out, land_run.VERIFY_OVERLAY_UNVERIFIED)
