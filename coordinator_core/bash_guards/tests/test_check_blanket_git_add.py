"""Tests for ``coordinator_core.bash_guards.dispatch_checks.check_blanket_git_add``
-- the untested guard whose doctrine-cited name has drifted from the real
symbol.

Doctrine (``docs/wiki/coordinator-tripwires.md`` § BLOCK-BLANKET-GIT-ADD)
cites ``coordinator_core.bash_guards.block_blanket_git_add`` as the
enforcement point. The real symbol is
``coordinator_core.bash_guards.dispatch_checks.check_blanket_git_add`` -- a
module-level function in the ``dispatch_checks`` cohort (see that module's
docstring: 11 checks folded from DoE's retired
``preuse-bash-dispatch.sh``, each following the bash predecessors'
``check_<name>(cmd, session_id[, cwd])`` sourceable-function contract, with
no ``block_*``-named discovery module of its own). This file does NOT "fix"
that drift -- it pins the guard's REAL behaviour by assertion, so the name
mismatch is visible to any future reader who greps doctrine and finds no
matching symbol.

Core scope claim under test (RE-SCOPED 2026-07-31, correcting an over-broad
same-day widening, ``2d6bca4e``): this guard is NOT a blanket every-repo
guard, and it is NOT the original narrow ~/.claude-only cwd-guard either --
it is a HAZARD-DISCRIMINATED guard. It fires (a) inside the ~/.claude
meta-repo (the origin incident's own repo -- unchanged from the original
scope), and (b) inside any repo the machine-local fleet registry
(``repos.*``, see ``coordinator_core.machine_resolver``) knows about
(claude-klabauter, DoE-claude, every other sibling repo this machine tracks
-- the concurrent-multi-session cross-contamination hazard that motivated
the guard in the first place). It stays INERT in a repo that is neither
the meta-repo nor a registered fleet sibling -- the OSS-consumer-install
case, whose absence from this test file's predecessor was itself the
defect a code review caught: the all-repo widening had NO test asserting
the guard stays a no-op somewhere, so the regression shipped without a
single red test.

Seam design: ``dispatch_checks._run_git`` is monkeypatched (as before) to
avoid a real subprocess/git spawn. ``dispatch_checks._is_hazard_repo`` --
the discriminator itself -- is ALSO monkeypatched directly in the
``check_blanket_git_add``-level tests below, mirroring the same
direct-function-monkeypatch pattern, so those tests pin
``check_blanket_git_add``'s own behaviour (deny/allow wiring, override
handling, wrapper unwrapping) without needing a real ``HOME``/registry on
the test machine. ``_is_hazard_repo`` and its two building blocks
(``_meta_repo_root``, ``_hazard_registry_repo_roots``) get their OWN
dedicated unit tests further down, exercising the real discriminator logic
against a temp ``HOME`` and a temp registry directory
(``MACHINE_LOCAL_REGISTRY_DIR``, the test-isolation override
``machine_resolver.registry_dir`` already honors) -- so both "the guard
wires its discriminator correctly" and "the discriminator classifies
correctly" are pinned, not just one or the other.

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py
(``check_blanket_git_add``, ``_is_hazard_repo`` and its helpers, "6.
check_blanket_git_add -- block-blanket-git-add.sh" section).
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope


def _set_fake_home(monkeypatch, home_path):
    """Redirects ``os.path.expanduser("~")`` (what ``_meta_repo_root`` uses)
    to ``home_path`` on every platform. ``HOME`` alone only works on POSIX --
    CPython's ``ntpath.expanduser`` prefers ``USERPROFILE`` over ``HOME`` on
    Windows, so a Windows dev box with a real ``USERPROFILE`` set silently
    ignores a test's ``HOME``-only monkeypatch and resolves ``~`` to the real
    user profile instead of the fixture. Setting both env vars makes the
    redirect actually take effect on every platform.
    """
    monkeypatch.setenv("HOME", str(home_path))
    monkeypatch.setenv("USERPROFILE", str(home_path))


def _wire_git_root(monkeypatch, root):

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        assert args == ["rev-parse", "--show-toplevel"]
        return 0, root + "\n"

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)


def _wire_hazard(monkeypatch, *, is_hazard):
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: is_hazard)


def _check(monkeypatch, tmp_path, cmd, *, hazard, session_id="sess1"):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=hazard)
    return guard.check_blanket_git_add(cmd, session_id)


def _denies(monkeypatch, tmp_path, cmd, **kw):
    result = _check(monkeypatch, tmp_path, cmd, hazard=True, **kw)
    assert result is not None, f"expected DENY for: {cmd!r}"
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), f"expected DENY for: {cmd!r}"
    return result


def _allows_in_hazard_repo(monkeypatch, tmp_path, cmd, **kw):
    result = _check(monkeypatch, tmp_path, cmd, hazard=True, **kw)
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


def test_blanket_add_dash_a_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add -A")


def test_blanket_add_dot_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add .")


def test_blanket_add_dash_dash_all_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add --all")


def test_blanket_add_dash_u_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add -u")


def test_blanket_add_dash_dash_update_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add --update")


def test_bundled_short_flags_dash_f_a_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add -fA")


def test_bundled_short_flags_dash_a_u_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add -Au")


def test_blanket_add_dash_a_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add -A", hazard=False)
    assert result is None


def test_blanket_add_dot_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add .", hazard=False)
    assert result is None


def test_blanket_add_dash_dash_all_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add --all", hazard=False)
    assert result is None


def test_bundled_short_flags_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add -fA", hazard=False)
    assert result is None


def test_chained_command_blanket_add_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(
        monkeypatch, tmp_path, "git status && git add -A && git commit -m x", hazard=False
    )
    assert result is None


def test_sh_c_wrapper_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "sh -c 'git add -A'", hazard=False)
    assert result is None


def test_chained_command_blanket_add_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git status && git add -A && git commit -m x")


def test_sh_c_wrapper_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "sh -c 'git add -A'")


def test_env_wrapper_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "env git add -A")


def test_dash_c_global_option_dash_a_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git -C /some/dir add -A")


def test_dash_c_global_option_dot_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git -C /some/dir add .")


def test_git_dir_global_option_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git --git-dir=/some/dir/.git add -A")


def test_dash_c_global_option_scoped_add_allows_in_hazard_repo(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git -C /some/dir add -- path/to/file")


def test_dash_c_value_used_to_resolve_git_root_cwd(monkeypatch, tmp_path):
    seen_cwd = {}

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        seen_cwd["cwd"] = cwd
        return 0, str(tmp_path / "repo") + "\n"

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: True)

    explicit_target = str(tmp_path / "explicit" / "target")
    guard.check_blanket_git_add("git -C %s add -A" % explicit_target, "sess1")

    assert seen_cwd["cwd"] == explicit_target


def test_magic_pathspec_colon_slash_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add :/")


def test_magic_pathspec_colon_slash_dot_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add :/.")


def test_absolute_pathspec_equal_to_repo_root_denies_in_hazard_repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add %s" % root, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_absolute_pathspec_trailing_slash_equal_to_repo_root_denies_in_hazard_repo(
    monkeypatch, tmp_path
):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add %s/" % root, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_absolute_pathspec_denies_in_the_backslash_spelling(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    backslashed = str(root).replace("/", "\\")
    result = guard.check_blanket_git_add("git add %s" % backslashed, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_both_separator_spellings_reach_the_same_verdict(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    fwd = guard.check_blanket_git_add("git add %s" % str(root).replace("\\", "/"), "s")
    back = guard.check_blanket_git_add("git add %s" % str(root).replace("/", "\\"), "s")
    assert (fwd is None) == (back is None)


def test_a_scoped_subtree_in_the_backslash_spelling_still_passes(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    sub = str(root / "sub").replace("/", "\\")
    assert guard.check_blanket_git_add("git add %s" % sub, "sess1") is None


def test_absolute_pathspec_subdirectory_allows_in_hazard_repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add %s/subdir/file.py" % root, "sess1")
    assert result is None


def test_explicit_pathspec_separator_at_repo_root_still_denies_in_hazard_repo(
    monkeypatch, tmp_path
):
    """SUPERSEDES the prior `..._allows_in_hazard_repo` ratification of this
    same scenario. That version asserted ALLOW on the theory that an
    explicit `--` pathspec is inherently the "deliberate, scoped form" and
    must never be a target of the root-anchor closure -- but DoE example-game-repo-em
    (2026-08-31, cross-repo/inbox/...-safe-commit-add-guard-blind-after-
    dashdash.md) found this is exactly backwards for the repo-root case: an
    absolute path that resolves to the repo root is `.`/`-A` written a
    different way regardless of which side of `--` it sits on, and the
    guard's OWN remediation text recommends `git add -- path/to/file` --
    the memo's constraint is that this form must be "at least as protected
    as `git add <dir>`", not exempt from the same closure that already
    covers the pre-`--` spelling. This is the ONE deliberate strictness
    change in that fix: a caller relying on `git add -- <repo_root>` being
    silently allowed now gets the same deny `git add <repo_root>` always
    got. Genuinely scoped subtrees/files after `--` are unaffected --
    see `test_a_scoped_subtree_in_the_backslash_spelling_still_passes` and
    `test_absolute_pathspec_subdirectory_allows_in_hazard_repo` above."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add -- %s" % root, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_scoped_add_allows_in_hazard_repo(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -- path/to/file1 path/to/file2")


def test_scoped_add_allows_without_hazard_marker(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add -- path/to/file1 path/to/file2", hazard=False)
    assert result is None


def test_dry_run_long_flag_allows_in_hazard_repo(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A --dry-run")


def test_dry_run_short_flag_allows_in_hazard_repo(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A -n")


def test_no_git_add_at_all_allows_in_hazard_repo(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git status")


def test_empty_command_allows(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "", hazard=True)
    assert result is None


@pytest.mark.parametrize("cmd", ["git add -A", "git add .", "git add -u"])
def test_blanket_add_deny_offers_scoped_git_commit(monkeypatch, tmp_path, cmd):
    result = _denies(monkeypatch, tmp_path, cmd)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git add -- path/to/file" in reason
    assert "git commit -m" in reason and " -- path/to/file" in reason
    assert "scoped-git-commit" not in reason


@pytest.mark.parametrize("cmd", ["git add -A", "git add .", "git add -u"])
def test_blanket_add_deny_stays_within_prose_cap(monkeypatch, tmp_path, cmd):
    result = _denies(monkeypatch, tmp_path, cmd)
    measurement = measure_envelope(result)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES, (
        result["hookSpecificOutput"]["permissionDecisionReason"],
        measurement.prose_bytes,
    )


def test_scoped_add_still_allows_after_offer_change(monkeypatch, tmp_path):
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -- path/to/file")


def test_override_env_var_allows_in_hazard_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BLANKET_ADD", "1")
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A")


def test_safe_commit_internal_blanket_marker_allows_in_hazard_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("_COORDINATOR_SAFE_COMMIT_INTERNAL_BLANKET", "1")
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A")


def test_unresolvable_git_root_allows(monkeypatch, tmp_path):
    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        return 128, ""

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)
    result = guard.check_blanket_git_add("git add -A", "sess1")
    assert result is None


def test_is_hazard_repo_true_for_meta_repo(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    meta_root = fake_home / ".claude"
    meta_root.mkdir(parents=True)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setattr(guard, "_hazard_registry_repo_roots", lambda: [])

    assert guard._is_hazard_repo(str(meta_root)) is True


def test_is_hazard_repo_true_for_registered_fleet_sibling(monkeypatch, tmp_path):
    fake_home = tmp_path / "home-unrelated"
    fake_home.mkdir(parents=True)
    _set_fake_home(monkeypatch, fake_home)
    sibling_root = tmp_path / "some-fleet-sibling-checkout"
    sibling_root.mkdir(parents=True)
    monkeypatch.setattr(
        guard, "_hazard_registry_repo_roots", lambda: [str(sibling_root)]
    )

    assert guard._is_hazard_repo(str(sibling_root)) is True


def test_is_hazard_repo_false_without_marker(monkeypatch, tmp_path):
    fake_home = tmp_path / "home-unrelated"
    fake_home.mkdir(parents=True)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setattr(guard, "_hazard_registry_repo_roots", lambda: [])
    oss_repo = tmp_path / "some-oss-consumer-repo"
    oss_repo.mkdir(parents=True)

    assert guard._is_hazard_repo(str(oss_repo)) is False


def test_is_hazard_repo_fails_open_on_registry_exception(monkeypatch, tmp_path):

    def _boom():
        raise RuntimeError("settings home unreadable")

    fake_home = tmp_path / "home-unrelated"
    fake_home.mkdir(parents=True)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setattr(guard, "_hazard_registry_repo_roots", _boom)
    some_repo = tmp_path / "some-repo"
    some_repo.mkdir(parents=True)

    assert guard._is_hazard_repo(str(some_repo)) is False


def test_check_blanket_git_add_allows_when_discriminator_unresolvable(monkeypatch, tmp_path):
    result = _check(monkeypatch, tmp_path, "git add -A", hazard=False)
    assert result is None


def test_hazard_registry_repo_roots_reads_repos_star_keys(monkeypatch, tmp_path):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    fake_sibling = tmp_path / "some-fleet-sibling-checkout"
    (reg_dir / "registry.toml").write_text(
        "\"repos.claude_klabauter\" = '%s'\n"
        '"repos.example-sim-repo" = ""\n'
        '"not_a_repo_key" = "/should/not/appear"\n' % str(fake_sibling)
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    roots = guard._hazard_registry_repo_roots()

    assert str(fake_sibling) in roots
    assert "/should/not/appear" not in roots
    assert "" not in roots


def test_hazard_registry_repo_roots_empty_when_registry_missing(monkeypatch, tmp_path):
    empty_dir = tmp_path / "no-registry-here"
    empty_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_dir))

    assert guard._hazard_registry_repo_roots() == []


# a literal three-file pathspec, exactly the discipline SC-DR-014 asks for, and


@pytest.mark.parametrize(
    "cmd",
    [
        "git add -u -- registry/schema.sql",
        "git add -u -- registry/schema.sql registry/materialize.ts",
        "git add --update -- registry/schema.sql",
        "git add -u --pathspec-from-file=list.txt",
        "git add -u --pathspec-file-nul",
    ],
)
def test_dash_u_with_a_pathspec_allows(monkeypatch, tmp_path, cmd):
    _allows_in_hazard_repo(monkeypatch, tmp_path, cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "git add -u --",
        "git add -u -- .",
        "git add -u -- ./",
        "git add -u -- :/",
        "git add -u -- :/.",
        "git add -A -- registry/schema.sql",
        "git add --all -- registry/schema.sql",
        "git add -Au -- registry/schema.sql",
    ],
)
def test_dash_u_relaxation_does_not_reach_these(monkeypatch, tmp_path, cmd):
    _denies(monkeypatch, tmp_path, cmd)


@pytest.mark.parametrize(
    "after,expected",
    [
        ("-u -- a.py", True),
        ("-u -- a.py b.py", True),
        ("-u --pathspec-from-file=l.txt", True),
        ("-u --pathspec-from-file", True),
        ("-u --pathspec-file-nul", True),
        ("-u", False),
        ("-u --", False),
        ("-A", False),
    ],
)
def test_add_has_narrowing_pathspec(after, expected):
    assert guard._add_has_narrowing_pathspec(after) is expected
