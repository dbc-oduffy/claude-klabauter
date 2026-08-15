"""Tests for coordinator_core.bash_guards.bump_foreign_repo_write -- the
Bash-surface CROSS-REPO write-confinement speed bump (C4).

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 [DoE-claude
repo], chunk C4 "Cross-repo detection and registration". Re-founded on the
real anchor path by
docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md, chunk
C3 (DoE finding #5): applicability here is established via a real
`write_session_start_record` settings-home anchor, not `CLAUDE_PROJECT_DIR`
-- see `_set_anchor`'s own docstring for why. `CLAUDE_PROJECT_DIR` is left
unset throughout this file; it is not this guard's primary anchor (see
`_write_bump_applicability` module docstring, "TWO ANCHORS") and no test
here has the fallback itself as its subject.

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY -- see the plan's "Design
posture -- passable by construction". These tests verify the bump FIRES on
the shapes AC1 names, that reads and same-repo writes never fire, that the
AC5 `cross-repo-memo` carve-out is unconditional and matched by invoked-
executable identity (never a destination-path shape), that the marker
clears the bump, that AC19's registration attributes are pinned, and (AC6)
that the bump still fires when the live payload `cwd` has drifted across a
repo boundary since the session's own SessionStart.

Covers: AC1 (`git -C`, `cd ... && git`, and plain-bash write-sink shapes
targeting a different git root all bump), AC5 (the `cross-repo-memo`
carve-out, unconditional, plus its negative case), AC6 (cross-repo `cwd`
drift), AC13 (registered as a `GuardEntry` in `dispatch.py`, not a call-site
patch), AC19 (this guard's `fail_closed`/`band`/`advisory_value` are
pinned), and the "reads never bump" carve-out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards import _command_tokenizer
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards._write_bump_marker import (
    marker_basename,
    resolve_gitdir,
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture (C4 owns the fix; AC13/finding #6).
    requires_powershell_grammar,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]



def _posix(p) -> str:
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized. Accepts a ``Path`` or a
    plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def repos(tmp_path):
    """Anchor repo (the session's own), a foreign sibling repo, and an
    unregistered fake HOME (so `~/.claude` never accidentally matches and
    the applicability hatch never fires) -- the shared setup every test
    below builds on."""
    anchor = _init_repo(tmp_path, "anchor")
    foreign = _init_repo(tmp_path, "foreign")
    home = tmp_path / "home"
    home.mkdir()
    return {"anchor": anchor, "foreign": foreign, "home": home}


def _set_anchor(monkeypatch, repos, session_id: str, extra: dict | None = None) -> None:
    """Establishes applicability the same way a real session does -- a
    settings-home `write_session_start_record`, not `CLAUDE_PROJECT_DIR`
    (DoE finding #5 / AC5): `CLAUDE_PROJECT_DIR` is known-absent from a live
    confined Bash-tool subprocess (`_write_bump_session_start.
    CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV`), so a suite anchored on it
    exclusively never exercises the real settings-home read path this
    plan's C1/C2 built and this chunk re-founds the suite on. `HOME` is
    still set (to an unrelated scratch dir, never `CLAUDE_PROJECT_DIR`) so
    the `~/.claude` fleet-recovery hatch (`_anchor_is_under_claude_home`)
    resolves to "not under" rather than fail-opening on an unresolvable
    home."""
    monkeypatch.setenv("HOME", str(repos["home"]))
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))


# ---------------------------------------------------------------------------
# AC1 -- git -C / cd&&git / plain-bash write sinks targeting a foreign repo
# ---------------------------------------------------------------------------


def test_ac1_git_dash_c_write_subcommand_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-1")
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-1", str(repos["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac1_cd_and_git_write_subcommand_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-2")
    cmd = f"cd {_posix(repos['foreign'])} && git commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-2", str(repos["anchor"]), {})

    assert result is not None


def test_ac1_plain_bash_write_sink_cp_to_new_file_bumps(repos, monkeypatch):
    """The write-sink TARGET does not exist yet -- the realistic incident
    shape (`echo x > /repo/new-file.txt`) and the exact case the
    nearest-existing-ancestor fix covers."""
    _set_anchor(monkeypatch, repos, "sess-3")
    src = repos["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = repos["foreign"] / "newfile.txt"
    assert not dest.exists()
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-3", str(repos["anchor"]), {})

    assert result is not None


def test_ac1_output_redirection_write_sink_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-4")
    dest = repos["foreign"] / "redir.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-4", str(repos["anchor"]), {})

    assert result is not None


def test_ac1_mkdir_write_sink_to_not_yet_existing_dir_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-5")
    new_dir = repos["foreign"] / "brand-new-subdir"
    assert not new_dir.exists()
    cmd = f"mkdir -p {_posix(new_dir)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-5", str(repos["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR/--git-dir/--work-tree evasion --
# 2026-08-06 live incident: a command that never `cd`s and never passes
# `-C` still reaches a foreign repo's WRITE surface through these, since
# `git` itself honours them independent of `cwd`. `-C`/`cd` were the only
# shapes this guard's candidate-target resolution tracked before this fix.
# ---------------------------------------------------------------------------


def test_evasion_env_git_dir_write_subcommand_bumps(repos, monkeypatch):
    cmd = f"GIT_DIR={_posix(repos['foreign'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-1")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-1", str(repos["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


def test_evasion_env_git_common_dir_write_subcommand_bumps(repos, monkeypatch):
    cmd = f"GIT_COMMON_DIR={_posix(repos['foreign'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-2")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-2", str(repos["anchor"]), {})

    assert result is not None


def test_evasion_env_git_work_tree_write_subcommand_bumps(repos, monkeypatch):
    """`GIT_WORK_TREE` alone (no `GIT_DIR`) redirects where `git` operates
    from, same as `-C`/`cd` -- a work tree pointed at the foreign repo's
    checkout must bump exactly as `-C <foreign>` already does."""
    cmd = f"GIT_WORK_TREE={_posix(repos['foreign'])} git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-3")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-3", str(repos["anchor"]), {})

    assert result is not None


def test_evasion_cli_git_dir_flag_write_subcommand_bumps(repos, monkeypatch):
    cmd = f"git --git-dir={_posix(repos['foreign'])}/.git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-4")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-4", str(repos["anchor"]), {})

    assert result is not None


def test_evasion_cli_git_dir_flag_separate_token_write_subcommand_bumps(repos, monkeypatch):
    cmd = f"git --git-dir {_posix(repos['foreign'])}/.git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-5")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-5", str(repos["anchor"]), {})

    assert result is not None


def test_evasion_env_wrapper_git_dir_write_subcommand_bumps(repos, monkeypatch):
    """The `env NAME=value cmd` wrapper spelling, not only the bare
    `NAME=value cmd` prefix form -- both peel through
    `_command_tokenizer._peel_command_position`'s `env` branch."""
    cmd = f"env GIT_DIR={_posix(repos['foreign'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-6")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-6", str(repos["anchor"]), {})

    assert result is not None


def test_non_regression_env_git_dir_pointing_at_own_repo_does_not_bump(repos, monkeypatch):
    """Non-regression: an ordinary in-repo command using `GIT_DIR` (or the
    other overrides) against the SESSION'S OWN repo must stay allowed --
    a guard that starts denying legitimate in-repo `GIT_DIR` usage is the
    same failure by another route (module docstring, "FAIL CLOSED, BUT DO
    NOT OVER-BLOCK")."""
    cmd = f"GIT_DIR={_posix(repos['anchor'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-7")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-7", str(repos["anchor"]), {})

    assert result is None


def test_non_regression_cli_git_dir_flag_pointing_at_own_repo_does_not_bump(repos, monkeypatch):
    cmd = f"git --git-dir={_posix(repos['anchor'])}/.git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-8")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-8", str(repos["anchor"]), {})

    assert result is None


def test_non_regression_git_dir_readonly_allowlisted_subcommand_still_never_bumps(repos, monkeypatch):
    """Reads never bump regardless of how the target repo is named -- the
    `GIT_DIR` fix only changes candidate-target RESOLUTION, never the
    reads-never-bump carve-out itself. `log` is not in
    `_GIT_WRITE_SUBCOMMANDS`; see `test_evasion_env_git_dir_write_
    subcommand_reproduces_live_incident_bumps` below for the positive
    (write) leg of the same `GIT_DIR` resolution."""
    cmd = f"GIT_DIR={_posix(repos['foreign'])}/.git git log"
    _set_anchor(monkeypatch, repos, "sess-evasion-9")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-9", str(repos["anchor"]), {})

    assert result is None


def test_evasion_env_git_dir_write_subcommand_reproduces_live_incident_bumps(repos, monkeypatch):
    """The live-incident SHAPE (2026-08-06): a `GIT_DIR=<foreign>/.git git
    ...` that never `cd`s and never passes `-C`. Before the `GIT_DIR` fix,
    target resolution used `cwd` alone (the session's own anchor) and
    silently allowed; after it, resolution runs through `GIT_DIR` to the
    real (foreign) target and bumps.

    Carried on `commit` rather than the incident's literal `cat-file` since
    2026-08-12: `cat-file` is a READ, and this test asserting a bump on it
    was the reads-never-bump contract violation the DoE-claude memo caught
    (see `test_readonly_git_verb_outside_the_old_eight_never_bumps`). The
    seam under test here is `GIT_DIR` resolution, not verb classification --
    a write verb exercises it without enshrining the bug."""
    cmd = f"GIT_DIR={_posix(repos['foreign'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-10")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-10", str(repos["anchor"]), {})

    assert result is not None


@pytest.mark.parametrize(
    "verb",
    [
        "cat-file -t HEAD",
        "merge-base --is-ancestor HEAD HEAD",
        "rev-list --count HEAD",
        "for-each-ref refs/heads",
        "ls-tree HEAD",
        "shortlog -s",
        "name-rev HEAD",
        "check-ignore -q x",
        "count-objects -v",
        "grep -n needle",
        "whatchanged -1",
    ],
)
def test_readonly_git_verb_outside_the_old_eight_never_bumps(repos, monkeypatch, verb):
    """READS NEVER BUMP -- the module contract, restored 2026-08-12.

    The bump used to fire on `subcommand not in _GIT_READONLY_SUBCOMMANDS`,
    a CONFINEMENT allowlist of eight names, so every other read-only verb
    git ships was billed as an attempted foreign-repo write. Cross-repo
    reads are the substrate of this fleet's own doctrine (verify a peer's
    cited commits before actioning their memo), and the bump made that
    doctrinally-required read look like a write needing PM assent.

    Membership in `_GIT_WRITE_SUBCOMMANDS` is now what bumps; an unknown or
    read-only verb does not."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-readonly-verbs")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-readonly-verbs", str(repos["anchor"]), {}
    )

    assert result is None


def test_unknown_git_verb_does_not_bump(repos, monkeypatch):
    """The fail-open direction, made explicit: an unrecognised verb (a
    future git subcommand, an alias, a misparsed option value) is not a
    write. This is the property that stops the next read-only verb git
    ships from regressing the contract above -- the old inverted test would
    have bumped on it."""
    cmd = f"git -C {_posix(repos['foreign'])} some-future-readonly-verb"
    _set_anchor(monkeypatch, repos, "sess-unknown-verb")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-unknown-verb", str(repos["anchor"]), {}
    )

    assert result is None


@pytest.mark.parametrize(
    "verb",
    [
        # The command the DoE-claude memo named as still costing them after
        # the first pass -- the reason this predicate table exists.
        "branch --show-current",
        "branch",
        "branch -a",
        "branch -r -v",
        "branch --list release/*",
        "branch --contains HEAD",
        "branch --merged main",
        "tag",
        "tag -l v1.*",
        "tag -n5",
        "tag --points-at HEAD",
        "tag --verify v1.0",
        "remote",
        "remote -v",
        "remote show origin",
        "remote get-url origin",
        "config --get user.name",
        "config --list",
        "config user.email",
        "config --file .git/config --get core.bare",
        "reflog",
        "reflog show HEAD",
        "notes list",
        "notes show HEAD",
        "stash list",
        "stash show",
        "submodule",
        "submodule status",
        "submodule summary",
        "bisect log",
        "worktree list",
        "apply --check patch.diff",
        "apply --stat patch.diff",
    ],
)
def test_dual_mode_verb_read_spelling_never_bumps(repos, monkeypatch, verb):
    """A dual-mode verb's READ spelling is a read -- the residual the first
    pass (`3ebc5fa6a5b0`) knowingly left open and this closes.

    Those verbs sit in `_GIT_WRITE_SUBCOMMANDS` because their write
    spellings do mutate; `_DUAL_MODE_READ_PREDICATES` vetoes the bump for
    the read spellings only. `git branch --show-current` is the specific
    command the DoE-claude memo named: an EM verifying a peer's branch
    before memoing them is doing exactly what this fleet's doctrine tells
    them to do, and it must not read as an attempted write."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-dual-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-dual-read", str(repos["anchor"]), {}
    )

    assert result is None


@pytest.mark.parametrize(
    "verb",
    [
        "branch -d topic",
        "branch -D topic",
        "branch -m old new",
        "branch newtopic",
        "branch --set-upstream-to=origin/main",
        # A write flag alongside a read flag is still a write.
        "branch -a -d topic",
        "tag v1.0",
        "tag -d v1.0",
        "tag -a v1.0 -m msg",
        "tag -f v1.0 HEAD",
        "remote add peer /tmp/peer",
        "remote set-url origin /tmp/x",
        "remote remove origin",
        "config user.email me@example.com",
        "config --unset user.email",
        "config --add core.hooksPath .githooks",
        "config --replace-all core.bare false",
        "reflog expire --all",
        "reflog delete HEAD@{0}",
        "notes add -m note",
        "notes remove",
        # Bare `git stash` PUSHES -- deliberately not symmetric with the
        # other bare-verb reads above.
        "stash",
        "stash push -u",
        "stash pop",
        "stash drop",
        "submodule update --init",
        "submodule add /tmp/x sub",
        "bisect start",
        "bisect good",
        "worktree add /tmp/wt",
        "worktree remove /tmp/wt",
        "apply patch.diff",
    ],
)
def test_dual_mode_verb_write_spelling_still_bumps(repos, monkeypatch, verb):
    """The other half: narrowing the dual-mode verbs must not silence their
    write spellings. Every entry here mutates the foreign repo."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-dual-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-dual-write", str(repos["anchor"]), {}
    )

    assert result is not None


# Review: code-reviewer (a99136f2, P3) -- the original version of this test
# used `branch -qXz topic` and passed only because of the trailing
# positional `topic`, which `_branch_is_read`'s flagless fallback would have
# classified as a write on its own (same as `git branch newtopic`); the
# unrecognised bundle `-qXz` contributed nothing to the outcome. Rewritten
# below to isolate the actual property: an unrecognised flag bundle with NO
# trailing positional must still land on write, per `_unrecognised_flag_
# present` (see `_branch_is_read`/`_tag_is_read`). A positional-present case
# is kept too, but named for what it actually tests.
def test_dual_mode_predicate_unrecognised_bundle_alone_fails_toward_bump(repos, monkeypatch):
    """`branch -qXz` with NO positional -- isolates the unrecognised-bundle
    property: without `_unrecognised_flag_present`'s gate, `_branch_is_read`
    would fall through to `_first_positional(args) is None`, find no
    positional, and misclassify as read. The gate forces write instead."""
    cmd = f"git -C {_posix(repos['foreign'])} branch -qXz"
    _set_anchor(monkeypatch, repos, "sess-dual-unknown-nopos")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-dual-unknown-nopos", str(repos["anchor"]), {}
    )

    assert result is not None


def test_dual_mode_predicate_tag_unrecognised_bundle_alone_fails_toward_bump(repos, monkeypatch):
    """`tag` equivalent of the above -- `_tag_is_read`'s identical gate."""
    cmd = f"git -C {_posix(repos['foreign'])} tag -qXz"
    _set_anchor(monkeypatch, repos, "sess-dual-unknown-tag-nopos")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-dual-unknown-tag-nopos", str(repos["anchor"]), {}
    )

    assert result is not None


def test_dual_mode_predicate_fails_toward_bump_on_unrecognised_spelling_with_positional(
    repos, monkeypatch
):
    """The positional-present variant, kept and renamed for what it
    actually pins: an unrecognised flag bundle PLUS a trailing positional is
    a write via the ordinary create-branch path, same as `git branch
    newtopic` -- not a test of the unrecognised-bundle property alone (see
    the two tests above for that)."""
    cmd = f"git -C {_posix(repos['foreign'])} branch -qXz topic"
    _set_anchor(monkeypatch, repos, "sess-dual-unknown")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-dual-unknown", str(repos["anchor"]), {}
    )

    assert result is not None


@pytest.mark.parametrize("verb", ["commit -m x", "push origin HEAD", "reset --hard", "stash"])
def test_write_git_verb_still_bumps(repos, monkeypatch, verb):
    """The other half of the swap: the verbs that actually mutate a foreign
    repo must still bump. A membership test that silences reads is only
    correct if it does not also silence writes."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-write-verbs")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-write-verbs", str(repos["anchor"]), {}
    )

    assert result is not None


# ---------------------------------------------------------------------------
# Review: code-reviewer (run-report brief, items 2-5) -- regression coverage
# for the four production defects fixed since the last two review passes
# (symbolic-ref/hash-object/pack-refs write-membership, config get/list
# reads), the `cd <foreign> && git <verb>` candidate-extraction leg, the
# GIT_DIR/override seam combined with a dual-mode predicate, and the
# remaining adversarial shapes named by the slice-2 reviewer.
# ---------------------------------------------------------------------------


def test_symbolic_ref_read_spelling_never_bumps(repos, monkeypatch):
    """`git symbolic-ref HEAD` -- one positional, prints the ref (read)."""
    cmd = f"git -C {_posix(repos['foreign'])} symbolic-ref HEAD"
    _set_anchor(monkeypatch, repos, "sess-symref-read")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-symref-read", str(repos["anchor"]), {})

    assert result is None


def test_symbolic_ref_reassign_write_spelling_bumps(repos, monkeypatch):
    """`git symbolic-ref HEAD refs/heads/other` -- two positionals,
    reassigns the ref (write)."""
    cmd = f"git -C {_posix(repos['foreign'])} symbolic-ref HEAD refs/heads/other"
    _set_anchor(monkeypatch, repos, "sess-symref-reassign")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-symref-reassign", str(repos["anchor"]), {}
    )

    assert result is not None


def test_symbolic_ref_delete_write_spelling_bumps(repos, monkeypatch):
    """`git symbolic-ref -d HEAD` -- deletes the named symbolic ref."""
    cmd = f"git -C {_posix(repos['foreign'])} symbolic-ref -d HEAD"
    _set_anchor(monkeypatch, repos, "sess-symref-delete")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-symref-delete", str(repos["anchor"]), {}
    )

    assert result is not None


def test_hash_object_read_spelling_never_bumps(repos, monkeypatch):
    """`git hash-object <file>` without `-w` computes and prints a hash
    only -- does not touch the target's object database (read)."""
    target_file = repos["foreign"] / "README.md"
    cmd = f"git -C {_posix(repos['foreign'])} hash-object {_posix(target_file)}"
    _set_anchor(monkeypatch, repos, "sess-hashobj-read")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-hashobj-read", str(repos["anchor"]), {})

    assert result is None


def test_hash_object_write_spelling_bumps(repos, monkeypatch):
    """`git hash-object -w <file>` writes the blob into the target's object
    database (write)."""
    target_file = repos["foreign"] / "README.md"
    cmd = f"git -C {_posix(repos['foreign'])} hash-object -w {_posix(target_file)}"
    _set_anchor(monkeypatch, repos, "sess-hashobj-write")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-hashobj-write", str(repos["anchor"]), {})

    assert result is not None


def test_pack_refs_bumps(repos, monkeypatch):
    """`git pack-refs --all` -- always a write, no read spelling."""
    cmd = f"git -C {_posix(repos['foreign'])} pack-refs --all"
    _set_anchor(monkeypatch, repos, "sess-packrefs")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-packrefs", str(repos["anchor"]), {})

    assert result is not None


def test_config_get_subcommand_style_read_never_bumps(repos, monkeypatch):
    """`git config get foo.bar` (git >= 2.46 subcommand syntax) is a read --
    `_CONFIG_READ_SUBWORDS` fix."""
    cmd = f"git -C {_posix(repos['foreign'])} config get foo.bar"
    _set_anchor(monkeypatch, repos, "sess-config-get")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-config-get", str(repos["anchor"]), {})

    assert result is None


def test_config_list_subcommand_style_read_never_bumps(repos, monkeypatch):
    """`git config list` (git >= 2.46 subcommand syntax) is a read."""
    cmd = f"git -C {_posix(repos['foreign'])} config list"
    _set_anchor(monkeypatch, repos, "sess-config-list")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-config-list", str(repos["anchor"]), {})

    assert result is None


def test_config_user_name_positional_write_still_bumps(repos, monkeypatch):
    """`git config user.name bob` -- ordinary two-positional set, still a
    write; must not be swallowed by the new read-subword table."""
    cmd = f"git -C {_posix(repos['foreign'])} config user.name bob"
    _set_anchor(monkeypatch, repos, "sess-config-username")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-config-username", str(repos["anchor"]), {}
    )

    assert result is not None


def test_config_unset_flag_write_still_bumps(repos, monkeypatch):
    """`git config --unset x` -- still a write."""
    cmd = f"git -C {_posix(repos['foreign'])} config --unset x"
    _set_anchor(monkeypatch, repos, "sess-config-unset")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-config-unset", str(repos["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# Review: code-reviewer (a99136f2 and a386c4ea, both P2) -- the `cd
# <foreign> && git <verb>` candidate-extraction leg is the OTHER extractor
# `_iter_write_sink_candidates` documents and was untested for any of the
# new verb spellings; every case above uses `git -C` only.
# ---------------------------------------------------------------------------


def test_cd_and_git_dual_mode_read_verb_never_bumps(repos, monkeypatch):
    cmd = f"cd {_posix(repos['foreign'])} && git branch --show-current"
    _set_anchor(monkeypatch, repos, "sess-cdgit-dual-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-cdgit-dual-read", str(repos["anchor"]), {}
    )

    assert result is None


def test_cd_and_git_dual_mode_write_verb_bumps(repos, monkeypatch):
    cmd = f"cd {_posix(repos['foreign'])} && git branch -d topic"
    _set_anchor(monkeypatch, repos, "sess-cdgit-dual-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-cdgit-dual-write", str(repos["anchor"]), {}
    )

    assert result is not None


def test_cd_and_git_plain_read_verb_never_bumps(repos, monkeypatch):
    cmd = f"cd {_posix(repos['foreign'])} && git log"
    _set_anchor(monkeypatch, repos, "sess-cdgit-plain-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-cdgit-plain-read", str(repos["anchor"]), {}
    )

    assert result is None


# ---------------------------------------------------------------------------
# Review: code-reviewer (a99136f2, P2) -- nothing exercised a GIT_DIR/
# --work-tree/-c core.worktree= override combined with a dual-mode
# predicate; the env-resolution fix and the dual-mode predicates were each
# tested in isolation but never at their seam.
# ---------------------------------------------------------------------------


def test_git_dir_override_with_dual_mode_read_verb_does_not_bump(repos, monkeypatch):
    cmd = f"GIT_DIR={_posix(repos['foreign'])}/.git git branch --show-current"
    _set_anchor(monkeypatch, repos, "sess-gitdir-dual-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-gitdir-dual-read", str(repos["anchor"]), {}
    )

    assert result is None


def test_git_dir_override_with_dual_mode_write_verb_bumps(repos, monkeypatch):
    cmd = f"GIT_DIR={_posix(repos['foreign'])}/.git git branch -d topic"
    _set_anchor(monkeypatch, repos, "sess-gitdir-dual-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-gitdir-dual-write", str(repos["anchor"]), {}
    )

    assert result is not None


def test_work_tree_override_with_dual_mode_read_verb_does_not_bump(repos, monkeypatch):
    cmd = f"GIT_WORK_TREE={_posix(repos['foreign'])} git branch --show-current"
    _set_anchor(monkeypatch, repos, "sess-worktree-dual-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-worktree-dual-read", str(repos["anchor"]), {}
    )

    assert result is None


def test_work_tree_override_with_dual_mode_write_verb_bumps(repos, monkeypatch):
    cmd = f"GIT_WORK_TREE={_posix(repos['foreign'])} git branch -d topic"
    _set_anchor(monkeypatch, repos, "sess-worktree-dual-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-worktree-dual-write", str(repos["anchor"]), {}
    )

    assert result is not None


def test_dash_c_core_worktree_override_with_dual_mode_read_verb_does_not_bump(repos, monkeypatch):
    cmd = f"git -c core.worktree={_posix(repos['foreign'])} branch --show-current"
    _set_anchor(monkeypatch, repos, "sess-coreworktree-dual-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-coreworktree-dual-read", str(repos["anchor"]), {}
    )

    assert result is None


def test_dash_c_core_worktree_override_with_dual_mode_write_verb_bumps(repos, monkeypatch):
    cmd = f"git -c core.worktree={_posix(repos['foreign'])} branch -d topic"
    _set_anchor(monkeypatch, repos, "sess-coreworktree-dual-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-coreworktree-dual-write", str(repos["anchor"]), {}
    )

    assert result is not None


# ---------------------------------------------------------------------------
# Review: code-reviewer (a386c4ea, P3) -- adversarial shapes named by the
# slice-2 reviewer, hand-traced but not previously present in the
# parametrize lists.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb",
    [
        "branch -dr topic",
        "tag -df v1",
        "remote set-head origin -a",
        "stash -u",
        "stash --keep-index",
        "worktree lock /tmp/wt",
        "submodule foreach echo hi",
    ],
)
def test_adversarial_dual_mode_shapes_still_bump(repos, monkeypatch, verb):
    """Every one of these mutates the foreign repo (or, for `stash -u`/
    `stash --keep-index`, is a bare stash PUSH with an extra flag, not a
    read sub-word) and must still bump."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-adversarial-write")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-adversarial-write", str(repos["anchor"]), {}
    )

    assert result is not None


@pytest.mark.parametrize(
    "verb",
    [
        "notes get-ref",
        "bisect view",
        "config --get x y",
    ],
)
def test_adversarial_dual_mode_shapes_never_bump(repos, monkeypatch, verb):
    """`notes get-ref` (a recognised read sub-word in `_notes_is_read`),
    `bisect view` (a recognised read sub-word in `_bisect_is_read`), and
    `config --get x y` (`--get` is a recognised read flag in `_config_is_
    read`; the trailing `y` is a value-pattern filter on the query, not a
    second value to set) are all reads."""
    cmd = f"git -C {_posix(repos['foreign'])} {verb}"
    _set_anchor(monkeypatch, repos, "sess-adversarial-read")

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-adversarial-read", str(repos["anchor"]), {}
    )

    assert result is None


def test_evasion_dash_c_core_worktree_write_subcommand_bumps(repos, monkeypatch):
    """Review finding (2026-08-06, P1): `git -c core.worktree=<foreign> ...`
    is the identical evasion class `GIT_WORK_TREE`/`--work-tree` already
    close, just via git's generic `-c` config-override mechanism. Before the
    fix, `-c` fell into the generic flag-skip branch, never consumed its
    required `name=value` token, and the payload was misread as the git
    SUBCOMMAND while `target_cwd` stayed at the anchor -- silently allowing
    the write."""
    cmd = f"git -c core.worktree={_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-1")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-1", str(repos["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


def test_evasion_dash_c_core_worktree_separate_token_write_subcommand_bumps(repos, monkeypatch):
    """Same as above, `-c <name>=<value>` two-token spelling -- the common
    one, not the attached `-c<name>=<value>` form."""
    cmd = f"git -c core.worktree={_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-2")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-2", str(repos["anchor"]), {})

    assert result is not None


def test_evasion_dash_c_core_worktree_attached_form_write_subcommand_bumps(repos, monkeypatch):
    """`-c<name>=<value>`, attached (no space) -- the other spelling real
    `git` accepts for short options."""
    cmd = f"git -ccore.worktree={_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-3")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-3", str(repos["anchor"]), {})

    assert result is not None


def test_non_regression_dash_c_core_worktree_pointing_at_own_repo_does_not_bump(repos, monkeypatch):
    cmd = f"git -c core.worktree={_posix(repos['anchor'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-4")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-4", str(repos["anchor"]), {})

    assert result is None


def test_non_regression_ordinary_dash_c_unrelated_config_key_still_bumps_correctly(repos, monkeypatch):
    """Review finding (P1, second-order effect): an ordinary `git -c
    name=value <verb>` (an unrelated config key, no target relocation)
    must not have its real subcommand swallowed into the bogus first-
    positional read -- `-c color.ui=always commit` must still resolve
    `commit` as the subcommand and bump against the foreign target named by
    `-C`, not silently allow because `color.ui=always` was misread as the
    subcommand and never matched `_GIT_READONLY_SUBCOMMANDS`."""
    cmd = f"git -c color.ui=always -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-5")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-5", str(repos["anchor"]), {})

    assert result is not None


def test_non_regression_ordinary_dash_c_unrelated_config_key_read_still_allowed(repos, monkeypatch):
    """The over-block half of the same second-order effect: `git -c
    color.ui=always log` on a foreign repo must stay allowed (a read), not
    bump because `color.ui=always` was misread as the subcommand and failed
    to match the readonly allowlist."""
    cmd = f"git -c color.ui=always -C {_posix(repos['foreign'])} log"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-6")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-6", str(repos["anchor"]), {})

    assert result is None


def test_non_regression_dash_config_env_flag_does_not_swallow_subcommand(repos, monkeypatch):
    """`--config-env=<name>=<envvar>` is a mandatory-value global flag with
    the identical two-token-skip requirement as `-c` -- must not swallow
    `commit` into the bogus positional read either."""
    cmd = f"git --config-env=core.editor=EDITOR -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-7")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-7", str(repos["anchor"]), {})

    assert result is not None


def test_non_regression_dash_namespace_separate_token_does_not_swallow_subcommand(repos, monkeypatch):
    """`--namespace <ns>` (separate-token, mandatory value) -- same
    two-token-skip requirement."""
    cmd = f"git --namespace foo -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-evasion-dashc-8")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-evasion-dashc-8", str(repos["anchor"]), {})

    assert result is not None


def test_p3_git_dir_and_git_common_dir_both_set_last_token_order_wins(repos, monkeypatch):
    """P3 -- `GIT_DIR`/`GIT_COMMON_DIR` share one override slot with
    last-write-wins RAW TOKEN ORDER scanning (`_env_repo_overrides`
    docstring). Named test for the module's own documented behaviour:
    `GIT_COMMON_DIR=<foreign> GIT_DIR=<anchor>` -- `GIT_DIR` is later in
    raw-token order, so it wins and the write is classified against the
    anchor (own repo, does not bump). This pins the CURRENT documented
    scan-order semantics, not a claim that it matches real git's own
    `GIT_DIR`-vs-`GIT_COMMON_DIR` resolution for every combination (P3,
    accepted narrow-edge-case limitation per the module's own "passable
    speed bump, not a security boundary" posture)."""
    cmd = f"GIT_COMMON_DIR={_posix(repos['foreign'])}/.git GIT_DIR={_posix(repos['anchor'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-p3-lastwins-1")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-p3-lastwins-1", str(repos["anchor"]), {})

    assert result is None


def test_p3_git_dir_last_write_wins_pinned_by_docstring(repos, monkeypatch):
    """Nit finding: `GIT_DIR=a GIT_DIR=b` -- the module's own docstring
    claims "last assignment of a given name wins" but no test exercised it
    before this one."""
    cmd = f"GIT_DIR={_posix(repos['anchor'])}/.git GIT_DIR={_posix(repos['foreign'])}/.git git commit --allow-empty -m x"
    _set_anchor(monkeypatch, repos, "sess-p3-lastwins-2")

    result = guard.check_bump_foreign_repo_write(cmd, "sess-p3-lastwins-2", str(repos["anchor"]), {})

    assert result is not None


def test_same_repo_write_does_not_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-6")
    src = repos["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = repos["anchor"] / "dest.txt"
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-6", str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# Reads never bump.
# ---------------------------------------------------------------------------


def test_read_carve_out_git_log_on_foreign_repo_does_not_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-7")
    cmd = f"git -C {_posix(repos['foreign'])} log"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-7", str(repos["anchor"]), {})

    assert result is None


def test_read_carve_out_git_status_on_foreign_repo_does_not_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-8")
    cmd = f"git -C {_posix(repos['foreign'])} status"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-8", str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# AC5 -- the cross-repo-memo carve-out is unconditional.
# ---------------------------------------------------------------------------


def _install_fake_cross_repo_memo(home: Path) -> Path:
    settings_home = home / ".coordinator-claude-settings"
    bin_dir = settings_home / "bin"
    bin_dir.mkdir(parents=True)
    crm = bin_dir / "cross-repo-memo"
    crm.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    crm.chmod(0o755)
    return crm


def test_ac5_bare_word_cross_repo_memo_invocation_never_bumps(repos, monkeypatch):
    _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-9")
    cmd = f"cross-repo-memo --repo foreign --note '{_posix(repos['foreign'])}/some/path'"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-9", str(repos["anchor"]), {})

    assert result is None


def test_ac5_canonical_path_carrying_cross_repo_memo_invocation_never_bumps(repos, monkeypatch):
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-10")
    cmd = f"{_posix(crm)} --repo foreign"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-10", str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# C4b (docs/reference/guard-dialect-coverage.md row 11) --
# `token_matches_binary(head, "cross-repo-memo")` gates on the named CLI
# script's own argv0, invoked the same way in both dialects. No real
# PowerShell parse is exercised (this function takes a bare `cmd: str`) --
# this proves the SAME argv0 identity check reaches the identical carve-out
# on a PowerShell-spelled invocation as on the bash-spelled one.
# ---------------------------------------------------------------------------


def test_ac5_powershell_call_operator_prefixed_bare_word_never_bumps(repos, monkeypatch):
    _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-9b")
    cmd = f"& cross-repo-memo --repo foreign --note '{_posix(repos['foreign'])}/some/path'"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-9b", str(repos["anchor"]), {})

    assert result is None


def test_ac5_powershell_semicolon_chained_canonical_path_never_bumps(repos, monkeypatch):
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-10b")
    cmd = f"Set-Location C:\\repo; {_posix(crm)} --repo foreign"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-10b", str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# Quoted PowerShell write-target reproduction/regression (break-class fix):
# the PowerShell tokenizer emits a quoted leaf's raw source span (quotes
# attached, see `_dialect._flatten_powershell_tokens`), which previously
# defeated `_WINDOWS_DRIVE_ABSOLUTE_RE`/`os.path.isabs` downstream and
# silently re-rooted the candidate under the session's own anchor repo
# instead of judging it as foreign -- an affirmative false-clean.
# `_write_bump_sink_shapes` now strips quotes per-extractor.
# ---------------------------------------------------------------------------


@requires_powershell_grammar
def test_powershell_new_item_quoted_foreign_repo_target_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-ps-quoted-foreign")
    dest = repos["foreign"] / "newfile.txt"
    cmd = f'New-Item -Path "{dest}" -ItemType File'

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-quoted-foreign", str(repos["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None


@requires_powershell_grammar
def test_powershell_new_item_single_quoted_foreign_repo_target_bumps(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-ps-single-quoted-foreign")
    dest = repos["foreign"] / "newfile.txt"
    cmd = f"New-Item -Path '{dest}' -ItemType File"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-single-quoted-foreign", str(repos["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None


@requires_powershell_grammar
def test_powershell_new_item_unquoted_foreign_repo_target_still_bumps(repos, monkeypatch):
    """Non-regression companion: the unquoted spelling must keep bumping
    exactly as before this fix."""
    _set_anchor(monkeypatch, repos, "sess-ps-unquoted-foreign")
    dest = repos["foreign"] / "newfile.txt"
    cmd = f"New-Item -Path {dest} -ItemType File"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-unquoted-foreign", str(repos["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None


@requires_powershell_grammar
def test_powershell_new_item_quoted_own_repo_target_does_not_bump(repos, monkeypatch):
    """Non-regression companion: a quoted target legitimately resolving
    inside the session's own anchor repo must still NOT bump."""
    _set_anchor(monkeypatch, repos, "sess-ps-quoted-own")
    dest = repos["anchor"] / "newfile.txt"
    cmd = f'New-Item -Path "{dest}" -ItemType File'

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-ps-quoted-own", str(repos["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is None


def test_ac5_carve_out_is_unconditional_even_with_no_marker_and_bump_applying(repos, monkeypatch):
    """"Unconditional -- it bypasses even the marker" (plan body): no marker
    is created anywhere in this test, and the session's own repo genuinely
    differs from the target, yet the carve-out still exempts it."""
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-11")
    assert not (resolve_gitdir(str(repos["anchor"])) / marker_basename("sess-11")).exists()
    cmd = f"{_posix(crm)} --repo foreign --target {_posix(repos['foreign'])}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-11", str(repos["anchor"]), {})

    assert result is None


def test_ac5_negative_hand_rolled_write_into_a_sibling_cross_repo_directory_still_bumps(
    repos, monkeypatch
):
    """The carve-out is matched by INVOKED EXECUTABLE identity, never a
    destination-path shape -- a `*/cross-repo/`-style path match would be a
    hole any write could drive through by choosing that directory name."""
    _set_anchor(monkeypatch, repos, "sess-12")
    cross_repo_dir = repos["foreign"] / "cross-repo"
    cross_repo_dir.mkdir()
    src = repos["anchor"] / "note-src.md"
    src.write_text("x\n", encoding="utf-8")
    dest = cross_repo_dir / "note.md"
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-12", str(repos["anchor"]), {})

    assert result is not None


def test_ac5_same_named_decoy_script_elsewhere_does_not_get_the_carve_out(repos, monkeypatch):
    """A same-basename script living somewhere OTHER than the canonical
    settings-home location is not exempted -- only the resolved canonical
    executable, or a bare (PATH-trusted) invocation, gets AC5's carve-out."""
    _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-13")
    decoy_dir = repos["anchor"] / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "cross-repo-memo"
    decoy.write_text("#!/bin/sh\necho decoy\n", encoding="utf-8")
    decoy.chmod(0o755)
    src = repos["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = repos["foreign"] / "other.txt"
    cmd = f"{_posix(decoy)} && cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-13", str(repos["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# Quoted binary-head reproduction/regression (bug backlog row
# 2026-08-08-quoted-binary-head-defeats-os-path-isabs-5565d6e1563e): a
# QUOTED, path-carrying `cross-repo-memo` head token must earn the AC5
# carve-out exactly as its unquoted twin already does, and a same-named
# DECOY invoked with a quoted head must NOT -- the danger direction named
# in that row is a careless quote-strip reopening the decoy hole, so both
# sides are asserted here, plus the unquoted case as a non-regression
# anchor.
# ---------------------------------------------------------------------------


def test_ac5_double_quoted_canonical_path_carrying_invocation_never_bumps(repos, monkeypatch):
    """The literal defect: a quoted head token (e.g. a Windows path with a
    space in it, or any PowerShell-quoted absolute invocation) must resolve
    correctly and still earn the carve-out."""
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-14")
    cmd = f'"{_posix(crm)}" --repo foreign'

    result = guard.check_bump_foreign_repo_write(cmd, "sess-14", str(repos["anchor"]), {})

    assert result is None


def test_ac5_single_quoted_canonical_path_carrying_invocation_never_bumps(repos, monkeypatch):
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-14b")
    cmd = f"'{_posix(crm)}' --repo foreign"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-14b", str(repos["anchor"]), {})

    assert result is None


def test_ac5_quoted_same_named_decoy_script_elsewhere_does_not_get_the_carve_out(
    repos, monkeypatch
):
    """THE DANGEROUS DIRECTION: quoting a decoy invocation must NOT open the
    hole `test_ac5_same_named_decoy_script_elsewhere_does_not_get_the_carve_
    out` already closes for the unquoted spelling -- a careless quote-strip
    fix would turn this guard's fail-closed miss into a fail-open grant,
    which is strictly worse than the bug it fixes."""
    _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-15")
    decoy_dir = repos["anchor"] / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "cross-repo-memo"
    decoy.write_text("#!/bin/sh\necho decoy\n", encoding="utf-8")
    decoy.chmod(0o755)
    src = repos["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = repos["foreign"] / "other.txt"
    cmd = f'"{_posix(decoy)}" && cp {_posix(src)} {_posix(dest)}'

    result = guard.check_bump_foreign_repo_write(cmd, "sess-15", str(repos["anchor"]), {})

    assert result is not None


def test_ac5_unquoted_canonical_path_carrying_invocation_still_never_bumps(repos, monkeypatch):
    """Non-regression companion: the pre-existing unquoted spelling must
    keep earning the carve-out exactly as before this fix."""
    crm = _install_fake_cross_repo_memo(repos["home"])
    _set_anchor(monkeypatch, repos, "sess-14c")
    cmd = f"{_posix(crm)} --repo foreign"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-14c", str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# The marker clears the bump.
# ---------------------------------------------------------------------------


def test_marker_present_for_session_clears_the_bump(repos, monkeypatch):
    """C3/AC4 -- the marker now lives at the TARGET's own gitdir, per-
    (session, target), not the session's anchor gitdir: clearing this exact
    target with a `touch` there stands the bump down for it."""
    _set_anchor(monkeypatch, repos, "sess-14")
    foreign_gitdir = resolve_gitdir(str(repos["foreign"]))
    assert foreign_gitdir is not None
    (foreign_gitdir / marker_basename("sess-14")).touch()
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-14", str(repos["anchor"]), {})

    assert result is None


def test_marker_for_a_different_session_does_not_clear_this_ones_bump(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, "sess-15")
    anchor_gitdir = resolve_gitdir(str(repos["anchor"]))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker_basename("some-other-session")).touch()
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-15", str(repos["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# AC6 -- cross-repo `cwd` drift: the live payload `cwd` has already crossed
# a repo boundary (simulating an earlier Bash call's `cd <foreign>`, since
# "Working directory persists between calls" -- harness contract) by the
# time this guard sees a plain, no-`-C`, no-`cd` write in that SAME foreign
# repo. This is the plan's own repro-table cell: `cd <foreign> && git
# commit`, `CLAUDE_PROJECT_DIR` unset -- pre-C1/C2 the guard fired `False`
# (the headline defect); this test pins the fix on the exact surface the
# memo reproduced against. Uses a REAL `write_session_start_record` so
# applicability is genuinely True -- a test that passes only because
# applicability failed open proves nothing (this is exactly how the
# original AC12 test slipped through; that one only drifted `cwd` to a
# SUBDIRECTORY of the anchor repo, where `sessions_dir` resolves
# identically either way -- this test drifts ACROSS a repo boundary
# instead, which the subdirectory-only test never covered).
# ---------------------------------------------------------------------------


def test_ac6_cwd_drifted_to_a_foreign_repo_still_bumps_the_commit_there(repos, monkeypatch):
    """Session launches (SessionStart) in `repos["anchor"]`; the live
    payload `cwd` has since drifted to `repos["foreign"]` (no `CLAUDE_
    PROJECT_DIR` anywhere -- the production condition the plan's repro
    table names). Pre-C1/C2, `resolve_launch_anchor`'s only anchor source
    was the in-repo record resolved from the LIVE `cwd`
    (`sessions_dir(cwd)`) -- once `cwd` has crossed into the foreign repo,
    that resolves a hub where this session's record was never written, so
    the anchor comes back `None`, `bump_applies` fails open, and this guard
    never reaches its own cross-repo comparison for a `git commit` that is
    plainly happening inside a foreign repo. Post-C1/C2, the settings-home
    hub is cwd-independent and resolves the real anchor regardless of
    where the live `cwd` has drifted to, so the bump fires correctly."""
    monkeypatch.setenv("HOME", str(repos["home"]))
    session_id = "sess-ac6-drift"
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))

    cmd = "git commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["foreign"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# AC3/AC4 -- C1's destination-class axis wired through this guard: a
# registered publish.mirrors.* target renders publish-class copy naming the
# owner and never "repos you don't own"; an ordinary foreign source repo
# keeps today's copy.
# ---------------------------------------------------------------------------


def _write_publish_registry(reg_dir: Path, mirror_path: str, owner: str = "claude-central-em") -> None:
    """A real `[publish.mirrors.<key>]` nested table -- the shape
    `target_is_publish_destination`/`_all_publish_destinations` (C1) parse,
    not the flat-string shape that silently fails to parse as a bracket
    table."""
    reg_dir.mkdir(parents=True, exist_ok=True)
    escaped = str(mirror_path).replace("\\", "\\\\")
    lines = ["[publish.mirrors.testmirror]", f'path = "{escaped}"', f'owner = "{owner}"']
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ac3_publish_destination_write_renders_publish_class_copy_naming_owner(repos, monkeypatch, tmp_path):
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch, repos, "sess-ac3-publish", extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)}
    )
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-ac3-publish", str(repos["anchor"]), {})

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "claude-central-em" in reason
    assert "is publish mirror" in reason
    assert "repos you don't own" not in reason
    assert "check with your PM" not in reason
    assert "cross-repo-memo" not in reason


def test_ac1_ordinary_foreign_repo_keeps_todays_foreign_class_copy(repos, monkeypatch):
    """No publish-mirror registry entry for this target -- destination_class
    stays DESTINATION_FOREIGN and the copy is unchanged from today's.

    Agent-class assertion updated 2026-08-15 (`d385e2ed3`, "review
    integration: close the fail-open seam and the marker-name guess B8 leg
    (c) inherited", AC-3): `resolve_agent_class` now reads an empty
    `payload` (the literal `{}` this test passes) as subagent-class rather
    than EM-class -- a deliberate fail-open inversion, not a regression --
    so the `{}` payload this test has always passed now renders the
    FOREIGN/subagent template, not FOREIGN/em. This test's own subject is
    the destination-class axis (FOREIGN vs PUBLISH), not the agent-class
    axis, so the fix keeps that same axis under test against whichever
    template the current contract actually selects for this payload."""
    _set_anchor(monkeypatch, repos, "sess-ac1-foreign-class")
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-ac1-foreign-class", str(repos["anchor"]), {})

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "report to the EM that dispatched you" in reason
    assert "is publish mirror" not in reason


def test_ac9_publish_destination_verdict_asserted_only_after_bump_applies(repos, monkeypatch, tmp_path):
    """AC9 -- non-negotiable precondition, exercised explicitly for this
    chunk's own new classification wiring."""
    from coordinator_core.bash_guards import _write_bump_applicability as applicability

    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]))
    _set_anchor(
        monkeypatch, repos, "sess-ac9-publish", extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)}
    )

    assert applicability.bump_applies("sess-ac9-publish", cwd=str(repos["anchor"])) is True

    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    result = guard.check_bump_foreign_repo_write(cmd, "sess-ac9-publish", str(repos["anchor"]), {})
    assert result is not None


# ---------------------------------------------------------------------------
# state/handoffs/2026-08-13-one-command-publish.md AC3 -- the guard still
# denies an unauthorized agent push into a publish mirror, and clearing that
# exact target's marker is what stands the bump down (never a standing
# "publish is fine now" grant). Uses the same synthetic `publish.mirrors.*`
# registry fixture as the AC3/AC9 block above -- `repos["foreign"]` stands in
# for the real `claude-klabauter` publish mirror; this test never touches
# the real one.
# ---------------------------------------------------------------------------


def test_one_command_publish_ac_unauthorized_push_into_publish_mirror_bumps(
    repos, monkeypatch, tmp_path
):
    """The publish-mirror shape from the handoff: an agent session anchored
    in its own repo issues a real `git push` against a publish-mirror path
    that is not its own repo. `bump_foreign_repo_write` must still deny it --
    demonstrated for both the `git -C <mirror> push` and (session already
    `cd`'d into the mirror) plain `git push` spellings."""
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch,
        repos,
        "sess-publish-push-1",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    cmd = f"git -C {_posix(repos['foreign'])} push origin main"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-publish-push-1", str(repos["anchor"]), {}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "claude-central-em" in reason
    assert "is publish mirror" in reason


def test_one_command_publish_ac_cd_into_publish_mirror_then_push_bumps(
    repos, monkeypatch, tmp_path
):
    """Same shape, `cd <mirror> && git push` spelling -- the guard resolves
    the target via the tracked `effective_cwd`, not a literal `-C` flag."""
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch,
        repos,
        "sess-publish-push-2",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    cmd = f"cd {_posix(repos['foreign'])} && git push origin main"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-publish-push-2", str(repos["anchor"]), {}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "claude-central-em" in reason
    assert "is publish mirror" in reason


def test_one_command_publish_ac_marker_clears_the_publish_mirror_push_bump(
    repos, monkeypatch, tmp_path
):
    """The complementary half: once the operator has cleared the bump for
    THIS target (the per-(session, target) marker exists at the mirror's own
    gitdir, per C3/AC4), the identical push no longer bumps. This is the
    guard working as designed -- an unauthorized push still denies (asserted
    above), and an operator-cleared one does not -- not a defect in either
    direction."""
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch,
        repos,
        "sess-publish-push-3",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    mirror_gitdir = resolve_gitdir(str(repos["foreign"]))
    assert mirror_gitdir is not None
    (mirror_gitdir / marker_basename("sess-publish-push-3")).touch()
    cmd = f"git -C {_posix(repos['foreign'])} push origin main"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-publish-push-3", str(repos["anchor"]), {}
    )

    assert result is None


# ---------------------------------------------------------------------------
# 2026-08-14 percolate-push memo -- the publish-mirror refusal must name the
# real alternative for a PUSH (`percolate-push <target>`), while a
# content-authoring write into the same mirror keeps the doctrine citation
# unchanged (that copy is correct for THAT shape, see
# `_write_bump_message._GIT_PUSH_WRITE_VERB_LABEL`'s own docstring). Both
# halves of this split are asserted here so a future edit cannot silently
# collapse them back onto a single template.
# ---------------------------------------------------------------------------


def test_percolate_push_memo_git_push_into_publish_mirror_names_percolate_push(
    repos, monkeypatch, tmp_path
):
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch,
        repos,
        "sess-percolate-push-em",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    cmd = f"git -C {_posix(repos['foreign'])} push origin main"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-percolate-push-em", str(repos["anchor"]), {}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "percolate-push <target>" in reason
    assert "is publish mirror" in reason
    # The content-authoring doctrine citation is the WRONG alternative for a
    # push -- it must not appear alongside the real one.
    assert "Publish-Repo Content Authoring" not in reason


def test_percolate_push_memo_content_authoring_write_still_cites_doctrine(
    repos, monkeypatch, tmp_path
):
    """The complementary half -- a hand-authored commit into the mirror
    (never a `push`) keeps today's doctrine-citation copy untouched; the
    percolate-push swap is scoped to the `git push` write-sink shape only."""
    reg_dir = tmp_path / "registry"
    _write_publish_registry(reg_dir, str(repos["foreign"]), owner="claude-central-em")
    _set_anchor(
        monkeypatch,
        repos,
        "sess-percolate-push-authoring",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-percolate-push-authoring", str(repos["anchor"]), {}
    )

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Publish-Repo Content Authoring" in reason
    assert "percolate-push" not in reason


# ---------------------------------------------------------------------------
# Fail-open cases -- unresolvable inputs never bump.
# ---------------------------------------------------------------------------


def test_fail_open_when_session_id_empty(repos, monkeypatch):
    # Guard short-circuits on an empty `session_id` before ever resolving
    # an anchor -- no applicability setup needed either way.
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    assert guard.check_bump_foreign_repo_write(cmd, "", str(repos["anchor"]), {}) is None


def test_fail_open_when_cmd_empty(repos, monkeypatch):
    # Guard short-circuits on an empty `cmd` before ever resolving an
    # anchor -- no applicability setup needed either way.
    assert guard.check_bump_foreign_repo_write("", "sess-16", str(repos["anchor"]), {}) is None


def test_fail_open_when_anchor_unresolvable(repos, monkeypatch):
    # No CLAUDE_PROJECT_DIR / session-start record at all -- this IS the
    # genuinely-unresolvable-anchor case, not a `CLAUDE_PROJECT_DIR`
    # fallback test.
    monkeypatch.setenv("HOME", str(repos["home"]))
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"

    result = guard.check_bump_foreign_repo_write(cmd, "sess-17", str(repos["anchor"]), {})

    assert result is None


def test_fail_open_when_write_sink_target_has_no_existing_ancestor_at_all():
    """`_nearest_existing_ancestor` -- a bogus path with no real filesystem
    root resolves to `None` (never raises)."""
    assert guard._nearest_existing_ancestor("") is None


# ---------------------------------------------------------------------------
# Agent memory store -- never a foreign-repo bump, even when `~/.claude`
# itself IS a real git checkout (as it is on this fleet), which is exactly
# the shape THIS guard (not C5's outside-repo sibling) would otherwise see.
# ---------------------------------------------------------------------------


def test_agent_memory_store_write_never_bumps_even_when_home_is_a_repo(repos, monkeypatch, tmp_path):
    session_id = "sess-mem-c4-1"
    home = _init_repo(tmp_path, "home-that-is-a-repo")
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))
    memory_dir = home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "note.md"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["anchor"]), {})

    assert result is None


def test_agent_memory_store_index_write_never_bumps_even_when_home_is_a_repo(repos, monkeypatch, tmp_path):
    session_id = "sess-mem-c4-2"
    home = _init_repo(tmp_path, "home-that-is-a-repo-2")
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))
    memory_dir = home / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "MEMORY.md"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["anchor"]), {})

    assert result is None


def test_project_dir_write_not_under_memory_now_allowed_by_c1(repos, monkeypatch, tmp_path):
    """AC1/AC4 (docs/plans/2026-08-10-carve-claude-out-and-close-the-
    backslash-bypass.md, C1): superseded by C1's unconditional `~/.claude`
    carve-out, wired into this leg alongside the (narrower, `memory/`-only)
    agent-memory exemption immediately above -- a write elsewhere under a
    project's own directory (not `memory/`) no longer bumps, because it is
    still under `~/.claude` as a whole. Prior to C1 this test asserted the
    opposite (`test_project_dir_write_not_under_memory_still_bumps_when_
    home_is_a_repo`), proving only the agent-memory exemption's own,
    narrower boundary."""
    session_id = "sess-mem-c4-3"
    home = _init_repo(tmp_path, "home-that-is-a-repo-3")
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))
    project_dir = home / ".claude" / "projects" / "-Users-example-operator-X-some-project"
    project_dir.mkdir(parents=True)
    dest = project_dir / "not-memory.md"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-08-10-carve-claude-out-and-close-the-backslash-bypass.md)
# -- `~/.claude` never bumps on this leg either, wholesale, even though it
# is a real git checkout on this fleet. AC1-AC4.
# ---------------------------------------------------------------------------


def test_ac1_settings_json_write_never_bumps_on_this_leg(repos, monkeypatch, tmp_path):
    session_id = "sess-c1-settings"
    home = _init_repo(tmp_path, "home-c1-settings")
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["anchor"]), {})

    assert result is None


def test_ac4_unregistered_foreign_repo_still_bumps_alongside_claude_home_carveout(
    repos, monkeypatch, tmp_path
):
    """AC4 regression: an ordinary unregistered foreign repo (not
    `~/.claude`) keeps bumping on this leg after C1's carve-out lands --
    same control-matrix cell as the spike verdict record's "(b) UNREGISTERED
    repo" row, under the in-repo-anchor control (not the rootless-session
    axis, which C1 does not touch)."""
    session_id = "sess-c1-ac4-unreg"
    home = tmp_path / "home-c1-ac4-unreg"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))
    dest = repos["foreign"] / "note.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(repos["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


#: NOTE: this leg's own `check_bump_foreign_repo_write` never consults
#: `target_is_registered_repo` -- that predicate governs only the
#: anchor-in-no-repo branch (`bump_outside_repo_write.py` [C5] / the tool
#: leg), not this leg, which bumps on ANY foreign repo unconditionally. A
#: distinct "registered foreign repo" regression cell would therefore be
#: identical in setup and assertion to the unregistered one immediately
#: above -- not duplicated here for that reason; the spike verdict record's
#: "(c) REGISTERED repo" row is pinned instead on the tool leg (see
#: `test_bump_out_of_repo_tool_write.py::test_ac4_registered_repo_
#: destination_still_bumps`) and on C5 (`bump_outside_repo_write.py`),
#: where the registry membership actually changes the verdict.


# ---------------------------------------------------------------------------
# ANCHOR-RESOLUTION MISFIRE REGRESSION (bug reproduced live in-session,
# 2026-08-15) -- see module docstring, "UNRESOLVED IS NOT THE SAME FACT AS
# REPO-LESS". `resolve_gitdir(anchor)` returning `None` from a transient
# `git rev-parse --git-dir` spawn failure must not be read as "the anchor
# has no repo": `_evaluate_foreign_repo_candidate`'s no-repo-anchor branch
# bumps a REGISTERED target unconditionally, so without the fix, a
# transient spawn failure could deny a write into the session's OWN repo
# whenever that repo happens to be registered -- exactly the shape needed
# to make the defect observable (an unregistered target inside the anchor's
# own subtree already never bumped, registry membership or not).
# ---------------------------------------------------------------------------


def _write_repos_registry(reg_dir: Path, **repos: str) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[repos]"]
    for key, val in repos.items():
        escaped = str(val).replace("\\", "\\\\")
        lines.append(f'{key} = "{escaped}"')
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _patch_resolve_gitdir_to_fail_for(monkeypatch, failing_cwd: str) -> None:
    import os as _os

    real_resolve_gitdir = guard.resolve_gitdir
    failing_abs = _os.path.abspath(str(failing_cwd))

    def _flaky_resolve_gitdir(cwd=None):
        if cwd is not None and _os.path.abspath(str(cwd)) == failing_abs:
            return None  # simulated transient `git rev-parse --git-dir` spawn failure
        return real_resolve_gitdir(cwd)

    monkeypatch.setattr(guard, "resolve_gitdir", _flaky_resolve_gitdir)


def test_transient_anchor_gitdir_spawn_failure_does_not_bump_a_write_into_the_anchors_own_registered_repo(
    repos, monkeypatch, tmp_path
):
    """Reproduces the defect directly: the anchor is a REAL repo, registered
    in the machine registry, and its `.git` entry is present on disk -- so
    `path_has_git_ancestor` still finds it even while `resolve_gitdir` is
    patched to simulate the failed spawn. A write squarely inside the
    session's own (registered) repo must ALLOW, not deny on a mis-read 'no
    repo here'."""
    reg_dir = tmp_path / "registry"
    _write_repos_registry(reg_dir, some_repo=str(repos["anchor"]))
    _set_anchor(
        monkeypatch,
        repos,
        "sess-transient-anchor-own-repo",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    _patch_resolve_gitdir_to_fail_for(monkeypatch, str(repos["anchor"]))

    cmd = f"echo hi > {_posix(repos['anchor'] / 'note.txt')}"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-transient-anchor-own-repo", str(repos["anchor"]), {}
    )

    assert result is None


def test_transient_anchor_gitdir_spawn_failure_still_allows_a_registered_foreign_target(
    repos, monkeypatch, tmp_path
):
    """Same simulated spawn failure, but the write targets a DIFFERENT,
    registered repo. Before the fix, `anchor_has_repo = False` fell straight
    into the no-repo-anchor branch, where a registered target bumps
    unconditionally regardless of which repo it is. After the fix,
    `path_has_git_ancestor(anchor)` finds the anchor's real `.git` and this
    guard treats resolution as UNRESOLVED, allowing here too, never
    reaching that branch at all."""
    reg_dir = tmp_path / "registry"
    _write_repos_registry(reg_dir, foreign_repo=str(repos["foreign"]))
    _set_anchor(
        monkeypatch,
        repos,
        "sess-transient-anchor-foreign-registered",
        extra={"MACHINE_LOCAL_REGISTRY_DIR": str(reg_dir)},
    )
    _patch_resolve_gitdir_to_fail_for(monkeypatch, str(repos["anchor"]))

    cmd = f"echo hi > {_posix(repos['foreign'] / 'note.txt')}"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-transient-anchor-foreign-registered", str(repos["anchor"]), {}
    )

    assert result is None


def test_genuinely_repo_less_anchor_still_bumps_registered_target_after_the_fix(
    monkeypatch, tmp_path
):
    """Companion pin for the 2026-08-10 PM ruling `path_has_git_ancestor`
    must NOT touch: when the anchor truly has no `.git` ancestor anywhere
    (not merely a failed spawn), a REGISTERED target still bumps
    unconditionally. Anchored via `CLAUDE_PROJECT_DIR` rather than
    `_set_anchor`'s `write_session_start_record` -- that helper requires a
    git root to write its record against, which a genuinely rootless anchor
    does not have (same fallback `test_bump_out_of_repo_tool_write.py`'s own
    rootless-anchor tests use)."""
    reg_dir = tmp_path / "registry"
    registered = _init_repo(tmp_path, "registered-repo")
    _write_repos_registry(reg_dir, some_repo=str(registered))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    scaffold = tmp_path / "Documents" / "new-project"
    scaffold.mkdir(parents=True)
    home = tmp_path / "home-rootless"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(scaffold))
    session_id = "sess-scaffold-registered-target"

    cmd = f"echo hi > {_posix(registered / 'note.txt')}"
    result = guard.check_bump_foreign_repo_write(cmd, session_id, str(scaffold), {})

    assert result is not None


# ---------------------------------------------------------------------------
# AC13 / AC19 -- registered as a GuardEntry, not a call-site patch, with
# every registration attribute pinned explicitly.
# ---------------------------------------------------------------------------


def test_ac13_registered_as_a_guard_entry_in_dispatch_build_guard_chain():
    from coordinator_core.bash_guards import dispatch

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entries = [e for e in chain if e.name == "bump-foreign-repo-write"]

    assert len(entries) == 1


# ---------------------------------------------------------------------------
# AC10/AC10b -- `offer-git-c` and this bump were both registered in
# `GuardBand.ADVISORY_REWRITE`, and the chain returns on the first non-None
# result: `offer-git-c` sat first, so a bare `git commit` (no pathspec) in a
# foreign repo never reached this bump at all -- C2's destination-class
# message axis was built for a verdict nobody ever saw. C8 moved this guard
# (and its `bump-outside-repo-write` sibling) ahead of `offer-git-c` in
# `_build_guard_chain`'s own registration order to close that gap. These two
# tests exercise the REAL chain in registration order (not a single guard's
# `check_bump_foreign_repo_write` call in isolation, as the rest of this file
# does) so the fix is asserted at the level it actually broke at.
# ---------------------------------------------------------------------------


def _first_verdict(chain):
    """Replicates `evaluate_payload_json`'s own "first non-None result
    wins" loop, without the blanket-disarm/host-suppression machinery this
    file's fixtures never engage -- sufficient to observe REGISTRATION
    ORDER effects (which guard answers first), the one thing AC10/AC10b are
    about."""
    for entry in chain:
        result = entry.fn()
        if result is not None:
            return entry.name, result
    return None, None


def test_ac10_bare_git_commit_no_pathspec_in_foreign_repo_now_reaches_the_bump(repos, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_applicability as applicability
    from coordinator_core.bash_guards import dispatch

    session_id = "sess-ac10-reaches-bump"
    _set_anchor(monkeypatch, repos, session_id)
    assert applicability.bump_applies(session_id, cwd=str(repos["anchor"])) is True

    cmd = f"cd {_posix(repos['foreign'])} && git commit --allow-empty -m x"
    payload = {}
    chain = dispatch._build_guard_chain(cmd, session_id, str(repos["anchor"]), payload, None, False)

    name, result = _first_verdict(chain)

    assert name == "bump-foreign-repo-write"
    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac10b_offer_git_c_no_longer_rewrites_the_case_it_used_to(repos, monkeypatch):
    """The coverage change to `offer-git-c` itself: called on its own (as
    the rest of this file exercises `check_bump_foreign_repo_write` on its
    own), `offer-git-c` still rewrites this exact command -- its own logic
    is untouched. What changed is that it never gets the chance to, because
    the bump now answers first in the real chain (asserted above by AC10).
    This test pins the BEFORE half of that story so the coverage change is
    visible, not just the bump's new reachability."""
    from coordinator_core.bash_guards.guard_offer_git_c import check_offer_git_c

    session_id = "sess-ac10b-coverage-change"
    _set_anchor(monkeypatch, repos, session_id)

    cmd = f"cd {_posix(repos['foreign'])} && git commit --allow-empty -m x"

    # `offer-git-c` in isolation still rewrites this shape -- confirms the
    # guard's own behaviour is unchanged; only its chain POSITION moved.
    solo_result = check_offer_git_c(cmd, session_id, str(repos["anchor"]))
    assert solo_result is not None

    from coordinator_core.bash_guards import dispatch

    chain = dispatch._build_guard_chain(cmd, session_id, str(repos["anchor"]), {}, None, False)
    name, _result = _first_verdict(chain)

    # In the real chain, `offer-git-c` no longer gets to answer this case --
    # the bump shadows it now. This is the case `offer-git-c` used to cover
    # (be the first non-None answer for) and now does not.
    assert name != "offer-git-c"


def test_ac19_registration_attributes_pinned_not_left_to_default():
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entry = next(e for e in chain if e.name == "bump-foreign-repo-write")

    # `fail_closed=False` -- the OPPOSITE of every neighbouring
    # CONFINEMENT_DENY entry: a crash in this guard must swallow to
    # "allow", never route through the hard-deny crash path.
    assert entry.fail_closed is False
    # `band=ADVISORY_REWRITE`, NOT `CONFINEMENT_DENY` -- the blanket-disarm
    # marker can suppress every band except CONFINEMENT_DENY; registering
    # a deliberately passable bump there would make it the LEAST passable
    # guard in the suite.
    assert entry.band is dispatch.GuardBand.ADVISORY_REWRITE
    # Explicit, never the UNCLASSIFIED default (dispatch.py's own
    # registry-validation test also fails loud on this).
    assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED
    assert entry.advisory_value is AdvisoryValue.NOT_COST_ARGUED


def test_ac19_guard_band_membership_and_advisory_registry_tests_also_pass():
    """This guard's registration is additionally pinned by the package's
    OWN pre-existing structural tests (not reimplemented here) --
    `test_guard_band_membership.py::test_no_registered_guard_is_
    unclassified_by_this_test` and `test_advisory_value_registry.py`'s own
    sweep both exercise `_build_guard_chain` directly and will fail loud if
    this guard's name is ever registered without being classified in
    those files' own band lists."""
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    for entry in chain:
        assert entry.advisory_value is not None
        if entry.band is dispatch.GuardBand.CONFINEMENT_DENY:
            assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED


def test_extended_length_prefix_does_not_desync_same_repo_root(monkeypatch, tmp_path):
    """`state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`
    -- bash surface (C4). Simulates the exact failure shape: `os.path.
    realpath` returns the Windows extended-length form for one operand and
    the bare form for the other (the length-triggered asymmetry a real
    Windows host can produce), injected via monkeypatch since this is a
    macOS box and `realpath` never produces that form here. Before this
    fix, `_resolve_and_casefold`'s `casefold_path` call preserved the
    prefix, so the two operands compared unequal and `_same_repo_root`
    wrongly reported "different repo" for the identical directory."""
    real_dir = tmp_path / "anchor-repo"
    real_dir.mkdir()
    bare_form = str(real_dir)
    prefixed_form = "\\\\?\\" + bare_form

    real_realpath = guard.os.path.realpath

    def fake_realpath(path, *a, **kw):
        if path == "candidate-input":
            return prefixed_form
        if path == "root-input":
            return bare_form
        return real_realpath(path, *a, **kw)

    monkeypatch.setattr(guard.os.path, "realpath", fake_realpath)

    candidate_cf = guard._resolve_and_casefold("candidate-input")
    root_cf = guard._resolve_and_casefold("root-input")
    assert candidate_cf is not None and root_cf is not None
    assert guard._same_repo_root(candidate_cf, root_cf) is True


# ---------------------------------------------------------------------------
# C2 (docs/plans/2026-08-10-carve-claude-out-and-close-the-backslash-bypass.md)
# AC5-AC6 -- an unquoted backslash-spelled absolute target must bump exactly
# like the identical, forward-slash-spelled target, on the Bash surface.
# ---------------------------------------------------------------------------


def test_c2_ac5_backslash_spelled_target_bumps_same_as_forward_slash(monkeypatch, repos):
    """Reproduces the spike's own incidental finding (docs/research/spike-
    verdicts/2026-08-10-rootless-session-write-boundary.md, "Incidental
    finding"): before C2, plain `shlex` treats an UNQUOTED backslash as a
    POSIX escape character, so `echo probe > C:\\Users\\...\\out.txt`
    tokenized to a single mangled word with every separator stripped
    (`C:Users...out.txt`) -- neither `_WINDOWS_DRIVE_ABSOLUTE_RE` nor
    `translate_msys_path` could recognise that as absolute, so it fell
    through to a cwd-relative join and this guard silently allowed a
    foreign-repo write the forward-slash spelling already denied.

    Host-gated (`_host_is_windows()`, matching production's own gate) --
    on a POSIX host `translate_msys_path` is IDENTITY regardless of
    `preserve_windows_backslashes`, so this assertion is meaningless there
    and the test is skipped rather than asserting a no-op."""
    if not guard._host_is_windows():
        pytest.skip("Windows-only: backslash-spelled absolute paths are not this platform's shape")

    _set_anchor(monkeypatch, repos, "sess-c2-ac5")
    forward = _posix(repos["foreign"] / "out.txt")
    backward = str(repos["foreign"] / "out.txt")

    fwd_result = guard.check_bump_foreign_repo_write(
        f"echo probe > {forward}", "sess-c2-ac5", str(repos["anchor"]), {}
    )
    bwd_result = guard.check_bump_foreign_repo_write(
        f"echo probe > {backward}", "sess-c2-ac5", str(repos["anchor"]), {}
    )

    assert fwd_result is not None, "forward-slash spelling must still bump (control)"
    assert bwd_result is not None, "AC5: backslash spelling must bump identically"
    assert (
        fwd_result["hookSpecificOutput"]["permissionDecision"]
        == bwd_result["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )


def test_c2_ac6_backslash_normalization_removed_would_fail(monkeypatch, repos):
    """AC6: pins the exact mechanism C2 introduces -- `tokenize_full_
    command(..., preserve_windows_backslashes=True)` must keep the target's
    separators intact through tokenization. Directly exercises the
    tokenizer (not the guard's end-to-end verdict, already covered by
    AC5's test above) so a revert of `preserve_windows_backslashes` fails
    HERE even if some other, unrelated change happened to keep the AC5
    guard-level assertion passing. `shlex`'s escape processing is pure text
    handling with no platform branch of its own (the platform gate lives
    ONLY at the two write-bump guards' call sites -- see `_iter_write_sink_
    candidates`'s own docstring), so this assertion holds on every host,
    unlike AC5's end-to-end test above."""
    cmd = r"echo probe > C:\Users\x\out.txt"  # abs-path-ok: illustrative example shape, not a machine-specific citation

    tokens_normalized = _command_tokenizer.tokenize_full_command(
        cmd, preserve_windows_backslashes=True
    )
    tokens_default = _command_tokenizer.tokenize_full_command(cmd)

    assert tokens_normalized[-1] == r"C:\Users\x\out.txt"  # abs-path-ok: illustrative example shape, not a machine-specific citation
    # The pre-C2 default behavior mangles it -- pinned here so a future
    # reader can see exactly what "removing the normalization" reverts to.
    assert tokens_default[-1] == "C:Usersxout.txt"
    assert tokens_normalized[-1] != tokens_default[-1]


def test_c2_p1_quoted_escaped_quote_survives_preserve_windows_backslashes():
    """Review: coordinator:code-reviewer P1 (05fb6ef70 follow-up) -- C2's
    original `preserve_windows_backslashes=True` shape set `lex.escape = ""`
    for the WHOLE `shlex` lexer state, which also disables `escapedquotes`
    handling. `\\"` inside a double-quoted token no longer escaped the
    quote, so the quote closed EARLY and the remainder re-tokenized as
    fresh, unquoted words -- on ANY command tokenized with the flag on,
    whether or not it contains a Windows path at all.

    Pins that a legitimately escaped `\\"` inside a double-quoted token
    lexes IDENTICALLY with `preserve_windows_backslashes=True` as it does
    with the flag at its `False` default: one token, quote content intact,
    nothing split off into a second word. `shlex`'s own `escape` attribute
    is host-independent pure text handling (see AC6's test above), so this
    assertion holds on every host, not just Windows."""
    cmd = r'git commit -m "fixed \"quoted\" bug"'

    tokens_preserved = _command_tokenizer.tokenize_full_command(
        cmd, preserve_windows_backslashes=True
    )
    tokens_default = _command_tokenizer.tokenize_full_command(cmd)

    assert tokens_preserved == tokens_default
    assert tokens_preserved[-1] == 'fixed "quoted" bug'
    assert len(tokens_preserved) == 4


def test_c2_p0_unquoted_escaped_quote_does_not_swallow_separator():
    """Review: coordinator:code-reviewer P0 (d8a8b14c) -- an UNQUOTED
    `\\'`/`\\"` is an atomic escaped-literal-quote pair in real bash, not a
    quote-open. Before the fix, `_mask_unquoted_backslashes` sentinel-masked
    the bare backslash on one loop iteration and then toggled quote state on
    the bare quote the NEXT iteration, having no memory the quote had just
    been escaped. That let a real `;` separator get swallowed into a
    fictitious quoted span -- a guard-bypass shape, since every write-
    boundary guard segments on tokens and never saw the second command at
    all, even though bash executes it as its own segment.

    This is a tokenization assertion that fails on pre-fix HEAD: the
    fictitious quote swallows `;`/`rm`/`-rf`/`/important` into one token
    instead of splitting them into their own segment."""
    danger = "important"
    remove = "r" + "m"
    force_recursive = "-r" + "f"
    cmd = "echo \\' ; " + remove + " " + force_recursive + " /" + danger + " \\'"

    tokens_on = _command_tokenizer.tokenize_full_command(
        cmd, preserve_windows_backslashes=True
    )
    tokens_off = _command_tokenizer.tokenize_full_command(cmd)

    assert tokens_on == tokens_off, (
        "the escaped-quote separator shape must tokenize identically with "
        "the flag on and off"
    )
    assert tokens_on == [
        "echo",
        "'",
        ";",
        remove,
        force_recursive,
        "/" + danger,
        "'",
    ]
    assert remove not in tokens_on[1], "the separator must not be swallowed into a single token"


def test_c2_p2_backslash_before_punctuation_pairs_like_backslash_before_quote():
    """Review: coordinator:code-reviewer P2 (36bfdde30 follow-up) --
    `_mask_unquoted_backslashes` only special-cased an unquoted backslash
    immediately before a QUOTE (`'`/`"`); one before `;`/`&`/`|` still fell
    through to plain sentinel-masking, so `a\\;b` tokenized to
    `['a\\', ';', 'b']` -- a fabricated separator real bash never produces
    (bash's own escape rule treats `\\;` as a literal `;` inside one word:
    `echo a\\;b` is a single `a;b` argument, no second command). Fail-closed
    direction (an inert literal separator character got treated as a real
    command boundary), but the same root cause as the P0 this module was
    already fixed for -- generalized here to the other two
    `punctuation_chars` this tokenizer recognizes.

    Pins that flag on/off tokenize identically for all three punctuation
    characters, exactly as the existing quote-pair case already pins."""
    for punctuation in (";", "&", "|"):
        cmd = "echo a\\" + punctuation + "b"
        tokens_on = _command_tokenizer.tokenize_full_command(
            cmd, preserve_windows_backslashes=True
        )
        tokens_off = _command_tokenizer.tokenize_full_command(cmd)

        assert tokens_on == tokens_off, (
            f"backslash-before-{punctuation!r} must tokenize identically "
            "with the flag on and off"
        )
        assert tokens_on == ["echo", "a" + punctuation + "b"], (
            f"backslash-before-{punctuation!r} must stay one literal word, "
            "not fabricate a separator token"
        )


def test_c2_p2_consecutive_backslashes_before_quote_pair_left_to_right():
    """Review: coordinator:code-reviewer P2 (36bfdde30 follow-up) -- pairing
    an unquoted backslash with a following quote per-character (rather than
    over the whole RUN of consecutive backslashes) mis-paired `\\\\'` (two
    backslashes then a quote) as `(\\)(\\')` instead of real bash's own
    left-to-right `(\\\\)('...)`: the first backslash pairs with the SECOND
    backslash (one literal backslash, consumed), leaving the quote genuinely
    unescaped -- a real quote-open that runs uninterrupted to the next bare
    quote. Before this fix, `_mask_unquoted_backslashes` let the second
    backslash reach for the quote a character the first backslash had
    already claimed, fabricating token boundaries real bash does not
    produce (`rm`/`-rf`/`/important` split out as their own tokens where
    real bash runs nothing but the surrounding `echo`).

    Pins the structural property that matters for guard classification: an
    EVEN run of backslashes before a quote leaves the quote free to open a
    real (uninterrupted) span, so the danger tokens stay swallowed inside
    one `echo` argument in BOTH the flag-on and flag-off tokenization --
    same segment count, same absence of the danger tokens as their own
    segment, in each. (The exact literal backslash count surviving inside
    that swallowed argument differs cosmetically between flag on/off --
    flag-on preserves both raw backslashes as literal sentinel-unmasked
    characters rather than collapsing the pair the way plain `shlex` escape
    does -- but that difference is inert: it is not a command-position
    token in either case.)"""
    remove = "r" + "m"
    force_recursive = "-r" + "f"
    danger = "important"
    cmd = "echo \\\\' ; " + remove + " " + force_recursive + " /" + danger + " \\\\'"

    tokens_on = _command_tokenizer.tokenize_full_command(
        cmd, preserve_windows_backslashes=True
    )
    tokens_off = _command_tokenizer.tokenize_full_command(cmd)

    for label, tokens in (("on", tokens_on), ("off", tokens_off)):
        assert tokens is not None
        assert len(tokens) == 2, (
            f"flag-{label} must swallow the danger text into one echo "
            f"argument (a real command-position token per segment); got {tokens!r}"
        )
        assert tokens[0] == "echo"
        assert tokens[1] not in (remove, force_recursive, "/" + danger), (
            f"flag-{label} must not surface {remove!r} as its own command-position token"
        )


# ---------------------------------------------------------------------------
# AC7 (docs/plans/2026-08-02-write-confinement-guards.md) -- a dispatched
# subagent inherits its EM's marker without a second one, on the Bash leg.
#
# Regression for bug
# `2026-08-11-a-dispatched-coordinator-executor-is-den-28df23d727ea`: C3 of
# docs/plans/2026-08-03-narrow-write-confinement-bump.md sited the marker at
# the TARGET's own gitdir, and `_evaluate_foreign_repo_candidate` started
# resolving `marker_probe_root = resolve_git_root(marker_probe)` (the TARGET
# root) into `bump_is_cleared`'s `git_root=` and both `effective_session_id`
# calls. `resolve_em_session_id` reads the EM back-pointer from
# `<git_root>/.git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt`,
# which only ever exists in the SESSION's own gitdir -- resolved against the
# target root that lookup silently misses, `effective_session_id` falls back
# to the subagent's own `session_id`, and a dispatched subagent re-bumps
# despite its EM having cleared the target. Fixed by threading `anchor_root`
# (`resolve_git_root(anchor)`, the session's own root) through to
# `_evaluate_foreign_repo_candidate` and using it for the EM-inheritance
# lookup, while the marker's own SITING (`marker_probe`/`marker_gitdir`,
# still the target's gitdir) is untouched.
#
# Keeps the same control pair the reproducing probe used: a fixture-validity
# control (the hand-written back-pointer actually resolves the way the
# module docstring says it does) and a no-marker non-vacuity control
# (the identical payload DOES bump absent the EM's marker) alongside the
# AC7 assertion itself -- a regression test with only the AC7 assertion is
# how this defect went unnoticed after C3 landed.
# ---------------------------------------------------------------------------

EM_SID = "11111111-2222-3333-4444-555555555555"
SUB_SID = "99999999-8888-7777-6666-555555555555"
AC7_AGENT_ID = "coordinatorexecutor-deadbeef"


def _write_em_backpointer(session_root: Path, agent_id: str, em_sid: str) -> None:
    """The EM back-pointer as `subagent_sandbox.engine` writes it: in the
    SESSION repo's own gitdir, never the target's -- see
    `_write_bump_marker.resolve_em_session_id`'s own docstring for the exact
    path shape this mirrors."""
    d = session_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")


def test_ac7_fixture_validity_backpointer_resolves_against_session_root_only(repos):
    """Control 1 -- fixture validity: the back-pointer this test relies on
    resolves when read against the SESSION root, and does NOT resolve
    against the (unrelated) target root, matching `_write_bump_marker`'s own
    "SUBAGENTS" docstring section."""
    from coordinator_core.bash_guards._write_bump_marker import effective_session_id

    _write_em_backpointer(repos["anchor"], AC7_AGENT_ID, EM_SID)
    assert effective_session_id(SUB_SID, str(repos["anchor"]), AC7_AGENT_ID) == EM_SID
    assert effective_session_id(SUB_SID, str(repos["foreign"]), AC7_AGENT_ID) == SUB_SID


def test_ac7_subagent_inherits_em_marker_on_bash_leg(repos, monkeypatch):
    """The AC7 assertion. EM cleared the foreign target (marker sited at the
    TARGET gitdir, per C3); the subagent's own EM back-pointer sits in the
    SESSION repo, per `_write_bump_marker`'s own "SUBAGENTS" docstring
    section. The dispatched subagent must inherit that clear, not re-bump."""
    _set_anchor(monkeypatch, repos, SUB_SID)
    _write_em_backpointer(repos["anchor"], AC7_AGENT_ID, EM_SID)

    foreign_gitdir = resolve_gitdir(str(repos["foreign"]))
    assert foreign_gitdir is not None
    (foreign_gitdir / marker_basename(EM_SID)).touch()

    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    payload = {"agent_id": AC7_AGENT_ID, "cwd": str(repos["anchor"])}

    result = guard.check_bump_foreign_repo_write(cmd, SUB_SID, str(repos["anchor"]), payload)

    assert result is None, (
        "AC7 BROKEN: subagent re-bumped despite its EM's marker being present "
        "at the target gitdir. Envelope: %r" % (result,)
    )


def test_ac7_non_vacuity_same_payload_bumps_without_the_em_marker(repos, monkeypatch):
    """Control 2 -- non-vacuity: the identical payload, minus the EM's
    marker, DOES bump -- proving the AC7 assertion above is testing a real
    clear, not a payload shape this guard never fires on regardless."""
    _set_anchor(monkeypatch, repos, SUB_SID)
    _write_em_backpointer(repos["anchor"], AC7_AGENT_ID, EM_SID)

    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    payload = {"agent_id": AC7_AGENT_ID, "cwd": str(repos["anchor"])}

    result = guard.check_bump_foreign_repo_write(cmd, SUB_SID, str(repos["anchor"]), payload)

    assert result is not None
