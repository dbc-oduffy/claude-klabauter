"""C6 — the foreign-tracked-overwrite guard for the percolation write path
(``coordinator_core.install.substrate._percolation_and_path_steps`` +
``_install_one``'s ``write_strategy`` mechanism selector).

Spec backlink: pln-percolate-root-rung-ordering-c-b2f52c, Tasks
§ C6, Acceptance Criteria AC6/AC6a/AC7/AC19/AC20/AC21.

New sibling module (not appended to test_install_one_overwrite_policy.py or
test_substrate.py): those two files cover `_install_one`'s pre-existing
suffix/name overwrite policy and unrelated substrate helpers respectively —
this module's subject is the NEW C6 machinery (`_resolve_directory_tracked_set`,
`_careful_write`, `_assert_careful_write_in_manifest`, `_percolation_and_path_
steps`'s tracked-ness classification, and `_install_one`'s new
`write_strategy` parameter), which is large and distinct enough to warrant
its own file rather than diluting either existing matrix.

Every test runs against `tmp_path` fixtures only — never the real
`~/.claude/setup/` or `~/.claude/setup-overwrite-backups/`.
"""
from __future__ import annotations

import shutil

import pytest

from coordinator_core.install import substrate
from coordinator_core.install._shared import atomic_write_bytes
from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _assert_careful_write_in_manifest,
    _careful_write,
    _install_one,
    _percolation_and_path_steps,
    _resolve_directory_tracked_set,
)

_SETUP_TEMPLATE_FILES = ["publish_sync.py", ".percolate-identity.example"]
_SETUP_TEMPLATE_HOOK_FILES = [
    "percolate-hooks/README.md",
    "percolate-hooks/percolate-store.yaml",
    "percolate-hooks/coordinator-claude/pre-ci/.gitkeep",
    "percolate-hooks/coordinator-claude/pre-rsync/.gitkeep",
    "percolate-hooks/coordinator-claude-toplevel-wiki/post-rsync/publish-native-allowlist.txt",
]


def _git(*args, cwd):
    return substrate._run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _init_tracking_repo(repo_dir, tracked_relpaths, content="tracked content\n"):
    """Create a real git repo at ``repo_dir`` with each of ``tracked_relpaths``
    committed (so ``git ls-files`` reports them TRACKED) — a stand-in for the
    PM-named "prime-v3... backed-up trove of documents" shape."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "test@example.invalid", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)
    for rel in tracked_relpaths:
        p = repo_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo_dir)
    _git("commit", "-q", "-m", "seed", cwd=repo_dir)


def _make_src_tree(tmp_path, files):
    src = tmp_path / "src"
    for rel in files:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("new template content\n", encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# _resolve_directory_tracked_set
# ---------------------------------------------------------------------------


def test_resolve_directory_tracked_set_returns_tracked_relpaths(tmp_path):
    dest = tmp_path / "setup"
    _init_tracking_repo(dest, ["publish_sync.py", "percolate-hooks/README.md"])

    tracked = _resolve_directory_tracked_set(dest)

    assert tracked == frozenset({"publish_sync.py", "percolate-hooks/README.md"})


def test_resolve_directory_tracked_set_empty_when_not_a_repo(tmp_path):
    # A dotfiles-managed HOME sitting inside a foreign repo but with nothing
    # tracked under THIS directory is correctly not-foreign (empty set, not
    # None) — here the directory isn't even inside a repo at all, the
    # simplest case of the same "nothing tracked" answer.
    dest = tmp_path / "not_a_repo" / "setup"
    dest.mkdir(parents=True)

    tracked = _resolve_directory_tracked_set(dest)

    assert tracked == frozenset()


def test_resolve_directory_tracked_set_none_when_git_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    dest = tmp_path / "setup"
    dest.mkdir()

    assert _resolve_directory_tracked_set(dest) is None


def test_resolve_directory_tracked_set_one_spawn_per_directory(tmp_path, monkeypatch):
    dest = tmp_path / "setup"
    _init_tracking_repo(dest, ["publish_sync.py", "percolate-hooks/README.md"])

    calls = []
    real_run = substrate.subprocess.run

    def _counting_run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(substrate.subprocess, "run", _counting_run)

    _resolve_directory_tracked_set(dest)

    ls_files_calls = [c for c in calls if "ls-files" in c]
    assert len(ls_files_calls) == 1


# ---------------------------------------------------------------------------
# AC19 — blast-radius negative-spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_path",
    [
        "settings.json",
        "hooks.json",
        "some/nested/dir/hooks.json",
        "not-in-manifest.py",
        "../../etc/passwd",
        "percolate-hooks/not-a-real-hook-file.yaml",
    ],
)
def test_assert_careful_write_in_manifest_rejects_out_of_manifest_paths(relative_path):
    manifest = frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    with pytest.raises(SubstrateFatalError):
        _assert_careful_write_in_manifest(relative_path, manifest)


@pytest.mark.parametrize("relative_path", _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
def test_assert_careful_write_in_manifest_accepts_manifest_paths(relative_path):
    manifest = frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    _assert_careful_write_in_manifest(relative_path, manifest)  # must not raise


def test_careful_write_rejects_out_of_manifest_destination_end_to_end(tmp_path):
    """The careful-write MECHANISM itself (not just the standalone assert
    helper) refuses an out-of-manifest destination — AC19's "rejected or
    structurally unreachable" bar applied to the actual write function."""
    install_base = tmp_path / "install_base"
    dst_dir = install_base / ".claude" / "setup"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "settings.json"
    dst.write_text("pre-existing\n", encoding="utf-8")
    src = tmp_path / "src" / "settings.json"
    src.parent.mkdir(parents=True)
    src.write_text("new\n", encoding="utf-8")

    with pytest.raises(SubstrateFatalError):
        _careful_write(
            dst, src,
            relative_path="settings.json",
            manifest_relative_paths=frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES),
            install_base=install_base,
        )
    # Rejected before ever touching the destination.
    assert dst.read_text(encoding="utf-8") == "pre-existing\n"


# ---------------------------------------------------------------------------
# AC20 — backup lands on disk BEFORE the atomic replace
# ---------------------------------------------------------------------------


def test_careful_write_backup_exists_before_replace(tmp_path, monkeypatch):
    install_base = tmp_path / "install_base"
    dst_dir = install_base / ".claude" / "setup"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "publish_sync.py"
    dst.write_text("pre-existing content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new content\n", encoding="utf-8")

    seen_backup_present_at_replace_time = {}

    real_atomic_write_bytes = substrate.atomic_write_bytes

    def _spying_atomic_write_bytes(target, data, **kwargs):
        backups_dir = install_base / ".claude" / "setup-overwrite-backups"
        matches = list(backups_dir.glob("publish_sync.py.pre-install-*.bak"))
        seen_backup_present_at_replace_time["matches"] = matches
        seen_backup_present_at_replace_time["content"] = (
            matches[0].read_text(encoding="utf-8") if matches else None
        )
        return real_atomic_write_bytes(target, data, **kwargs)

    monkeypatch.setattr(substrate, "atomic_write_bytes", _spying_atomic_write_bytes)

    backup_path = _careful_write(
        dst, src,
        relative_path="publish_sync.py",
        manifest_relative_paths=frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES),
        install_base=install_base,
    )

    assert seen_backup_present_at_replace_time["matches"], (
        "backup file must already exist on disk by the time the atomic replace runs"
    )
    assert seen_backup_present_at_replace_time["content"] == "pre-existing content\n"
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == "pre-existing content\n"
    assert dst.read_text(encoding="utf-8") == "new content\n"
    # Restore-path shape assertion (AC20's documentation half).
    assert backup_path.parent == install_base / ".claude" / "setup-overwrite-backups"
    assert backup_path.name.startswith("publish_sync.py.pre-install-")
    assert backup_path.name.endswith(".bak")


def test_careful_write_backup_failure_refuses_the_overwrite(tmp_path, monkeypatch):
    install_base = tmp_path / "install_base"
    dst_dir = install_base / ".claude" / "setup"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "publish_sync.py"
    dst.write_text("pre-existing content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new content\n", encoding="utf-8")

    def _boom(*_a, **_kw):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(substrate.shutil, "copyfile", _boom)

    with pytest.raises(SubstrateFatalError):
        _careful_write(
            dst, src,
            relative_path="publish_sync.py",
            manifest_relative_paths=frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES),
            install_base=install_base,
        )

    assert dst.read_text(encoding="utf-8") == "pre-existing content\n"


# ---------------------------------------------------------------------------
# AC21 — interrupted write leaves the destination byte-identical
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_interrupted_replace_leaves_destination_untouched(tmp_path, monkeypatch):
    target = tmp_path / "dst" / "publish_sync.py"
    target.parent.mkdir(parents=True)
    target.write_text("original content\n", encoding="utf-8")

    def _boom(_src, _dst):
        raise OSError("simulated interrupted os.replace")

    monkeypatch.setattr(substrate.os, "replace", _boom)

    with pytest.raises(OSError):
        atomic_write_bytes(target, b"new content\n")

    assert target.read_text(encoding="utf-8") == "original content\n"
    # No stray tempfile left behind in the destination directory either.
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".atomic-write.")]
    assert leftovers == []


def test_careful_write_interrupted_replace_leaves_destination_untouched(tmp_path, monkeypatch):
    install_base = tmp_path / "install_base"
    dst_dir = install_base / ".claude" / "setup"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "publish_sync.py"
    dst.write_text("pre-existing content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new content\n", encoding="utf-8")

    def _boom(_src, _dst):
        raise OSError("simulated interrupted os.replace")

    monkeypatch.setattr(substrate.os, "replace", _boom)

    with pytest.raises(OSError):
        _careful_write(
            dst, src,
            relative_path="publish_sync.py",
            manifest_relative_paths=frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES),
            install_base=install_base,
        )

    # Backup was already written (the whole point), but the destination
    # itself must be byte-identical to before the attempt -- no truncated
    # or partially-written file.
    assert dst.read_text(encoding="utf-8") == "pre-existing content\n"


# ---------------------------------------------------------------------------
# AC6 — end-to-end through _install_one's write_strategy selector
# ---------------------------------------------------------------------------


def test_install_one_careful_strategy_backs_up_then_replaces_and_reports(tmp_path, capsys):
    install_base = tmp_path / "install_base"
    dst_dir = install_base / ".claude" / "setup"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "publish_sync.py"
    dst.write_text("foreign-tracked pre-existing\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    _install_one(
        src, dst, False, "machine-local", False,
        force_overwrite=True,
        write_strategy="careful",
        careful_manifest_relative_paths=frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES),
        careful_relative_path="publish_sync.py",
        careful_install_base=install_base,
    )

    assert dst.read_text(encoding="utf-8") == "new template\n"
    backups = list((install_base / ".claude" / "setup-overwrite-backups").glob("publish_sync.py.pre-install-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "foreign-tracked pre-existing\n"
    out = capsys.readouterr().out
    assert str(dst) in out
    assert str(backups[0]) in out


def test_install_one_cold_creation_always_forces_regardless_of_write_strategy(tmp_path):
    """Cold creation of a destination that does not yet exist proceeds via
    the force mechanism unconditionally -- write_strategy="refuse" must NOT
    block a fresh install."""
    dst = tmp_path / "dst" / "publish_sync.py"
    dst.parent.mkdir(parents=True)
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False, write_strategy="refuse")

    assert dst.read_text(encoding="utf-8") == "new template\n"


def test_install_one_refuse_strategy_preserves_destination_and_reports(tmp_path, capsys):
    dst = tmp_path / "dst" / "publish_sync.py"
    dst.parent.mkdir(parents=True)
    dst.write_text("stale content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    _install_one(
        src, dst, False, "machine-local", False,
        force_overwrite=True, write_strategy="refuse",
    )

    assert dst.read_text(encoding="utf-8") == "stale content\n"
    out = capsys.readouterr().out
    assert "refusing to overwrite" in out


def test_install_one_check_only_foreign_tracked_stale_reports_not_managed_here(tmp_path, capsys):
    dst = tmp_path / "dst" / "publish_sync.py"
    dst.parent.mkdir(parents=True)
    dst.write_text("stale content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    # Must not raise SubstrateFatalError -- today's unconditional-raise
    # behaviour is exactly what AC6's check-mode contract changes for this
    # one classification.
    _install_one(
        src, dst, False, "machine-local", True,
        force_overwrite=True, write_strategy="careful",
    )

    out = capsys.readouterr().out
    assert "not managed here" in out
    assert dst.read_text(encoding="utf-8") == "stale content\n"


def test_install_one_check_only_still_raises_for_absent_destination(tmp_path):
    """Regression guard: check_only's pre-existing "absent" hard-fail is
    untouched by the new careful/refuse branch -- verified on disk per the
    plan's own instruction to check this before changing behaviour."""
    dst = tmp_path / "dst" / "publish_sync.py"
    dst.parent.mkdir(parents=True)
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    with pytest.raises(SubstrateFatalError):
        _install_one(
            src, dst, False, "machine-local", True,
            force_overwrite=True, write_strategy="careful",
        )


def test_install_one_check_only_non_foreign_stale_still_raises(tmp_path):
    """A stale destination that is NOT foreign-tracked (write_strategy=
    "force") still hard-fails check -- the new "not managed here" leniency
    is scoped to careful/refuse only."""
    dst = tmp_path / "dst" / "publish_sync.py"
    dst.parent.mkdir(parents=True)
    dst.write_text("stale content\n", encoding="utf-8")
    src = tmp_path / "src" / "publish_sync.py"
    src.parent.mkdir(parents=True)
    src.write_text("new template\n", encoding="utf-8")

    with pytest.raises(SubstrateFatalError):
        _install_one(src, dst, False, "machine-local", True, force_overwrite=True)


# ---------------------------------------------------------------------------
# AC6 / AC6a / AC7 — through _percolation_and_path_steps end-to-end
# ---------------------------------------------------------------------------


def _run_percolation(tmp_path, *, tracked_content=None, no_git=False, monkeypatch=None):
    """Shared fixture builder: a src tree with the full manifest, a
    destination `.claude/setup/` optionally pre-seeded as a foreign-tracked
    git repo (``tracked_content`` truthy) with ONE stale, differing file
    (`publish_sync.py`), then runs `_percolation_and_path_steps`."""
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    setup_dest = install_base / ".claude" / "setup"

    if tracked_content is not None:
        _init_tracking_repo(
            setup_dest,
            _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES,
            content=tracked_content,
        )

    if no_git:
        monkeypatch.setattr(shutil, "which", lambda name: None)

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )
    return install_base, setup_dest


def test_percolation_foreign_tracked_overwrite_goes_through_careful_path(tmp_path, capsys):
    install_base, setup_dest = _run_percolation(
        tmp_path, tracked_content="foreign-tracked stale content\n"
    )

    dst = setup_dest / "publish_sync.py"
    assert dst.read_text(encoding="utf-8") == "new template content\n"
    backups = list((install_base / ".claude" / "setup-overwrite-backups").glob("publish_sync.py.pre-install-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "foreign-tracked stale content\n"
    out = capsys.readouterr().out
    assert "backup:" in out


def test_percolation_git_identity_resolved_once_per_directory(tmp_path, monkeypatch):
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    setup_dest = install_base / ".claude" / "setup"
    _init_tracking_repo(setup_dest, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)

    calls = []
    real_run = substrate.subprocess.run

    def _counting_run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(substrate.subprocess, "run", _counting_run)

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )

    ls_files_calls = [c for c in calls if "ls-files" in c]
    assert len(ls_files_calls) == 1


def test_percolation_cold_creation_uses_force_regardless_of_tracked_ness(tmp_path):
    """AC6/AC6a: a destination that does not exist yet installs unconditionally,
    even though its directory IS a foreign-tracked repo -- tracked-ness never
    blocks cold creation."""
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    setup_dest = install_base / ".claude" / "setup"
    # Seed the repo tracking a DIFFERENT file only, so setup_dest is a real
    # git repo (foreign) but every manifest destination is a cold create.
    _init_tracking_repo(setup_dest, ["some-other-tracked-file.txt"])

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )

    for rel in _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES:
        assert (setup_dest / rel).is_file(), f"{rel} missing -- full manifest must be delivered"
        assert (setup_dest / rel).read_text(encoding="utf-8") == "new template content\n"
    # No backups directory created -- nothing was an overwrite.
    assert not (install_base / ".claude" / "setup-overwrite-backups").exists()


def test_percolation_cold_install_no_git_on_path_delivers_full_manifest(tmp_path, monkeypatch):
    """AC6a: a cold install with NO git on PATH at all still delivers every
    manifest entry -- the probe-unavailable degrade only affects the
    overwrite decision, never creation."""
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    setup_dest = install_base / ".claude" / "setup"

    monkeypatch.setattr(shutil, "which", lambda name: None)

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )

    for rel in _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES:
        assert (setup_dest / rel).is_file(), f"{rel} missing -- full manifest must be delivered"


def test_percolation_no_git_stale_overwrite_refuses_but_reports(tmp_path, monkeypatch, capsys):
    """AC6a's degrade: with no git on PATH, a STALE (already-existing,
    differing) destination is refused (not clobbered blind) but reported --
    never a silent skip, and cold entries alongside it still deliver."""
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    setup_dest = install_base / ".claude" / "setup"
    setup_dest.mkdir(parents=True)
    stale = setup_dest / "publish_sync.py"
    stale.write_text("stale content\n", encoding="utf-8")

    monkeypatch.setattr(shutil, "which", lambda name: None)

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )

    assert stale.read_text(encoding="utf-8") == "stale content\n"
    out = capsys.readouterr().out
    assert "refusing to overwrite" in out
    # The rest of the manifest (cold entries) still delivered.
    assert (setup_dest / ".percolate-identity.example").is_file()


def test_percolation_dotfiles_home_with_nothing_tracked_under_setup_is_not_foreign(tmp_path):
    """A directory merely sitting inside a foreign repo, with NOTHING
    tracked under it (a dotfiles-managed HOME), is correctly NOT foreign --
    a stale destination there gets force-overwritten like any other."""
    install_base = tmp_path / "install_base"
    setup_src = _make_src_tree(tmp_path, _SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)
    # install_base itself is a git repo (dotfiles-managed HOME), but nothing
    # under .claude/setup/ is tracked.
    install_base.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=install_base)
    setup_dest = install_base / ".claude" / "setup"
    setup_dest.mkdir(parents=True, exist_ok=True)
    stale = setup_dest / "publish_sync.py"
    stale.write_text("stale content\n", encoding="utf-8")

    bin_dst = install_base / "bin"
    _percolation_and_path_steps(
        setup_src, _SETUP_TEMPLATE_FILES, [], _SETUP_TEMPLATE_HOOK_FILES,
        str(install_base), bin_dst, False,
    )

    assert stale.read_text(encoding="utf-8") == "new template content\n"
    assert not (install_base / ".claude" / "setup-overwrite-backups").exists()


def test_ac7_guard_tracked_path_set_derives_from_manifest_module_not_hardcoded(tmp_path):
    """AC7: the guard's tracked-path set is derived from
    coordinator/lib/setup-templates-manifest.py's SETUP_TEMPLATE_FILES +
    SETUP_TEMPLATE_HOOK_FILES entries, not a hardcoded count. Load the real
    manifest module and assert `_percolation_and_path_steps` builds its
    `manifest_relative_paths`/careful-write allowlist from exactly those
    entries (7 today) -- a change to the manifest module changes the guard's
    membership automatically, with no second list to keep in sync."""
    import importlib.util

    manifest_path = (
        substrate.Path(__file__).resolve().parents[2]
        / "coordinator" / "lib" / "setup-templates-manifest.py"
    )
    spec = importlib.util.spec_from_file_location("_c6_test_manifest", manifest_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    combined = frozenset(module.SETUP_TEMPLATE_FILES) | frozenset(module.SETUP_TEMPLATE_HOOK_FILES)
    assert len(combined) == 7
    assert combined == frozenset(_SETUP_TEMPLATE_FILES + _SETUP_TEMPLATE_HOOK_FILES)

    # And that set is exactly what a foreign-tracked overwrite in
    # _percolation_and_path_steps accepts through the careful-write path --
    # any member is acceptable, nothing outside it is (already covered by
    # test_assert_careful_write_in_manifest_* above; this test pins the
    # SOURCE of that set to the manifest module, not a hand-copied literal).
    for rel in combined:
        _assert_careful_write_in_manifest(rel, combined)  # must not raise
