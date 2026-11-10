"""The commit-request marker carries a row's ``optional_writes:`` paths as ``ChunkCommit.optional``."""

import subprocess

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.emit import _terminal_commit_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}


def _chunks(body, writes=("a.py", "b.npz")):
    row = WaveRow("C2", "t", "s", list(writes), [], [], body=body)
    marker = _terminal_commit_marker(
        [row],
        {"C2": list(writes)},
        plan_path="docs/plans/p.md",
        deliverable_id=None,
        session_id=None,
        row_prefixes={},
    )
    assert marker is not None
    return parse_marker(marker).chunks


def test_optional_write_is_carried_and_leaves_gating():
    (chunk,) = _chunks("do it\noptional_writes: `b.npz`\n")
    assert chunk.optional == ("b.npz",)
    assert chunk.gating_paths() == ("a.py",)


def test_unmarked_row_keeps_every_write_gating():
    (chunk,) = _chunks("do it\n")
    assert chunk.optional == ()
    assert chunk.gating_paths() == ("a.py", "b.npz")


def test_optional_naming_an_undeclared_path_is_ignored():
    (chunk,) = _chunks("optional_writes: other.py\n")
    assert chunk.optional == ()
    assert chunk.gating_paths() == ("a.py", "b.npz")


def test_declared_write_marked_required_false_in_the_row_body_is_optional():
    (chunk,) = _chunks("phase: code\n- `b.npz` (required: false)\nb.npz is built\n")
    assert chunk.optional == ("b.npz",)
    assert chunk.gating_paths() == ("a.py",)


def test_required_false_beside_prose_naming_a_path_is_not_a_marking():
    (chunk,) = _chunks("when b.npz is absent the field required: false is unrelated\n")
    assert chunk.optional == ()


_SID = "11111111-2222-3333-4444-555555555555"


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _landing(tmp_path, body):
    """A repo whose DONE row C2 declares a.py (present) and b.npz (absent), the marker emitted
    from a spine row body, fired through ``terminal_commit._handler``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.py").write_text("old\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.py").write_text("new\n", encoding="utf-8")
    (root / "report-C2.md").write_text("DONE: built\n", encoding="utf-8")
    row = WaveRow("C2", "t", "s", ["a.py", "b.npz"], [], [], body=body)
    marker = _terminal_commit_marker(
        [row], {"C2": ["a.py", "b.npz"]}, plan_path=None, deliverable_id=None,
        session_id=None, row_prefixes={},
    )
    assert '"report":""' in marker
    marker = marker.replace('"report":""', '"report":"report-C2.md"')
    (root / "run.mjs").write_text("// emitted\n" + marker + "\n", encoding="utf-8")
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
        "session_id": _SID,
    }
    return terminal_commit._handler(params, repo_root=root / ".git"), root


def test_required_false_write_absent_lands_through_terminal_commit(tmp_path):
    out, root = _landing(tmp_path, "- `b.npz` (required: false)\n")
    assert out["committed"] is True, out
    assert "a.py" in _git(["ls-tree", "-r", "--name-only", "HEAD"], root).split()


def test_unmarked_absent_write_still_refuses_through_terminal_commit(tmp_path):
    out, _ = _landing(tmp_path, "build it\n")
    assert out["committed"] is False, out
    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": ["b.npz"]}, out
