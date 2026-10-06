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

import coordinator_core.ipc
from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.inventory_mint import as_git_pathspec
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


# A marked run lands only with review-stage output; tests that aren't about it carry this one.
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _call(repo: Path, params: dict) -> dict:
    return terminal_commit._handler({"inline_review": _REVIEWED, **params}, repo_root=repo / ".git")


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
    assert out == {"committed": False, "nothing_to_commit": True, "stranded": {}}


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


def test_landed_chunks_leave_the_reported_incomplete_list_and_are_committed(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C5", title="t5", paths=("b.py",)),
        ),
    )
    script = _write_script(repo, request)

    out = _call(
        repo,
        {"script_path": script, "incomplete_chunks": ["C5"], "landed_chunks": ["C5"]},
    )
    assert out["committed"] is True, out
    assert set(out["chunks_committed"]) == {"C3", "C5"}
    assert out["incomplete_chunks"] == []
    assert out["stranded"] == {}


def test_a_chunk_not_named_landed_stays_incomplete(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C5", title="t5", paths=("b.py",)),
        ),
    )
    script = _write_script(repo, request)

    out = _call(
        repo,
        {"script_path": script, "incomplete_chunks": ["C5"], "landed_chunks": ["C9"]},
    )
    assert out["incomplete_chunks"] == ["C5"]
    assert "C5" in out["stranded"]


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


def test_inline_review_stays_a_git_trailer_beside_a_declared_deletion(repo):
    # The removal line is not a trailer; sharing a paragraph with it hid the
    # anchor from git's trailer parser, and review_stamp mint found no commit.
    (repo / "tracked.py").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "tracked.py"], repo)
    _git(["commit", "-q", "-m", "add tracked"], repo)
    (repo / "tracked.py").unlink()
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("tracked.py", "a.py")),),
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
    assert out["committed"] is True, out
    trailers = subprocess.run(
        ["git", "show", "-s", "--format=%(trailers:key=Inline-Review,valueonly=true)", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert "applies stem1 -- execute-review: 3 slices, 2 fixes" in trailers


def test_zero_stage_inline_review_runs_bookkeep_wave_and_review_stamp_mints(repo):
    """2026-09-28 PM follow-up: `bookkeep_wave` has no production caller
    until this wiring exists. End-to-end, no manual `bookkeep_wave` call in
    this test: a zero-stage digest's `inline_review` shape (carrying
    `wave_sidecar_paths`/`prep_sidecar`/`plan_id`, exactly what
    `wake_digest.py`'s zero-stage branch now builds) goes straight into
    `dispatch.terminal_commit`, which runs the mechanical bookkeeping step
    itself, lands the record in the SAME commit, and
    `review_stamp.mint` succeeds against it afterward."""
    import hashlib

    from coordinator_core.ops import review_stamp
    from coordinator_core.ops.review_mint.wave_bookkeeping import (
        review_wave_bookkeeping_stem,
    )

    session_id = "11111111-2222-3333-4444-555555555555"
    plan_id = "pln-terminal-commit-e2e-abc123"

    share = repo / ".coordinator-local" / "subagent-share" / session_id
    share.mkdir(parents=True)

    prep_rel = f".coordinator-local/subagent-share/{session_id}/2026-09-28-prep.md"
    (repo / prep_rel).write_text(
        "---\n"
        "agent_type: coordinator:test-runner\n"
        "run_base_sha: deadbeef\n"
        "product_files: [coordinator_core/foo.py]\n"
        "foreign_claims: []\n"
        "slices: [{id: A}]\n"
        "whole_diff_sidecars:\n"
        f"  delivery: .coordinator-local/subagent-share/{session_id}/2026-09-28-delivery.md\n"
        "---\nprep\n",
        encoding="utf-8",
    )
    (share / "2026-09-28-delivery.md").write_text(
        "---\nagent_type: coordinator:delivery-verifier\nverdict: PASS\n---\ndelivery\n",
        encoding="utf-8",
    )

    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    baseline_hash = hashlib.sha256(b"x = 1\n").hexdigest()
    wave_path = share / "2026-09-28-wave-code-reviewer.md"
    wave_path.write_text(
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        "applied: 1\n"
        "baseline_sha256:\n"
        f"  coordinator_core/foo.py: {baseline_hash}\n"
        "---\n"
        "## Findings\n\n### Finding 1\nSomething.\n"
        "## Findings Ledger\n\n```json\n"
        '[{"id": "finding-1", "file": "coordinator_core/foo.py", "before": "x = 1", "after": "x = 2"}]\n'
        "```\n",
        encoding="utf-8",
    )
    wave_sidecar_rel = f".coordinator-local/subagent-share/{session_id}/2026-09-28-wave-code-reviewer.md"

    build_test = share / "2026-09-28-test-runner.md"
    build_test.write_text(
        "---\nagent_type: coordinator:test-runner\nstatus: pass\nrun: 1\nfailed: 0\n---\nbuild/test\n",
        encoding="utf-8",
    )

    stem = review_wave_bookkeeping_stem(plan_id, session_id)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("coordinator_core/foo.py",)),),
        session_id=session_id,
    )
    script = _write_script(repo, request)

    out = _call(
        repo,
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": session_id,
            "inline_review": {
                "integration_stem": stem,
                "slices": 1,
                "fixes": 1,
                "prep_sidecar": prep_rel,
                "plan_id": plan_id,
                "wave_sidecar_paths": [wave_sidecar_rel],
            },
        },
    )
    assert out["committed"] is True, out

    record_path = share / f"{stem}.md"
    assert record_path.is_file()
    log = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert f"{stem}.md" in log, "the bookkeeping record did not land in the terminal commit"

    plan_path = repo / "docs" / "plans" / "example.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(f"---\nplan_id: {plan_id}\n---\n# Example\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "add plan"], repo)

    stamp = review_stamp.mint(plan_path, repo, build_test_path=str(build_test))
    assert stamp["fixes_applied"] == 1
    assert stamp["unresolved"] == []


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


def test_commits_a_literal_wrapped_bracketed_path(repo):
    """content-root-em memo 2026-10-05-...-drops-literal-wrapped-paths: emit
    hands the marker `as_git_pathspec`-wrapped paths, and a tracked, modified
    App Router file was classed absent and left out of the commit."""
    route = "src/app/(ddct)/photo/[sha256]/route.ts"
    (repo / route).parent.mkdir(parents=True)
    (repo / route).write_text("v1\n", encoding="utf-8")
    _git(["add", "--", f":(literal){route}"], repo)
    _git(["commit", "-q", "-m", "route"], repo)
    (repo / route).write_text("v2\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=(as_git_pathspec(route),)),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert out["dropped_absent"] == []
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert route not in status


def test_refuses_a_path_carrying_other_pathspec_magic(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py", ":(top)a.py")),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert out["refused"] == "malformed-request"


def test_stages_a_declared_write_gone_from_disk_as_a_deletion(repo):
    (repo / "tracked.py").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "tracked.py"], repo)
    _git(["commit", "-q", "-m", "add tracked"], repo)
    (repo / "tracked.py").unlink()

    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("tracked.py", "a.py")),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert out["deleted_paths"] == ["tracked.py"]
    ls = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.split()
    assert "tracked.py" not in ls
    assert "a.py" in ls


def test_refuses_an_undeclared_deletion_of_a_tracked_path(repo):
    # A prefix claim is not a declared write: its absence stays refused.
    (repo / "state").mkdir()
    (repo / "state" / "gone.md").write_text("g\n", encoding="utf-8")
    _git(["add", "state/gone.md"], repo)
    _git(["commit", "-q", "-m", "add gone"], repo)
    (repo / "state" / "gone.md").unlink()
    (repo / "report.md").write_text(
        "created-under-prefix: state/gone.md\n", encoding="utf-8"
    )
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", prefixes=("state/",), report="report.md"),
        ),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert "undeclared deletion" in out["error"]


def test_an_incomplete_id_the_marker_never_carried_is_reported_not_refused(repo):
    # A row that never started is in the digest's incomplete list but not the marker.
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": ["C99"]})
    assert out["committed"] is True
    assert out["unmarked_incomplete"] == ["C99"]


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


# Three git processes via run_git: two for the commit, plus commit_v2's one batched
# check-ignore over the paths new to HEAD (example-stats-repo b7d67aa385ac). A fourth means a
# new spawn joined the hot path.
_EXPECTED_SPAWNS = 3


def test_process_time_and_spawns_on_a_40_path_request(repo):
    # AC17: process time < 200ms and a pinned spawn count for a clean 40-path
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

    # Count at Popen, not run: git/run.py::run_git spawns through Popen, so a
    # run-only counter reads zero whatever the op does.
    spawn_count = 0
    real_init = _subprocess.Popen.__init__

    def _counting_init(self, *args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        real_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _counting_init
    try:
        start = time.process_time()
        out = _call(repo, {"script_path": script, "incomplete_chunks": []})
        elapsed_ms = (time.process_time() - start) * 1000
    finally:
        _subprocess.Popen.__init__ = real_init

    assert out["committed"] is True
    assert elapsed_ms < 200, f"process time {elapsed_ms}ms exceeds the 200ms bar"
    assert spawn_count == _EXPECTED_SPAWNS, f"expected {_EXPECTED_SPAWNS} spawns, saw {spawn_count}"


_PLAN = """---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
- id: C4
  title: t4
  change_kind: code-edit
  surface: a.py
  deferred: false
  body: |
    multi-line
    body
- id: C5
  title: t5
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
- id: C9
  title: t9
  change_kind: code-edit
  surface: a.py
  disposition: coded
  disposition_ref: abc1234
```

Trailing prose.
"""


def _show(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "show", *args], cwd=str(repo), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout


def _spine(text: str) -> list:
    import yaml

    from coordinator_core.frontmatter.body_blocks import locate_fenced_block

    return yaml.safe_load(locate_fenced_block(text).body)


def test_stamps_committed_rows_coded_in_a_second_plan_only_commit(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C4", title="t4", paths=("b.py",)),
            ChunkCommit(id="C5", title="t5", paths=("c.py",)),
        ),
        plan_path="docs/plan.md",
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": ["C5"]})
    assert out["committed"] is True
    assert out["rows_coded"] == {"docs/plan.md": ["C3", "C4"]}
    sha = out["sha"]
    assert out["coded_sha"] and out["coded_sha"] != sha
    # Product commit carries no plan edit; the stamp commit carries only it.
    assert _show(repo, f"{sha}:docs/plan.md") == _PLAN
    stamp = _show(repo, "--stat", "--format=%s", "HEAD")
    assert f"mark 2 rows coded ({sha[:7]})" in stamp
    assert "docs/plan.md" in stamp and "a.py" not in stamp

    committed = _show(repo, "HEAD:docs/plan.md")
    assert committed == (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    rows = _spine(committed)
    assert [r["id"] for r in rows] == ["C5", "C3", "C4", "C9"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["C5"]["disposition"] == "open"  # incomplete: untouched
    assert by_id["C3"]["disposition"] == "coded"
    assert by_id["C4"]["disposition"] == "coded"
    assert by_id["C3"]["disposition_ref"] == sha
    assert by_id["C4"]["body"] == "multi-line\nbody\n"
    assert committed.endswith("```\n\nTrailing prose.\n")

    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    assert check_plan_tasks_source(_PLAN) is None
    assert check_plan_tasks_source(committed) is None


def test_inventory_spine_maps_rows_back_to_source_plans(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    inv = repo / "state" / "mise-inventory"
    inv.mkdir(parents=True)
    (inv / "run1.md").write_text(
        "---\nrun_id: run1\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| P1 | `docs/plan.md` | s | `a.py` | — | v | S | pending |\n"
        "| P1-C5 | `docs/plan.md` | s | `b.py` | — | v | S | pending |\n",
        encoding="utf-8",
    )
    spine_text = "---\nrun_id: run1\nderived_from: mise inventory record\n---\n\n## Tasks\n"
    (inv / "run1.spine.md").write_text(spine_text, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="P1.C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="P1-C5", title="t5", paths=("b.py",)),
            ChunkCommit(id="P1.C4", title="t4", paths=("c.py",)),
        ),
        plan_path="state/mise-inventory/run1.spine.md",
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": ["P1.C4"]})
    assert out["committed"] is True
    assert out["rows_coded"] == {"docs/plan.md": ["C3", "C5"]}
    by_id = {r["id"]: r for r in _spine(_show(repo, "HEAD:docs/plan.md"))}
    assert by_id["C3"]["disposition"] == "coded"
    assert by_id["C5"]["disposition"] == "coded"
    assert by_id["C5"]["disposition_ref"] == out["sha"]
    assert by_id["C4"].get("disposition", "open") == "open"
    # The minted spine is never stamped.
    assert (inv / "run1.spine.md").read_text(encoding="utf-8") == spine_text


def test_stamp_failure_is_reported_and_product_commit_stands(repo, monkeypatch):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    real_lookup = get_op_handler
    real = real_lookup("ceremony.commit_v2")
    calls = []

    def flaky(params, repo_root):
        calls.append(params)
        if len(calls) == 2:
            return {"committed": False, "sha": None, "error": "refused"}
        return real(params, repo_root)

    def fake_lookup(key, msg=None):
        return flaky if key == "ceremony.commit_v2" else real_lookup(key, msg)

    monkeypatch.setattr(coordinator_core.ipc, "get_op_handler", fake_lookup)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        plan_path="docs/plan.md",
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True and out["sha"]
    assert "refused" in out["coded_stamp_error"]
    assert "rows_coded" not in out
    assert (repo / "docs" / "plan.md").read_text(encoding="utf-8") == _PLAN
    assert _show(repo, "--format=%H", "-s", "HEAD").strip() == out["sha"]


def _branch_request(repo: Path, expected) -> str:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        expected_branch=expected,
    )
    return _write_script(repo, request)


def _head(repo: Path) -> str:
    return _show(repo, "--format=%H", "-s", "HEAD").strip()


def test_branch_mismatch_refuses_and_head_is_unchanged(repo):
    script = _branch_request(repo, "some-other-branch")
    before = _head(repo)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert out["refused"] == "branch-mismatch"
    assert out["expected_branch"] == "some-other-branch"
    assert out["observed_branch"]
    assert _head(repo) == before


def test_branch_mismatch_writes_no_bookkeeping_record(repo):
    script = _branch_request(repo, "some-other-branch")
    out = _call(
        repo,
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": "12345678-1234-4234-8234-123456789abc",
            "inline_review": {
                "integration_stem": "stem1",
                "slices": 1,
                "fixes": 0,
                "wave_sidecar_paths": ["w.md"],
                "prep_sidecar": "p.md",
                "plan_id": "plan-x",
            },
        },
    )
    assert out["refused"] == "branch-mismatch"
    assert not (repo / ".coordinator-local").exists()


def test_detached_head_refuses(repo):
    script = _branch_request(repo, None)
    _git(["checkout", "-q", "--detach"], repo)
    before = _head(repo)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert out["refused"] == "detached-head"
    assert out["observed_branch"] == "HEAD"
    assert _head(repo) == before


@pytest.mark.parametrize("inline_review", [None, {"slices": 2, "fixes": 0}, {"integration_stem": "s", "fixes": 0}])
def test_a_run_with_no_review_stage_output_refuses_before_any_write(repo, inline_review):
    script = _branch_request(repo, None)
    before = _head(repo)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "inline_review": inline_review},
        repo_root=repo / ".git",
    )
    assert out["committed"] is False
    assert out["refused"] == "unreviewed"
    assert _head(repo) == before


def test_matching_branch_commits_with_branch_check_ok(repo):
    head_ref = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    observed = head_ref.removeprefix("ref: refs/heads/")
    script = _branch_request(repo, observed)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert out["branch_check"] == "ok"


def test_marker_without_expected_branch_commits_not_recorded(repo):
    script = _branch_request(repo, None)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True
    assert out["branch_check"] == "not-recorded"


def test_unreadable_head_refuses(repo, monkeypatch):
    script = _branch_request(repo, None)
    before = _head(repo)
    monkeypatch.setattr(terminal_commit, "head_branch", lambda _root: None)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is False
    assert out["refused"] == "branch-unreadable"
    assert _head(repo) == before


def test_a_digest_carrying_stage_returns_mints_the_review_stamp_into_the_coded_commit(repo):
    """The run record holds the stage returns, so the op mints the plan's
    `review_stamp` against the commit it just landed -- no EM `mint` step, no
    trailer walk -- and the stamp rides the coded-stamp commit."""
    plan = _PLAN.replace("title: p\n", "title: p\nplan_id: pln-x-123456\n", 1)
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(plan, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        plan_path="docs/plan.md",
    )
    script = _write_script(repo, request)
    session = "11111111-2222-3333-4444-555555555555"
    inline_review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping",
        "slices": 1,
        "fixes": 0,
        "plan_id": "pln-x-123456",
        "prep_sidecar": None,
        "wave_sidecar_paths": [],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "a.py exists", "sidecar": None},
    }
    out = _call(repo, {"script_path": script, "incomplete_chunks": [], "session_id": session,
                       "inline_review": inline_review})
    assert out["committed"] is True
    assert out["review_stamp"] == "minted", out.get("review_stamp_refusal")
    committed_plan = _show(repo, "HEAD:docs/plan.md")
    assert f"terminal_commit_sha: {out['sha']}" in committed_plan
    assert out["rows_coded"] == {"docs/plan.md": ["C3"]}
    record = ".coordinator-local/subagent-share/" + session + "/pln-x-123456.review-wave-bookkeeping.md"
    assert (repo / record).is_file()
    # Open spine rows remain, so the plan is not complete: the flip refuses.
    assert out["plan_status"] == "refused"
    assert "C4, C5" in out["plan_status_refusal"]


_SINGLE_ROW_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
prime_exit_criterion:
  text: a.py exists
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def test_a_met_criterion_on_the_last_open_row_flips_the_plan_implemented(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_SINGLE_ROW_PLAN, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path="docs/plan.md")
    script = _write_script(repo, request)
    inline_review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
        "criterion": {"status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md"},
    }
    import subprocess as _subprocess

    spawns = 0
    real_init = _subprocess.Popen.__init__

    def _counting_init(self, *a, **kw):
        nonlocal spawns
        spawns += 1
        real_init(self, *a, **kw)

    _subprocess.Popen.__init__ = _counting_init
    try:
        start = time.process_time()
        out = _call(repo, {"script_path": script, "incomplete_chunks": [],
                           "session_id": "11111111-2222-3333-4444-555555555555", "inline_review": inline_review})
        elapsed_ms = (time.process_time() - start) * 1000
    finally:
        _subprocess.Popen.__init__ = real_init
    print(f"COMPOSED terminal_commit: {elapsed_ms:.0f}ms process, {spawns} spawns")
    assert out["review_stamp"] == "minted", out.get("review_stamp_refusal")
    assert out["plan_status"] == "implemented", out.get("plan_status_refusal")
    assert elapsed_ms < 500, f"composed terminal_commit {elapsed_ms:.0f}ms exceeds the 500ms bar"
    committed = _show(repo, "HEAD:docs/plan.md")
    assert "status: implemented" in committed
    assert "falsifier_output: a.py exists at HEAD" in committed


def test_a_refused_mint_is_reported_and_the_product_commit_stands(repo):
    plan = _PLAN.replace("title: p\n", "title: p\nplan_id: pln-x-123456\n", 1)
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(plan, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path="docs/plan.md")
    script = _write_script(repo, request)
    inline_review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "FAIL", "product_files": 1, "claims_unbacked": 2,
                     "unbacked": [{"claim": "adds retry", "anchor": "no diff hunk in x.py"},
                                  {"claim": "pins it", "anchor": "no test names it"}]},
        "tests": {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "o", "sidecar": None},
    }
    out = _call(repo, {"script_path": script, "incomplete_chunks": [],
                       "session_id": "11111111-2222-3333-4444-555555555555", "inline_review": inline_review})
    assert out["committed"] is True and out["sha"]
    assert out["review_stamp"] == "refused"
    assert "delivery verdict is 'FAIL'" in out["review_stamp_refusal"]
    assert "adds retry [lacked: no diff hunk in x.py]" in out["review_stamp_refusal"]
    assert "pins it [lacked: no test names it]" in out["review_stamp_refusal"]
    assert "review_stamp:" not in _show(repo, "HEAD:docs/plan.md")


def test_deleting_a_file_added_one_commit_ago_is_not_a_rollback(repo):
    # Deleting a declared write that the previous commit added restores that
    # commit's parent (absence); commit_v2's staged-rollback check refused
    # it until terminal_commit declared its own planned deletions.
    (repo / "added.py").write_text("x\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(["add", "added.py"], repo)
    _git(["commit", "-q", "-m", "add added"], repo)
    (repo / "added.py").unlink()

    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("added.py", "a.py")),),
    )
    script = _write_script(repo, request)
    out = _call(repo, {"script_path": script, "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert out["deleted_paths"] == ["added.py"]


@pytest.mark.parametrize(
    "params, field",
    [
        ("not-a-dict", "must be an object"),
        ({"incomplete_chunks": []}, "script_path is required"),
        ({"script_path": 7, "incomplete_chunks": []}, "script_path must be"),
        ({"script_path": "run.mjs", "incomplete_chunks": "C1"}, "incomplete_chunks must be"),
        ({"script_path": "run.mjs", "incomplete_chunks": [1]}, "incomplete_chunks must be"),
        (
            {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": "x"},
            "inline_review must be",
        ),
    ],
)
def test_malformed_request_is_refused_not_raised(repo, params, field):
    out = terminal_commit._handler(params, repo_root=repo / ".git")
    assert out["committed"] is False
    assert out["sha"] is None
    assert field in out["error"], out


def test_a_zero_file_pass_rerun_lands_a_fresh_review_anchor_and_stamps_the_plan(repo):
    """A FAIL run lands first; the marker-less verify-only PASS re-run (no product
    file) must still land an `Inline-Review` commit so `review_stamp` resolves the
    new review, not the stale FAIL one."""
    from coordinator_core.ops.review_stamp import mint

    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_SINGLE_ROW_PLAN, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    first = _write_script(
        repo,
        CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path="docs/plan.md"),
        name="first.mjs",
    )

    from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem

    def review(verdict: str, files: int, session: str) -> dict:
        return {
            "integration_stem": review_wave_bookkeeping_stem("pln-x-123456", session), "slices": 1, "fixes": 0,
            "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
            "prep": {"run_base_sha": "a" * 40, "product_files": files, "foreign_claims": [],
                     "slice_files": ["a.py"]},
            "delivery": {"verdict": verdict, "product_files": files, "claims_unbacked": 0},
            "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
            "criterion": {"status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md"},
        }

    s1, s2 = "11111111-2222-3333-4444-555555555555", "99999999-2222-3333-4444-555555555555"
    failed = _call(repo, {"script_path": first, "incomplete_chunks": [], "session_id": s1,
                          "inline_review": review("FAIL", 1, s1)})
    assert failed["committed"] is True and failed["review_stamp"] == "refused"

    rerun = _write_script(repo, CommitRequest(chunks=()), name="rerun.mjs")
    before = _head(repo)
    out = _call(repo, {"script_path": rerun, "incomplete_chunks": [], "plan_path": "docs/plan.md",
                       "session_id": s2, "inline_review": review("PASS", 0, s2)})
    assert out["committed"] is True, out
    assert out["sha"] != before
    assert out["review_stamp"] == "minted", out.get("review_stamp_refusal")
    assert out["plan_status"] == "implemented", out.get("plan_status_refusal")
    anchor = _show(repo, f"{out['sha']}:docs/plan.md")
    assert "review_stamp:" not in anchor
    body = subprocess.run(
        ["git", "show", "-s", "--format=%B", out["sha"]], cwd=str(repo), capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert f"Inline-Review: applies {review_wave_bookkeeping_stem('pln-x-123456', s2)}" in body

    # Resolution follows the anchor's Session-Id, not mtime: touch the stale FAIL record newest.
    import os

    old = next((repo / ".coordinator-local" / "subagent-share" / s1).glob("*.md"))
    os.utime(old, (time.time() + 100, time.time() + 100))
    # The standalone `review-stamp mint --plan` resolution walks to the new anchor.
    mint(repo / "docs" / "plan.md", repo, build_test_path=None)


def test_a_zero_file_run_that_is_not_a_pass_commits_nothing(repo):
    script = _write_script(repo, CommitRequest(chunks=()), name="rerun.mjs")
    out = _call(repo, {"script_path": script, "incomplete_chunks": [], "plan_path": "docs/plan.md",
                       "inline_review": {"delivery": {"verdict": "FAIL", "product_files": 0},
                                         "integration_stem": "s", "slices": 0}})
    assert out == {"committed": False, "nothing_to_commit": True, "stranded": {}}
