"""Regression guard: `envelope.emit()` must not regain an O(records) subprocess
scan anywhere on its path — not just the two specific mechanisms already
pinned elsewhere.

Prior coverage and the gap this closes
---------------------------------------
Two existing guards each pin ONE specific mechanism of the fix that cut a
cold ``envelope.emit()`` run from ~23s to ~9.3s (commit 2993c608f):

  * ``test_priority_resolve_cache.py::test_build_parent_map_skips_git_history_tier``
    — asserts ``_build_parent_map`` passes ``include_history_tier=False`` and
    that ``dag._git_path_ever_tracked`` is unreachable from that call site.
  * ``coordinator/bin/test_emit_cadence.py`` — pins the cadence gate polarity.

Neither fails if a DIFFERENT full-corpus scan appears somewhere else on the
emit path — e.g. a new section, or a rewritten ``_build_parent_map``, that
reintroduces a per-record subprocess spawn by some other route. That is the
actual acceptance criterion ("something fails if the cadence path regains a
full-corpus scan") and this test is what discharges it.

Oracle chosen and why it generalises
-------------------------------------
Total real ``subprocess.run`` spawn count across ONE ``envelope.emit()`` call
over a synthetic corpus, held to a CONSTANT ceiling as the corpus size grows.
This is deliberately mechanism-agnostic: it does not name ``_build_parent_map``,
``resolve_target``, or any other symbol — any code on the emit path that spawns
a subprocess once per record (the defect class named in the dispatch brief)
inflates the total, regardless of which section or function introduces it.

Verified discriminating (measured against this same test's fixture, not
asserted from theory): with this repo's actual fix in place, total spawns for
the synthetic corpus are IDENTICAL at N=5, N=50, N=250 handoff records (a
constant 12, dominated by the file_attributions subprocess producer and
per-emit git branch/sha lookups — none of it record-scaled). Reverting the
fix's call-site polarity alone (``include_history_tier=True``, simulated via
monkeypatch, not committed) reproduces the O(N) shape this guards against:
27 / 162 / 762 spawns for the same N=5/50/250 — an obvious, unmistakable
signal against an O(1) baseline of 12.

Each synthetic handoff's ``predecessor`` points at a name with no on-disk
match anywhere under ``state/handoffs`` (tiers 1/2 both miss) — this is
deliberate: a predecessor ref that already resolves on disk never reaches
whatever fallback tier would spawn a subprocess in the first place, so a
same-directory chain (a same-corpus fixture shape used elsewhere in this
package) would NOT exercise this property at all and the guard would pass
vacuously.

Ceiling justification
----------------------
``_SPAWN_CEILING = 40``. Basis: a measured cold real-corpus run of
``envelope.emit()`` after the fix issued 38 subprocess spawns total (21
``git -C <repo>`` calls, 2 external Python child processes, assorted
rev-list/log/diff-tree/cat-file/config calls — see this file's own dispatch
brief for the full breakdown). This synthetic fixture's own measured
fixed-path baseline is a constant 12 regardless of N, so 40 leaves headroom
for legitimate fixed-cost growth (one or two new fixed-cost git/subprocess
calls elsewhere on the path) while staying far below what even a handful of
per-record spawns would produce at this corpus size (N=200 below: a single
per-record spawn alone would add +200, blowing the ceiling by 5x).

What this test does NOT cover
-------------------------------
  * It does not exercise the real coordinator corpus (23.5MB / ~17K records)
    — a real-corpus run is deliberately out of scope for the fast tier (too
    slow, too environment-dependent); if a per-record pattern were somehow
    invisible at N=200 synthetic records but present at real-corpus scale,
    this guard would not catch it. No known mechanism on this path has that
    shape, but the possibility is not excluded by this test.
  * It does not attribute WHICH section or call introduced a new spawn if
    the ceiling is exceeded — only that the total grew. Diagnosing which
    section regressed still requires the kind of per-mechanism guard the two
    existing tests already provide.
  * It does not assert on ``git log``/``git rev-parse`` spawns specifically
    (unlike the coverage-gate spawn-bound sibling test) — it counts ALL
    ``subprocess.run`` calls, deliberately, so a new non-git external
    process (e.g. another Python child spawned per record) is caught too.
  * It is not a wall-clock assertion — this box runs 50-70 concurrent LLM
    sessions (see ``docs/wiki/machine-load-norm.md``); spawn COUNT is
    deterministic under load, elapsed time is not.

Spec backlink: state/handoffs/2026-08-10-emit-cadence-over-budget-design-call.md
(acceptance criterion closed PARTIAL prior to this file; DR-287 halted the
cadence itself but ``artifact.emit`` still runs on demand).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import emit

# The oracle this guard asserts on IS the count of real `subprocess.run`
# spawns `envelope.emit()` issues -- a mock would make the metric under test
# vacuous (see module docstring "Oracle chosen and why it generalises").
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

#: Distinct synthetic handoff records in the corpus. Must be large enough
#: that a single per-record subprocess spawn is unmistakable against the
#: measured O(1) baseline (12) — at N=200, one spawn/record alone adds +200,
#: 5x past the ceiling below.
_CORPUS_SIZE = 200

#: See module docstring "Ceiling justification" for the full accounting.
_SPAWN_CEILING = 40


def _make_corpus(state_root: Path, n: int) -> None:
    """Write ``n`` synthetic handoff records, each frontmatter-complete
    (title/created/status/deployment_state — the four fields
    ``sections/handoffs.py`` requires to avoid pre-priority-resolution
    quarantine) and each pointing its ``predecessor`` at a name that exists
    nowhere on disk, so every ``resolve_target`` call for it misses tiers
    1/2 and reaches whatever tier would spawn a subprocess.
    """
    handoff_dir = state_root / "handoffs"
    handoff_dir.mkdir(parents=True)
    for i in range(n):
        name = f"{i:04d}.md"
        text = "\n".join(
            [
                "---",
                f"handoff_id: id_{i}",
                f"title: Synthetic handoff {i}",
                "created: 2026-08-01",
                "status: claimed",
                "deployment_state: closed",
                f"predecessor: orphaned-{i}.md",
                "---",
                "",
                f"# {name}",
            ]
        )
        (handoff_dir / name).write_text(text, encoding="utf-8")


def _make_ctx(repo_root: Path, central_state_root: Path) -> EmitContext:
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central_state_root,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


@pytest.mark.usefixtures("requires_vendor_pin")
def test_emit_subprocess_spawn_count_does_not_scale_with_corpus_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``envelope.emit()`` run over a ``_CORPUS_SIZE``-record synthetic
    corpus must spawn at most ``_SPAWN_CEILING`` real subprocesses total —
    a CONSTANT, not a figure that scales with corpus size. See module
    docstring for the oracle, ceiling basis, and explicit non-coverage.
    """
    repo_root = tmp_path
    central_state_root = repo_root / "state"
    _make_corpus(central_state_root, _CORPUS_SIZE)
    ctx = _make_ctx(repo_root, central_state_root)

    spawned: List[tuple] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        spawned.append(tuple(str(a) for a in argv) if argv else ("<unknown>",))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    out_path = tmp_path / "out.json"
    result = emit(ctx, out=out_path)

    assert result["ok"] is True
    assert out_path.exists(), "emit() must still write the artifact under guard"

    assert len(spawned) <= _SPAWN_CEILING, (
        f"envelope.emit() issued {len(spawned)} subprocess spawns for a "
        f"{_CORPUS_SIZE}-record synthetic corpus (ceiling {_SPAWN_CEILING}) — "
        f"this is the shape of a full-corpus scan regaining an O(records) "
        f"subprocess pattern (measured fixed-cost baseline for this fixture "
        f"is a constant 12 regardless of N; see module docstring). Spawns: "
        f"{spawned}"
    )
