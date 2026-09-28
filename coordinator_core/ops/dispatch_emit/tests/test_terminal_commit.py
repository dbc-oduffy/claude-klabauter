"""dispatch.terminal_commit -- AC15, AC17, AC18.

Drives the handler directly against a real temp git repo (mirrors
coordinator_core/ops/ceremony/tests/test_commit_v2_ledger_row.py's shape),
exercising it through ``ceremony.commit_v2`` in-process exactly as the
handler itself does (no mock of commit_v2 -- the whole point of D3 is ONE
real commit_v2 call).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _call(repo: Path, params: dict) -> dict:
    return terminal_commit._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _write_script(repo: Path, request: CommitRequest, name: str = "run.mjs") -> str:
    marker = render_marker(request)
    text = "// emitted script\n"
    if marker is not None:
        text += marker + "\n"
    (repo / name).write_text(text, encoding="utf-8")
    return name


def test_op_registers_scope_and_classification():
    # AC18. Import the owning module directly rather than relying on
    # coordinator_core.ops' eager-import list -- that list is
    # coordinator_core/ops/__init__.py's own file, outside this row's
    # footprint (M3.C4's declared write, per the cross-plan order this row's
    # body names); get_op_handler's lazy-miss fallback still resolves this
    # key via OP_MODULE_MAP regardless.
    import coordinator_core.ops.dispatch_emit.terminal_commit  # noqa: F401

    assert get_op_handler("dispatch.terminal_commit") is not None

    from coordinator_core import op_scopes

    assert op_scopes.OP_KEY_SCOPE["dispatch.terminal_commit"] == "common_dir"

    from coordinator_core.authz import classification

    assert (
        classification.OP_CLASSIFICATION["dispatch.terminal_commit"]
        == classification.OpClass.MUTATING
    )


def test_a_script_with_no_marker_commits_nothing(repo):
    (repo / "run.mjs").write_text("// nothing here\n", encoding="utf-8")
    out = _call(repo, {"script_path": "run.mjs", "incomplete_chunks": []})
    assert out == {"committed": False, "nothing_to_commit": True}


def test_lands_one_commit_over_done_chunks_only(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C4", title="t4", paths=("b.py",)),
            ChunkCommit(id="C5", title="t5", paths=("missing-incomplete.py",)),
        ),
        deliverable_id="dlv-abc123",
        session_id=None,
    )
    script = _write_script(repo, request)

    out = _call(
        repo,
        {"script_path": script, "incomplete_chunks": ["C5"]},
    )
    assert out["committed"] is True
    assert set(out["chunks_committed"]) == {"C3", "C4"}

    log = subprocess.run(
        ["git", "show", "--stat", "--format=%s%n%b", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert "C3, C4: t3; t4" in log
    assert "Deliverable-Id: dlv-abc123" in log
    assert "a.py" in log
    assert "b.py" in log
    assert "missing-incomplete.py" not in log


def test_inline_review_trailer(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
    )
    script = _write_script(repo, request)
    out = _call(
        repo,
        {
            "script_path": script,
            "incomplete_chunks": [],
            "inline_review": {"integration_stem": "stem1", "slices": 3, "fixes": 2},
        },
    )
    assert out["committed"] is True
    log = subprocess.run(
        ["git", "show", "-s", "--format=%B", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert "Inline-Review: applies stem1 -- execute-review: 3 slices, 2 fixes" in log


def test_refuses_inline_review_missing_stem_or_slices(repo):
    """example-retrieval-repo EM memo 2026-09-28-...-trailer-none: the digest handed
    `inline_review` with no `integration_stem`/`slices` (post-review-
    integrator-retirement drift), and this op rendered them as the literal
    string 'None' in the landed trailer. Refuse instead of writing None."""
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
    )
    script = _write_script(repo, request)

    missing_stem = _call(
        repo,
        {
            "script_path": script,
            "incomplete_chunks": [],
            "inline_review": {"integration_stem": None, "slices": 3, "fixes": 2},
        },
    )
    assert missing_stem["committed"] is False
    assert "Inline-Review: applies" not in missing_stem["error"]
    assert "integration_stem" in missing_stem["error"] or "slices" in missing_stem["error"]

    missing_slices = _call(
        repo,
        {
            "script_path": script,
            "incomplete_chunks": [],
            "inline_review": {"integration_stem": "stem1", "slices": None, "fixes": 2},
        },
    )
    assert missing_slices["committed"] is False

    # No commit landed either time -- HEAD is unmoved.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert "Inline-Review" not in log


def test_drops_absent_untracked_path_and_reports_it(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py", "never-created.py")),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert "never-created.py" in out["dropped_absent"]


def test_refuses_an_undeclared_deletion_of_a_tracked_path(repo):
    (repo / "tracked.py").write_text("x\n", encoding="utf-8")
    _git(["add", "tracked.py"], repo)
    _git(["commit", "-q", "-m", "add tracked"], repo)
    (repo / "tracked.py").unlink()

    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("tracked.py",)),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert "undeclared deletion" in out["error"]


def test_refuses_an_unknown_incomplete_id(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": ["C99"]})
    assert out["committed"] is False
    assert "C99" in out["error"]


def test_refuses_a_prefix_chunk_whose_report_has_no_claim_list(repo):
    (repo / "report.md").write_text("no claims here\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(
                id="C3", title="t3", prefixes=("state/audits/",), report="report.md"
            ),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert "C3" in out["error"]
    assert "created-under-prefix" in out["error"]


def test_commits_files_claimed_under_own_prefix_only(repo):
    (repo / "state").mkdir()
    (repo / "state" / "audits").mkdir()
    (repo / "state" / "audits" / "x.md").write_text("x\n", encoding="utf-8")
    (repo / "state" / "audits" / "peer.md").write_text("peer\n", encoding="utf-8")
    (repo / "report.md").write_text(
        "created-under-prefix: state/audits/x.md\n", encoding="utf-8"
    )
    request = CommitRequest(
        chunks=(
            ChunkCommit(
                id="C3", title="t3", prefixes=("state/audits/",), report="report.md"
            ),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert out["prefix_files"] == ["state/audits/x.md"]

    log = subprocess.run(
        ["git", "show", "--stat", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert "state/audits/x.md" in log
    assert "peer.md" not in log


def test_forwards_session_id_when_uuid_shaped(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    script = _write_script(repo, request)
    sid = "c4e53fa2-2bda-51b1-833a-cf629f2b0cb6"
    out = _call(
        repo, {"script_path": script, "incomplete_chunks": [], "session_id": sid}
    )
    assert out["committed"] is True
    log = subprocess.run(
        ["git", "show", "-s", "--format=%B", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert sid in log


def test_all_clean_request_is_nothing_to_commit_without_error(repo):
    _git(["add", "README.md"], repo)  # already committed; nothing new to add
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("README.md",)),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out.get("nothing_to_commit") is True
    assert "error" not in out or out["committed"] is False


def test_process_time_and_spawns_on_a_40_path_request(repo):
    # AC17: process time < 200ms, zero subprocess spawns for a clean 40-path
    # request (no eol-fallback path in this fixture).
    paths = []
    for i in range(40):
        name = f"f{i}.py"
        (repo / name).write_text(f"{i}\n", encoding="utf-8")
        paths.append(name)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=tuple(paths)),),
    )
    script = _write_script(repo, request)

    import subprocess as _subprocess

    spawn_count = 0
    real_run = _subprocess.run

    def _counting_run(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return real_run(*args, **kwargs)

    orig = _subprocess.run
    _subprocess.run = _counting_run
    try:
        start = time.process_time()
        out = _call(repo, {"script_path": script, "incomplete_chunks": []})
        elapsed_ms = (time.process_time() - start) * 1000
    finally:
        _subprocess.run = orig

    assert out["committed"] is True
    assert elapsed_ms < 200, f"process time {elapsed_ms}ms exceeds the 200ms bar"
    assert spawn_count == 0, f"expected zero spawns, saw {spawn_count}"
