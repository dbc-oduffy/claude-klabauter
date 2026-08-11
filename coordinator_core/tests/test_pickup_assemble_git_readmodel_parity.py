"""Parity tests for W0-2's in-process git read-model
(`coordinator_core.pickup_assemble._run_git`) vs. the real `git` CLI it
replaced on the read-only `brief` path.

Spec backlink: docs/plans/2026-07-24-canonical-resolution-engine.md
task W0-2 + AC-6/AC-8 (example-doctrine-repo).

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

_NO_CONSOLE = {"creationflags": __import__("subprocess").__dict__.get("CREATE_NO_WINDOW", 0)}


def _same_path(mine: Path | None, real_toplevel: str) -> bool:
    """Compare a read-model repo root against `git rev-parse --show-toplevel`.

    Path-level, never string-level: on Windows git ALWAYS emits forward slashes
    (forward-slashed, drive-lettered) while `str(Path(...))` renders native
    backslash separators. Both name the same directory, and every production
    consumer of `resolve_repo_root` takes the `Path`, not its rendering — so a
    raw `str(...) == stdout` comparison here was asserting a POSIX-only
    accident of separator style, not parity.
    """
    assert mine is not None
    return mine == Path(real_toplevel)


def _real_git(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
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
    """Real `git log -- path` applies merge-history simplification
    (a merge commit is hidden unless it's the *only* path to some path-
    touching change); the read-model's `_commit_touches_path` (negative-
    spec, module docstring) does not replicate that algorithm and instead
    includes every commit where the blob differs from ANY parent — a
    strict superset, never missing a real hit."""
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


# `test_commit_recency_signal_matches_spawn` REMOVED (post-W0-2): the
# commit-recency liveness signal it parity-checked (`_commit_recency_signal`)
# was itself deleted later the same day by C7 (commit `68796c55`,
# `compute_liveness_signal`'s "DELETED (this amendment): signal (b)
# commit-recency" note) — it was a false-positive-prone proxy that fired on
# the plan's own execution-authorization commit. The capability is
# deliberately retired, not renamed; there is no successor symbol to parity-
# check against.


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


# ---------------------------------------------------------------------------
# `_find_stamp_commit` (`gates.execution_stamp_match` history-walk) — pins
# it against real `git log -1 --follow -S<needle>`, NOT the in-process
# `_in_process_pickaxe` read-model it used to route through. That
# reimplementation's own negative-spec comment (module docstring) used to
# call the gap "narrow... a renamed-then-edited file's pre-rename history
# is invisible" — this fixture has NO rename at all and still diverges: a
# merge commit `M` that resolves to (is tree-identical to) its first
# parent `C1` on the needle's path is real git's TREESAME-to-a-parent case
# under default merge simplification, so `git log -S` walks straight past
# `M` into `C1` (where the needle was actually introduced). The read-
# model's `_in_process_pickaxe` has no such simplification — it loops over
# `M`'s parents in order and returns `M` itself the moment ANY parent's
# needle count differs (here, the second parent `B1`, which never carried
# the needle), even though the FIRST parent already accounts for `M`'s own
# content. Reproduces the stamp-integrity investigation's Root cause B,
# Case 2 (`tasks/mise-findings/stamp-integrity.md`, example-doctrine-repo) end to end
# through the real `_find_stamp_commit` entry point, not just the inner
# walk.
# ---------------------------------------------------------------------------


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
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "file.txt").write_text("no needle here\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "C0"], check=True, capture_output=True)

    (root / "file.txt").write_text("NEEDLE here\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "C1 introduces NEEDLE"], check=True, capture_output=True
    )
    c1_sha = _real_git(["rev-parse", "HEAD"], root).stdout.strip()

    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", "-b", "branch", "HEAD~1"], check=True, capture_output=True
    )
    (root / "file.txt").write_text("no needle here\nbranch change\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "B1 branch change, no NEEDLE"],
        check=True,
        capture_output=True,
    )
    branch_sha = _real_git(["rev-parse", "HEAD"], root).stdout.strip()

    subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "merge", "-q", "--no-ff", "-m", "M merge, keep main", "-X", "ours", "branch"],
        check=True,
        capture_output=True,
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

        # The OLD in-process read-model disagrees with real git here — pin
        # this as the documented divergence, not merely assert the fixed
        # behavior in isolation.
        dirs = pa._discover_git_dirs(root)[1]
        head_sha = pa._resolve_revision(dirs, "HEAD")
        readmodel_answer = pa._in_process_pickaxe(dirs.common_dir, head_sha, "NEEDLE", "file.txt")
        assert readmodel_answer == merge_sha, (
            "fixture sanity check: the in-process pickaxe read-model must reproduce the "
            "known TREESAME-to-first-parent divergence this fixture is built to exercise"
        )
        assert readmodel_answer != real_answer

        # `_find_stamp_commit` — the actual `compute_execution_stamp_match`
        # entry point — must agree with real git, not the read-model.
        found = pa._find_stamp_commit(root, "file.txt", "NEEDLE")
        assert found == real_answer == c1_sha


# ---------------------------------------------------------------------------
# Detached HEAD + linked-worktree fixtures — built with real `git` (test
# setup only, not the hot path this task removes spawns from).
# ---------------------------------------------------------------------------


def _init_fixture_repo(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "fixture@example.com"],
        ["config", "user.name", "Fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "first"],
        check=True,
        capture_output=True,
    )
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "second"],
        check=True,
        capture_output=True,
    )


def test_detached_head_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        _init_fixture_repo(root)
        first_sha = _real_git(["rev-parse", "HEAD~1"], root).stdout.strip()
        subprocess.run(["git", "-C", str(root), "checkout", "-q", first_sha], check=True, capture_output=True)

        real_branch = _real_git(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
        mine_branch = pa._current_branch(root)
        assert real_branch == "HEAD"
        assert mine_branch == "HEAD"

        real_toplevel = _real_git(["rev-parse", "--show-toplevel"], root).stdout.strip()
        mine_toplevel = pa.resolve_repo_root(root)
        assert _same_path(mine_toplevel, real_toplevel)


# ---------------------------------------------------------------------------
# Pack-read perf regression (2026-07-24 W0-2 -> 2026-07-26 hotfix). `brief()`
# measured 9053 ms against a <=60 ms budget: `_read_pack_object_at` handed
# `zlib.decompressobj().decompress(memoryview(pack_bytes)[pos:])` the ENTIRE
# remainder of the pack file per object read. Decompression itself stops at
# the real stream end, but CPython still copies everything past that point
# into `decompressobj.unused_data` — a full pack-tail copy per object, so
# cost scaled with PACK size, not OBJECT size. Fixed by
# `_zlib_decompress_bounded` feeding small growing input windows instead.
# This test pins that fix: reading an object near the START of a large pack
# (worst case for the old code — longest possible tail) must not touch a
# byte count proportional to the pack's size.
# ---------------------------------------------------------------------------


def test_read_pack_object_at_does_not_scale_with_pack_size(claude_klabauter_root):
    common_dir = pa._discover_git_dirs(claude_klabauter_root)[1].common_dir
    packs = pa._iter_pack_files(common_dir)
    assert packs, "expected at least one real pack in this repo's .git/objects/pack/"
    idx_path, pack_path = max(packs, key=lambda pair: pair[1].stat().st_size)
    pack_bytes = pa._read_pack_bytes(pack_path)
    pidx = pa._parse_pack_index(idx_path)
    assert pidx is not None
    assert len(pack_bytes) > 1_000_000, "fixture pack too small to make the regression observable"

    # Object nearest the FRONT of the pack file — the old whole-tail-decompress
    # code's worst case, since everything after it is the longest possible tail.
    offset = min(pidx.offsets)
    # `_PACK_OBJECT_AT_CACHE` is process-lifetime (module-level, per the
    # cache-scoping precedent in `__init__.py`); an earlier test in this same
    # pytest process may have already resolved this object, which would let
    # this test pass trivially via the cache hit rather than actually
    # exercising the decompress path. Evict this one entry to force a real
    # decompress; do not clear the whole cache, other tests may rely on it.
    pa._PACK_OBJECT_AT_CACHE.pop((str(pack_path), offset), None)

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
    # Bounded input windows: even with doubling retries, this must stay a
    # small multiple of the object's own content size — never anywhere near
    # the multi-MB pack. 1 MiB is generous headroom over any single object
    # in this repo's real packs while still being orders of magnitude below
    # a 30+ MB pack tail.
    assert max_slice_len < 1_000_000, (
        f"_read_pack_object_at fed a {max_slice_len}-byte slice into zlib — "
        "input is scaling with pack size again, not object size"
    )


def test_linked_worktree_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        _init_fixture_repo(root)
        subprocess.run(["git", "-C", str(root), "branch", "wt-branch"], check=True, capture_output=True)
        worktree_dir = Path(tmp) / "fixture-wt"
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "-q", str(worktree_dir), "wt-branch"],
            check=True,
            capture_output=True,
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
