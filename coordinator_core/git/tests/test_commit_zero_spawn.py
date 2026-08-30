"""`commit.commit_paths` lands a correct commit with ZERO git spawns and
leaves `git status` telling the truth.

Every assertion uses real `git` as the oracle. The spawn count is measured by
patching `subprocess` for the duration of the call under test -- the oracle
calls sit outside that window deliberately.
"""

import subprocess
import pytest

from coordinator_core.git import commit as gcommit

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


class _SpawnCounter:
    """Counts processes started inside the `with` block."""

    def __init__(self):
        self.argvs = []

    def __enter__(self):
        self._run, self._popen = subprocess.run, subprocess.Popen
        counter = self

        def run_spy(*a, **k):
            if a:
                counter.argvs.append(a[0])
            return counter._run(*a, **k)

        class PopenSpy(counter._popen):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **k):
                if a:
                    counter.argvs.append(a[0])
                super().__init__(*a, **k)

        subprocess.run, subprocess.Popen = run_spy, PopenSpy
        return self

    def __exit__(self, *exc):
        subprocess.run, subprocess.Popen = self._run, self._popen
        return False


def _commit(repo, paths, msg, **kw):
    with _SpawnCounter() as counter:
        outcome = gcommit.commit_paths(repo, paths, msg, **kw)
    return outcome, counter.argvs


def test_new_file_zero_spawns_and_clean_status(tmp_path):
    """fd shape A: a brand-new untracked file. Stale-index symptom would be
    `D  new.txt` + `?? new.txt` -- the same path staged-deleted AND untracked."""
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    outcome, spawns = _commit(repo, ["new.txt"], "add new")

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "show", "HEAD:new.txt").stdout == "new\n"
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "add new"


def test_edited_tracked_file_zero_spawns_and_clean_status(tmp_path):
    """fd shape B: a tracked file edited but never staged. Stale-index
    symptom would be `MM seed.txt` -- a staged modification that never was."""
    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("edited\n", encoding="utf-8", newline="\n")

    outcome, spawns = _commit(repo, ["seed.txt"], "edit seed")

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "show", "HEAD:seed.txt").stdout == "edited\n"


def test_new_file_in_new_directory_zero_spawns(tmp_path):
    """fd shape C: a new file under a directory HEAD's tree does not carry.
    The spine cannot re-point a level that does not exist, so this is the
    shape that used to fall to the deleted ladder."""
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8", newline="\n")

    outcome, spawns = _commit(repo, ["sub/deep.txt"], "add deep")

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "show", "HEAD:sub/deep.txt").stdout == "deep\n"


def test_staged_bytes_beat_worktree_bytes(tmp_path):
    """INVARIANT 1. A deliberately staged version must be what lands, not the
    newer worktree bytes -- on a shared tree the worktree may hold a peer's
    half-finished edit. DECLARED via `prefer_staged`, never inferred from
    "index differs from worktree", which is also true of an ordinary edit."""
    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("STAGED\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "--", "seed.txt")
    (repo / "seed.txt").write_text("WORKTREE-AFTER\n", encoding="utf-8", newline="\n")

    outcome, spawns = _commit(repo, ["seed.txt"], "keep staged",
                              prefer_staged=["seed.txt"])

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "show", "HEAD:seed.txt").stdout == "STAGED\n"
    assert outcome.staged_preferred == ("seed.txt",)


def test_deletion_zero_spawns_and_clean_status(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.txt").unlink()

    outcome, spawns = _commit(repo, [], "remove seed", deleted_paths=["seed.txt"])

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "cat-file", "-e", f"{outcome.sha}:seed.txt", check=False).returncode != 0


def test_absolute_pathspec_commits_repo_relative_not_a_drive_tree(tmp_path):
    """An absolute pathspec must route through `_index_key` exactly like the
    repo-relative spelling: same tree, no `X:`-style top-level entry, and the
    spliced index keyed by the repo-relative name -- not the absolute string
    used verbatim, which is the defect this test pins."""
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    outcome, spawns = _commit(repo, [str(repo / "new.txt")], "add new abs")

    assert spawns == [], f"expected zero spawns, got {spawns}"
    tree_names = _git(repo, "ls-tree", "HEAD").stdout
    assert "new.txt" in tree_names, tree_names
    assert not any(line.split()[-1].endswith(":") for line in tree_names.splitlines())
    assert _git(repo, "show", "HEAD:new.txt").stdout == "new\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    from coordinator_core.git.git_index import parse_index_identity

    identity = parse_index_identity(repo, wanted={"new.txt"})
    assert "new.txt" in identity


def test_absolute_deleted_paths_also_routed_through_index_key(tmp_path):
    """`deleted_paths` was equally unrouted -- an absolute deletion pathspec
    must resolve to the same repo-relative index key as the relative form."""
    repo = _repo(tmp_path)
    (repo / "seed.txt").unlink()

    outcome, spawns = _commit(
        repo, [], "remove seed abs", deleted_paths=[str(repo / "seed.txt")]
    )

    assert spawns == [], f"expected zero spawns, got {spawns}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo, "cat-file", "-e", f"{outcome.sha}:seed.txt", check=False).returncode != 0


def test_pathspec_outside_repo_raises_commit_refused(tmp_path):
    """A path that does not resolve under the repo root must refuse, not
    silently commit against whatever name results."""
    repo = _repo(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("nope\n", encoding="utf-8", newline="\n")

    with pytest.raises(gcommit.CommitRefused):
        gcommit.commit_paths(repo, [str(outside)], "should refuse")


def test_lost_cas_race_refuses_and_writes_no_ref(tmp_path):
    """INVARIANT 2. If the ref moved under us, refuse -- never commit past a
    peer, and never retry silently on a tree built against the old HEAD."""
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    import coordinator_core.git.commit as mod
    real_head = mod.head_sha
    mod.head_sha = lambda r: "0" * 40  # a HEAD that was never there
    try:
        with pytest.raises(gcommit.CommitRefused, match="compare-and-swap"):
            gcommit.commit_paths(repo, ["new.txt"], "should refuse")
    finally:
        mod.head_sha = real_head

    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "seed"


def test_empty_pathspec_is_refused_never_defaulted(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(gcommit.CommitRefused, match="empty pathspec"):
        gcommit.commit_paths(repo, [], "nothing")


def test_directory_pathspec_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    with pytest.raises(gcommit.CommitRefused, match="directory"):
        gcommit.commit_paths(repo, ["sub"], "dir")


def _attrs_repo(tmp_path):
    """This repo's checkin surface: autocrlf on, and the real attribute pins."""
    repo = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    (repo / ".gitattributes").write_text(
        "*.cmd text eol=crlf\n*.sh text eol=lf\n_goldens/** -text\n",
        encoding="utf-8", newline="\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    return repo


def test_refused_paths_batch_into_one_fallback_call(tmp_path):
    """A path this module cannot convert must not explode the commit: it is
    collected and handed to the injected fallback in ONE batch. That is the
    difference between the pipeline going red and the pipeline routing."""
    repo = _attrs_repo(tmp_path)
    (repo / "plain.txt").write_text("lf only\n", encoding="utf-8", newline="\n")
    (repo / "a.cmd").write_bytes(b"echo one\r\necho two\r\n")
    (repo / "b.cmd").write_bytes(b"echo three\r\n")

    calls = []

    def fallback(paths):
        calls.append(list(paths))
        out = {}
        for rel in paths:
            data = (repo / rel).read_bytes()
            out[rel] = gcommit.write_object(repo / ".git", b"blob", data)
        return out

    outcome, spawns = _commit(
        repo, ["plain.txt", "a.cmd", "b.cmd"], "mixed", blob_fallback=fallback
    )

    assert spawns == [], f"the module itself must still spawn nothing: {spawns}"
    # ONE call carrying BOTH refused paths -- not one call per path.
    assert len(calls) == 1, calls
    assert sorted(calls[0]) == ["a.cmd", "b.cmd"], calls
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "mixed"


def test_no_fallback_supplied_refuses_and_names_the_paths(tmp_path):
    """Without a fallback the refusal stays loud and names what it refused --
    it must never quietly commit the raw bytes."""
    repo = _attrs_repo(tmp_path)
    (repo / "c.cmd").write_bytes(b"echo\r\n")
    with pytest.raises(gcommit.FilterUnsupported, match="c.cmd"):
        gcommit.commit_paths(repo, ["c.cmd"], "no fallback")


def test_all_lf_commit_never_calls_the_fallback(tmp_path):
    """The common case must cost nothing: nothing refused, fallback untouched,
    zero spawns."""
    repo = _attrs_repo(tmp_path)
    (repo / "one.txt").write_text("a\n", encoding="utf-8", newline="\n")
    (repo / "two.txt").write_text("b\n", encoding="utf-8", newline="\n")

    calls = []
    outcome, spawns = _commit(
        repo, ["one.txt", "two.txt"], "all lf",
        blob_fallback=lambda paths: calls.append(list(paths)) or {},
    )
    assert spawns == []
    assert calls == [], f"fallback should not have been called: {calls}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
