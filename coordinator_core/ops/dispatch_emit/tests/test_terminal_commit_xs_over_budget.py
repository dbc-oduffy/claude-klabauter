"""dispatch.terminal_commit refuses an XS-arm run whose whole own diff exceeds the XS budget."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "ask-xsb"
SCRIPT = f"scratch/warp/{RUN_ID}.workflow.mjs"


def _git(root, *a):
    return subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


@pytest.fixture
def root(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("k\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("t\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _call(root, paths, *, slices=0, base=None, arm="xs"):
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(paths)),), plan_path=None, version=1
    )
    script = root / SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(render_marker(request) + "\n", encoding="utf-8")
    token = run_token.mint(root / ".git", RUN_ID, SCRIPT)
    params = {
        "script_path": SCRIPT,
        "incomplete_chunks": [],
        "run_id": RUN_ID,
        "run_token": token,
        "run_base_sha": base or _git(root, "rev-parse", "HEAD"),
        **({"arm": arm} if arm else {}),
        "inline_review": {
            "integration_stem": f"{RUN_ID}.review-wave-bookkeeping",
            "slices": slices,
            "fixes": 0,
            "wave_sidecar_paths": [],
            "wave_reviews": [],
            "prep": None,
            "delivery": None,
            "wave_not_run": [],
        },
    }
    reply = tc._terminal_commit(params, root / ".git", {}, {})
    return reply, token


def _new_files(root, count, lines):
    names = []
    for i in range(count):
        rel = f"new_{i}.py"
        (root / rel).write_text("x = 1\n" * lines, encoding="utf-8")
        names.append(rel)
    return names


def test_untracked_only_over_budget_diff_is_refused_without_a_commit(root):
    head = _git(root, "rev-parse", "HEAD")
    names = _new_files(root, 3, 14)
    reply, token = _call(root, names)
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"] == {"files": 3, "lines": 42}
    assert "3 files, 42 lines" in reply["next_action"]
    assert not reply.get("committed")
    assert _git(root, "rev-parse", "HEAD") == head
    assert run_token.admits(root / ".git", RUN_ID, token, SCRIPT), "a refusal must release the run token"


def test_tracked_edit_over_the_line_budget_is_refused(root):
    (root / "tracked.txt").write_text("t\n" + "y\n" * 40, encoding="utf-8")
    reply, _ = _call(root, ["tracked.txt"])
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"]["files"] == 1 and reply["measured"]["lines"] == 40


def test_an_in_budget_diff_is_not_refused_by_the_budget(root):
    names = _new_files(root, 1, 5)
    reply, _ = _call(root, names)
    assert reply.get("refused") != "xs-over-budget", reply
    assert reply.get("committed") is True, reply


def test_a_tracked_unchanged_declared_path_counts_zero(root):
    names = _new_files(root, 2, 5)
    reply, _ = _call(root, [*names, "keep.txt", "tracked.txt"])
    assert reply.get("refused") != "xs-over-budget"


def test_a_binary_untracked_file_counts_one_file_zero_lines(root):
    (root / "blob.bin").write_bytes(b"\0" * 5000)
    names = _new_files(root, 2, 5)
    reply, _ = _call(root, ["blob.bin", *names])
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"] == {"files": 3, "lines": 10}


def test_an_unresolvable_base_fails_closed(root):
    names = _new_files(root, 1, 1)
    reply, _ = _call(root, names, base="0" * 40)
    assert reply["refused"] == "xs-over-budget"
    assert "unmeasurable" in reply["error"]


def test_an_xs_run_with_a_review_shape_is_still_gated(root):
    names = _new_files(root, 3, 14)
    reply, _ = _call(root, names, slices=1)
    assert reply["refused"] == "xs-over-budget"


def test_a_non_xs_arm_over_budget_skips_the_check(root):
    names = _new_files(root, 3, 14)
    reply, _ = _call(root, names, arm="s")
    assert reply.get("refused") != "xs-over-budget"


def test_an_unresolved_arm_is_not_gated(root):
    names = _new_files(root, 3, 14)
    reply, _ = _call(root, names, arm=None)
    assert reply.get("refused") != "xs-over-budget"


def test_undeclared_files_are_not_priced(root):
    declared = _new_files(root, 1, 3)
    for i in range(3):
        (root / f"stray_{i}.py").write_text("z = 1\n" * 12, encoding="utf-8")
    reply, _ = _call(root, declared)
    assert reply.get("refused") != "xs-over-budget", reply


def test_bookkeeping_paths_are_not_priced(root):
    declared = _new_files(root, 1, 3)
    (root / "state").mkdir()
    for i in range(4):
        (root / "state" / f"rec_{i}.md").write_text("r\n" * 50, encoding="utf-8")
    reply, _ = _call(root, declared)
    assert reply.get("refused") != "xs-over-budget", reply


def test_peer_committed_hunks_are_not_priced(root):
    base = _git(root, "rev-parse", "HEAD")
    (root / "tracked.txt").write_text("t\n" + "p\n" * 60, encoding="utf-8")
    _git(root, "commit", "-q", "-am", "peer work\n\nSession-Id: peer-session")
    names = _new_files(root, 1, 3)
    reply, _ = _call(root, names, base=base)
    assert reply.get("refused") != "xs-over-budget", reply
    assert reply.get("committed") is True, reply


def test_peer_commits_of_other_paths_are_not_priced(root):
    base = _git(root, "rev-parse", "HEAD")
    for i in range(30):
        (root / f"peer_{i}.py").write_text("p = 1\n" * 20, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "peer work")
    (root / "tracked.txt").write_text("t\n" + "o\n" * 5, encoding="utf-8")
    (root / "keep.txt").write_text("k\n" + "o\n" * 5, encoding="utf-8")
    reply, _ = _call(root, ["tracked.txt", "keep.txt"], base=base)
    assert reply.get("refused") != "xs-over-budget", reply


def test_own_oversized_diff_still_refuses_beside_peer_commits(root):
    base = _git(root, "rev-parse", "HEAD")
    (root / "peer.py").write_text("p = 1\n" * 20, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "peer work")
    (root / "tracked.txt").write_text("t\n" + "o\n" * 40, encoding="utf-8")
    reply, _ = _call(root, ["tracked.txt"], base=base)
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"] == {"files": 1, "lines": 40}


def test_own_edit_on_top_of_a_peer_commit_is_priced_from_the_peer_commit(root):
    base = _git(root, "rev-parse", "HEAD")
    (root / "tracked.txt").write_text("t\n" + "p\n" * 60, encoding="utf-8")
    _git(root, "commit", "-q", "-am", "peer work\n\nSession-Id: peer-session")
    (root / "tracked.txt").write_text("t\n" + "p\n" * 60 + "o\n" * 35, encoding="utf-8")
    reply, _ = _call(root, ["tracked.txt"], base=base)
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"] == {"files": 1, "lines": 35}
