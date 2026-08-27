"""
coordinator_core.test_cockpit_contract_freshness — co-located pytest for
coordinator_core.workday_complete.cockpit_contract_freshness, the
`gates.cockpit_contract_freshness` OBSERVABILITY-row probe `workday_complete
.brief` emits on EVERY run in EVERY repo.

Covers:
  - unresolvable DoE root -> UNKNOWN, ZERO subprocess/ls-remote calls (the
    fleet-wide cost guard: this is the common consumer-machine path and must
    never shell out).
  - ls-remote timeout -> UNKNOWN, no raise.
  - an annotated-tag (tag-object) ls-remote SHA peels to a commit before the
    ancestry test runs.
  - all four verdicts: FRESH, STALE, DIVERGED, UNKNOWN.
  - an unexpected internal exception anywhere in the probe -> UNKNOWN,
    compute_cockpit_contract_freshness() still returns normally (never
    raises), and workday_complete.brief() still returns its envelope.

No network: every subprocess/git seam is monkeypatched at the module-function
level (`_resolve_doe_root_local`, `_ls_remote_release_tag`, `_peel_to_commit`,
`_candidate_sha`, `_contract_version_at`, `_is_ancestor`) rather than exercised
against a real DoE clone or origin.

Spec backlink: DoE-claude ceremony Step 10 Final Summary line (2026-07-25 ask)

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_cockpit_contract_freshness.py -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workday_complete.brief as workday_brief
import coordinator_core.workday_complete.cockpit_contract_freshness as ccf

# Real-git spawn is load-bearing: only the NETWORK-facing seams (ls-remote et
# al.) are monkeypatched per the module docstring above — the ancestry-walk
# and annotated-tag-peeling tests build and read ACTUAL local git repos/tags
# (_is_ancestor, _peel_to_commit real behaviour) that no mock stands in for.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FAKE_ROOT = Path("/fake/doe-root")


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_head_sha(cwd):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_real_repo(tmp_path):
    """A throwaway real git repo (tmp_path-based) — used by the Finding-3
    real-git tests below to exercise the actual subprocess/git-syntax layer
    of the four low-level helpers, which every other test in this file
    monkeypatches as whole-function stubs (see module docstring)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], cwd=repo)
    _run_git(["config", "user.email", "test@example.com"], cwd=repo)
    _run_git(["config", "user.name", "Test"], cwd=repo)
    return repo


@pytest.fixture(autouse=True)
def _assume_doe_clone_is_readable(monkeypatch):
    """Neutralize the DoE-clone readability gate for every test that stubs the
    git seams rather than materialising a real clone.

    `_compute` gates the whole probe on `_doe_clone_unusable_reason` so that an
    unreachable — or environment-retargeted — DoE clone degrades to UNKNOWN
    instead of emitting a confident FRESH/STALE/DIVERGED computed against the
    LOCAL repo (see the module's git-scoping negative-spec). Every test here
    points `_resolve_doe_root_local` at the synthetic `_FAKE_ROOT`, which is
    correctly unreadable, so without this the gate short-circuits all of them
    before the verdict logic they exist to exercise ever runs.

    The gate itself is covered explicitly by
    `test_unreadable_doe_clone_is_unknown_and_never_a_diverged_claim` and by
    `coordinator_core/tests/test_git_scope.py`, which opt out via
    `_assume_doe_clone_is_readable`-free direct calls.
    """
    monkeypatch.setattr(ccf, "_doe_clone_unusable_reason", lambda _root: None)


def test_unreadable_doe_clone_is_unknown_and_never_a_diverged_claim(monkeypatch, tmp_path):
    """A DoE clone that cannot be read AS a git repository is INDETERMINATE.

    Regression for the 2026-08-03 class: `git -C <doe_root>` scopes only the
    working directory, so an inherited GIT_DIR (git exports one to every hook it
    runs) silently retargets every hop below at the LOCAL repo while the emitted
    entry still names DoE's path. Unguarded, that path emits DIVERGED — "a human
    must reconcile" — about a history this process never read. Could-not-check
    must be its own outcome and must never be phrased as staleness or
    divergence.
    """
    not_a_repo = tmp_path / "not-a-doe-clone"
    not_a_repo.mkdir()
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: not_a_repo)
    monkeypatch.setattr(ccf, "_doe_clone_unusable_reason", ccf.foreign_repo_unusable_reason)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "no git hop may run against a DoE clone that failed the readability "
            "gate — every answer would be about the wrong repository"
        )

    monkeypatch.setattr(ccf, "_ls_remote_release_tag", _must_not_run)
    monkeypatch.setattr(ccf, "_candidate_sha", _must_not_run)
    monkeypatch.setattr(ccf, "_is_ancestor", _must_not_run)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert entry["verdict"] not in ("STALE", "DIVERGED")
    assert "could not be read as a git repository" in entry["reason"]
    assert "NOT a claim" in entry["reason"], (
        "the third state must disclaim itself explicitly, or a reader takes it "
        "as a finding about DoE"
    )


def test_git_dir_poison_does_not_produce_a_verdict_about_the_wrong_repo(
    monkeypatch, tmp_path
):
    """End-to-end poisoned-environment regression.

    A real repo standing in for the DoE clone, plus a real repo standing in for
    ours, plus GIT_DIR pointing at ours: the probe must still read DoE's own
    object database. Before the fix, `_candidate_sha` and `_is_ancestor`
    answered from the poisoned target and the emitted entry named DoE.
    """
    doe = _init_real_repo(tmp_path)
    (doe / "coordinator" / "cockpit-contract" / "schema").mkdir(parents=True)
    schema = doe / ccf._SCHEMA_FILE_RELPATH
    schema.write_text('{"version": "9.9.9"}\n', encoding="utf-8")
    _run_git(["add", "-A"], cwd=doe)
    _run_git(["commit", "-qm", "schema"], cwd=doe)
    doe_head = _git_head_sha(doe)

    other = tmp_path / "other"
    other.mkdir()
    _run_git(["init", "-q"], cwd=other)
    _run_git(["config", "user.email", "test@example.com"], cwd=other)
    _run_git(["config", "user.name", "Test"], cwd=other)
    (other / "f.txt").write_text("x\n", encoding="utf-8")
    _run_git(["add", "-A"], cwd=other)
    _run_git(["commit", "-qm", "other"], cwd=other)

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: doe)
    monkeypatch.setattr(ccf, "_doe_clone_unusable_reason", ccf.foreign_repo_unusable_reason)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda _root: doe_head)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "FRESH", (
        "the poisoned GIT_DIR retargeted the probe at the wrong repository "
        f"— got {entry['verdict']}: {entry['reason']}"
    )
    assert entry["candidate"]["sha"] == doe_head
    assert entry["candidate"]["contract_version"] == "9.9.9", (
        "the contract version was read out of the wrong repository's history"
    )


def test_unresolvable_root_is_unknown_and_makes_zero_network_calls(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: None)

    calls = []

    def _spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("ls-remote seam must never be invoked when DoE root is unresolvable")

    monkeypatch.setattr(ccf, "_ls_remote_release_tag", _spy)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "no DoE clone" in entry["reason"]
    assert entry["remediation"] is None
    assert entry["published"] == {"ref": ccf._RELEASE_REF, "peel": None, "contract_version": None}
    assert entry["candidate"] == {
        "sha": None,
        "contract_version": None,
        "resolved_from_ref": None,
    }
    assert calls == []


def test_ls_remote_timeout_is_unknown_not_raised(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)

    # _ls_remote_release_tag itself catches TimeoutExpired internally; exercise
    # that real behavior by monkeypatching subprocess.run underneath it instead
    # of the function itself, so the timeout-handling code path is covered.
    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "git", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ccf.subprocess, "run", _fake_run)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "timed out" in entry["reason"]


def test_annotated_tag_sha_is_peeled_before_ancestry_test(monkeypatch):
    """ls-remote returns a tag-OBJECT sha for an annotated tag; the probe must
    peel it to a commit sha before running merge-base --is-ancestor, else
    merge-base would receive the tag-object sha (exit 128 in real git)."""
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda root: "TAGOBJSHA")
    monkeypatch.setattr(ccf, "_peel_to_commit", lambda root, sha: "PEELEDCOMMITSHA")
    monkeypatch.setattr(ccf, "_candidate_sha", lambda root: "PEELEDCOMMITSHA")
    monkeypatch.setattr(ccf, "_contract_version_at", lambda root, sha: "2.5.0")

    ancestry_calls = []

    def _is_ancestor(root, ancestor_sha, descendant_sha):
        ancestry_calls.append((ancestor_sha, descendant_sha))
        return True

    monkeypatch.setattr(ccf, "_is_ancestor", _is_ancestor)

    entry = ccf.compute_cockpit_contract_freshness()

    # candidate == peeled commit sha -> FRESH, and the ancestry test is never
    # reached on the equality fast path, so no tag-object sha ever reaches it.
    assert entry["verdict"] == "FRESH"
    assert entry["published"]["peel"] == "PEELEDCOMMITSHA"
    assert ancestry_calls == []


def test_verdict_fresh(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda root: "TAGSHA")
    monkeypatch.setattr(ccf, "_peel_to_commit", lambda root, sha: "SHA_A")
    monkeypatch.setattr(ccf, "_candidate_sha", lambda root: "SHA_A")
    monkeypatch.setattr(ccf, "_contract_version_at", lambda root, sha: "2.5.0")
    monkeypatch.setattr(
        ccf, "_is_ancestor", lambda *a: pytest.fail("ancestry test unreachable on SHA-equal path")
    )

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "FRESH"
    assert entry["remediation"] is None
    assert entry["published"]["peel"] == "SHA_A"
    assert entry["candidate"]["sha"] == "SHA_A"
    assert entry["published"]["contract_version"] == "2.5.0"
    assert entry["candidate"]["contract_version"] == "2.5.0"
    assert entry["reason"]


def test_verdict_stale(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda root: "TAGSHA")
    monkeypatch.setattr(ccf, "_peel_to_commit", lambda root, sha: "SHA_PUBLISHED")
    monkeypatch.setattr(ccf, "_candidate_sha", lambda root: "SHA_NEWER")
    monkeypatch.setattr(
        ccf, "_contract_version_at", lambda root, sha: "2.5.0" if sha == "SHA_PUBLISHED" else "2.6.0"
    )
    monkeypatch.setattr(ccf, "_is_ancestor", lambda root, ancestor, descendant: True)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "STALE"
    assert entry["remediation"] is not None
    assert "publish" in entry["remediation"]
    assert entry["published"]["peel"] == "SHA_PUBLISHED"
    assert entry["candidate"]["sha"] == "SHA_NEWER"


def test_verdict_diverged_when_not_a_strict_descendant(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda root: "TAGSHA")
    monkeypatch.setattr(ccf, "_peel_to_commit", lambda root, sha: "SHA_PUBLISHED")
    monkeypatch.setattr(ccf, "_candidate_sha", lambda root: "SHA_BEHIND_OR_UNRELATED")
    monkeypatch.setattr(ccf, "_contract_version_at", lambda root, sha: None)
    monkeypatch.setattr(ccf, "_is_ancestor", lambda root, ancestor, descendant: False)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "DIVERGED"
    assert entry["remediation"] is not None
    assert "reconcile" in entry["remediation"]


def test_verdict_diverged_when_ancestry_indeterminate(monkeypatch):
    """merge-base returning an indeterminate result (git error, missing
    objects) must degrade to DIVERGED (human required), never crash."""
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)
    monkeypatch.setattr(ccf, "_ls_remote_release_tag", lambda root: "TAGSHA")
    monkeypatch.setattr(ccf, "_peel_to_commit", lambda root, sha: "SHA_PUBLISHED")
    monkeypatch.setattr(ccf, "_candidate_sha", lambda root: "SHA_OTHER")
    monkeypatch.setattr(ccf, "_contract_version_at", lambda root, sha: None)
    monkeypatch.setattr(ccf, "_is_ancestor", lambda root, ancestor, descendant: None)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "DIVERGED"


def test_ref_absent_on_origin_is_unknown(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)

    def _absent(root):
        raise ccf._FreshnessProbeError(f"'{ccf._RELEASE_REF}' is not published yet on the DoE origin")

    monkeypatch.setattr(ccf, "_ls_remote_release_tag", _absent)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "not published yet" in entry["reason"]


def test_unexpected_exception_anywhere_degrades_to_unknown_never_raises(monkeypatch):
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: _FAKE_ROOT)

    def _boom(root):
        raise RuntimeError("something truly unexpected")

    monkeypatch.setattr(ccf, "_ls_remote_release_tag", _boom)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "something truly unexpected" in entry["reason"]


def test_env_root_ladder_nonexistent_doe_root_fails_hard_registry_never_consulted(monkeypatch, tmp_path):
    """An explicit DOE_ROOT that does not exist is a directive, not a hint —
    resolution fails (UNKNOWN) rather than falling through to REPO_DOE_CLAUDE
    or the machine-local registry."""
    bad_path = str(tmp_path / "does-not-exist")
    monkeypatch.setenv("DOE_ROOT", bad_path)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    def _registry_spy():
        raise AssertionError("registry must never be consulted when DOE_ROOT is set but invalid")

    monkeypatch.setattr(ccf.doe_drift, "resolve_doe_clone", _registry_spy)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "DOE_ROOT" in entry["reason"]
    assert bad_path in entry["reason"]


def test_env_root_ladder_doe_root_valid_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DOE_ROOT", str(tmp_path))
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    resolved = ccf._resolve_doe_root_local()

    assert resolved == tmp_path


def test_env_root_ladder_doe_root_unset_repo_doe_claude_nonexistent_fails_hard(monkeypatch, tmp_path):
    bad_path = str(tmp_path / "also-does-not-exist")
    monkeypatch.delenv("DOE_ROOT", raising=False)
    monkeypatch.setenv("REPO_DOE_CLAUDE", bad_path)

    def _registry_spy():
        raise AssertionError("registry must never be consulted when REPO_DOE_CLAUDE is set but invalid")

    monkeypatch.setattr(ccf.doe_drift, "resolve_doe_clone", _registry_spy)

    entry = ccf.compute_cockpit_contract_freshness()

    assert entry["verdict"] == "UNKNOWN"
    assert "REPO_DOE_CLAUDE" in entry["reason"]
    assert bad_path in entry["reason"]


def test_env_root_ladder_empty_doe_root_is_treated_as_unset(monkeypatch, tmp_path):
    """An empty-string DOE_ROOT is UNSET, not a directive — REPO_DOE_CLAUDE
    still gets to win."""
    monkeypatch.setenv("DOE_ROOT", "")
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path))

    resolved = ccf._resolve_doe_root_local()

    assert resolved == tmp_path


def test_env_root_ladder_both_unset_consults_registry(monkeypatch, tmp_path):
    monkeypatch.delenv("DOE_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    calls = []

    def _registry():
        calls.append(True)
        return tmp_path

    monkeypatch.setattr(ccf.doe_drift, "resolve_doe_clone", _registry)

    resolved = ccf._resolve_doe_root_local()

    assert resolved == tmp_path
    assert calls == [True]


def test_brief_carries_the_gate_and_never_raises(monkeypatch):
    """workday_complete.brief() itself must return normally with the gate
    entry present under the common zero-DoE-clone path (the never-raise
    contract for internal probe failures is proven directly against
    compute_cockpit_contract_freshness() above)."""
    monkeypatch.setattr(workday_brief, "resolve_operator_config", lambda **_: {})
    monkeypatch.setattr(ccf, "_resolve_doe_root_local", lambda: None)

    exit_code, envelope = workday_brief.brief()

    assert exit_code == int(workday_brief.WorkdayExitCode.SUCCESS)
    gate = envelope["gates"]["cockpit_contract_freshness"]
    assert gate["verdict"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Review: code-reviewer (F3) — real-git coverage of the four low-level
# helpers. Every test above monkeypatches `_peel_to_commit`, `_candidate_sha`,
# `_contract_version_at`, and `_is_ancestor` as whole-function stubs, so the
# actual subprocess/git-command syntax and output parsing of those helpers
# is never exercised. These tests run each helper against a real throwaway
# git repo (tmp_path `git init` + real commits + a real annotated tag) —
# additive coverage of the layer the monkeypatched tests above skip. No
# network: no remote/origin is involved.
# ---------------------------------------------------------------------------

def test_peel_to_commit_real_git_lightweight_tag_is_a_no_op(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)
    sha = _git_head_sha(repo)
    _run_git(["tag", "lightweight"], cwd=repo)

    assert ccf._peel_to_commit(repo, sha) == sha


def test_peel_to_commit_real_git_annotated_tag_peels_to_underlying_commit(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)
    commit_sha = _git_head_sha(repo)
    _run_git(["tag", "-a", "annotated", "-m", "release"], cwd=repo)
    tag_obj_sha = subprocess.run(
        ["git", "rev-parse", "annotated"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tag_obj_sha != commit_sha  # sanity: annotated tags are their own object

    assert ccf._peel_to_commit(repo, tag_obj_sha) == commit_sha


def test_peel_to_commit_real_git_object_not_in_local_db_falls_back_to_raw_sha(tmp_path):
    """A sha that git rev-parse cannot resolve locally (simulating an
    unfetched tag object per Finding 2) falls back to the raw sha, mirroring
    doe_drift.probe_freshness_ref's documented graceful fallback."""
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)

    unresolvable_sha = "deadbeef" * 5  # 40 hex chars, not an object in this repo

    assert ccf._peel_to_commit(repo, unresolvable_sha) == unresolvable_sha


def test_candidate_sha_real_git_finds_newest_commit_touching_schema_dir(tmp_path):
    repo = _init_real_repo(tmp_path)
    schema_dir = repo / ccf._SCHEMA_DIR_RELPATH
    schema_dir.mkdir(parents=True)

    (repo / "unrelated.txt").write_text("x")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "unrelated"], cwd=repo)

    (schema_dir / "cockpit-contract.schema.json").write_text('{"version": "1.0.0"}')
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "schema change"], cwd=repo)
    schema_sha = _git_head_sha(repo)

    (repo / "unrelated2.txt").write_text("y")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "unrelated again"], cwd=repo)

    assert ccf._candidate_sha(repo) == schema_sha


def test_candidate_sha_real_git_no_commit_touching_schema_dir_raises(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "unrelated.txt").write_text("x")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "unrelated"], cwd=repo)

    with pytest.raises(ccf._FreshnessProbeError, match="no commit found"):
        ccf._candidate_sha(repo)


def test_contract_version_at_real_git_reads_version_from_schema_json(tmp_path):
    repo = _init_real_repo(tmp_path)
    schema_dir = repo / ccf._SCHEMA_DIR_RELPATH
    schema_dir.mkdir(parents=True)
    (schema_dir / "cockpit-contract.schema.json").write_text('{"version": "3.3.0"}')
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "schema"], cwd=repo)
    sha = _git_head_sha(repo)

    assert ccf._contract_version_at(repo, sha) == "3.3.0"


def test_contract_version_at_real_git_invalid_json_raises(tmp_path):
    repo = _init_real_repo(tmp_path)
    schema_dir = repo / ccf._SCHEMA_DIR_RELPATH
    schema_dir.mkdir(parents=True)
    (schema_dir / "cockpit-contract.schema.json").write_text("not valid json")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "bad schema"], cwd=repo)
    sha = _git_head_sha(repo)

    with pytest.raises(ccf._FreshnessProbeError, match="not valid JSON"):
        ccf._contract_version_at(repo, sha)


def test_is_ancestor_real_git_true_and_false_cases(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)
    sha1 = _git_head_sha(repo)

    (repo / "f.txt").write_text("b")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c2"], cwd=repo)
    sha2 = _git_head_sha(repo)

    assert ccf._is_ancestor(repo, sha1, sha2) is True
    assert ccf._is_ancestor(repo, sha2, sha1) is False


def test_current_ref_label_real_git_names_the_checked_out_branch(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)
    _run_git(["checkout", "-q", "-b", "some-feature-branch"], cwd=repo)

    assert ccf._current_ref_label(repo) == "some-feature-branch"


def test_current_ref_label_real_git_detached_head_reports_detached(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "f.txt").write_text("a")
    _run_git(["add", "."], cwd=repo)
    _run_git(["commit", "-q", "-m", "c1"], cwd=repo)
    sha = _git_head_sha(repo)
    _run_git(["checkout", "-q", sha], cwd=repo)

    assert ccf._current_ref_label(repo) == "HEAD (detached)"
