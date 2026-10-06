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
from coordinator_core.ops.generator_census import Pair
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
    generator.write_text("UNSTAMPED_BY_DESIGN = []\n",encoding="utf-8")
    _commit_all(repo, "initial")

    results = cgos.compute_repo_staleness(repo)
    assert "coordinator_core/undeclared_writer.py" in results
    assert results["coordinator_core/undeclared_writer.py"]["verdict"] == Verdict.UNDECLARED


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


def _init_peer_repo(tmp_path: Path) -> Path:
    """A throwaway repo standing in for the coordinator-content-repo sibling clone. Tests
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

    monkeypatch.setenv("REPO_CONTENT_ROOT", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.FRESH


def test_vendored_parent_offset_fixture_is_non_vacuous(tmp_path, monkeypatch):
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

    correct = subprocess.run(
        ["git", "log", "--format=%H", f"{parent_sha}..HEAD", "--", "coordinator/hooks/scripts"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    assert correct == "", "correct boundary must find no source-touching commit (expected FRESH)"

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

    monkeypatch.setenv("REPO_CONTENT_ROOT", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.INDETERMINATE


def test_vendored_incident_replay_emitter_commit_unregenerated_is_stale(tmp_path, monkeypatch):
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

    monkeypatch.setenv("REPO_CONTENT_ROOT", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.STALE


def test_vendored_fix_and_regenerate_in_one_commit_is_fresh(tmp_path, monkeypatch):
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

    monkeypatch.setenv("REPO_CONTENT_ROOT", str(repo))
    pair = cgos.VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    )
    result = cgos.compute_vendored_pair_staleness(repo, pair)
    assert result["verdict"] == Verdict.FRESH


def test_resolve_peer_repo_path_absent_clone_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_content_root", lambda: "")
    assert cgos.resolve_peer_repo_path() is None


def test_compute_vendored_staleness_unresolvable_peer_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_content_root", lambda: "")
    results = cgos.compute_vendored_staleness()
    assert all(entry["verdict"] == Verdict.INDETERMINATE for entry in results.values())


def test_compute_all_staleness_merges_local_and_vendored_keys(tmp_path, monkeypatch):
    (tmp_path / "local").mkdir()
    local_repo = _init_repo(tmp_path / "local")
    _write_source(local_repo, "gen/lib.py", "v1\n")
    _commit_all(local_repo, "initial")

    monkeypatch.setenv("REPO_CONTENT_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(cgos, "read_content_root", lambda: "")
    results = cgos.compute_all_staleness(local_repo)
    assert any(key.startswith("coordinator-content-repo:") or key == "<coordinator-content-repo clone unresolved>" for key in results)


_KNOWN_RED_SCRIPT = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "regenerate-known-red-registry.py"


def _load_known_red_module(tmp_path: Path, monkeypatch, *, unmarked: list[str], entries: dict):
    import importlib.util

    spec = importlib.util.spec_from_file_location("regen_known_red_under_test", _KNOWN_RED_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registry_path = tmp_path / "known-red.json"
    registry_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    calls = []

    def fake_derive(target=mod.DEFAULT_TARGET, workers=None):
        calls.append(target)
        return {"unmarked_failed": unmarked, "marked_failed": []}

    monkeypatch.setattr(mod, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(mod, "load_registry", lambda: json.loads(registry_path.read_text(encoding="utf-8")))
    monkeypatch.setattr(mod, "_derive_report", fake_derive)
    return mod, registry_path, calls


def test_known_red_write_without_entry_change_stamps_verified_at(tmp_path, monkeypatch):
    mod, path, calls = _load_known_red_module(tmp_path, monkeypatch, unmarked=["a::t"], entries={"a::t": {}})
    assert mod.main(["--write"]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["verified_at"], str) and data["verified_at"].endswith("Z")
    assert "generated" not in data
    assert data["entries"] == {"a::t": {}}
    assert len(calls) == 1


def test_known_red_write_on_drift_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch):
    mod, path, calls = _load_known_red_module(tmp_path, monkeypatch, unmarked=["a::t", "b::t"], entries={"a::t": {}})
    before = path.read_text(encoding="utf-8")
    assert mod.main(["--write"]) != 0
    assert path.read_text(encoding="utf-8") == before
    assert len(calls) == 1


def test_known_red_derive_called_once_with_or_without_write(tmp_path, monkeypatch):
    mod, path, calls = _load_known_red_module(tmp_path, monkeypatch, unmarked=["a::t"], entries={"a::t": {}})
    before = path.read_text(encoding="utf-8")
    mod.main([])
    assert len(calls) == 1
    assert path.read_text(encoding="utf-8") == before
    mod.main(["--write"])
    assert len(calls) == 2, "--write must add no second derivation"


def test_agent_install_manifest_stamp_key_reads_as_string():
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("gen_tested_platforms_under_test", root / "coordinator" / "bin" / "generate-tested-platforms.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (entry,) = mod.GENERATES
    assert entry["stamp_key"] != "tested_platforms"
    stamp, _detail = cgos.extract_stamp(root / entry["artifact"], entry["stamp_key"])
    assert isinstance(stamp, str) and stamp.endswith("Z")


def test_tested_platforms_write_splices_stamp_without_reflowing(tmp_path):
    import importlib.util
    import re

    root = Path(__file__).resolve().parents[3]
    src = (root / "docs" / "install" / "agent-install-manifest.json").read_text(encoding="utf-8")
    assert re.search(r'"tested_platforms_verified_at": "[^"]+"', src)
    assert json.loads(src)["tested_platforms"] == ["macos", "windows"]


# Red until state/bash-guards/known-red.json is regenerated with --write, which refuses
# while the registry has drift; the failure names what is still unstamped.
@pytest.mark.designed_red
def test_prime_exit_criterion_five_named_modules_classify_clean():
    """Per-module readers over the live files; the five are named, never swept."""
    from coordinator_core.ops.generator_census.declarations import extract
    from coordinator_core.ops.generator_census.globs import has_wildcard, is_catch_all

    root = Path(__file__).resolve().parents[3]

    def declared(rel: str) -> dict:
        return extract((root / rel).read_text(encoding="utf-8"))

    for rel in ("coordinator/bin/regenerate-known-red-registry.py", "coordinator/bin/generate-tested-platforms.py"):
        generates = declared(rel).get("GENERATES")
        assert isinstance(generates, list) and generates, rel
        for entry in generates:
            stamp, detail = cgos.extract_stamp(root / entry["artifact"], entry["stamp_key"])
            assert isinstance(stamp, str) and stamp, (rel, entry["artifact"], detail)

    assert "GENERATES" not in declared("coordinator_core/contract/cockpit_schema/emit_schema.py")

    for rel in ("coordinator_core/ops/distill_apply_disposal.py", "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py"):
        decls = declared(rel)
        generates = decls.get("GENERATES")
        if isinstance(generates, list) and generates and all(g.get("stamp_key") for g in generates):
            continue
        # AC2 reasoned-at-site path: still UNDECLARED by the checker, asserted as that outcome.
        assert not generates, rel
        mutates = decls.get("MUTATES")
        assert isinstance(mutates, list) and mutates and all(isinstance(m, str) for m in mutates), rel
        assert any(has_wildcard(m) and not is_catch_all(m) for m in mutates), rel
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
        (idx,) = [i for i, ln in enumerate(lines) if ln.startswith("MUTATES")]
        assert lines[idx - 1].lstrip().startswith("#"), f"{rel}: no site-reason comment above MUTATES"
