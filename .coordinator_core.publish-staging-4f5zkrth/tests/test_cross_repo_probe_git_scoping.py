"""
coordinator_core.tests.test_cross_repo_probe_git_scoping — one poisoned-environment
regression per surviving cross-repo git probe.

`coordinator_core/tests/test_git_scope.py` pins the shared seam. This file pins
the CALL SITES: each of the probes below targets a repository other than the
ambient one, and each of them, before 2026-08-03, would have answered from the
wrong repository and rendered the answer as a definite finding about the target.

Covered sites (one test each — the `-C` target named in parentheses):
  - `frontmatter.schema_validate.check_schema_drift`          (doe_repo_path)
  - `frontmatter.schema_validate.check_schema_drift_advisory` (doe_repo_path)
  - `ops.emit.doe_drift.probe_freshness_ref`                  (doe_clone)
  - `ops.emit.doe_drift._tag_is_ancestor_of_pin`              (doe_clone)
  - `engine_version.resolve_engine_sha`                       (engine_dir)

The shared shape of every test: build a real target repo, build a real DECOY
repo, point `GIT_DIR` at the decoy, and assert the probe still answers from the
target — or, where the target is genuinely unreadable, that it says so instead of
manufacturing a negative claim.

Run: python3 -m pytest coordinator_core/tests/test_cross_repo_probe_git_scoping.py -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

import coordinator_core.engine_version as engine_version
import coordinator_core.ops.emit.doe_drift as doe_drift
from coordinator_core.frontmatter.schema_validate import (
    SchemaDriftError,
    check_schema_drift,
    check_schema_drift_advisory,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SCHEMA_NAME = "handoff.schema.json"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False, timeout=30,
        stdin=subprocess.DEVNULL,
    )


def _seed_repo(root: Path, files: "dict[str, str]") -> str:
    """Materialise a real repo with one commit containing *files*; return HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "git scoping test")
    for relpath, body in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(autouse=True)
def _require_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")


@pytest.fixture
def decoy(tmp_path: Path) -> Path:
    """A real repo standing in for OURS — what a poisoned GIT_DIR retargets at.

    It deliberately carries the SAME paths the probes read out of the DoE clone,
    with different content. That is what makes the poisoning silent rather than
    merely noisy: an unscoped probe finds a file, reads it, and reports a
    confident finding about a repo it never opened.
    """
    root = tmp_path / "ours"
    _seed_repo(
        root,
        {
            f"coordinator/schemas/{_SCHEMA_NAME}": json.dumps({"title": "OURS"}) + "\n",
            "f.txt": "ours\n",
        },
    )
    return root


@pytest.fixture
def doe(tmp_path: Path) -> Path:
    """A real repo shaped like a DoE clone."""
    root = tmp_path / "DoE-fake"
    _seed_repo(
        root,
        {
            f"coordinator/schemas/{_SCHEMA_NAME}": json.dumps({"title": "DOE"}) + "\n",
            "f.txt": "doe\n",
        },
    )
    return root


@pytest.fixture
def vendored(tmp_path: Path, doe: Path) -> Path:
    """A vendored schema copy byte-identical to the DoE clone's HEAD."""
    path = tmp_path / "vendored" / _SCHEMA_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (doe / "coordinator" / "schemas" / _SCHEMA_NAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# frontmatter.schema_validate — the gating tamper-check
# ---------------------------------------------------------------------------

def test_check_schema_drift_is_not_retargeted_by_an_inherited_git_dir(
    monkeypatch, doe, decoy, vendored
):
    """The vendored copy MATCHES DoE HEAD, so this must pass silently.

    Unscoped, `git -C <doe> show HEAD:coordinator/schemas/...` reads the DECOY's
    copy of the same path, finds a byte difference, and raises a tamper finding
    with a direction inferred from the wrong side — a gating failure caused
    entirely by an environment variable the caller never set.
    """
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    check_schema_drift(vendored, doe)  # must not raise


def test_check_schema_drift_reports_an_unreadable_clone_as_not_a_drift_finding(
    tmp_path, vendored
):
    """A DoE path that is not a git repository is a could-not-check, and the
    raised message must say so rather than reading as a tamper verdict."""
    not_a_repo = tmp_path / "not-a-clone"
    not_a_repo.mkdir()

    with pytest.raises(SchemaDriftError) as excinfo:
        check_schema_drift(vendored, not_a_repo)

    message = str(excinfo.value)
    assert "could not be read as a git repository" in message
    assert "NOT a drift finding" in message, (
        "the third state must disclaim itself, or a reader takes an unreachable "
        "clone as evidence the vendored file was tampered with"
    )


# ---------------------------------------------------------------------------
# frontmatter.schema_validate — the non-gating advisory
# ---------------------------------------------------------------------------

def test_advisory_is_not_retargeted_by_an_inherited_git_dir(
    monkeypatch, doe, decoy, vendored
):
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    result = check_schema_drift_advisory(vendored, doe)

    assert result["diverged"] is False, (
        "an inherited GIT_DIR made the advisory compare the vendored schema "
        "against the WRONG repository's copy and report drift"
    )
    assert result["determinate"] is True


def test_advisory_reports_an_unreadable_clone_as_indeterminate_not_drift(tmp_path, vendored):
    """Unreadable is not evidence of divergence. `determinate` is the field that
    carries that distinction; collapsing it is the whole defect."""
    not_a_repo = tmp_path / "not-a-clone"
    not_a_repo.mkdir()

    result = check_schema_drift_advisory(vendored, not_a_repo)

    assert result["diverged"] is False
    assert result["determinate"] is False
    assert "could not be read as a git repository" in result["detail"]
    assert "not a claim" in result["detail"]


# ---------------------------------------------------------------------------
# ops.emit.doe_drift
# ---------------------------------------------------------------------------

def test_probe_freshness_ref_reads_the_doe_clones_own_origin(monkeypatch, doe, decoy):
    """`remote get-url origin` is the first hop, and the one that decides which
    remote gets ls-remote'd. Retargeted, the probe queries OUR origin, finds no
    cockpit-contract-release there, and returns None — reported downstream as
    "DoE has not published it yet", a confident false claim about a remote this
    process never contacted.
    """
    _git(doe, "remote", "add", "origin", "https://example.invalid/doe.git")
    _git(decoy, "remote", "add", "origin", "https://example.invalid/ours.git")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    seen: "list[str]" = []

    real_run = subprocess.run

    def _capture(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) > 1 and cmd[1] == "ls-remote":
            seen.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(doe_drift.subprocess, "run", _capture)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doe_drift.probe_freshness_ref(doe)

    assert seen == ["https://example.invalid/doe.git"], (
        "the freshness probe queried the wrong remote — an inherited GIT_DIR "
        f"retargeted `remote get-url origin` (queried: {seen})"
    )


def test_probe_freshness_ref_skips_an_unreadable_clone_without_claiming_absence(tmp_path):
    not_a_repo = tmp_path / "not-a-clone"
    not_a_repo.mkdir()

    with pytest.warns(UserWarning) as recorded:
        result = doe_drift.probe_freshness_ref(not_a_repo)

    assert result is None
    text = " ".join(str(w.message) for w in recorded)
    assert "could not be read as a git repository" in text
    assert "not a finding about what DoE has or has not published" in text


def test_tag_ancestry_distinguishes_absent_commits_from_a_real_negative(doe, decoy):
    """1-vs-128 at the call site. An absent commit makes the question
    unanswerable (None -> caller fails loud); a present, unrelated commit makes
    it answerably False. Rendering both as False would silently green-light a
    pin the probe never verified."""
    doe_head = _git(doe, "rev-parse", "HEAD").stdout.strip()
    decoy_head = _git(decoy, "rev-parse", "HEAD").stdout.strip()
    _git(doe, "fetch", "-q", str(decoy), decoy_head)

    assert doe_drift._tag_is_ancestor_of_pin(doe, doe_head, doe_head) is True
    assert doe_drift._tag_is_ancestor_of_pin(doe, decoy_head, doe_head) is False, (
        "both commits are present and unrelated — that is a DETERMINATE negative"
    )
    assert doe_drift._tag_is_ancestor_of_pin(doe, "0" * 40, doe_head) is None, (
        "an absent commit means git could not answer (exit 128); reporting that "
        "as False is the conflation this fix removes"
    )


def test_tag_ancestry_is_not_retargeted_by_an_inherited_git_dir(monkeypatch, doe, decoy):
    doe_head = _git(doe, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    assert doe_drift._tag_is_ancestor_of_pin(doe, doe_head, doe_head) is True, (
        "a commit that exists only in the DoE clone became unresolvable — the "
        "probe answered from the decoy's object database"
    )


# ---------------------------------------------------------------------------
# engine_version — the self-locating probe
# ---------------------------------------------------------------------------

def test_resolve_engine_sha_is_not_retargeted_by_an_inherited_git_dir(monkeypatch, decoy):
    """`resolve_engine_sha` reports the RUNNING engine's commit; `engine_drift`
    then computes an ancestry verdict from it. An inherited GIT_DIR made it
    report the HEAD of whatever repo the hook was running in, which can render a
    current engine as "behind the known-good floor"."""
    baseline = engine_version.resolve_engine_sha()
    if baseline is None:
        pytest.skip("engine checkout is not a git work tree — nothing to retarget")
    decoy_head = _git(decoy, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    poisoned = engine_version.resolve_engine_sha()

    assert poisoned == baseline, (
        "the engine self-version probe answered from the poisoned GIT_DIR"
    )
    assert poisoned != decoy_head
