"""Tests for coordinator_core.ops.check_generator_output_staleness.

Real throwaway git repos (tmp_path + `git init`/`git commit`), one fixture
pair per verdict, never the real repo's generators (C2 runs concurrently and
may not have landed declarations yet). Covers:

    - STALE: a `sources` commit lands after the artifact's stamp.
    - FRESH: the incident-replay regeneration commit resolves a prior STALE.
    - UNSTAMPED: artifact missing, and artifact present but unreadable stamp.
    - INDETERMINATE: a `since_point` git can't resolve/parse.
    - AC2 regression: a `sources` commit ONE MINUTE after the stamp reads
      STALE — no calendar grace.
    - AC2 regression: a commit touching only the generator's own (shim) path,
      with `sources` untouched, reads FRESH — proves the pathspec, not the
      generator's own path, drives the verdict.

Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § C3
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import check_generator_output_staleness as cgos
from coordinator_core.ops.generator_provenance import Pair
from coordinator_core.ops.staleness_git import Verdict
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    return repo


def _commit_all(repo: Path, message: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


def _commit_all_at(repo: Path, message: str, iso_date: str) -> None:
    """Commit with an explicit, controlled author/committer date.

    Real-clock commits made back-to-back inside one test can land on the
    same wall-clock second (git's committer-date resolution) — at that
    resolution `git log --since=<that second>` treats the boundary as
    inclusive, which makes a same-second commit indistinguishable from one
    genuinely after the stamp. Tests that need "committed strictly before
    vs. after a given stamp" pin explicit, well-separated dates instead of
    depending on real-clock ordering.
    """
    _run_git(repo, "add", "-A")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = iso_date
    env["GIT_COMMITTER_DATE"] = iso_date
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo),
        env=env,
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _write_artifact(repo: Path, rel_path: str, stamp_key: str, stamp_value: str) -> Path:
    artifact = repo / rel_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.suffix.lower() == ".json":
        artifact.write_text(f'{{"{stamp_key}": "{stamp_value}"}}\n', encoding="utf-8")
    else:
        artifact.write_text(f"---\n{stamp_key}: {stamp_value}\n---\nbody\n", encoding="utf-8")
    return artifact


def _write_source(repo: Path, rel_path: str, content: str) -> Path:
    source = repo / rel_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    return source


def test_stale_when_sources_commit_lands_after_stamp(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_artifact(repo, "out/artifact.json", "emitted_at", "2020-01-01T00:00:00+00:00")
    _commit_all(repo, "initial")

    _write_source(repo, "gen/lib.py", "v2\n")
    _commit_all(repo, "touch sources after stamp")

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.STALE


def test_incident_replay_regenerate_resolves_stale(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_artifact(repo, "out/artifact.md", "emitted_at", "2020-01-01T00:00:00+00:00")
    _commit_all_at(repo, "initial", "2020-01-01T00:00:00+00:00")

    _write_source(repo, "gen/lib.py", "v2\n")
    _commit_all_at(repo, "sources move, artifact untouched", "2020-01-01T00:10:00+00:00")

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.md", stamp_key="emitted_at", sources=("gen/lib.py",))
    stale_result = cgos.compute_pair_staleness(repo, pair)
    assert stale_result["verdict"] == Verdict.STALE

    new_stamp = "2020-01-01T00:20:00+00:00"
    _write_artifact(repo, "out/artifact.md", "emitted_at", new_stamp)
    _write_source(repo, "gen/lib.py", "v3\n")
    _commit_all_at(repo, "regenerate artifact", new_stamp)

    regen_pair = Pair(
        generator="gen/shim.py",
        artifact="out/artifact.md",
        stamp_key="emitted_at",
        sources=("gen/lib.py",),
    )
    result = cgos.compute_pair_staleness(repo, regen_pair)
    assert result["verdict"] == Verdict.FRESH


def test_unstamped_when_artifact_missing(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _commit_all(repo, "initial")

    pair = Pair(generator="gen/shim.py", artifact="out/missing.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.UNSTAMPED


def test_unstamped_when_stamp_unreadable(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    artifact = repo / "out" / "artifact.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("not valid json {{{\n", encoding="utf-8")
    _commit_all(repo, "initial")

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.UNSTAMPED


def test_indeterminate_on_unparseable_stamp(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_artifact(repo, "out/artifact.json", "emitted_at", "not-a-timestamp")
    _commit_all(repo, "initial")

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.INDETERMINATE


def test_ac2_no_calendar_grace_one_minute_after_stamp_is_stale(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_artifact(repo, "out/artifact.json", "emitted_at", "2020-01-01T00:00:00+00:00")
    _commit_all(repo, "initial")

    _write_source(repo, "gen/lib.py", "v2\n")
    _run_git(repo, "add", "-A")
    _run_git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-q",
        "-m",
        "one minute after stamp",
        "--date=2020-01-01T00:01:00+00:00",
    )

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.STALE


def test_ac2_commit_touching_only_generator_shim_is_fresh(tmp_path):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_source(repo, "gen/shim.py", "shim v1\n")
    _write_artifact(repo, "out/artifact.json", "emitted_at", "2020-01-01T00:00:00+00:00")
    _commit_all_at(repo, "initial", "2020-01-01T00:00:00+00:00")

    # A distinct, later stamp so this test isolates "sources untouched since
    # the stamp" from the unrelated fact that the repo's very first commit
    # necessarily touches everything at once.
    stamp = "2020-01-01T00:10:00+00:00"
    _write_artifact(repo, "out/artifact.json", "emitted_at", stamp)
    _commit_all_at(repo, "record stamp", stamp)

    _write_source(repo, "gen/shim.py", "shim v2 -- unrelated tweak\n")
    _commit_all_at(repo, "touch only the shim, not sources", "2020-01-01T00:20:00+00:00")

    pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
    result = cgos.compute_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.FRESH


def test_compute_repo_staleness_keys_undeclared_by_generator_name(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "coordinator_core").mkdir()
    generator = repo / "coordinator_core" / "undeclared_writer.py"
    generator.write_text(
        "from pathlib import Path\n"
        "Path('out.json').write_text('{}')\n",
        encoding="utf-8",
    )
    # The write target must be tracked for write-behaviour discovery to count this
    # module a generator: an uncommitted output is not an artifact any reader consults.
    (repo / "out.json").write_text("{}", encoding="utf-8")
    _commit_all(repo, "initial")

    results = cgos.compute_repo_staleness(repo)
    assert "coordinator_core/undeclared_writer.py" in results
    assert results["coordinator_core/undeclared_writer.py"]["verdict"] == Verdict.UNDECLARED


def test_compute_repo_staleness_keys_write_target_unresolved_by_generator_name(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "coordinator_core").mkdir()
    generator = repo / "coordinator_core" / "write_target_unresolved.py"
    generator.write_text(
        "from pathlib import Path\n"
        "def run(target):\n"
        "    Path(target).write_text('{}')\n",
        encoding="utf-8",
    )
    _commit_all(repo, "initial")

    results = cgos.compute_repo_staleness(repo)
    assert "coordinator_core/write_target_unresolved.py" in results
    assert results["coordinator_core/write_target_unresolved.py"]["verdict"] == Verdict.WRITE_TARGET_UNRESOLVED


def test_compute_repo_staleness_unresolvable_root_is_indeterminate(tmp_path):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    results = cgos.compute_repo_staleness(non_repo)
    assert all(entry["verdict"] == Verdict.INDETERMINATE for entry in results.values())


def test_main_returns_nonzero_on_stale(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _write_source(repo, "gen/lib.py", "v1\n")
    _write_artifact(repo, "out/artifact.json", "emitted_at", "2020-01-01T00:00:00+00:00")
    _commit_all(repo, "initial")
    _write_source(repo, "gen/lib.py", "v2\n")
    _commit_all(repo, "sources move")

    def fake_compute_repo_staleness(repo_root=None):
        pair = Pair(generator="gen/shim.py", artifact="out/artifact.json", stamp_key="emitted_at", sources=("gen/lib.py",))
        return {"out/artifact.json": cgos.compute_pair_staleness(repo, pair)}

    monkeypatch.setattr(cgos, "compute_repo_staleness", fake_compute_repo_staleness)
    assert cgos.main([]) == cgos.EXIT_STALE


def test_main_returns_zero_when_nothing_stale(monkeypatch):
    monkeypatch.setattr(cgos, "compute_repo_staleness", lambda repo_root=None: {})
    assert cgos.main([]) == 0


# --- C6: the vendored leg ---------------------------------------------


def _init_peer_repo(tmp_path: Path) -> Path:
    """A throwaway repo standing in for the DoE-claude sibling clone. Tests
    in this module call `compute_vendored_pair_staleness` directly with this
    path, bypassing `resolve_peer_repo_path` (and its
    `PEER_REPO_SENTINEL` gate) entirely — the bare `coordinator/` subdir
    here is not itself the sentinel `resolve_peer_repo_path` checks for."""
    repo = _init_repo(tmp_path)
    (repo / "coordinator").mkdir()
    (repo / "coordinator" / "marker.txt").write_text("peer repo sentinel\n", encoding="utf-8")
    return repo


def _write_vendored_artifact(repo: Path, rel_path: str, sha: str, dirty: bool) -> Path:
    artifact = repo / rel_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "x-effective-delivery": {
                    "version": 1,
                    "generated_from_sha": sha,
                    "generated_at": "2020-01-01T00:00:00Z",
                    "generated_from_dirty_tree": dirty,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact


def test_vendored_parent_offset_freshly_regenerated_reads_fresh(tmp_path, monkeypatch):
    """A `generated_from_sha` naming the PARENT of a freshly-regenerated
    artifact's own commit must read FRESH, not drifted (trap a).

    Review: code-reviewer da34f46b flagged the original fixture as vacuous —
    its regen commit never touched `sources`, so the assertion passed
    identically whether `sha` was treated as the correct EXCLUSIVE lower
    bound or an off-by-one-shifted one, because the (correctly) narrower
    range and the (buggily) wider range were both empty of source-touching
    commits either way. This fixture is deliberately THREE commits so the
    parent-offset boundary is load-bearing without leaning on the
    `artifact_path` exclusion `test_vendored_fix_and_regenerate_in_one_commit
    _is_fresh` already covers:

        scaffold (root, no sources/artifact)
          -> sources-commit (touches `sources`; THIS is `parent_sha`,
             i.e. `generated_from_sha`)
          -> regen commit (touches ONLY the artifact; HEAD)

    Correct (`parent_sha` exclusive): range = parent_sha..HEAD = {regen}.
    regen never touches `sources` -> FRESH.
    An off-by-one that widens the lower bound by one commit (e.g. computing
    `parent_sha^..HEAD`) pulls sources-commit into the range too, and
    sources-commit DOES touch `sources` -> STALE. See
    `test_vendored_parent_offset_fixture_is_non_vacuous` below for a direct
    demonstration that this fixture distinguishes the two.
    """
    repo = _init_peer_repo(tmp_path)
    _commit_all(repo, "scaffold: no sources, no artifact yet")

    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v1\n")
    _commit_all(repo, "sources land here -- this becomes generated_from_sha")
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()

    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", parent_sha, dirty=False)
    _commit_all(repo, "regenerate hooks.json only, stamp names the parent commit")

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.FRESH


def test_vendored_parent_offset_fixture_is_non_vacuous(tmp_path, monkeypatch):
    """Companion proof for the fixture above: a deliberately-widened lower
    bound (`parent_sha^..HEAD` instead of `parent_sha..HEAD`) DOES flip the
    verdict to STALE on this exact fixture, confirming the fixture can
    actually catch a parent-offset regression rather than passing
    identically under correct and buggy boundary handling."""
    repo = _init_peer_repo(tmp_path)
    _commit_all(repo, "scaffold: no sources, no artifact yet")

    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v1\n")
    _commit_all(repo, "sources land here -- this becomes generated_from_sha")
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()

    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", parent_sha, dirty=False)
    _commit_all(repo, "regenerate hooks.json only, stamp names the parent commit")

    # Correct boundary: parent_sha..HEAD -- excludes parent_sha itself.
    correct = subprocess.run(
        ["git", "log", "--format=%H", f"{parent_sha}..HEAD", "--", "coordinator/hooks/scripts"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    assert correct == "", "correct boundary must find no source-touching commit (expected FRESH)"

    # Buggy off-by-one: parent_sha^..HEAD -- widens the window by one commit,
    # pulling the sources-commit back in.
    buggy = subprocess.run(
        ["git", "log", "--format=%H", f"{parent_sha}^..HEAD", "--", "coordinator/hooks/scripts"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    assert buggy != "", "widened boundary must find the sources-commit (would misread STALE)"


def test_vendored_dirty_tree_reads_indeterminate_not_fresh(tmp_path, monkeypatch):
    """`generated_from_dirty_tree: true` must read INDETERMINATE even when
    the SHA comparison would otherwise say FRESH (trap b)."""
    repo = _init_peer_repo(tmp_path)
    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v1\n")
    _commit_all(repo, "initial")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()

    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", head_sha, dirty=True)
    _commit_all(repo, "regenerate hooks.json, dirty tree at emit time")

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.INDETERMINATE


def test_vendored_incident_replay_emitter_commit_unregenerated_is_stale(tmp_path, monkeypatch):
    """Emitter commit lands, artifact unregenerated -> STALE."""
    repo = _init_peer_repo(tmp_path)
    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v1\n")
    _commit_all(repo, "initial")
    stamp_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", stamp_sha, dirty=False)
    _commit_all(repo, "record hooks.json stamp")

    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v2 -- emitter fix\n")
    _commit_all(repo, "emitter fix lands, hooks.json not regenerated")

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.STALE


def test_vendored_fix_and_regenerate_in_one_commit_is_fresh(tmp_path, monkeypatch):
    """One commit touching BOTH the emitter's `sources` and the declared
    artifact -> FRESH, not STALE (trap c)."""
    repo = _init_peer_repo(tmp_path)
    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v1\n")
    _commit_all(repo, "initial")
    stamp_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", stamp_sha, dirty=False)
    _commit_all(repo, "record hooks.json stamp")

    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    _write_source(repo, "coordinator/hooks/scripts/emit_effective_delivery.py", "v2 -- emitter fix\n")
    _write_vendored_artifact(repo, "coordinator/hooks/hooks.json", new_head, dirty=False)
    _commit_all(repo, "fix emitter and regenerate hooks.json in one commit")

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.FRESH


def test_resolve_peer_repo_path_absent_clone_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_doe_root_pointer", lambda: "")
    assert cgos.resolve_peer_repo_path() is None


def test_compute_vendored_staleness_unresolvable_peer_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_doe_root_pointer", lambda: "")
    results = cgos.compute_vendored_staleness()
    assert all(entry["verdict"] == Verdict.INDETERMINATE for entry in results.values())


def test_compute_all_staleness_merges_local_and_vendored_keys(tmp_path, monkeypatch):
    (tmp_path / "local").mkdir()
    local_repo = _init_repo(tmp_path / "local")
    _write_source(local_repo, "gen/lib.py", "v1\n")
    _commit_all(local_repo, "initial")

    monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_doe_root_pointer", lambda: "")
    results = cgos.compute_all_staleness(local_repo)
    assert any(key.startswith("DoE-claude:") or key == "<DoE-claude clone unresolved>" for key in results)
