
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    run_kwargs = {**no_console_creationflags(), **kwargs}
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **run_kwargs
    )


def _porcelain(repo: Path) -> list[str]:
    result = _git(["status", "--porcelain"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _head_sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], repo).stdout.strip()


def _assert_ac2_oracle(repo: Path) -> None:
    assert _porcelain(repo) == [], "git status --porcelain is not empty"
    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert fsck.returncode == 0, f"git fsck --strict failed: {fsck.stdout}\n{fsck.stderr}"


_CANARY_HOOKS = ("pre-commit", "post-commit", "pre-push")


def _install_hook_canary(repo: Path) -> Path:
    """Install a witness-writing hook per `_CANARY_HOOKS`; return the
    directory the witnesses land in. Written as `#!/bin/sh` because that is
    what git itself invokes hooks with on every platform it supports,
    including Windows (git ships its own sh) -- this is not a shell-out by
    this repo's own code and so is not a `shell-out-carve-outs.md` site."""
    witness_dir = repo.parent / "hook-witness"
    witness_dir.mkdir(exist_ok=True)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook in _CANARY_HOOKS:
        script = hooks_dir / hook
        script.write_text(
            f'#!/bin/sh\ntouch "{witness_dir.as_posix()}/{hook}"\nexit 0\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    return witness_dir


def _assert_no_hook_fired(witness_dir: Path) -> None:
    fired = sorted(p.name for p in witness_dir.iterdir())
    assert fired == [], (
        f"the destination repository's hooks fired on our behalf: {fired} -- "
        "this entrypoint exists to make that impossible"
    )


@contextmanager
def _count_git_spawns(monkeypatch: pytest.MonkeyPatch):
    counted: list[list[str]] = []
    real_popen = subprocess.Popen

    def _argv_is_git(args) -> bool:
        if isinstance(args, (str, Path)):
            return Path(str(args)).name.lower().startswith("git")
        try:
            first = args[0]
        except (TypeError, IndexError, KeyError):
            return False
        return Path(str(first)).name.lower().startswith("git")

    class _Popen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            if _argv_is_git(args):
                counted.append([str(x) for x in args])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    try:
        yield counted
    finally:
        monkeypatch.undo()


def _msg(tmp_path: Path, text: str = "deliver memo\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _sync_worktree(repo: Path, rel: str, content: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8", newline="")


def test_creates_a_path_absent_from_head_with_one_hookless_spawn(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    witness = _install_hook_canary(repo)
    rel = "memo.md"
    content = "# memo\n\nbody\n"
    _sync_worktree(repo, rel, content)
    before = _head_sha(repo)

    with _count_git_spawns(monkeypatch) as spawns:
        result = git_native.commit_authored_new_file(
            rel, content, _msg(tmp_path), repo,
        )

    assert result.ok, result.stderr
    assert result.stdout.strip() != before
    assert _head_sha(repo) == result.stdout.strip()
    assert _git(["show", f"HEAD:{rel}"], repo).stdout == content

    argvs = [" ".join(a[1:3]) for a in spawns]
    assert len(spawns) == 1, f"expected exactly one spawn (update-index), got {argvs}"
    assert spawns[0][1] == "update-index", argvs

    _assert_no_hook_fired(witness)
    _assert_ac2_oracle(repo)


def test_creates_a_nested_path_under_directories_absent_from_head(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    witness = _install_hook_canary(repo)
    rel = "cross-repo/inbox/2026-08-25-a-memo.md"
    content = "nested\n"
    _sync_worktree(repo, rel, content)

    with _count_git_spawns(monkeypatch) as spawns:
        result = git_native.commit_authored_new_file(rel, content, _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert _git(["show", f"HEAD:{rel}"], repo).stdout == content
    assert len(spawns) == 1, [a[:3] for a in spawns]
    _assert_no_hook_fired(witness)
    _assert_ac2_oracle(repo)


def test_creates_a_nested_path_under_a_directory_that_does_exist_in_head(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    (repo / "cross-repo" / "inbox" / "prior.md").write_text("prior\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "prior memo"], repo)
    rel = "cross-repo/inbox/second.md"
    _sync_worktree(repo, rel, "second\n")

    result = git_native.commit_authored_new_file(rel, "second\n", _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert _git(["show", f"HEAD:{rel}"], repo).stdout == "second\n"
    assert _git(["show", "HEAD:cross-repo/inbox/prior.md"], repo).stdout == "prior\n", (
        "a sibling already in the directory must survive the spine rewrite"
    )
    _assert_ac2_oracle(repo)


def test_refuses_when_an_ancestor_name_is_a_committed_file_not_a_directory(tmp_path):
    repo = real_git_repo(tmp_path)
    before = _head_sha(repo)

    result = git_native.commit_authored_new_file(
        "seed.txt/memo.md", "x\n", _msg(tmp_path), repo,
    )

    assert not result.ok
    assert _head_sha(repo) == before
    assert _git(["cat-file", "-t", "HEAD:seed.txt"], repo).stdout.strip() == "blob"


def test_refresh_off_is_zero_spawns_and_leaves_the_index_contradiction(tmp_path, monkeypatch):
    """The commit itself costs ZERO spawns -- and this test pins the exact
    end state that makes `refresh_shared_index=True` the default anyway: the
    path reads as a STAGED DELETION alongside an untracked copy of itself,
    so a later blanket `git add -A`/`git commit -a` in that repo deletes the
    file we just delivered. Asserted as the real end state rather than
    described in a docstring, so a future reader cannot mistake the default
    for a preference."""
    repo = real_git_repo(tmp_path)
    witness = _install_hook_canary(repo)
    rel = "memo.md"
    content = "zero spawn\n"
    _sync_worktree(repo, rel, content)

    with _count_git_spawns(monkeypatch) as spawns:
        result = git_native.commit_authored_new_file(
            rel, content, _msg(tmp_path), repo, refresh_shared_index=False,
        )

    assert result.ok, result.stderr
    assert spawns == [], f"the commit leg must spawn nothing: {[a[:3] for a in spawns]}"
    assert _git(["show", f"HEAD:{rel}"], repo).stdout == content
    _assert_no_hook_fired(witness)

    assert sorted(_porcelain(repo)) == [f"?? {rel}", f"D  {rel}"]

    _git(["update-index", "--add", "--cacheinfo", f"100644,{_blob_sha(repo, rel)},{rel}"], repo)
    _assert_ac2_oracle(repo)


def _blob_sha(repo: Path, rel: str) -> str:
    return _git(["rev-parse", f"HEAD:{rel}"], repo).stdout.strip()


def test_refuses_a_path_that_already_exists_in_head(tmp_path):
    repo = real_git_repo(tmp_path)
    before = _head_sha(repo)

    result = git_native.commit_authored_new_file(
        "seed.txt", "overwritten\n", _msg(tmp_path), repo,
    )

    assert not result.ok
    assert "already exists in HEAD" in result.stderr
    assert "commit_authored_content" in result.stderr, (
        "the refusal must name the sibling entrypoint that does handle this case"
    )
    assert _head_sha(repo) == before


def test_unmet_in_process_precondition_fails_loud_instead_of_taking_the_ladder(
    tmp_path, monkeypatch
):
    repo = real_git_repo(tmp_path)
    witness = _install_hook_canary(repo)
    rel = "memo.md"
    _sync_worktree(repo, rel, "x\n")
    before = _head_sha(repo)
    monkeypatch.setattr(git_native, "_commit_via_head_spine", lambda *a, **kw: None)

    with _count_git_spawns(monkeypatch) as spawns:
        result = git_native.commit_authored_new_file(rel, "x\n", _msg(tmp_path), repo)

    assert result is not None
    assert not result.ok
    assert "refusing rather than falling back to the spawning ladder" in result.stderr
    assert _head_sha(repo) == before
    assert spawns == [], f"a refusal must not spawn: {[a[:3] for a in spawns]}"
    _assert_no_hook_fired(witness)


def test_refuses_content_carrying_a_cr_byte(tmp_path):
    repo = real_git_repo(tmp_path)
    result = git_native.commit_authored_new_file(
        "memo.md", "line\r\n", _msg(tmp_path), repo,
    )
    assert not result.ok
    assert "CR byte" in result.stderr


def test_refuses_when_a_local_attributes_file_declares_a_clean_filter(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.md filter=redact\n", encoding="utf-8")
    result = git_native.commit_authored_new_file(
        "memo.md", "body\n", _msg(tmp_path), repo,
    )
    assert not result.ok
    assert "filter" in result.stderr


def test_refuses_a_filter_macro_it_cannot_resolve(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("[attr]redacted filter=redact\n", encoding="utf-8")
    result = git_native.commit_authored_new_file(
        "memo.md", "body\n", _msg(tmp_path), repo,
    )
    assert not result.ok
    assert "macro" in result.stderr


_FLEET_LFS_ATTRIBUTE_LINES = (
    "*.exe filter=lfs diff=lfs merge=lfs -text",
    "*.uasset filter=lfs diff=lfs merge=lfs -text",
    "*.png filter=lfs diff=lfs merge=lfs -text",
    "chunks/**/*.jsonl filter=lfs diff=lfs merge=lfs -text",
    "state/resume-snapshots/** filter=lfs diff=lfs merge=lfs -text",
)


@pytest.mark.parametrize("attribute_line", _FLEET_LFS_ATTRIBUTE_LINES)
def test_a_filter_that_cannot_match_the_path_does_not_refuse(tmp_path, attribute_line):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text(attribute_line + "\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "attributes"], repo)
    rel = "cross-repo/inbox/2026-08-25-a-memo.md"
    _sync_worktree(repo, rel, "body\n")

    result = git_native.commit_authored_new_file(rel, "body\n", _msg(tmp_path), repo)

    assert result.ok, result.stderr
    _assert_ac2_oracle(repo)


def test_a_commented_out_filter_attribute_does_not_refuse(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("# *.md filter=redact\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "attributes"], repo)
    rel = "memo.md"
    _sync_worktree(repo, rel, "body\n")

    result = git_native.commit_authored_new_file(rel, "body\n", _msg(tmp_path), repo)

    assert result.ok, result.stderr
    _assert_ac2_oracle(repo)


def test_a_filter_in_a_subdirectory_attributes_file_is_scoped_to_that_directory(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "other").mkdir()
    (repo / "other" / ".gitattributes").write_text("*.md filter=redact\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "attributes"], repo)

    rel = "cross-repo/inbox/memo.md"
    _sync_worktree(repo, rel, "body\n")
    result = git_native.commit_authored_new_file(rel, "body\n", _msg(tmp_path), repo)
    assert result.ok, result.stderr
    _assert_ac2_oracle(repo)

    refused = git_native.commit_authored_new_file(
        "other/memo.md", "body\n", _msg(tmp_path), repo,
    )
    assert not refused.ok
    assert "filter" in refused.stderr


def test_an_anchored_filter_pattern_is_matched_against_the_full_path(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text(
        "/cross-repo/inbox/*.md filter=redact\n", encoding="utf-8"
    )
    result = git_native.commit_authored_new_file(
        "cross-repo/inbox/memo.md", "body\n", _msg(tmp_path), repo,
    )
    assert not result.ok
    assert "filter" in result.stderr


@pytest.mark.parametrize(
    "path",
    ["/abs/memo.md", "../escape.md", "", "C:/abs/memo.md"],
)
def test_containment_refusals_return_a_failed_result_never_none(tmp_path, path):
    repo = real_git_repo(tmp_path)
    result = git_native.commit_authored_new_file(path, "x\n", _msg(tmp_path), repo)
    assert result is not None
    assert not result.ok


def test_refuses_a_directory_path(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "adir").mkdir()
    result = git_native.commit_authored_new_file("adir", "x\n", _msg(tmp_path), repo)
    assert not result.ok


def test_deliverable_id_mismatch_refuses_rather_than_rewriting_the_message(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    rel = "memo.md"
    _sync_worktree(repo, rel, "x\n")
    msg_file = _msg(tmp_path, "deliver memo\n\nDeliverable-Id: dlv-other\n")
    before_bytes = msg_file.read_bytes()

    with _count_git_spawns(monkeypatch) as spawns:
        result = git_native.commit_authored_new_file(
            rel, "x\n", msg_file, repo, deliverable_id="dlv-mine",
        )

    assert not result.ok
    assert "validates the trailer it is given and never rewrites it" in result.stderr
    assert msg_file.read_bytes() == before_bytes, "msg_file must not be rewritten"
    assert spawns == [], "no interpret-trailers spawn, on any path"


def test_matching_deliverable_id_commits_the_message_bytes_verbatim(tmp_path):
    repo = real_git_repo(tmp_path)
    rel = "memo.md"
    _sync_worktree(repo, rel, "x\n")
    msg_text = "deliver memo\n\nDeliverable-Id: dlv-mine\n"
    msg_file = _msg(tmp_path, msg_text)

    result = git_native.commit_authored_new_file(
        rel, "x\n", msg_file, repo, deliverable_id="dlv-mine",
    )

    assert result.ok, result.stderr
    body = _git(["log", "-1", "--format=%B"], repo).stdout
    assert "Deliverable-Id: dlv-mine" in body
    assert body.count("Deliverable-Id:") == 1, "the trailer must not be duplicated"
    _assert_ac2_oracle(repo)


def test_record_ledger_defaults_off_so_a_cross_tree_commit_writes_no_ledger(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    rel = "memo.md"
    _sync_worktree(repo, rel, "x\n")
    calls: list = []
    import coordinator_core.contract.apply_base as apply_base

    monkeypatch.setattr(
        apply_base, "record_ledger_entry", lambda *a, **kw: calls.append((a, kw))
    )

    result = git_native.commit_authored_new_file(rel, "x\n", _msg(tmp_path), repo)

    assert result.ok, result.stderr
    assert calls == [], "the default must not deposit our ledger in the destination's state/"


def test_record_ledger_true_writes_one_entry_for_the_committed_path(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    rel = "memo.md"
    _sync_worktree(repo, rel, "x\n")
    calls: list = []
    import coordinator_core.contract.apply_base as apply_base

    monkeypatch.setattr(
        apply_base, "record_ledger_entry", lambda *a, **kw: calls.append((a, kw))
    )

    result = git_native.commit_authored_new_file(
        rel, "x\n", _msg(tmp_path), repo, record_ledger=True,
    )

    assert result.ok, result.stderr
    assert len(calls) == 1
    assert calls[0][0][1] == [rel]
    assert calls[0][0][2] == result.stdout.strip()
