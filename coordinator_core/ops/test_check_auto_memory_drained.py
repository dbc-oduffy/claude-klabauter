"""
Tests for coordinator_core.ops.check_auto_memory_drained.

Spec backlink: DoE-claude
  docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md § C13, AC15.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.check_auto_memory_drained import (
    _own_index_rows,
    _own_memory_dirs,
    _own_residue,
    _slugify_repo_root,
    main,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(*extra_args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(list(extra_args))
    return exit_code, out.getvalue(), err.getvalue()


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_passthrough_kwargs())


def test_slugify_replaces_path_separators_with_dashes() -> None:
    assert _slugify_repo_root("/home/example/repos/claude-klabauter") == (
        "-home-example-repos-claude-klabauter"
    )
    assert _slugify_repo_root("/home/example/repos/DoE-claude") == (
        "-home-example-repos-DoE-claude"
    )


def test_slugify_normalizes_backslash_separators() -> None:
    win_style = "example-drive" + chr(92) + "Users" + chr(92) + "example" + chr(92) + "repo"
    assert _slugify_repo_root(win_style) == "example-drive-Users-example-repo"


def test_slugify_encodes_drive_letter_colon() -> None:
    """Real Windows paths carry a drive-letter colon (``X:\\claude-klabauter``),
    which Claude Code's own ``~/.claude/projects/<slug>/`` naming also
    encodes (verified on-disk: ``X--claude-klabauter``). A test path without a
    colon (like the sibling backslash-only case above) does not exercise
    this and previously let a separator-only encoding ship broken on every
    real Windows drive-letter root."""
    assert _slugify_repo_root("X:\\claude-klabauter") == "X--claude-klabauter"
    assert _slugify_repo_root("C:\\Users\\someone\\repo") == (
        "C--Users-someone-repo"
    )


def test_no_memory_dir_is_a_clean_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, out, err = _run("--root", str(repo))
    assert exit_code == 0
    assert out == ""
    assert err == ""


def test_empty_memory_dir_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, _ = _run("--root", str(repo))
    assert exit_code == 0


_SELF_SID = "self-session-id"
_PEER_SID = "peer-session-id"


def _body_text(origin_sid: str | None) -> str:
    if origin_sid is None:
        return "no frontmatter here\n"
    return (
        "---\n"
        "name: some-fact\n"
        "metadata:\n"
        f"  originSessionId: {origin_sid}\n"
        "---\n"
        "body\n"
    )


def test_index_row_owned_by_closer_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Some fact](some-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 1
    assert "MEMORY.md" in err


def test_index_rows_all_peer_owned_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "peer-fact.md").write_text(_body_text(_PEER_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Peer fact](peer-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0
    assert err == ""


def test_peer_owned_body_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body owned by a live peer session must never be reported as this
    closer's residue, let alone ordered deleted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "peer-fact.md").write_text(_body_text(_PEER_SID))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0
    assert err == ""


def test_own_body_blocks_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 1
    assert "some-fact.md" in err


def test_dangling_index_row_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Gone](missing-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0
    assert err == ""


def test_body_with_no_frontmatter_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "no-frontmatter.md").write_text(_body_text(None))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0
    assert err == ""


def test_body_with_metadata_but_no_origin_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "no-origin.md").write_text(
        "---\nname: some-fact\nmetadata:\n  type: feedback\n---\nbody\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 0
    assert err == ""


def test_home_union_checks_both_home_and_userprofile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Git-Bash HOME/USERPROFILE divergence must never leave a real
    memory dir unchecked -- residue under either resolved root blocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    slug = _slugify_repo_root(str(repo))
    (userprofile / ".claude" / "projects" / slug / "memory").mkdir(parents=True)
    (userprofile / ".claude" / "projects" / slug / "memory" / "stray.md").write_text(
        _body_text(_SELF_SID)
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo), "--session-id", _SELF_SID)
    assert exit_code == 1
    assert "stray.md" in err


def test_own_index_rows_returns_line_and_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID))
    line = "- [Some fact](some-fact.md) — hook"
    (memory_dir / "MEMORY.md").write_text(f"# Memory Index\n\n{line}\n")

    rows = _own_index_rows(memory_dir, _SELF_SID)
    assert len(rows) == 1
    assert rows[0][0] == line
    assert rows[0][1] == memory_dir / "some-fact.md"


def test_own_index_rows_excludes_dangling_row(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Gone](missing-fact.md) — hook\n"
    )
    assert _own_index_rows(memory_dir, _SELF_SID) == []


def test_own_index_rows_excludes_peer_row(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "peer-fact.md").write_text(_body_text(_PEER_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Peer fact](peer-fact.md) — hook\n"
    )
    assert _own_index_rows(memory_dir, _SELF_SID) == []


def test_own_index_rows_returns_empty_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("# Memory Index\n\n- [X](x.md)\n")

    real_read_text = Path.read_text

    def _boom(self: Path, *a, **kw):
        if self.name == "MEMORY.md":
            raise OSError("simulated read failure")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert _own_index_rows(memory_dir, _SELF_SID) == []


def test_own_index_rows_does_not_collapse_duplicate_rows(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Some fact](some-fact.md) — hook\n"
        "- [Some fact again](some-fact.md) — hook\n"
    )
    rows = _own_index_rows(memory_dir, _SELF_SID)
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1] == memory_dir / "some-fact.md"


def test_own_memory_dirs_unions_home_and_userprofile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    slug = _slugify_repo_root(str(repo))
    dirs = _own_memory_dirs(str(repo))
    expected = {
        home / ".claude" / "projects" / slug / "memory",
        userprofile / ".claude" / "projects" / slug / "memory",
    }
    assert expected.issubset(set(dirs))


def test_own_residue_aggregates_bodies_and_rows_across_home_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    slug = _slugify_repo_root(str(repo))
    home_memory = home / ".claude" / "projects" / slug / "memory"
    up_memory = userprofile / ".claude" / "projects" / slug / "memory"
    home_memory.mkdir(parents=True)
    up_memory.mkdir(parents=True)
    (home_memory / "home-fact.md").write_text(_body_text(_SELF_SID))
    (up_memory / "up-fact.md").write_text(_body_text(_SELF_SID))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    bodies, rows = _own_residue(str(repo), _SELF_SID)
    body_names = {p.name for p in bodies}
    assert body_names == {"home-fact.md", "up-fact.md"}
    assert rows == []


def test_index_has_own_row_unchanged_across_existing_case_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the invariant that makes C1's extraction safe: `_index_has_own_row`
    (now `bool(_own_index_rows(...))`) returns exactly what it did before the
    extraction, across the same own-row / peer-row / dangling-row cases the
    existing suite already covers -- this is the oracle, not a rewrite of it."""
    from coordinator_core.ops.check_auto_memory_drained import _index_has_own_row

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_SELF_SID))
    (memory_dir / "peer-fact.md").write_text(_body_text(_PEER_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Some fact](some-fact.md) — hook\n"
        "- [Peer fact](peer-fact.md) — hook\n"
        "- [Gone](missing-fact.md) — hook\n"
    )
    assert _index_has_own_row(memory_dir, _SELF_SID) is True
    assert _index_has_own_row(memory_dir, _PEER_SID) is True
    assert _index_has_own_row(memory_dir, "nobody") is False


def test_check_auto_memory_drained_exposes_no_write_path() -> None:
    """AC6: source-inspect for mutation calls, and both negative-spec lines
    are still present verbatim."""
    import inspect

    import coordinator_core.ops.check_auto_memory_drained as mod

    source = inspect.getsource(mod)
    for banned in (
        "os.remove(",
        "os.unlink(",
        "shutil.rmtree",
        ".write_text(",
        ".write_bytes(",
        "open(",
        "Path.unlink",
    ):
        assert banned not in source, f"unexpected mutation call: {banned}"
    assert (
        "Does NOT decide promote vs. drop for any entry" in source
    )
    assert (
        "Does NOT delete, truncate, or otherwise mutate any file. Read-only"
        in source
    )


def test_unresolvable_root_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code, out, err = _run()
    assert exit_code == 0


def test_unresolvable_identity_skips_and_reports_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolvable self-identity must fail CLOSED to zero residue and exit
    0 with a stderr advisory -- never fall through to the old
    everything-is-residue behaviour, which would order a deletion this
    gate cannot honestly attribute."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text(_body_text(_PEER_SID))
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Some fact](some-fact.md) — hook\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    exit_code, out, err = _run("--root", str(repo), "--session-id", "")
    assert exit_code == 0
    assert out == ""
    assert "identity" in err.lower()


def test_slugify_encodes_every_character_in_the_documented_scheme():
    r"""All four of ``:`` ``\`` ``/`` ``.`` encode to ``-``, not just separators.

    ``discover_working_repos._decode_projects_dir_name`` documents the scheme as
    ``: \ / . -> -``. This encoder shipped handling ``\`` and ``/`` only, was then
    fixed for ``:``, and STILL omitted ``.`` — so any root with a dotted segment
    (``C:\Users\me\.claude``, a repo named ``foo.bar``) computed a slug that could
    not match its real directory, leaving the drain gate blind for those roots.

    Checked against the live store rather than against belief:
    ``C:\Users\example-operator\.claude`` is ``C--Users-example-operator--claude`` on disk — the
    doubled hyphen IS the encoded dot.

    negative-spec: do not drop a character from this set to make some future case
    pass. It mirrors an external encoder; diverging silently re-blinds the gate
    instead of failing loudly.
    """
    assert _slugify_repo_root("C:\\Users\\me\\.claude") == "C--Users-me--claude"
    assert _slugify_repo_root("X:\\repo.name\\sub") == "X--repo-name-sub"
    assert _slugify_repo_root("/home/me/.config/repo") == "-home-me--config-repo"
