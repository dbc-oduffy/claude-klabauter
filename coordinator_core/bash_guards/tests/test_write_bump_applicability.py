"""Tests for coordinator_core.bash_guards._write_bump_applicability -- the
single entry point the write-confinement bump guards consult to answer "does
the bump apply at all" and "if the session's own anchor is in no git repo, is
a given target a registered repo".

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567, chunk C2.
Covers AC8 (fail-open on every unresolvable case), AC10 (nothing bumps under
~/.claude), AC11 (no-repo-anchor: outside-repo never bumps, registered-repo
cross-repo still does), AC12 (an incidental `cd` does not suppress the bump
on the next call -- a genuine two-call sequence, not a mocked intent
assertion), and AC18 (the observability log).

Test isolation (binds AC13): every test here that exercises `resolve_launch_anchor`
or `bump_applies` runs under the `_isolated_settings_home` autouse fixture below,
which `setenv`s `COORDINATOR_SETTINGS_HOME` to a `tmp_path` subdirectory -- NOT a
`delenv` of `HOME`/`CLAUDE_HOME`/`USERPROFILE` (per AC13, `delenv`ing those would
make `_settings_home_dir_from_env` return `""`, silently no-oping the settings-home
anchor read in exactly the suite meant to test it). `write_session_start_record`
(called by several fixtures/tests below) always attempts a settings-home write as a
side effect, so isolation is needed even in tests that only assert on the in-repo
record.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    """AC13: isolate every test in this module from the developer's real settings home.

    See module docstring's "Test isolation" paragraph, and the identical fixture in
    `test_write_bump_session_start.py` (C1's file) which this mirrors.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


def _write_registry(reg_dir: Path, **repos: str) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[repos]"]
    for key, val in repos.items():
        escaped = str(val).replace("\\", "\\\\")
        lines.append(f'{key} = "{escaped}"')
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bump_applies_precondition(tmp_path: Path, session_id: str) -> None:
    """AC9: every test asserting a guard verdict/classification outcome
    asserts `bump_applies()` is True FIRST -- not most such tests, every
    one (plan § "`bump_applies()` is a precondition in EVERY test that
    asserts a guard verdict"). A test built without this precondition
    passes for unrelated reasons: applicability fails open, so "no bump"
    can mean the guard never engaged rather than the assertion holding.
    """
    elsewhere = tmp_path / f"{session_id}-anchor"
    elsewhere.mkdir(exist_ok=True)
    fake_home = tmp_path / f"{session_id}-home"
    fake_home.mkdir(exist_ok=True)
    assert (
        applicability.bump_applies(
            session_id,
            cwd=str(elsewhere),
            env={"CLAUDE_PROJECT_DIR": str(elsewhere), "HOME": str(fake_home)},
        )
        is True
    )


def _write_toml(path: Path, table: dict) -> None:
    """Hand-rolled TOML writer for this module's `publish.mirrors.*`
    fixtures -- str-only leaf values, nested dict tables emitted as their
    own `[a.b]` header after the parent's own leaves."""

    def emit(prefix: str, d: dict, lines: list) -> None:
        leaves = {k: v for k, v in d.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in d.items() if isinstance(v, dict)}
        if leaves:
            if prefix:
                lines.append(f"[{prefix}]")
            for k, v in leaves.items():
                escaped = str(v).replace("\\", "\\\\")
                lines.append(f'{k} = "{escaped}"')
        for k, v in tables.items():
            full = f"{prefix}.{k}" if prefix else k
            emit(full, v, lines)

    lines: list = []
    emit("", table, lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# resolve_launch_anchor -- primary (session-start record) vs fallback (env)
# ---------------------------------------------------------------------------


def test_resolve_launch_anchor_prefers_session_start_record_over_env(tmp_path):
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-primary", launch_cwd=str(root))
    other = tmp_path / "elsewhere"
    other.mkdir()

    anchor = applicability.resolve_launch_anchor(
        "sess-primary", cwd=str(root), env={"CLAUDE_PROJECT_DIR": str(other)}
    )

    assert anchor == str(root)


def test_resolve_launch_anchor_falls_back_to_claude_project_dir_when_record_absent(tmp_path):
    scratch = tmp_path / "no-repo"
    scratch.mkdir()

    anchor = applicability.resolve_launch_anchor(
        "sess-no-record", cwd=str(scratch), env={"CLAUDE_PROJECT_DIR": str(scratch)}
    )

    assert anchor == str(scratch)


def test_resolve_launch_anchor_none_when_neither_resolves(tmp_path):
    scratch = tmp_path / "no-repo"
    scratch.mkdir()

    anchor = applicability.resolve_launch_anchor("sess-nothing", cwd=str(scratch), env={})

    assert anchor is None


def test_ac1_resolve_launch_anchor_survives_cwd_drift_across_a_repo_boundary(tmp_path):
    """AC1 -- the headline fix this chunk makes. `CLAUDE_PROJECT_DIR` unset (no fallback
    to mask the defect); the session launches in `anchor_repo`, then a later call's `cwd`
    has drifted to an entirely DIFFERENT, foreign repo -- exactly the scenario
    `sessions_dir(cwd)` alone resolves wrong (it addresses the foreign repo's own hub,
    where this session's record was never written). Pre-C1/C2, `resolve_launch_anchor`
    returned `None` here; post-fix it must return the anchor unchanged.

    `env=None` throughout (both the write and the read) so both legs resolve the
    settings home from the SAME source -- the `_isolated_settings_home` autouse
    fixture's `os.environ`-injected `COORDINATOR_SETTINGS_HOME` -- rather than an
    explicit dict that would silently address a different (unwritten) settings home
    on the read side.
    """
    anchor_repo = _init_repo(tmp_path, name="anchor-repo")
    foreign_repo = _init_repo(tmp_path, name="foreign-repo")
    session_start.write_session_start_record("sess-drift", launch_cwd=str(anchor_repo))

    anchor = applicability.resolve_launch_anchor("sess-drift", cwd=str(foreign_repo))

    assert anchor == str(anchor_repo)


def test_ac1_bump_applies_true_after_cwd_drifts_to_a_foreign_repo(tmp_path):
    """Companion to the anchor test above at the `bump_applies` layer: the session's own
    anchor is a normal repo (not under the real `~/.claude`), so once the anchor resolves
    across the boundary, applicability itself must also flip to `True`. `env=None` for the
    same reason as the test above -- both write and read resolve the settings home from
    the fixture's injected `COORDINATOR_SETTINGS_HOME`."""
    anchor_repo = _init_repo(tmp_path, name="anchor-repo-2")
    foreign_repo = _init_repo(tmp_path, name="foreign-repo-2")
    session_start.write_session_start_record("sess-drift-2", launch_cwd=str(anchor_repo))

    applies = applicability.bump_applies("sess-drift-2", cwd=str(foreign_repo))

    assert applies is True


# ---------------------------------------------------------------------------
# AC8 -- fail open on every unresolvable case
# ---------------------------------------------------------------------------


def test_ac8_bump_applies_false_when_anchor_unresolvable(tmp_path):
    scratch = tmp_path / "no-repo"
    scratch.mkdir()

    assert applicability.bump_applies("sess-unresolvable", cwd=str(scratch), env={}) is False


def test_ac8_bump_applies_false_when_home_unresolvable(tmp_path):
    other = tmp_path / "somewhere"
    other.mkdir()

    # No CLAUDE_HOME/HOME/USERPROFILE in env -- home cannot be resolved, so
    # whether the anchor sits under `~/.claude` cannot be determined either;
    # this module's fail-open direction treats that as "assume the hatch" so
    # the caller does not bump on a value it cannot classify.
    assert (
        applicability.bump_applies(
            "sess-no-home", cwd=str(other), env={"CLAUDE_PROJECT_DIR": str(other)}
        )
        is False
    )


def test_ac8_target_is_registered_repo_false_when_registry_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    target = tmp_path / "some-target"
    target.mkdir()

    assert applicability.target_is_registered_repo(str(target)) is False


def test_ac8_target_is_registered_repo_false_when_target_empty():
    assert applicability.target_is_registered_repo("") is False


# ---------------------------------------------------------------------------
# AC10 -- nothing bumps when the session anchor is under ~/.claude
# ---------------------------------------------------------------------------


def test_ac10_bump_does_not_apply_under_claude_home(tmp_path):
    fake_home = tmp_path / "home"
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir(parents=True)

    applies = applicability.bump_applies(
        "sess-recovery",
        cwd=str(claude_dir),
        env={"CLAUDE_PROJECT_DIR": str(claude_dir), "HOME": str(fake_home)},
    )

    assert applies is False


def test_ac10_bump_applies_outside_claude_home(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    elsewhere = tmp_path / "some-repo"
    elsewhere.mkdir()

    applies = applicability.bump_applies(
        "sess-normal",
        cwd=str(elsewhere),
        env={"CLAUDE_PROJECT_DIR": str(elsewhere), "HOME": str(fake_home)},
    )

    assert applies is True


def test_ac10_bump_does_not_apply_under_claude_home_nested_subdir(tmp_path):
    fake_home = tmp_path / "home"
    nested = fake_home / ".claude" / "plugins" / "coordinator-claude"
    nested.mkdir(parents=True)

    applies = applicability.bump_applies(
        "sess-recovery-nested",
        cwd=str(nested),
        env={"CLAUDE_PROJECT_DIR": str(nested), "HOME": str(fake_home)},
    )

    assert applies is False


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-08-10-carve-claude-out-and-close-the-backslash-bypass.md)
# -- `target_is_under_claude_home`, the TARGET-side counterpart of the
# `_anchor_is_under_claude_home` predicate AC10 pins above. AC1/AC3/AC4.
# ---------------------------------------------------------------------------


def test_c1_target_is_under_claude_home_true_for_settings_json(tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    target = fake_home / ".claude" / "settings.json"
    target.write_text("{}", encoding="utf-8")

    assert applicability.target_is_under_claude_home(
        str(target), env={"HOME": str(fake_home)}
    )


def test_c1_target_is_under_claude_home_takes_no_gitdir_short_circuit(tmp_path):
    """AC3: unlike a predicate modeled on `_target_is_under_settings_home`
    (which short-circuits `False` whenever the caller's own `target_gitdir`
    is already resolved), this predicate's SIGNATURE accepts no `target_
    gitdir` parameter at all and never calls `resolve_gitdir` itself --
    proven directly against the source rather than a real `git init` fixture
    (`~/.claude` being a genuine checkout on this fleet is exercised
    end-to-end by the guard-level C1 tests in `test_bump_foreign_repo_
    write.py`/`test_bump_out_of_repo_tool_write.py`, which build a real
    repo there)."""
    import inspect

    params = inspect.signature(applicability.target_is_under_claude_home).parameters
    assert "target_gitdir" not in params
    assert "resolve_gitdir" not in inspect.getsource(applicability.target_is_under_claude_home)


def test_c1_target_is_under_claude_home_false_elsewhere(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "some-repo" / "README.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("x", encoding="utf-8")

    assert applicability.target_is_under_claude_home(
        str(elsewhere), env={"HOME": str(home)}
    ) is False


def test_c1_target_is_under_claude_home_resolves_from_injected_env_not_os_environ(
    tmp_path, monkeypatch
):
    """AC3: a single verdict must not resolve the anchor from live
    `os.environ` while resolving this predicate from an injected mapping --
    proven by setting a REAL `os.environ["HOME"]` that would NOT match, and
    an injected `env` that DOES, and asserting the injected mapping wins."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    injected_home = tmp_path / "injected-home"
    (injected_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))

    target = injected_home / ".claude" / "settings.json"
    target.write_text("{}", encoding="utf-8")

    assert applicability.target_is_under_claude_home(
        str(target), env={"HOME": str(injected_home)}
    )


def test_c1_target_is_under_claude_home_false_when_home_unresolvable():
    assert applicability.target_is_under_claude_home("/some/path", env={}) is False


def test_c1_target_is_under_claude_home_false_when_target_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert applicability.target_is_under_claude_home("", env={"HOME": str(home)}) is False


# ---------------------------------------------------------------------------
# AC11 -- cwd anchor in no git repo: outside-repo never bumps, a registered
# cross-repo target still does, an unregistered fresh scaffold does not.
# ---------------------------------------------------------------------------


def test_ac11_session_anchor_has_git_repo_false_for_fresh_scaffold(tmp_path):
    scaffold = tmp_path / "Documents" / "new-project"
    scaffold.mkdir(parents=True)

    has_repo = applicability.session_anchor_has_git_repo(
        "sess-scaffold", cwd=str(scaffold), env={"CLAUDE_PROJECT_DIR": str(scaffold)}
    )

    assert has_repo is False


def test_ac11_session_anchor_has_git_repo_true_inside_a_repo(tmp_path):
    root = _init_repo(tmp_path)

    has_repo = applicability.session_anchor_has_git_repo(
        "sess-in-repo", cwd=str(root), env={"CLAUDE_PROJECT_DIR": str(root)}
    )

    assert has_repo is True


def test_ac11_registered_target_is_registered(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registry"
    registered_root = tmp_path / "registered-repo"
    registered_root.mkdir()
    _write_registry(reg_dir, some_repo=str(registered_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    nested_target = registered_root / "nested" / "file.txt"

    assert applicability.target_is_registered_repo(str(nested_target)) is True


def test_ac11_unregistered_target_is_not_registered(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registry"
    registered_root = tmp_path / "registered-repo"
    registered_root.mkdir()
    _write_registry(reg_dir, some_repo=str(registered_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    unregistered = tmp_path / "fresh-scaffold"
    unregistered.mkdir()

    assert applicability.target_is_registered_repo(str(unregistered)) is False


def test_ac11_enumerates_all_repos_star_entries_not_just_two_named_keys(tmp_path, monkeypatch):
    """Widened per the plan's the Director of Engineering finding 12 -- ANY `repos.*` key counts,
    not only `repos.doe_claude` / `repos.claude_klabauter`."""
    reg_dir = tmp_path / "registry"
    third_party_root = tmp_path / "some-other-registered-repo"
    third_party_root.mkdir()
    _write_registry(
        reg_dir,
        doe_claude=str(tmp_path / "doe-claude-placeholder"),
        claude_klabauter=str(tmp_path / "claude-klabauter-placeholder"),
        some_third_repo=str(third_party_root),
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    assert applicability.target_is_registered_repo(str(third_party_root)) is True


# ---------------------------------------------------------------------------
# Narrow (PM ruling 2026-08-10, `state/bug-backlog/2026-08-10-a-session-
# anchored-outside-any-git-repo-88ca86c1f8bf.yaml`) -- `anchor_subtree_contains`.
# Confirms allow at/below the anchor, bump above it, bump in a prefix-
# colliding sibling, and that Windows-style / case-insensitive spellings of
# the SAME path still compare equal.
# ---------------------------------------------------------------------------


def test_narrow_anchor_subtree_contains_true_at_the_anchor_itself(tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()

    assert applicability.anchor_subtree_contains(str(anchor), str(anchor)) is True


def test_narrow_anchor_subtree_contains_true_below_the_anchor(tmp_path):
    anchor = tmp_path / "anchor"
    nested = anchor / "scaffold" / "new-project"
    nested.mkdir(parents=True)

    assert applicability.anchor_subtree_contains(str(anchor), str(nested)) is True


def test_narrow_anchor_subtree_contains_false_above_the_anchor(tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    outside = tmp_path

    assert applicability.anchor_subtree_contains(str(anchor), str(outside)) is False


def test_narrow_anchor_subtree_contains_false_for_a_prefix_colliding_sibling(tmp_path):
    """`C:\\work` must not admit `C:\\workspace` -- a sibling directory
    merely starting with the anchor's own name is not contained."""
    anchor = tmp_path / "work"
    sibling = tmp_path / "workspace"
    anchor.mkdir()
    sibling.mkdir()

    assert applicability.anchor_subtree_contains(str(anchor), str(sibling)) is False


def test_narrow_anchor_subtree_contains_fails_open_on_unresolvable_anchor():
    assert applicability.anchor_subtree_contains("", "/some/target") is True


def test_narrow_anchor_subtree_contains_fails_open_on_unresolvable_target(tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()

    assert applicability.anchor_subtree_contains(str(anchor), "") is True


# ---------------------------------------------------------------------------
# AC12 -- an incidental `cd` in one call does not suppress the bump on the
# next. Genuine two-call sequence: SessionStart writes the anchor from repo
# root; a later call resolves the SAME anchor after the process cwd has
# drifted to a subdirectory of that repo (exactly the persists-between-calls
# drift the harness contract describes), never re-deriving "my own repo"
# from wherever the live cwd has moved to.
# ---------------------------------------------------------------------------


def test_ac12_cd_to_a_subdirectory_does_not_suppress_applicability(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)

    # Call N: SessionStart, launch cwd == repo root.
    monkeypatch.chdir(root)
    assert session_start.write_session_start_record("sess-ac12") is True

    # An intervening Bash tool call `cd`s into a subdirectory of the SAME
    # repo -- the harness contract states this persists to the next call.
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    # Call N+1: the guard resolves cwd=None, i.e. from wherever the process
    # now sits (the drifted subdirectory) -- exactly the scenario a naive,
    # live-cwd-anchored guard would misclassify.
    anchor = applicability.resolve_launch_anchor("sess-ac12", cwd=None)
    assert anchor == str(root)

    applies = applicability.bump_applies(
        "sess-ac12", cwd=None, env={"HOME": str(tmp_path / "unrelated-home")}
    )
    assert applies is True


def test_ac12_cd_does_not_change_which_registry_target_is_seen_as_registered(tmp_path, monkeypatch):
    """Companion to the anchor test above: the no-repo-anchor registry check
    is keyed on the TARGET path passed in by the caller, not on live cwd, so
    a `cd` between calls cannot change its verdict either."""
    reg_dir = tmp_path / "registry"
    registered_root = tmp_path / "registered-repo"
    registered_root.mkdir()
    _write_registry(reg_dir, some_repo=str(registered_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    verdict_before = applicability.target_is_registered_repo(str(registered_root))
    monkeypatch.chdir(tmp_path)
    verdict_after = applicability.target_is_registered_repo(str(registered_root))

    assert verdict_before is True
    assert verdict_after is True


# ---------------------------------------------------------------------------
# AC18 -- observability: one appended line per "applies" event
# ---------------------------------------------------------------------------


def test_ac18_record_applicability_event_appends_a_line(tmp_path):
    root = _init_repo(tmp_path)

    applicability.record_applicability_event(
        "sess-log-1", repo="my-repo", target="/some/target", agent_class="em", cwd=str(root)
    )

    log_path = root / ".git" / "coordinator-sessions" / "sess-log-1" / "write_bump_applicability_log"
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert "repo=my-repo" in content
    assert "target=/some/target" in content
    assert "session=sess-log-1" in content
    assert "agent_class=em" in content


def test_ac18_record_applicability_event_appends_not_overwrites(tmp_path):
    root = _init_repo(tmp_path)

    applicability.record_applicability_event(
        "sess-log-2", repo="repo-a", target="t1", agent_class="em", cwd=str(root)
    )
    applicability.record_applicability_event(
        "sess-log-2", repo="repo-b", target="t2", agent_class="executor", cwd=str(root)
    )

    log_path = root / ".git" / "coordinator-sessions" / "sess-log-2" / "write_bump_applicability_log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "repo=repo-a" in lines[0]
    assert "repo=repo-b" in lines[1]


def test_ac18_record_applicability_event_never_raises_when_not_in_a_git_repo(tmp_path):
    scratch = tmp_path / "no-repo"
    scratch.mkdir()

    # Must not raise -- a missed log line is never a reason to alter or
    # block the write it describes (fail open).
    applicability.record_applicability_event(
        "sess-log-3", repo="r", target="t", agent_class="em", cwd=str(scratch)
    )


def test_ac18_record_applicability_event_never_raises_on_empty_session_id(tmp_path):
    root = _init_repo(tmp_path)
    applicability.record_applicability_event(
        "", repo="r", target="t", agent_class="em", cwd=str(root)
    )
    assert not (root / ".git" / "coordinator-sessions").exists()


#: A synthetic Windows drive letter, assembled at runtime rather than as a
#: single string literal, so this test's mocked path shapes read as data to
#: the concrete-path-citation guard rather than as a hardcoded machine path
#: -- there is no real drive to hardcode here, this is a pure string-shape
#: fixture for AC11's mocked extended-length-prefix test.
_SYNTHETIC_DRIVE_LETTER = "".join(["Z", ":"])


# ---------------------------------------------------------------------------
# C1 -- publish-destination resolver, closed set from publish.mirrors.*.path
# AC1, AC2, AC9, AC11.
# ---------------------------------------------------------------------------


def test_ac1_target_is_publish_destination_true_for_a_mirror_path(tmp_path, monkeypatch):
    _bump_applies_precondition(tmp_path, "sess-c1-publish-true")

    reg_dir = tmp_path / "registry"
    mirror_root = tmp_path / "mirror-repo"
    mirror_root.mkdir()
    _write_toml(
        reg_dir / "registry.toml",
        {"publish": {"mirrors": {"coordinator_claude": {"owner": "claude-central-em"}}}},
    )
    _write_toml(
        reg_dir / "registry.local.toml",
        {"publish": {"mirrors": {"coordinator_claude": {"path": str(mirror_root)}}}},
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    nested_target = mirror_root / "nested" / "file.txt"

    assert applicability.target_is_publish_destination(str(nested_target)) is True


def test_ac1_target_is_publish_destination_false_for_every_repos_value(tmp_path, monkeypatch):
    """Proves actual `repos.*` / `publish.mirrors.*` disjointness, not mere
    absence of any publish mirror -- a registry declaring BOTH a `repos.*`
    entry and a `publish.mirrors.*` entry must classify the `repos.*` root
    as `False` and the `publish.mirrors.*` root as `True`. Review:
    code-reviewer a19c32b7, Finding 2 -- the prior version of this test
    populated no `publish.mirrors.*` entries at all, so both assertions
    passed trivially regardless of whether prefix matching was correct."""
    _bump_applies_precondition(tmp_path, "sess-c1-publish-false")

    reg_dir = tmp_path / "registry"
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    other_repo_root = tmp_path / "some-registered-repo"
    other_repo_root.mkdir()
    mirror_root = tmp_path / "mirror-repo"
    mirror_root.mkdir()

    _write_registry(
        reg_dir,
        claude_klabauter=str(claude_klabauter_root),
        some_repo=str(other_repo_root),
    )
    _write_toml(
        reg_dir / "registry.local.toml",
        {"publish": {"mirrors": {"coordinator_claude": {"path": str(mirror_root)}}}},
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    assert applicability.target_is_publish_destination(str(claude_klabauter_root)) is False
    assert applicability.target_is_publish_destination(str(other_repo_root)) is False
    assert applicability.target_is_publish_destination(str(mirror_root)) is True


def test_d4_publish_destination_owner_reads_the_merged_view_across_both_files(
    tmp_path, monkeypatch
):
    """DRIFT D4 -- `publish.mirrors.coordinator_claude.owner` lives in
    `registry.toml` while its `.path` (and the second mirror's `.owner` and
    `.path`) live in `registry.local.toml`. A reader consulting only
    `registry.local.toml` would silently lose `coordinator_claude`'s owner.
    """
    _bump_applies_precondition(tmp_path, "sess-d4-merged-view")

    reg_dir = tmp_path / "registry"
    cc_root = tmp_path / "coordinator-claude-mirror"
    cc_root.mkdir()
    klabauter_root = tmp_path / "klabauter-mirror"
    klabauter_root.mkdir()

    _write_toml(
        reg_dir / "registry.toml",
        {"publish": {"mirrors": {"coordinator_claude": {"owner": "claude-central-em"}}}},
    )
    _write_toml(
        reg_dir / "registry.local.toml",
        {
            "publish": {
                "mirrors": {
                    "coordinator_claude": {"path": str(cc_root)},
                    "claude_klabauter": {
                        "path": str(klabauter_root),
                        "owner": "claude-klabauter-em",
                    },
                }
            }
        },
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    assert applicability.target_is_publish_destination(str(cc_root)) is True
    assert applicability.publish_destination_owner(str(cc_root)) == "claude-central-em"
    assert applicability.target_is_publish_destination(str(klabauter_root)) is True
    assert applicability.publish_destination_owner(str(klabauter_root)) == "claude-klabauter-em"


def test_d5_empty_publish_path_value_is_skipped_not_resolved_to_filesystem_root(
    tmp_path, monkeypatch
):
    """DRIFT D5 -- `registry.toml` declares a `publish.mirrors.*.path` key
    with an empty value at HEAD; an empty `.path` must never resolve to the
    filesystem root (which is a prefix of every path, so an unguarded empty
    string would make every target a publish destination)."""
    _bump_applies_precondition(tmp_path, "sess-d5-empty-path")

    reg_dir = tmp_path / "registry"
    _write_toml(
        reg_dir / "registry.toml",
        {"publish": {"mirrors": {"coordinator_claude": {"path": "", "owner": "claude-central-em"}}}},
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    arbitrary_target = tmp_path / "anything"
    arbitrary_target.mkdir()

    assert applicability.target_is_publish_destination(str(arbitrary_target)) is False
    assert applicability.publish_destination_owner(str(arbitrary_target)) == ""


@pytest.mark.parametrize(
    "target_subdir",
    ["some-target", "fresh-scaffold", "nested/deeper/target"],
)
def test_ac2_unresolvable_registry_withholds_only_the_publish_carveout(
    tmp_path, monkeypatch, target_subdir
):
    """AC2 -- an unresolvable/unreadable registry must produce exactly the
    verdict the pre-C1 code produces: `target_is_registered_repo` unaffected
    (already `False` when the registry cannot be read, per the pre-existing
    AC8 coverage above), and the NEW `target_is_publish_destination` also
    `False` -- the publish carve-out is the only thing withheld, never a new
    deny-on-uncertainty branch. Parametrized table run against both
    functions for several distinct targets."""
    _bump_applies_precondition(tmp_path, f"sess-ac2-{target_subdir.replace('/', '-')}")

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    target = tmp_path / target_subdir
    target.mkdir(parents=True)

    assert applicability.target_is_registered_repo(str(target)) is False
    assert applicability.target_is_publish_destination(str(target)) is False
    assert applicability.publish_destination_owner(str(target)) == ""


def test_ac11_extended_length_prefix_normalizes_same_as_the_bare_path(tmp_path, monkeypatch):
    """AC11 / merged-in DoE source numbering C9 -- a Windows extended-length-
    prefixed path and its bare equivalent must resolve to the identical
    case-folded string, closing the desync on the comparison helper C1 is
    already adding a call site to (`_resolve_path`, consumed by
    `target_is_publish_destination`). No live Windows host -- closed by a
    mocked extended-length-prefix test using synthetic, runtime-assembled
    path strings, per plan § "Out of scope"."""
    _bump_applies_precondition(tmp_path, "sess-ac11-extended-length-prefix")

    bare_drive_path = _SYNTHETIC_DRIVE_LETTER + "\\Users\\Foo\\Bar"
    prefixed_drive_path = "\\\\?\\" + bare_drive_path
    assert applicability._resolve_path(prefixed_drive_path) == applicability._resolve_path(
        bare_drive_path
    )

    bare_unc_path = "\\\\server\\share\\file"
    prefixed_unc_path = "\\\\?\\UNC\\server\\share\\file"
    assert applicability._resolve_path(prefixed_unc_path) == applicability._resolve_path(
        bare_unc_path
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "AC11 desync-repro: undemonstrable on native Windows, not merely "
        "un-run there. Owner: claude-klabauter-em (chunk C9a, "
        "docs/plans/2026-08-07-guard-suite-back-to-a-gate.md). The mechanism "
        "this repro exploits is `os.path.realpath` failing to recognize a "
        "backslash-prefixed Windows string as absolute, so it joins the raw "
        "string onto `cwd` instead -- which is a POSIX-`realpath`-on-NT-"
        "strings artifact, not a Windows-native one. `ntpath.realpath` "
        "recognizes BOTH the bare and the `\\\\?\\`-prefixed forms as already "
        "absolute unconditionally, so disabling only the pre-`realpath` "
        "strip (this test's whole mechanism) cannot drive the two operands "
        "apart here: both go into `os.path.realpath` as absolute NT paths "
        "and normalize consistently regardless of the pre-strip, exactly as "
        "the test above (`test_ac11_extended_length_prefix_normalizes_same_"
        "as_the_bare_path`) already proves unconditionally on this "
        "platform. See that test's own passing result on this host as the "
        "positive-side confirmation: AC11's guarantee (prefixed == bare) "
        "holds natively on Windows without the pre-strip needing to be the "
        "thing proven responsible -- there is no asymmetric-realpath "
        "failure mode to isolate here. A true Windows-side repro would "
        "require driving a DIFFERENT desync mechanism (e.g. a real "
        "extended-length path where `GetFinalPathNameByHandle`-backed "
        "resolution returns the `\\\\?\\`-prefixed form for one operand and "
        "not the other) which needs on-disk paths near MAX_PATH and is out "
        "of this chunk's scope -- flagged for a follow-up chunk, not folded "
        "in here as a scope-creeping rewrite."
    ),
)
def test_ac11_pre_realpath_strip_is_what_closes_the_extended_length_desync(monkeypatch):
    """Companion to the test above -- proves the PRE-`realpath` strip is
    what makes the two forms compare equal, rather than the equality being
    an artifact of `_resolve_path` stripping both operands down to an
    already-identical string before `realpath` ever runs on them (Review:
    code-reviewer a19c32b7, Finding 3 -- the prior version of this test
    could not distinguish those two explanations, since the strip always
    ran on both operands before comparison).

    On a POSIX test host, `os.path.realpath` does not recognize a
    backslash-prefixed Windows string as absolute and joins it onto `cwd`
    instead (see `_resolve_path`'s own docstring) -- which moves the
    extended-length marker off the string's HEAD, past where
    `casefold_path`'s own post-resolve strip can still find it (that strip
    only matches a prefix at the string's start). Bypassing ONLY the
    pre-`realpath` strip (leaving `casefold_path`'s own strip in place,
    unmocked, and `os.path.realpath` itself unmocked) reproduces the exact
    asymmetric-realpath failure shape AC11 exists to close: the prefixed and
    bare forms resolve to genuinely DIFFERENT strings absent the fix.

    Native-Windows skip: see the `skipif` reason above -- `ntpath.realpath`
    treats both operands as already-absolute regardless of the pre-strip,
    so this mechanism cannot be driven to diverge on this platform."""
    bare_drive_path = _SYNTHETIC_DRIVE_LETTER + "\\Users\\Foo\\Bar"
    prefixed_drive_path = "\\\\?\\" + bare_drive_path

    monkeypatch.setattr(applicability, "strip_extended_length_prefix", lambda p: p)

    resolved_bare = applicability._resolve_path(bare_drive_path)
    resolved_prefixed = applicability._resolve_path(prefixed_drive_path)

    assert resolved_bare != resolved_prefixed


def test_ac11_extended_length_prefix_strip_helper_is_a_pure_string_operation():
    """Unit-level companion to the resolver-level test above: the stripping
    helper itself, isolated from `os.path.realpath`/`casefold_path`."""
    bare_drive_path = _SYNTHETIC_DRIVE_LETTER + "\\Users\\Foo"
    assert (
        applicability._strip_windows_extended_length_prefix("\\\\?\\" + bare_drive_path)
        == bare_drive_path
    )
    assert (
        applicability._strip_windows_extended_length_prefix("\\\\?\\UNC\\server\\share")
        == "\\\\server\\share"
    )
    assert (
        applicability._strip_windows_extended_length_prefix("/already/posix/style")
        == "/already/posix/style"
    )


# ---------------------------------------------------------------------------
# `is_agent_memory_store_path` -- the memory-store false-positive fix. See
# module docstring's own reuse note: this predicate resolves the SAME
# `<home>/.claude/projects` root `guard_memory_store_cap._guarded_project_
# roots()` already carries, rather than a second, independently-derived
# definition of the same path shape.
# ---------------------------------------------------------------------------


def _isolate_home_only(monkeypatch, home_dir) -> None:
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home_dir))


def test_memory_store_file_path_matches(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    memory_dir = home / ".claude" / "projects" / "-some-slug" / "memory"
    memory_dir.mkdir(parents=True)
    target = memory_dir / "note.md"

    assert applicability.is_agent_memory_store_path(str(target)) is True


def test_memory_store_index_file_matches(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    memory_dir = home / ".claude" / "projects" / "-some-slug" / "memory"
    memory_dir.mkdir(parents=True)
    target = memory_dir / "MEMORY.md"

    assert applicability.is_agent_memory_store_path(str(target)) is True


def test_memory_store_any_project_slug_matches(tmp_path, monkeypatch):
    """AC -- matches for ANY project slug, not just one hardcoded project."""
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    for slug in ("-Users-a-project-one", "-Users-b-project-two"):
        memory_dir = home / ".claude" / "projects" / slug / "memory"
        memory_dir.mkdir(parents=True)
        target = memory_dir / "note.md"
        assert applicability.is_agent_memory_store_path(str(target)) is True


def test_project_dir_not_under_memory_does_not_match(tmp_path, monkeypatch):
    """Scoped to `memory/` only -- a sibling path under the same project
    slug (e.g. a doctrine or settings surface) must NOT match."""
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    project_dir = home / ".claude" / "projects" / "-some-slug"
    project_dir.mkdir(parents=True)
    target = project_dir / "not-memory.md"

    assert applicability.is_agent_memory_store_path(str(target)) is False


def test_claude_home_settings_json_does_not_match(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    (home / ".claude").mkdir(parents=True)
    target = home / ".claude" / "settings.json"

    assert applicability.is_agent_memory_store_path(str(target)) is False


def test_memory_store_case_insensitive_directory_matches(tmp_path, monkeypatch):
    """Case-fold parity with `guard_memory_store_cap.py`'s own case-bypass
    fix -- a case-varied `Memory/` directory segment still matches."""
    home = tmp_path / "home"
    home.mkdir()
    _isolate_home_only(monkeypatch, home)
    memory_dir = home / ".claude" / "projects" / "-some-slug" / "Memory"
    memory_dir.mkdir(parents=True)
    target = memory_dir / "Note.MD"

    assert applicability.is_agent_memory_store_path(str(target)) is True


def test_memory_store_path_empty_string_does_not_match():
    assert applicability.is_agent_memory_store_path("") is False
