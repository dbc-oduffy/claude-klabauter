"""Contract tests for `coordinator_core.git.hook_dispositions` using synthetic
entries against tmp_path repos (ordinary `.git` dir and worktree `.git` file)."""

from __future__ import annotations

import os
import subprocess as _sp
from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd

RETIRED = "#!/bin/sh\n# RETIRED-BODY\nexit 0\n"
FOREIGN = "#!/bin/sh\necho foreign\n"
BLOCK = "# >>> retired block\nrm -rf nothing\n# <<< retired block\n"


def _identify_exact(text):
    return hd.Match(0, len(text)) if text == RETIRED else None


def _identify_block(text):
    i = text.find(BLOCK)
    return hd.Match(i, i + len(BLOCK)) if i >= 0 else None


def _identify_stale_replace(text):
    return hd.Match(0, len(text)) if text == "old\n" else None


REMOVE = hd.HookDisposition("t-remove", "post-commit", "remove", _identify_exact)
EXCISE = hd.HookDisposition("t-excise", "post-merge", "excise-block", _identify_block)
REPLACE = hd.HookDisposition(
    "t-replace", "pre-push", "replace", _identify_stale_replace, replacement=lambda: "new\n"
)


@pytest.fixture(params=["dir", "worktree"])
def repo(request, tmp_path):
    if request.param == "dir":
        root = tmp_path / "clone"
        (root / ".git" / "hooks").mkdir(parents=True)
        return root, root / ".git" / "hooks"
    common = tmp_path / "main" / ".git"
    (common / "hooks").mkdir(parents=True)
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    return root, common / "hooks"


@pytest.fixture
def table(monkeypatch):
    def install(*entries):
        monkeypatch.setattr(hd, "DISPOSITIONS", tuple(entries), raising=False)

    return install


def _put(path: Path, text: str):
    path.write_bytes(text.encode("utf-8"))


def test_module_names_no_process_spawner():
    src = Path(hd.__file__).read_text(encoding="utf-8")
    assert src.count("sub" + "process") == 0


def test_contract_symbols_exist():
    for name in ("HookDisposition", "classify_repo", "apply_repo"):
        assert hasattr(hd, name)


def test_remove_backs_up_then_unlinks_and_is_idempotent(repo, table):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit", RETIRED)
    assert hd.classify_repo(root) == [("t-remove", "post-commit", "stale")]
    assert (hooks / "post-commit").read_text(encoding="utf-8") == RETIRED
    assert hd.apply_repo(root, check_only=False) == [("t-remove", "post-commit", "removed")]
    assert not (hooks / "post-commit").exists()
    assert (hooks / "post-commit.retired").read_bytes() == RETIRED.encode()
    assert hd.apply_repo(root, check_only=False) == [("t-remove", "post-commit", "absent")]


def test_excise_keeps_foreign_text_and_backs_up(repo, table):
    root, hooks = repo
    table(EXCISE)
    original = FOREIGN + BLOCK
    _put(hooks / "post-merge", original)
    assert hd.apply_repo(root, check_only=False) == [("t-excise", "post-merge", "block-excised")]
    assert (hooks / "post-merge").read_text(encoding="utf-8") == FOREIGN
    assert (hooks / "post-merge.retired").read_bytes() == original.encode()
    assert hd.apply_repo(root, check_only=False) == [("t-excise", "post-merge", "current")]


def test_replace_then_current(repo, table):
    root, hooks = repo
    table(REPLACE)
    _put(hooks / "pre-push", "old\n")
    assert hd.apply_repo(root, check_only=False) == [("t-replace", "pre-push", "replaced")]
    assert (hooks / "pre-push").read_text(encoding="utf-8") == "new\n"
    assert (hooks / "pre-push.retired").read_bytes() == b"old\n"
    assert hd.apply_repo(root, check_only=False) == [("t-replace", "pre-push", "current")]


def test_foreign_body_is_byte_identical(repo, table):
    root, hooks = repo
    table(REMOVE, EXCISE, REPLACE)
    for name in ("post-commit", "post-merge", "pre-push"):
        _put(hooks / name, FOREIGN)
    verdicts = hd.apply_repo(root, check_only=False)
    assert [v[2] for v in verdicts] == ["current", "current", "unidentified-left-alone"]
    for name in ("post-commit", "post-merge", "pre-push"):
        assert (hooks / name).read_bytes() == FOREIGN.encode()
    assert not list(hooks.glob("*.retired*"))


def test_non_utf8_body_left_alone(repo, table):
    root, hooks = repo
    table(REMOVE)
    (hooks / "post-commit").write_bytes(b"\xff\xfe\x00bad")
    assert hd.apply_repo(root, check_only=False)[0][2] == "unidentified-left-alone"
    assert (hooks / "post-commit").read_bytes() == b"\xff\xfe\x00bad"


def test_check_only_never_writes(repo, table):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit", RETIRED)
    assert hd.apply_repo(root, check_only=True) == [("t-remove", "post-commit", "stale")]
    assert [p.name for p in hooks.iterdir()] == ["post-commit"]


def test_existing_retired_backup_is_never_overwritten(repo, table):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit.retired", "earlier backup")
    _put(hooks / "post-commit", RETIRED)
    hd.apply_repo(root, check_only=False)
    assert (hooks / "post-commit.retired").read_text(encoding="utf-8") == "earlier backup"
    assert (hooks / "post-commit.retired.1").read_bytes() == RETIRED.encode()


def test_applies_to_filters_entries(repo, table):
    root, hooks = repo
    skipped = hd.HookDisposition(
        "t-skip", "post-commit", "remove", _identify_exact, applies_to=lambda r: False
    )
    table(skipped)
    _put(hooks / "post-commit", RETIRED)
    assert hd.apply_repo(root, check_only=False) == []
    assert (hooks / "post-commit").exists()


def test_replace_requires_replacement():
    with pytest.raises(ValueError):
        hd.HookDisposition("x", "pre-push", "replace", lambda t: None)


def test_refused_when_backup_cannot_be_written(repo, table, monkeypatch):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit", RETIRED)
    monkeypatch.setattr(hd, "_write_backup", lambda p, r: None)
    assert hd.apply_repo(root, check_only=False) == [("t-remove", "post-commit", "refused-no-backup")]
    assert (hooks / "post-commit").read_bytes() == RETIRED.encode()


def test_permission_error_yields_busy_and_keeps_original(repo, table, monkeypatch):
    root, hooks = repo
    table(REMOVE, REPLACE)
    _put(hooks / "post-commit", RETIRED)
    _put(hooks / "pre-push", "old\n")

    def deny(*a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", deny)
    monkeypatch.setattr(Path, "unlink", deny)
    verdicts = hd.apply_repo(root, check_only=False)
    assert [v[2] for v in verdicts] == ["busy", "busy"]
    monkeypatch.undo()
    assert (hooks / "post-commit").read_bytes() == RETIRED.encode()
    assert (hooks / "pre-push").read_bytes() == b"old\n"


def test_file_vanishing_mid_act_reads_as_absent(repo, table, monkeypatch):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit", RETIRED)

    def gone(self, *a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "unlink", gone)
    assert hd.apply_repo(root, check_only=False) == [("t-remove", "post-commit", "absent")]


def test_walk_spawns_nothing(repo, table, monkeypatch):
    root, hooks = repo
    table(REMOVE)
    _put(hooks / "post-commit", RETIRED)

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(_sp, "Popen", boom)
    assert hd.apply_repo(root, check_only=False)[0][2] == "removed"


def test_repo_without_git_is_absent(tmp_path, table):
    table(REMOVE)
    assert hd.apply_repo(tmp_path, check_only=False) == [("t-remove", "post-commit", "absent")]
