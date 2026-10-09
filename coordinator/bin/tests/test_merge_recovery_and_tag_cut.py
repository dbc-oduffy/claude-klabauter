from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# Declared, not excused: 6 of this file's tests spawn a real git process because the
# property under test is git's own behaviour -- idempotent annotated-tag cut/push
# (test_cut_tag_*) and branch/HEAD state after a real recovery-branch dance
# (test_*_verdict_*), neither reproducible against a mock. Each mutation test needs
# its own fresh repo (tag-cut idempotency, branch creation, and push-landing checks
# all depend on starting from a known-clean state), so the per-test
# `_init_repo_with_origin` fixture is not hoisted to module scope -- see
# test_verify_shipped.py's docstring for the failure mode that hoisting produces here.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly
# not the route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_recovery_and_tag_cut",
        _BIN_DIR / "merge-recovery-and-tag-cut.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
cut_tag = _mod.cut_tag
resolve_tag_prefix = _mod.resolve_tag_prefix
cmd_recovery_branch = _mod.cmd_recovery_branch


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _init_repo_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], cwd=work)
    _git(["config", "user.email", "test@example.com"], cwd=work)
    _git(["config", "user.name", "Test User"], cwd=work)
    _git(["checkout", "-b", "main"], cwd=work)
    (work / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "f.txt"], cwd=work)
    _git(["commit", "-m", "initial"], cwd=work)
    _git(["remote", "add", "origin", str(origin)], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)
    return work


def test_cut_tag_creates_and_pushes_annotated_tag(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    cut, merge_sha = cut_tag(work, "v1.0.0")

    assert cut is True
    assert merge_sha == head_sha

    tag_type = _git(["cat-file", "-t", "v1.0.0"], cwd=work).stdout.strip()
    assert tag_type == "tag"
    peeled = _git(["rev-parse", "v1.0.0^{}"], cwd=work).stdout.strip()
    assert peeled == head_sha

    origin_tags = _git(["ls-remote", "--tags", "origin"], cwd=work).stdout
    assert "refs/tags/v1.0.0" in origin_tags


def test_cut_tag_is_idempotent_on_retry(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)

    first_cut, first_sha = cut_tag(work, "v1.0.0")
    assert first_cut is True

    second_cut, second_sha = cut_tag(work, "v1.0.0")
    assert second_cut is False
    assert second_sha == first_sha


def test_cut_tag_must_contain_passes_when_ancestor(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    merge_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    (work / "g.txt").write_text("more\n", encoding="utf-8")
    _git(["add", "g.txt"], cwd=work)
    _git(["commit", "-m", "second"], cwd=work)
    descendant_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    cut, cut_sha = cut_tag(work, "v1.0.0", merge_ref=merge_sha, must_contain=descendant_sha)

    assert cut is True
    assert cut_sha == merge_sha
    peeled = _git(["rev-parse", "v1.0.0^{}"], cwd=work).stdout.strip()
    assert peeled == merge_sha


def test_cut_tag_must_contain_fails_loud_when_not_ancestor(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    merge_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    _git(["checkout", "--orphan", "unrelated"], cwd=work)
    (work / "h.txt").write_text("branch\n", encoding="utf-8")
    _git(["add", "h.txt"], cwd=work)
    _git(["commit", "-m", "unrelated root commit"], cwd=work)
    unrelated_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()
    _git(["checkout", "main"], cwd=work)

    with pytest.raises(SystemExit) as exc_info:
        cut_tag(work, "v1.0.0", merge_ref=merge_sha, must_contain=unrelated_sha)
    assert exc_info.value.code == 1

    tag_exists = _run_git_allow_fail(["tag", "-l", "v1.0.0"], cwd=work)
    assert tag_exists.strip() == ""


def _run_git_allow_fail(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        **_mod._win_portability_flags(),
    )
    return result.stdout


def test_resolve_tag_prefix_extracts_value(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: game-dev\n"
        "tag_prefix: example-game-repo-\n"
        "---\n"
        "# Body\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_strips_inline_comment(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "tag_prefix: example-game-repo-  # namespace prefix\n"
        "---\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_absent_key_returns_empty(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: general\n"
        "---\n"
        "tag_prefix: should-not-be-seen\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == ""


def test_resolve_tag_prefix_quoted_value_fails_loud(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        'tag_prefix: "example-game-repo-"\n'
        "---\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc_info:
        resolve_tag_prefix(config)
    assert exc_info.value.code == 1


class _FakeVerdict:
    def __init__(self, outcome: str, reason: str) -> None:
        self.outcome = outcome
        self.reason = reason


class _Args:
    def __init__(self, repo_root: Path, branch_name: str = "work/testhost/2026-08-07") -> None:
        self.repo_root = str(repo_root)
        self.branch_name = branch_name


def _current_branch(work: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=work).stdout.strip()


def _head_sha(work: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], cwd=work).stdout.strip()


def test_refused_verdict_blocks_all_mutation(tmp_path: Path, monkeypatch) -> None:
    work = _init_repo_with_origin(tmp_path)
    before_branch = _current_branch(work)
    before_sha = _head_sha(work)
    before_branches = _git(["branch"], cwd=work).stdout

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None, **kw: _FakeVerdict(
            "refused", "1 live peer session(s): abc123 (on main)"
        )),
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_recovery_branch(_Args(work))
    assert exc_info.value.code == 1

    assert _current_branch(work) == before_branch
    assert _head_sha(work) == before_sha
    assert _git(["branch"], cwd=work).stdout == before_branches
    assert "work/testhost/2026-08-07" not in before_branches


def test_unknown_verdict_treated_same_as_refused(tmp_path: Path, monkeypatch) -> None:
    work = _init_repo_with_origin(tmp_path)
    before_branch = _current_branch(work)
    before_sha = _head_sha(work)
    before_branches = _git(["branch"], cwd=work).stdout

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None, **kw: _FakeVerdict(
            "unknown", "cannot resolve live-session set"
        )),
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_recovery_branch(_Args(work))
    assert exc_info.value.code == 1

    assert _current_branch(work) == before_branch
    assert _head_sha(work) == before_sha
    assert _git(["branch"], cwd=work).stdout == before_branches


def test_ok_verdict_still_runs_recovery_dance(tmp_path: Path, monkeypatch, capsys) -> None:
    work = _init_repo_with_origin(tmp_path)

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None, **kw: _FakeVerdict("ok", "no live peer sessions")),
    )

    rc = cmd_recovery_branch(_Args(work))
    assert rc == 0

    out = capsys.readouterr().out
    assert "BRANCH=work/testhost/2026-08-07" in out
    assert _current_branch(work) == "work/testhost/2026-08-07"
    assert "refs/heads/work/testhost/2026-08-07" in _git(
        ["ls-remote", "--heads", "origin"], cwd=work
    ).stdout


def test_cut_tag_tags_fetched_tip_when_tracking_ref_is_stale(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    peer = tmp_path / "peer"
    _git(["clone", "-b", "main", str(tmp_path / "origin.git"), str(peer)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=peer)
    _git(["config", "user.name", "T"], cwd=peer)
    (peer / "g.txt").write_text("merged\n", encoding="utf-8")
    _git(["add", "g.txt"], cwd=peer)
    _git(["commit", "-m", "post-merge"], cwd=peer)
    _git(["push", "origin", "HEAD:main"], cwd=peer)
    new_tip = _git(["rev-parse", "HEAD"], cwd=peer).stdout.strip()
    _git(["config", "--unset-all", "remote.origin.fetch"], cwd=work)

    cut, merge_sha = cut_tag(work, "v2.0.0")

    assert cut is True
    assert merge_sha == new_tip
    assert _git(["rev-parse", "v2.0.0^{}"], cwd=work).stdout.strip() == new_tip


def _land_second_commit(work: Path) -> tuple[str, str]:
    first = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()
    (work / "g.txt").write_text("merge\n", encoding="utf-8")
    _git(["add", "g.txt"], cwd=work)
    _git(["commit", "-m", "merge commit"], cwd=work)
    _git(["push", "origin", "main"], cwd=work)
    return first, _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()


def test_cut_tag_targets_pr_merge_commit_not_local_head(tmp_path: Path, monkeypatch) -> None:
    work = _init_repo_with_origin(tmp_path)
    _, merge_sha = _land_second_commit(work)
    (work / "h.txt").write_text("later\n", encoding="utf-8")
    _git(["add", "h.txt"], cwd=work)
    _git(["commit", "-m", "later"], cwd=work)
    _git(["push", "origin", "main"], cwd=work)
    monkeypatch.setattr(_mod, "_merge_commit_of_pr", lambda root, pr: merge_sha)

    cut, sha = cut_tag(work, "v1.0.0", pr="7")

    assert cut is True and sha == merge_sha
    assert _git(["rev-parse", "v1.0.0^{}"], cwd=work).stdout.strip() == merge_sha


def test_cut_tag_refuses_when_remote_tag_points_elsewhere(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    first, merge_sha = _land_second_commit(work)
    _git(["tag", "-a", "v1.0.0", first, "-m", "v1.0.0"], cwd=work)
    _git(["push", "origin", "v1.0.0"], cwd=work)
    _git(["tag", "-d", "v1.0.0"], cwd=work)

    with pytest.raises(SystemExit):
        cut_tag(work, "v1.0.0", merge_ref=merge_sha)

    remote = _git(["ls-remote", "origin", "refs/tags/v1.0.0^{}"], cwd=work).stdout
    assert remote.split()[0] == first


def test_cut_tag_refusal_names_next_free_patch_tag_across_a_gap(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    work = _init_repo_with_origin(tmp_path)
    first, merge_sha = _land_second_commit(work)
    monkeypatch.setattr(_mod, "_merge_commit_of_pr", lambda root, pr: merge_sha)
    for t in ("v0.6.9", "v0.6.11"):
        _git(["tag", "-a", t, first, "-m", t], cwd=work)
        _git(["push", "origin", t], cwd=work)
        _git(["tag", "-d", t], cwd=work)

    with pytest.raises(SystemExit):
        cut_tag(work, "v0.6.9", merge_ref=merge_sha, pr="7")

    err = capsys.readouterr().err
    assert "Next free patch tag: v0.6.10" in err
    assert "cut-tag v0.6.10 --pr 7 --merge-ref " + merge_sha in err
    assert "v0.6.10" not in _git(["ls-remote", "--tags", "origin"], cwd=work).stdout
    remote = _git(["ls-remote", "origin", "refs/tags/v0.6.9^{}"], cwd=work).stdout
    assert remote.split()[0] == first


def test_cut_tag_pushes_when_local_tag_exists_but_remote_lacks_it(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    head = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()
    _git(["tag", "-a", "v1.0.0", head, "-m", "v1.0.0"], cwd=work)

    cut, _ = cut_tag(work, "v1.0.0")

    assert cut is True
    assert "refs/tags/v1.0.0" in _git(["ls-remote", "--tags", "origin"], cwd=work).stdout


def test_merge_commit_of_pr_dies_loudly_when_gh_is_missing(tmp_path: Path, monkeypatch) -> None:
    def _no_gh(cmd, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(_mod, "_run", _no_gh)
    with pytest.raises(SystemExit):
        _mod._merge_commit_of_pr(tmp_path, "7")


@pytest.mark.parametrize("stdout", ["", "null\n"])
def test_merge_commit_of_pr_dies_when_pr_not_merged(tmp_path: Path, monkeypatch, stdout: str) -> None:
    monkeypatch.setattr(
        _mod,
        "_run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""),
    )
    with pytest.raises(SystemExit):
        _mod._merge_commit_of_pr(tmp_path, "7")


def test_cut_tag_never_force_pushes() -> None:
    src = (_BIN_DIR / "merge-recovery-and-tag-cut.py").read_text(encoding="utf-8")
    for flag in ("--force", "-f\"", "+refs", "--force-with-lease"):
        assert flag not in src


clone_merge = _mod.clone_merge


def _diverge(work: Path) -> str:
    """A side branch with one commit, then a different commit on main."""
    _git(["checkout", "-b", "side"], cwd=work)
    (work / "side.txt").write_text("side\n", encoding="utf-8")
    _git(["add", "side.txt"], cwd=work)
    _git(["commit", "-m", "side"], cwd=work)
    _git(["checkout", "main"], cwd=work)
    (work / "main.txt").write_text("main\n", encoding="utf-8")
    _git(["add", "main.txt"], cwd=work)
    _git(["commit", "-m", "main"], cwd=work)
    return _head_sha(work, "side")


def test_clone_merge_lands_despite_staged_files_in_the_shared_tree(tmp_path: Path, capsys) -> None:
    work = _init_repo_with_origin(tmp_path)
    _git(["push", "origin", "main"], cwd=work)
    side = _diverge(work)
    _git(["checkout", "side"], cwd=work)
    (work / "peer.txt").write_text("a peer's staged work\n", encoding="utf-8")
    _git(["add", "peer.txt"], cwd=work)

    assert clone_merge(work, "main", "side", push=True) == 0

    out = capsys.readouterr().out
    merge_sha = out.split("MERGE_SHA=")[1].strip()
    assert _head_sha(work, "main") == merge_sha
    assert _git(["rev-parse", "main"], cwd=tmp_path / "origin.git").stdout.strip() == merge_sha
    assert _git(["rev-parse", f"{merge_sha}^2"], cwd=work).stdout.strip() == side
    assert "peer.txt" in _git(["diff", "--cached", "--name-only"], cwd=work).stdout
    assert not list((work / "scratch").glob("clone-merge-*"))


def test_clone_merge_into_the_checked_out_branch_leaves_a_fast_forward(tmp_path: Path, capsys) -> None:
    work = _init_repo_with_origin(tmp_path)
    _diverge(work)
    before = _head_sha(work, "main")

    assert clone_merge(work, "main", "side", push=False) == 0

    out = capsys.readouterr().out
    assert "FF_PENDING=git merge --ff-only" in out
    assert _head_sha(work, "main") == before


def test_clone_merge_conflict_keeps_the_clone_and_touches_nothing(tmp_path: Path, capsys) -> None:
    work = _init_repo_with_origin(tmp_path)
    _git(["checkout", "-b", "side"], cwd=work)
    (work / "f.txt").write_text("side\n", encoding="utf-8")
    _git(["commit", "-am", "side"], cwd=work)
    _git(["checkout", "main"], cwd=work)
    (work / "f.txt").write_text("main\n", encoding="utf-8")
    _git(["commit", "-am", "main"], cwd=work)
    _git(["checkout", "side"], cwd=work)
    before = _head_sha(work, "main")

    with pytest.raises(SystemExit):
        clone_merge(work, "main", "side", push=True)

    out = capsys.readouterr().out
    assert "CLONE=" in out
    assert _head_sha(work, "main") == before
    assert list((work / "scratch").glob("clone-merge-*"))
