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
    """Short-circuits ``_run_git`` so ``check_blanket_git_add`` resolves the
    invoking command's git root to ``root`` without spawning a real ``git``
    subprocess.
    """

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        assert args == ["rev-parse", "--show-toplevel"]
        return 0, root + "\n"

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)


def _wire_hazard(monkeypatch, *, is_hazard):
    """Short-circuits the hazard discriminator itself so
    ``check_blanket_git_add``'s own deny/allow wiring can be pinned
    independently of ``_is_hazard_repo``'s real (filesystem/registry-backed)
    classification logic -- see that function's own dedicated tests below.
    """
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: is_hazard)


def _check(monkeypatch, tmp_path, cmd, *, hazard, session_id="sess1"):
    """``hazard`` directly selects whether ``_is_hazard_repo`` reports this
    invocation's git root as a hazard repo (the ~/.claude / fleet-registry
    case) or not (the OSS-consumer case) -- the git root value itself is
    inert once ``_is_hazard_repo`` is wired, so a single placeholder path
    suffices for every test in this section.
    """
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


# ---------------------------------------------------------------------------
# Core scope pin (re-scoped): DENY only where ``_is_hazard_repo`` reports a
# hazard; every previously-covered blanket shape must still deny there.
# ---------------------------------------------------------------------------


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
    """Bundled short flags (``-fA``) -- ``A`` present among the bundled
    chars is still a blanket-add trigger.
    """
    _denies(monkeypatch, tmp_path, "git add -fA")


def test_bundled_short_flags_dash_a_u_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add -Au")


# ---------------------------------------------------------------------------
# THE test whose absence caused the over-broad widening: a repo where the
# hazard discriminator does NOT match (the OSS-consumer case) must ALLOW
# every one of the same blanket shapes -- the guard is a no-op there.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Chained-command segment splitting -- the guard splits on unquoted
# `;`/`&`/`|` and checks each segment independently.
# ---------------------------------------------------------------------------


def test_chained_command_blanket_add_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git status && git add -A && git commit -m x")


# ---------------------------------------------------------------------------
# Wrapper-unwrap forms -- `sh -c`/`env`/`nice` etc. must still be caught
# wherever the guard fires.
# ---------------------------------------------------------------------------


def test_sh_c_wrapper_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "sh -c 'git add -A'")


def test_env_wrapper_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "env git add -A")


# ---------------------------------------------------------------------------
# example-market-data-repo-em scoped-commit-guard-asymmetry finding (2026-08-03
# relay): a `-C <dir>`/`--git-dir=...`/other git GLOBAL option sitting
# between `git` and `add` previously escaped this guard entirely -- the
# top-of-function short-circuit and the per-segment matcher both required
# "add" literally adjacent to "git", so any global option in between
# silently allowed the identical blanket sweep. These shapes must deny
# wherever the equivalent option-free shape already denies.
# ---------------------------------------------------------------------------


def test_dash_c_global_option_dash_a_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git -C /some/dir add -A")


def test_dash_c_global_option_dot_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git -C /some/dir add .")


def test_git_dir_global_option_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git --git-dir=/some/dir/.git add -A")


def test_dash_c_global_option_scoped_add_allows_in_hazard_repo(monkeypatch, tmp_path):
    """A `-C <dir>` prefix does not itself make a genuinely scoped add
    blanket -- the option-free equivalent (`git add -- path/to/file`) is
    already an ALLOW, and this must stay symmetric with it."""
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git -C /some/dir add -- path/to/file")


def test_dash_c_value_used_to_resolve_git_root_cwd(monkeypatch, tmp_path):
    """The hazard-repo probe runs against the LAST `-C <dir>` value
    preceding `add`, not the guard process's own cwd -- so the command's
    OWN declared target repo is what gets hazard-classified, per
    `_bt_blanket_add_dash_c_cwd`'s "last -C wins" contract.
    """
    seen_cwd = {}

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        seen_cwd["cwd"] = cwd
        return 0, str(tmp_path / "repo") + "\n"

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: True)

    # A platform-derived absolute path (``tmp_path``-rooted), not a second
    # hardcoded literal -- ``_bt_blanket_add_dash_c_cwd`` returns an
    # ``os.path.isabs`` value unchanged, so the expectation is exactly this
    # same value on every platform, POSIX or Windows.
    explicit_target = str(tmp_path / "explicit" / "target")
    guard.check_blanket_git_add("git -C %s add -A" % explicit_target, "sess1")

    assert seen_cwd["cwd"] == explicit_target


# ---------------------------------------------------------------------------
# example-market-data-repo-em scoped-commit-guard-asymmetry finding: `:/`/`:/.`
# are git's own "magic pathspec" for the top of the working tree -- the
# identical blast radius as `.`/`-A`, just spelled differently.
# ---------------------------------------------------------------------------


def test_magic_pathspec_colon_slash_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add :/")


def test_magic_pathspec_colon_slash_dot_denies_in_hazard_repo(monkeypatch, tmp_path):
    _denies(monkeypatch, tmp_path, "git add :/.")


# ---------------------------------------------------------------------------
# example-market-data-repo-em scoped-commit-guard-asymmetry finding: an absolute
# pathspec that resolves to the repo root itself is `.` written a different
# way. A DEEPER absolute path (a genuinely scoped subtree) must stay ALLOW --
# this guard is not widened into shapes that are genuinely scoped.
# ---------------------------------------------------------------------------


#: state/bash-guards/known-red.json group "dispatch-checks-windows-path"
#: (check_blanket_git_add stripping backslashes). Owner:
#: docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md.
@pytest.mark.pending_fix
def test_absolute_pathspec_equal_to_repo_root_denies_in_hazard_repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add %s" % root, "sess1")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.pending_fix
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


def test_absolute_pathspec_subdirectory_allows_in_hazard_repo(monkeypatch, tmp_path):
    """A genuinely scoped absolute path (a subtree, not the repo root
    itself) must stay ALLOW -- negative spec for the repo-root closure
    above."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add %s/subdir/file.py" % root, "sess1")
    assert result is None


def test_explicit_pathspec_separator_at_repo_root_allows_in_hazard_repo(monkeypatch, tmp_path):
    """The ratified explicit-scope form (`git add -- <paths>`) stays ALLOW
    even when the named path happens to equal the repo root -- an explicit
    `--` pathspec is the deliberate, scoped form this guard's own
    remediation message recommends; it must never itself become a target of
    the closure above."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _wire_git_root(monkeypatch, str(root))
    _wire_hazard(monkeypatch, is_hazard=True)
    result = guard.check_blanket_git_add("git add -- %s" % root, "sess1")
    assert result is None


# ---------------------------------------------------------------------------
# Deliberate ALLOW -- scoped adds and dry-runs are not blanket adds anywhere,
# hazard repo or not.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Override escape hatches -- both bypass DENY in a hazard repo.
# ---------------------------------------------------------------------------


def test_override_env_var_allows_in_hazard_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BLANKET_ADD", "1")
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A")


def test_safe_commit_internal_blanket_marker_allows_in_hazard_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("_COORDINATOR_SAFE_COMMIT_INTERNAL_BLANKET", "1")
    _allows_in_hazard_repo(monkeypatch, tmp_path, "git add -A")


# ---------------------------------------------------------------------------
# No git root resolvable -- fail-open (not this guard's problem; a hard
# guard elsewhere owns "git command run outside any git repo").
# ---------------------------------------------------------------------------


def test_unresolvable_git_root_allows(monkeypatch, tmp_path):
    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        return 128, ""

    monkeypatch.setattr(guard, "_run_git", _fake_run_git)
    result = guard.check_blanket_git_add("git add -A", "sess1")
    assert result is None


# ---------------------------------------------------------------------------
# The discriminator itself (``_is_hazard_repo`` and its two building
# blocks) -- exercised against real (temp) filesystem/registry state, not
# monkeypatched away, so the classification logic is pinned directly and
# not merely assumed by the wiring tests above.
# ---------------------------------------------------------------------------


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
    """The test whose absence caused the original defect: a repo that is
    neither the meta-repo nor a registered fleet sibling must classify as
    NOT a hazard -- this is the OSS-consumer-install case.
    """
    fake_home = tmp_path / "home-unrelated"
    fake_home.mkdir(parents=True)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setattr(guard, "_hazard_registry_repo_roots", lambda: [])
    oss_repo = tmp_path / "some-oss-consumer-repo"
    oss_repo.mkdir(parents=True)

    assert guard._is_hazard_repo(str(oss_repo)) is False


def test_is_hazard_repo_fails_open_on_registry_exception(monkeypatch, tmp_path):
    """The discriminator's own contract: an exception classifying the
    hazard-registry half degrades to False (not a hazard), never propagates
    or defaults to True. This is what makes ``check_blanket_git_add``
    fail-open when the discriminator is unresolvable.
    """

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
    """End-to-end fail-open: when the discriminator resolves to "not a
    hazard" (the only fail-open-consistent outcome -- see
    ``test_is_hazard_repo_fails_open_on_registry_exception`` for the
    discriminator's own contract in isolation), ``check_blanket_git_add``
    allows rather than denies.
    """
    result = _check(monkeypatch, tmp_path, "git add -A", hazard=False)
    assert result is None


def test_hazard_registry_repo_roots_reads_repos_star_keys(monkeypatch, tmp_path):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    fake_sibling = tmp_path / "some-fleet-sibling-checkout"
    # TOML literal strings (single-quoted) do not interpret backslash
    # escapes -- a plain double-quoted (basic) TOML string does, so
    # interpolating a raw Windows path (backslash-delimited) into one
    # produces invalid escape sequences (``\U``, ``\s``, ...) that
    # ``tomllib`` rejects, and ``_load_toml``'s fail-open ``except
    # Exception`` silently degrades the whole file to ``{}``. The literal
    # form keeps this fixture path-separator-agnostic without hardcoding
    # either separator. POSIX paths (no backslashes) were never at risk from
    # a double-quoted TOML string -- this is a Windows-path-safety fix, not
    # a general double-quoted-strings-are-unsafe claim.
    # Review: coordinator:code-reviewer (slice B, P3) -- clarify the fixture
    # comment is Windows-path-specific, not a general TOML-format claim.
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
