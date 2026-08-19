"""
coordinator_core.ops.fleet.tests.test_shipped_in_batch_resolvable

Regression coverage for `archive_handoffs._shipped_in_batch_resolvable` — the
batched counterpart to `_shipped_in_resolvable` that boot_sweep.py's
heir-retain surfacing loop now calls once per sweep instead of once per
heir-retained candidate (T3 h4-ops-b deferred item, session/boot_sweep.py's
`for handoff_path in live_paths_for_heir_retain:` loop; see
coordinator_core/ops/session/boot_sweep.py and archive_handoffs.py's own
docstring on the function under test).

Coverage:
  (a) Multi-item correctness — a resolvable sha, an unresolvable (well-formed
      but absent) sha, and a duplicate of the resolvable sha in the SAME
      batch each resolve independently and correctly (per-item attribution
      survives the dedup-then-lookup-by-sha shape).
  (b) Spawn count — N distinct shas resolve via exactly ONE `git cat-file`
      subprocess, not one per sha. This is the assertion that FAILS against
      the pre-batch per-item loop (`_shipped_in_resolvable` called once per
      candidate): reverting `_shipped_in_batch_resolvable` to a per-item
      `for sha in shas: await _shipped_in_resolvable(...)` loop makes this
      test observe N spawns instead of 1 and fail. Confirmed by the
      dispatching agent via local revert-and-rerun; see the run report.
  (c) Empty input spawns nothing and returns {}.
  (d) Blank/falsy shas never resolve True and are excluded from the spawn.

Real git spawn is load-bearing (a mocked git has nothing to resolve against);
tiered onto cadence, not the per-commit path.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.archive_handoffs import _shipped_in_batch_resolvable

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "batch-resolvable-test@claude-klabauter.test")
    _git("config", "user.name", "Batch Resolvable Test")
    _git("config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "chore: seed")
    return root


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, check=True,
    )
    return result.stdout.decode().strip()


_UNRESOLVABLE_SHA = "deadbeef" * 5  # well-formed 40-hex, never a real object


def test_multi_item_per_sha_attribution(repo):
    resolvable = _head_sha(repo)
    result = _run(
        _shipped_in_batch_resolvable(
            repo, [resolvable, _UNRESOLVABLE_SHA, resolvable]
        )
    )
    assert result[resolvable] is True, f"HEAD sha must resolve True; got {result!r}"
    assert result[_UNRESOLVABLE_SHA] is False, (
        f"a well-formed but absent sha must resolve False, never True or "
        f"missing from the map; got {result!r}"
    )
    assert len(result) == 2, (
        f"a duplicate input sha must not fork into two map entries; got {result!r}"
    )


def test_spawn_count_is_one_for_multiple_shas(repo, monkeypatch):
    """FAILS against the pre-batch per-item loop: N candidates would spawn N
    `git cat-file` processes; the batched form spawns exactly one regardless
    of how many distinct shas are requested."""
    resolvable = _head_sha(repo)
    calls = []
    real_run = subprocess.run

    def _counting_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and "cat-file" in argv:
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.git_native.subprocess.run", _counting_run
    )

    _run(
        _shipped_in_batch_resolvable(
            repo, [resolvable, _UNRESOLVABLE_SHA, "another" + "0" * 33]
        )
    )

    assert len(calls) == 1, (
        f"expected exactly one `git cat-file` spawn for 3 distinct shas "
        f"(the batched form), got {len(calls)}: {calls!r}"
    )


def test_empty_input_spawns_nothing(repo, monkeypatch):
    calls = []
    real_run = subprocess.run

    def _counting_run(argv, *args, **kwargs):
        calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.git_native.subprocess.run", _counting_run
    )

    result = _run(_shipped_in_batch_resolvable(repo, []))
    assert result == {}
    assert calls == [], f"empty input must spawn nothing; got {calls!r}"


def test_blank_and_falsy_shas_never_resolve_true(repo):
    result = _run(_shipped_in_batch_resolvable(repo, ["", "   ", None]))  # type: ignore[list-item]
    assert result == {}, (
        f"blank/None shas must be excluded from the resolved map entirely "
        f"(mirrors _shipped_in_resolvable's own guard, which returns False "
        f"for them rather than spawning); got {result!r}"
    )
