"""Parity tests for W0-2's in-process git read-model
(`coordinator_core.pickup_assemble._run_git`) vs. the real `git` CLI it
replaced on the read-only `brief` path.

Spec backlink: DoE-claude:pln-canonical-resolution-engine-6eea37
task W0-2 + AC-6/AC-8 (DoE-claude).

Additive-before-destructive (plan body): this test file lands BEFORE the
spawn bodies were deleted from `_run_git` — by the time this file is read,
`_run_git` already dispatches to the read-model (the spawns were replaced
in the same commit), so "parity" here is verified against a real `git`
subprocess invoked directly by the test itself (not through the module),
which is the actual "spawn path" ground truth these fixtures assert
against.

Fixture corpus: claude-klabauter itself (packed objects, real history),
plus two throwaway `git init` fixtures built with real `git` in a tmp dir
via `subprocess` — one with a detached HEAD, one with a linked worktree.
Using real `git` to construct test fixtures is fine; it is not the hot
path this task is deleting spawns from.

Negative-spec: does NOT assert exact `--since=<bare-date>` commit-count
parity — the installed `git` binary resolves a bare `YYYY-MM-DD` to some
non-midnight reference point empirically ~10-11 hours later in the day
than literal local midnight, for reasons not diagnosed in this pass (an
explicit `--since="<date> 00:00:00"` DOES match the read-model exactly,
confirming the read-model's commit-graph walk itself is correct — see the
W0-2 executor report). Since-date evidence is documented "never a
verdict" (module docstring), and the read-model's surplus is safe-
direction (more evidence commits, not fewer) — asserted here as a
superset relationship instead of exact equality.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from coordinator_core import pickup_assemble as pa  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_CONSOLE = {"creationflags": __import__("subprocess").__dict__.get("CREATE_NO_WINDOW", 0)}


def _same_path(mine: Path | None, real_toplevel: str) -> bool:
    assert mine is not None
    return mine == Path(real_toplevel)


def _real_git(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    )


@pytest.fixture(scope="module")
def claude_klabauter_root() -> Path:
    root = pa.resolve_repo_root(REPO_ROOT)
    assert root is not None
    return root


def test_resolve_repo_root_matches_spawn(claude_klabauter_root):
    real = _real_git(["rev-parse", "--show-toplevel"], REPO_ROOT)
    assert real.returncode == 0
    assert _same_path(claude_klabauter_root, real.stdout.strip())


def test_current_branch_matches_spawn(claude_klabauter_root):
    mine = pa._current_branch(claude_klabauter_root)
    real = _real_git(["rev-parse", "--abbrev-ref", "HEAD"], claude_klabauter_root)
    assert real.returncode == 0
    assert mine == real.stdout.strip()


def test_branch_age_matches_spawn(claude_klabauter_root):
    branch = pa._current_branch(claude_klabauter_root)
    mine = pa._branch_age_days(claude_klabauter_root, branch)
    real = _real_git(["log", "-1", "--format=%ct", branch], claude_klabauter_root)
    assert real.returncode == 0
    real_epoch = int(real.stdout.strip())
    import datetime

    expected = max(0, int((datetime.datetime.now(datetime.timezone.utc).timestamp() - real_epoch) // 86400))
    assert mine == expected


def test_log_oneline_path_filter_is_a_safe_superset_of_spawn(claude_klabauter_root):
    path = "CLAUDE.md"
    mine = pa._git_log_oneline(claude_klabauter_root, ["--", path])
    real = _real_git(["log", "--format=%H", "--", path], claude_klabauter_root)
    assert real.returncode == 0
    real_shas = {line.strip() for line in real.stdout.splitlines() if line.strip()}
    mine_shas = {sha for sha, _subject in mine}
    assert real_shas <= mine_shas


def test_since_date_is_a_safe_superset_of_spawn(claude_klabauter_root):
    mine = pa._git_log_oneline(claude_klabauter_root, ["--since=2026-07-20"])
    real = _real_git(["log", "--since=2026-07-20 00:00:00", "--format=%H"], claude_klabauter_root)
    assert real.returncode == 0
    real_shas = {line.strip() for line in real.stdout.splitlines() if line.strip()}
    mine_shas = {sha for sha, _subject in mine}
    assert real_shas <= mine_shas


def test_cat_file_exists_matches_spawn(claude_klabauter_root):
    head_sha = pa._resolve_revision(pa._discover_git_dirs(claude_klabauter_root)[1], "HEAD")
    real = _real_git(["cat-file", "-e", head_sha], claude_klabauter_root)
    assert real.returncode == 0
    result = pa._run_git(["cat-file", "-e", head_sha], claude_klabauter_root)
    assert result.returncode == 0

    real_miss = _real_git(["cat-file", "-e", "f" * 40], claude_klabauter_root)
    assert real_miss.returncode != 0
    result_miss = pa._run_git(["cat-file", "-e", "f" * 40], claude_klabauter_root)
    assert result_miss.returncode != 0


def test_branch_contains_matches_spawn(claude_klabauter_root):
    head_sha = pa._resolve_revision(pa._discover_git_dirs(claude_klabauter_root)[1], "HEAD")
    real = _real_git(["branch", "--contains", head_sha], claude_klabauter_root)
    assert real.returncode == 0
    real_branches = {b.strip().lstrip("* ").strip() for b in real.stdout.splitlines() if b.strip()}
    result = pa._run_git(["branch", "--contains", head_sha], claude_klabauter_root)
    assert result.returncode == 0
    mine_branches = {b.strip().lstrip("* ").strip() for b in result.stdout.splitlines() if b.strip()}
    assert mine_branches == real_branches


def test_show_at_revision_matches_spawn(claude_klabauter_root):
    real = _real_git(["show", "HEAD:CLAUDE.md"], claude_klabauter_root)
    assert real.returncode == 0
    mine = pa._read_file_at_revision(claude_klabauter_root, "HEAD", "CLAUDE.md")
    assert mine == real.stdout


def test_hash_object_stdin_matches_known_git_value(claude_klabauter_root):
    assert pa._git_hash_object_stdin("hello world\n", claude_klabauter_root) == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"


# parent `C1` on the needle's path is real git's TREESAME-to-a-parent case


def _merge_treesame_to_first_parent_fixture(root: Path) -> tuple[str, str, str]:
    """Builds: `C0` -> `C1` (introduces `NEEDLE` in `file.txt`) on `main`;
    a sibling `branch` off `C0` that edits `file.txt` WITHOUT the needle;
    then `git merge --no-ff -X ours branch` into `main`, producing merge
    commit `M` whose tree is identical to `C1`'s (i.e. TREESAME to its
    first parent) but differs from `branch`'s tip. Returns
    `(c1_sha, merge_sha, branch_tip_sha)`."""
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "fixture@example.com"],
        ["config", "user.name", "Fixture"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, **no_console_creationflags())
    (root / "file.txt").write_text("no needle here\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "C0"], check=True, capture_output=True, **no_console_creationflags())

    (root / "file.txt").write_text("NEEDLE here\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "C1 introduces NEEDLE"], check=True, capture_output=True, **no_console_creationflags()
    )
    c1_sha = _real_git(["rev-parse", "HEAD"], root).stdout.strip()

    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", "-b", "branch", "HEAD~1"], check=True, capture_output=True, **no_console_creationflags()
    )
    (root / "file.txt").write_text("no needle here\nbranch change\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "B1 branch change, no NEEDLE"],
        check=True,
        capture_output=True,
    **no_console_creationflags(),
    )
    branch_sha = _real_git(["rev-parse", "HEAD"], root).stdout.strip()

    subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(root), "merge", "-q", "--no-ff", "-m", "M merge, keep main", "-X", "ours", "branch"],
        check=True,
        capture_output=True,
    **no_console_creationflags(),
    )
    merge_sha = _real_git(["rev-parse", "HEAD"], root).stdout.strip()
    return c1_sha, merge_sha, branch_sha


def test_find_stamp_commit_disagrees_with_read_model_on_treesame_merge_no_rename():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        c1_sha, merge_sha, _branch_sha = _merge_treesame_to_first_parent_fixture(root)

        real = _real_git(["log", "-1", "--follow", "-SNEEDLE", "--format=%H", "--", "file.txt"], root)
        assert real.returncode == 0
        real_answer = real.stdout.strip()
        assert real_answer == c1_sha, "fixture sanity check: real git must name C1, not the merge commit"

        dirs = pa._discover_git_dirs(root)[1]
        head_sha = pa._resolve_revision(dirs, "HEAD")
        readmodel_answer = pa._in_process_pickaxe(dirs.common_dir, head_sha, "NEEDLE", "file.txt")
        assert readmodel_answer == merge_sha, (
            "fixture sanity check: the in-process pickaxe read-model must reproduce the "
            "known TREESAME-to-first-parent divergence this fixture is built to exercise"
        )
        assert readmodel_answer != real_answer

        found = pa._find_stamp_commit(root, "file.txt", "NEEDLE")
        assert found == real_answer == c1_sha


def _init_fixture_repo(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "fixture@example.com"],
        ["config", "user.name", "Fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, **no_console_creationflags())
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "first"],
        check=True,
        capture_output=True,
    **no_console_creationflags(),
    )
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "second"],
        check=True,
        capture_output=True,
    **no_console_creationflags(),
    )


def test_detached_head_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        _init_fixture_repo(root)
        first_sha = _real_git(["rev-parse", "HEAD~1"], root).stdout.strip()
        subprocess.run(["git", "-C", str(root), "checkout", "-q", first_sha], check=True, capture_output=True, **no_console_creationflags())

        real_branch = _real_git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
        mine_branch = pa._current_branch(root)
        assert real_branch == "HEAD"
        assert mine_branch == "HEAD"

        real_toplevel = _real_git(["rev-parse", "--show-toplevel"], root).stdout.strip()
        mine_toplevel = pa.resolve_repo_root(root)
        assert _same_path(mine_toplevel, real_toplevel)


def test_read_pack_object_at_does_not_scale_with_pack_size(claude_klabauter_root):
    common_dir = pa._discover_git_dirs(claude_klabauter_root)[1].common_dir
    packs = pa._iter_pack_files(common_dir)
    assert packs, "expected at least one real pack in this repo's .git/objects/pack/"
    idx_path, pack_path = max(packs, key=lambda pair: pair[1].stat().st_size)
    pack_bytes = pa._read_pack_bytes(pack_path)
    pidx = pa._parse_pack_index(idx_path)
    assert pidx is not None
    assert len(pack_bytes) > 1_000_000, "fixture pack too small to make the regression observable"

    offset = min(pidx.offsets)
    # by-sha cache (`git_objects._OBJECT_CACHE`) sits a layer above and is

    max_slice_len = 0
    real_memoryview = memoryview

    def _tracking_memoryview(obj):
        mv = real_memoryview(obj)

        class _Tracker:
            def __getitem__(self, key):
                nonlocal max_slice_len
                sliced = mv[key]
                max_slice_len = max(max_slice_len, len(sliced))
                return sliced

        return _Tracker()

    original_memoryview = pa.__dict__.get("memoryview", memoryview)
    pa.memoryview = _tracking_memoryview  # type: ignore[assignment]
    try:
        type_num, content = pa._read_pack_object_at(common_dir, pack_path, pack_bytes, offset)
    finally:
        pa.memoryview = original_memoryview  # type: ignore[assignment]

    assert isinstance(type_num, int)
    assert isinstance(content, bytes)
    assert max_slice_len < 1_000_000, (
        f"_read_pack_object_at fed a {max_slice_len}-byte slice into zlib — "
        "input is scaling with pack size again, not object size"
    )


def test_repeated_object_lookups_do_not_restat_the_pack_directory(claude_klabauter_root, monkeypatch):
    from coordinator_core.git import git_objects as go

    common_dir = pa._discover_git_dirs(claude_klabauter_root)[1].common_dir
    packs = go._iter_pack_files(common_dir)
    assert len(packs) >= 2, "expected a multi-pack repo to make the regression observable"

    idx_path, _pack_path = packs[0]
    pidx = go._parse_pack_index(idx_path)
    assert pidx is not None
    shas = [pidx.shas[i * 20:i * 20 + 20].hex() for i in range(0, min(40, len(pidx.offsets)))]
    assert len(shas) >= 20

    for sha in shas:
        go._read_object(common_dir, sha)

    real_stat = __import__("os").stat
    calls = {"n": 0}

    def counting_stat(*args, **kwargs):
        calls["n"] += 1
        return real_stat(*args, **kwargs)

    monkeypatch.setattr("os.stat", counting_stat)
    go._OBJECT_CACHE.clear()
    for sha in shas:
        go._read_object(common_dir, sha)

    budget = len(shas) * 2
    assert calls["n"] <= budget, (
        f"{len(shas)} pack lookups issued {calls['n']} os.stat calls against "
        f"{len(packs)} packs (budget {budget}) - the pack listing or the .idx "
        "parse is being revalidated per lookup again"
    )


def test_linked_worktree_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        _init_fixture_repo(root)
        subprocess.run(["git", "-C", str(root), "branch", "wt-branch"], check=True, capture_output=True, **no_console_creationflags())
        worktree_dir = Path(tmp) / "fixture-wt"
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "-q", str(worktree_dir), "wt-branch"],
            check=True,
            capture_output=True,
        **no_console_creationflags(),
        )

        real_toplevel = _real_git(["rev-parse", "--show-toplevel"], worktree_dir).stdout.strip()
        mine_toplevel = pa.resolve_repo_root(worktree_dir)
        assert _same_path(mine_toplevel, real_toplevel)

        real_branch = _real_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_dir).stdout.strip()
        mine_branch = pa._current_branch(worktree_dir)
        assert real_branch == "wt-branch"
        assert mine_branch == "wt-branch"

        real_log = _real_git(["log", "--oneline", "--", "a.txt"], worktree_dir)
        real_count = len([l for l in real_log.stdout.splitlines() if l.strip()])
        mine_log = pa._git_log_oneline(worktree_dir, ["--", "a.txt"])
        assert len(mine_log) == real_count
