"""
coordinator_core.tests.test_sibling_fact — edge-path coverage for
`sibling_fact.resolve_leg`'s honest-indeterminate observation contract (C2).

Coverage (the Staff Engineer F10 — authored in the C2 chunk itself, not deferred):
  (a) return-shape assertion: NO verdict/resolved/freed field, ever.
  (b) unregistered repo -> read_ok False, explicit reason, never a guess.
  (c) registered repo whose root is absent from disk ("absent clone") ->
      read_ok False, distinguished from "unregistered".
  (d) `doe_claude` routes through `read_doe_root_pointer`, every other repo id
      through bare `registry_get`.
  (e) `frontmatter_field`: absent field, unreadable file, literal null, empty
      value, and a real value — five distinguishable outcomes, not one `""`.
  (f) `commit_ancestor`: true case, false case (real, asked-and-answered
      negative), and unreachable-SHA / git-not-on-PATH indeterminate cases.
  (g) `file_exists`: present and absent, both a genuine (not indeterminate)
      observation once the clone root itself resolves.
  (h) unsupported kind and missing required fields raise ValueError — caller
      bugs are not laundered into a silent indeterminate observation.
  (i) `MACHINE_LOCAL_REPOS_<ID>` env override points a leg at a tmp_path repo.
  (j) AC7 regression (C8): a fake sibling whose cached frontmatter prose says
      `status: draft` while its own commit history proves the work shipped --
      the 2026-07-08 pathology, reproduced against a fixture, never a copied
      literal.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C2, § C8
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import sibling_fact
from coordinator_core.sibling_fact import resolve_leg
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: the `commit_ancestor` leg (f) resolves real merge-base
# ancestry, including the "git not on PATH"/unreachable-SHA indeterminate cases, which
# no mock reproduces. Tests build/mutate their own repo per test (distinct commit
# histories per scenario, including AC7's fabricated-vs-real-history fixture).
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Git repo helper (mirrors coordinator_core/tests/test_git_ancestry.py convention)
# ---------------------------------------------------------------------------


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
        **no_console_creationflags(),
    )


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with two commits: `first` -> `second`."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _make_commit(root, "first")
    _make_commit(root, "second")
    return root


@pytest.fixture
def registered_repo(monkeypatch: pytest.MonkeyPatch, repo: Path) -> Path:
    """Point `repos.fixture-repo` at `repo` via the sanctioned env-override
    test hook (`MACHINE_LOCAL_REPOS_<ID>`), never a registry file write."""
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_FIXTURE-REPO", str(repo))
    return repo


# ---------------------------------------------------------------------------
# (a) Return-shape negative spec
# ---------------------------------------------------------------------------


def test_return_shape_has_no_verdict_field(registered_repo: Path) -> None:
    """The observation dict is EXACTLY {leg_id, read_ok, observed, source,
    error} — no `resolved`/`verdict`/`freed`/`still-blocked` key of any kind,
    for any leg kind, ever (D1/eng-director F2)."""
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "fixture-repo", "path": "README.md"}
    observation = resolve_leg(leg)
    assert set(observation.keys()) == {"leg_id", "read_ok", "observed", "source", "error"}
    for forbidden in ("resolved", "verdict", "freed", "still_blocked", "still-blocked"):
        assert forbidden not in observation


# ---------------------------------------------------------------------------
# (b)/(c) Repo root resolution — unregistered vs. absent-clone
# ---------------------------------------------------------------------------


def test_unregistered_repo_is_read_ok_false_with_explicit_reason() -> None:
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "definitely-not-a-registered-repo-id", "path": "x"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is False
    assert observation["observed"] is None
    assert "unregistered" in observation["error"]


def test_registered_but_absent_clone_is_read_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A registry entry pointing at a path that does not exist on THIS disk
    ("the clone was never cloned here") is distinguished from an
    unregistered repo — both are read_ok False, but for a different reason."""
    missing = tmp_path / "never-cloned"
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_STALE-REPO", str(missing))
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "stale-repo", "path": "x"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is False
    assert "does not exist on disk" in observation["error"]


def test_doe_claude_routes_through_doe_root_pointer(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """`repo: doe_claude` MUST resolve via `read_doe_root_pointer`, not bare
    `registry_get` (D6/eng-director F6) — asserted by making the two
    resolvers disagree and checking which one wins."""
    monkeypatch.setattr(sibling_fact, "read_doe_root_pointer", lambda: str(repo))
    monkeypatch.setattr(sibling_fact, "registry_get", lambda key: None)
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "doe_claude", "path": "README.md"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["source"].startswith(str(repo))


def test_non_doe_repo_never_calls_doe_root_pointer(monkeypatch: pytest.MonkeyPatch, registered_repo: Path) -> None:
    def _fail() -> str:
        raise AssertionError("read_doe_root_pointer must not be called for a non-doe_claude repo id")

    monkeypatch.setattr(sibling_fact, "read_doe_root_pointer", _fail)
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "fixture-repo", "path": "README.md"}
    resolve_leg(leg)  # must not raise


# ---------------------------------------------------------------------------
# (g) file_exists
# ---------------------------------------------------------------------------


def test_file_exists_true_when_present(registered_repo: Path) -> None:
    (registered_repo / "present.txt").write_text("x", encoding="utf-8")
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "fixture-repo", "path": "present.txt"}
    observation = resolve_leg(leg)
    assert observation == {
        "leg_id": "L1",
        "read_ok": True,
        "observed": True,
        "source": str(registered_repo / "present.txt"),
        "error": None,
    }


def test_file_exists_false_when_absent(registered_repo: Path) -> None:
    leg = {"leg_id": "L1", "kind": "file_exists", "repo": "fixture-repo", "path": "nope.txt"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is False
    assert observation["error"] is None


# ---------------------------------------------------------------------------
# AC11 (C6) — archive fallback for path-keyed legs. A leg whose target moved
# to archive/handoffs/ (month-nested, per handoff_archive_dest) still
# resolves rather than manufacturing a permanent false negative; `source`
# says which path answered. See docs/plans/2026-08-18-auto-reconcile-must-
# fire.md § C6.
# ---------------------------------------------------------------------------


def test_file_exists_resolves_via_archive_when_moved(registered_repo: Path) -> None:
    archived_dir = registered_repo / "archive" / "handoffs" / "2026-08"
    archived_dir.mkdir(parents=True)
    archived_file = archived_dir / "shipped.md"
    archived_file.write_text("stub_id: sat-04\n", encoding="utf-8")

    leg = {
        "leg_id": "L1",
        "kind": "file_exists",
        "repo": "fixture-repo",
        "path": "state/handoffs/shipped.md",
    }
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is True
    assert observation["error"] is None
    # `source` names the archive path that actually answered, not the
    # literal (now-nonexistent) path the leg named -- the only way a
    # reader can see the fallback fired.
    assert observation["source"] == str(archived_file)


def test_file_exists_live_wins_over_archived_namesake(registered_repo: Path) -> None:
    """A live file at the literal path always wins over an archived
    same-basename file, mirroring gate_eval's live-then-archive ordering."""
    live_dir = registered_repo / "state" / "handoffs"
    live_dir.mkdir(parents=True)
    (live_dir / "shipped.md").write_text("stub_id: sat-04\n", encoding="utf-8")
    archived_dir = registered_repo / "archive" / "handoffs" / "2026-08"
    archived_dir.mkdir(parents=True)
    (archived_dir / "shipped.md").write_text("stub_id: stale-copy\n", encoding="utf-8")

    leg = {
        "leg_id": "L1",
        "kind": "file_exists",
        "repo": "fixture-repo",
        "path": "state/handoffs/shipped.md",
    }
    observation = resolve_leg(leg)
    assert observation["observed"] is True
    assert observation["source"] == str(live_dir / "shipped.md")


def test_file_exists_ambiguous_archive_match_is_not_guessed_at(registered_repo: Path) -> None:
    """Two same-basename files under archive/handoffs/ is an ambiguous
    fallback -- treated exactly like no match, never a silent pick."""
    for month in ("2026-07", "2026-08"):
        month_dir = registered_repo / "archive" / "handoffs" / month
        month_dir.mkdir(parents=True)
        (month_dir / "shipped.md").write_text("stub_id: sat-04\n", encoding="utf-8")

    leg = {
        "leg_id": "L1",
        "kind": "file_exists",
        "repo": "fixture-repo",
        "path": "state/handoffs/shipped.md",
    }
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is False


def test_reasoned_fallback_refuses_a_non_bare_basename(registered_repo: Path) -> None:
    """`_resolve_archive_handoffs_fallback_reasoned` is the single, hardened
    definition every caller (this module's own two path-keyed legs, and
    quick_wrap_assemble's `_c3_ancestry`) now reads from. A basename
    carrying a traversal segment is refused before any filesystem access,
    never resolved -- promoted verbatim from quick_wrap_assemble's own
    predecessor guard (2026-09-03 consolidation)."""
    path, reason = sibling_fact._resolve_archive_handoffs_fallback_reasoned(
        registered_repo, "../escape.md"
    )
    assert path is None
    assert "not a bare filename" in reason


def test_reasoned_fallback_refuses_an_empty_basename(registered_repo: Path) -> None:
    path, reason = sibling_fact._resolve_archive_handoffs_fallback_reasoned(registered_repo, "")
    assert path is None
    assert "not a bare filename" in reason


def test_reasoned_fallback_reports_absent_subtree(registered_repo: Path) -> None:
    path, reason = sibling_fact._resolve_archive_handoffs_fallback_reasoned(
        registered_repo, "shipped.md"
    )
    assert path is None
    assert reason == "archive/handoffs subtree absent"


def test_file_exists_archive_match_that_is_a_directory_never_resolves(registered_repo: Path) -> None:
    """Hardening promoted from quick_wrap_assemble's own probe (2026-09-03):
    an `rglob` match that is itself a DIRECTORY (not a file) named the same
    as the target basename must never resolve as the archived record — the
    `is_file()` filter this module's `_resolve_archive_handoffs_fallback_
    reasoned` now applies on every match. Strictly narrower than the
    predecessor (which had no such filter): a directory match now reads as
    "no match" rather than a false positive, never the reverse."""
    archived_dir = registered_repo / "archive" / "handoffs" / "2026-08"
    archived_dir.mkdir(parents=True)
    # A DIRECTORY named exactly like the target basename — not the file.
    (archived_dir / "shipped.md").mkdir()

    leg = {
        "leg_id": "L1",
        "kind": "file_exists",
        "repo": "fixture-repo",
        "path": "state/handoffs/shipped.md",
    }
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is False


def test_frontmatter_field_resolves_via_archive_when_moved(registered_repo: Path) -> None:
    archived_dir = registered_repo / "archive" / "handoffs" / "2026-08"
    archived_dir.mkdir(parents=True)
    archived_file = archived_dir / "shipped.md"
    archived_file.write_text("deployment_state: shipped\n", encoding="utf-8")

    leg = {
        "leg_id": "L1",
        "kind": "frontmatter_field",
        "repo": "fixture-repo",
        "path": "state/handoffs/shipped.md",
        "field": "deployment_state",
    }
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] == "shipped"
    assert observation["error"] is None
    assert observation["source"] == str(archived_file)


# ---------------------------------------------------------------------------
# (e) frontmatter_field
# ---------------------------------------------------------------------------


def test_frontmatter_field_present_with_value(registered_repo: Path) -> None:
    (registered_repo / "doc.md").write_text("status: fulfilled\nother: x\n", encoding="utf-8")
    leg = {"leg_id": "L1", "kind": "frontmatter_field", "repo": "fixture-repo", "path": "doc.md", "field": "status"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] == "fulfilled"
    assert observation["error"] is None


def test_frontmatter_field_absent(registered_repo: Path) -> None:
    (registered_repo / "doc.md").write_text("other: x\n", encoding="utf-8")
    leg = {"leg_id": "L1", "kind": "frontmatter_field", "repo": "fixture-repo", "path": "doc.md", "field": "status"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is None
    assert "absent" in observation["error"]


def test_frontmatter_field_literal_null(registered_repo: Path) -> None:
    (registered_repo / "doc.md").write_text("status: null\n", encoding="utf-8")
    leg = {"leg_id": "L1", "kind": "frontmatter_field", "repo": "fixture-repo", "path": "doc.md", "field": "status"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is None
    assert "literal null" in observation["error"]


def test_frontmatter_field_empty_value(registered_repo: Path) -> None:
    (registered_repo / "doc.md").write_text("status:\n", encoding="utf-8")
    leg = {"leg_id": "L1", "kind": "frontmatter_field", "repo": "fixture-repo", "path": "doc.md", "field": "status"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is None
    assert "present but empty" in observation["error"]


def test_frontmatter_field_unreadable_file(registered_repo: Path) -> None:
    """A path that does not resolve to a readable file (here: a directory,
    guaranteed cross-platform to raise on `.read_text()`) is `read_ok: False`,
    distinct from an absent field."""
    (registered_repo / "a-directory").mkdir()
    leg = {
        "leg_id": "L1",
        "kind": "frontmatter_field",
        "repo": "fixture-repo",
        "path": "a-directory",
        "field": "status",
    }
    observation = resolve_leg(leg)
    assert observation["read_ok"] is False
    assert observation["observed"] is None
    assert "unreadable" in observation["error"]


# ---------------------------------------------------------------------------
# (f) commit_ancestor
# ---------------------------------------------------------------------------


def test_commit_ancestor_true(registered_repo: Path) -> None:
    first = _git(["rev-list", "--max-parents=0", "HEAD"], registered_repo).stdout.strip()
    head = _git(["rev-parse", "HEAD"], registered_repo).stdout.strip()
    leg = {"leg_id": "L1", "kind": "commit_ancestor", "repo": "fixture-repo", "commit": first, "ref": head}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is True
    assert observation["error"] is None


def test_commit_ancestor_false_is_a_real_negative(registered_repo: Path) -> None:
    """`head` is not an ancestor of the root commit — a genuine, answered
    negative, NOT indeterminate."""
    first = _git(["rev-list", "--max-parents=0", "HEAD"], registered_repo).stdout.strip()
    head = _git(["rev-parse", "HEAD"], registered_repo).stdout.strip()
    leg = {"leg_id": "L1", "kind": "commit_ancestor", "repo": "fixture-repo", "commit": head, "ref": first}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is True
    assert observation["observed"] is False
    assert observation["error"] is None


def test_commit_ancestor_unreachable_sha_is_indeterminate(registered_repo: Path) -> None:
    head = _git(["rev-parse", "HEAD"], registered_repo).stdout.strip()
    bogus = "deadbeef" * 5
    leg = {"leg_id": "L1", "kind": "commit_ancestor", "repo": "fixture-repo", "commit": bogus, "ref": head}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is False
    assert observation["observed"] is None
    assert observation["error"] is not None


def test_commit_ancestor_git_not_on_path_is_indeterminate(monkeypatch: pytest.MonkeyPatch, registered_repo: Path) -> None:
    """Simulates `git` missing from PATH entirely — `FileNotFoundError` from
    the subprocess spawn must resolve to indeterminate, never a silent
    `False`-as-not-an-ancestor."""

    def _raise_missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _raise_missing_git)
    leg = {"leg_id": "L1", "kind": "commit_ancestor", "repo": "fixture-repo", "commit": "abc123", "ref": "def456"}
    observation = resolve_leg(leg)
    assert observation["read_ok"] is False
    assert observation["observed"] is None


# ---------------------------------------------------------------------------
# (h) fail-loud on caller bugs
# ---------------------------------------------------------------------------


def test_unsupported_kind_raises_value_error(registered_repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_leg({"leg_id": "L1", "kind": "human", "repo": "fixture-repo"})


def test_missing_leg_id_raises_value_error(registered_repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_leg({"kind": "file_exists", "repo": "fixture-repo", "path": "x"})


def test_file_exists_missing_path_raises_value_error(registered_repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_leg({"leg_id": "L1", "kind": "file_exists", "repo": "fixture-repo"})


def test_commit_ancestor_missing_ref_raises_value_error(registered_repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_leg({"leg_id": "L1", "kind": "commit_ancestor", "repo": "fixture-repo", "commit": "abc123"})


# ---------------------------------------------------------------------------
# AC7 regression (C8) — the 2026-07-08 case: a gate naming a sibling artifact
# whose real state contradicts the locally-cached prose resolves against the
# sibling, not the prose. See docs/plans/2026-07-26-structured-sibling-
# evidence-gates.md § C8/AC7.
# ---------------------------------------------------------------------------


def test_ac7_prose_says_draft_disk_proves_shipped(registered_repo: Path) -> None:
    """Reproduces the real 2026-07-08 shape against a synthetic fixture: a
    plan whose frontmatter reads `status: draft` while the sibling's own
    commit history shows the work already shipped.

    Two independent legs against the SAME sibling clone: the cached-prose leg
    (`frontmatter_field`) is read verbatim — `resolve_leg` never corrects,
    overrides, or launders it toward the disk leg's answer — while the
    disk-truth leg (`commit_ancestor`) is answered by a live git query made
    AFTER the prose was authored. The disk leg proves the ship happened even
    though the prose leg still reads draft, and neither leg's observation is
    derived from the other's — that independence is what "resolves against
    the sibling, not the prose" means at the `resolve_leg` layer (the verdict
    that PREFERS the disk leg over the prose leg is `gate_eval`'s job, C3,
    not this module's — see the module's negative spec).

    Asserted against the fixture's own commit, never a hardcoded literal
    (AC7's explicit anti-copied-literal requirement): `shipped_sha` comes
    from `_make_commit`, and the equality check against `HEAD` proves the
    assertion tracks the fixture rather than a value this test also invented.
    """
    (registered_repo / "plan.md").write_text("status: draft\n", encoding="utf-8")

    prose_leg = {
        "leg_id": "prose",
        "kind": "frontmatter_field",
        "repo": "fixture-repo",
        "path": "plan.md",
        "field": "status",
    }
    prose_before_ship = resolve_leg(prose_leg)
    assert prose_before_ship["read_ok"] is True
    assert prose_before_ship["observed"] == "draft"

    shipped_sha = _make_commit(registered_repo, "ship: plan actually landed")
    head = _git(["rev-parse", "HEAD"], registered_repo).stdout.strip()
    assert shipped_sha == head

    disk_leg = {
        "leg_id": "disk",
        "kind": "commit_ancestor",
        "repo": "fixture-repo",
        "commit": shipped_sha,
        "ref": head,
    }
    disk_observation = resolve_leg(disk_leg)
    assert disk_observation["read_ok"] is True
    assert disk_observation["observed"] is True
    assert disk_observation["error"] is None

    # Re-resolving the identical prose leg after the sibling shipped still
    # reads the file verbatim, unchanged -- resolve_leg is not a verdict
    # layer that reconciles the two answers (that's gate_eval's job); it
    # honestly reports what each artifact independently says, live, on
    # every call. The contradiction between the two legs is real and is
    # exactly the 2026-07-08 pathology: the cached prose is stale, the
    # sibling's disk is not.
    prose_after_ship = resolve_leg(prose_leg)
    assert prose_after_ship["observed"] == "draft"
    assert prose_after_ship["observed"] != disk_observation["observed"]


# NOTE (divergence, C8): the chunk brief also asks this file to cover the
# honest-indeterminate paths at the gate_evidence-leg level -- unregistered
# repo, absent clone, and kind: human. All three already exist in C2's own
# edge-path suite above (test_unregistered_repo_is_read_ok_false_with_explicit_reason,
# test_registered_but_absent_clone_is_read_ok_false, and
# test_unsupported_kind_raises_value_error, which exercises kind="human" --
# "human" is not a sibling_fact primitive kind (see module docstring), so it
# fail-louds exactly like any other unsupported kind rather than resolving a
# silent indeterminate here). Per the Staff Engineer F10 (recorded in C2's task body),
# C2 owns that edge-path coverage and C8 only adds the AC7 regression case;
# duplicating the same assertions here would violate this chunk's own
# instruction not to rewrite C2's existing cases.


def test_cold_import_does_not_wedge_the_op_registry():
    """Importing this primitive FIRST must not break op registration.

    `sibling_fact` reaches into `coordinator_core.ops.read_frontmatter_field`,
    and importing anything under `coordinator_core.ops.` runs that package's
    `__init__`, which eagerly imports ~159 op modules — among them
    `handoff_transition`, which imports `resolve_leg` from this module. Held at
    module scope that closes a cycle, and the failure is silent in the worst
    way: `handoff_transition` and `handoff_ship_archive` fail on a partially
    initialised `sibling_fact` and never register their ops, so dispatching one
    re-raises an ImportError instead of working.

    The whole in-process test suite passed while this was broken, because
    pytest's collection order imports `coordinator_core.ops` first and masks it.
    Only a genuinely cold interpreter reproduces it, hence the subprocess.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from coordinator_core.sibling_fact import resolve_leg\n"
            "from coordinator_core.ops import get_poisoned_modules\n"
            "poisoned = get_poisoned_modules()\n"
            "assert not poisoned, f'op modules failed to import: {sorted(poisoned)}'\n"
            "print('ok')",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "cold `import coordinator_core.sibling_fact` wedged the op registry.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "FAILED to import" not in proc.stderr, (
        "op modules failed to register on a cold sibling_fact import:\n"
        f"{proc.stderr}"
    )
