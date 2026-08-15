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
    _slugify_repo_root,
    main,
)

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
    subprocess.run(["git", "init", "-q", str(root)], check=True)


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
