"""
coordinator_core.tests.test_chain_ancestry_waiver_corpus_bound — standing bound assertion
(W3): the chain-ancestry-waiver corpus under `state/review-trail/chain-ancestry-waivers/`
stays inside an agreed bound, and fails loudly the moment it does not.

THE DISCHARGE TEST for the whole waiver item (§ North star, project CLAUDE.md). W1
(`coordinator_core.chain_ancestry_waivers.chain_reached_terminal_close`) added the retention
predicate; W2 (`coordinator_core.ops.reap_chain_ancestry_waivers`) added a remove-only,
idempotent reaper built on it. Nothing invokes W2's reaper automatically — an operator has
to remember to run it. "The operator remembers" is explicitly not an answer this repo
accepts (project CLAUDE.md § North star), so this module is what converts an unbounded,
only-grows corpus into something that goes red on its own, on a schedule no one has to
remember either (any normal test run).

ONE collector, TWO outputs (mirrors `test_no_unbatched_per_item_git_spawn.py`'s G1/G2
"one collector, two assertions" shape, reused deliberately per this chunk's own brief):
`_collect_corpus_counts` walks the corpus exactly once, returning the total waiver-file
count AND the subset that is already reapable per W1's own predicate
(`chain_reached_terminal_close`). The STANDING gate below asserts only the total stays
under `_BOUND` — asserting the reapable count must be zero would be asserting nothing ever
becomes reapable, which is false by construction the moment any chain closes, and asserting
it stays UNDER some bound would just be re-deriving the same total-corpus-size claim one
step removed. The reapable count is reported for visibility instead (assertion failure
message, and the explicit visibility test), exactly as this chunk's brief asks for: "a count
... plus the count that are terminal-closed ... feeding a STANDING assertion that the corpus
stays under an agreed bound, and a logged/reported total for visibility."

BOUND, chosen from a FRESH COUNT taken at implementation time (2026-08-08), not the plan's
stale 868: a read-only `pathlib` walk of `state/review-trail/chain-ancestry-waivers/`
found 959 waiver files across 69 chain subdirectories — already above the plan's own
figure, confirming the corpus is actively growing (a peer session staged five new waiver
files under a new chain during this very execution run, per this chunk's own brief).
`_BOUND = 1500` sits ~56% above that 959 observation: large enough that the corpus growing
by another `1500 - 959 = 541` files (roughly six sessions' worth, at the ~91-file
single-session growth this same execution window produced) does not trip the gate on
routine PM-vouch-free, gate-minted waiver traffic — but small enough that unchecked,
runaway growth (e.g. a chain-close bug that re-mints on every gate run rather than being
idempotent, or W1's own O_CREAT|O_EXCL no-op protection regressing) trips this gate within
a bounded number of future sessions, not asymptotically never. The risk deliberately
weighted heavier: a bound that can never fire is theatre (this item's whole point), so the
bound sits close enough to the observed count that it is a live tripwire, not a formality —
at the cost of needing a deliberate PM-approved bump (not a silent auto-follow of the
count) once legitimate growth approaches it. Bumping this constant is NOT the same
operation as running the reaper: shrinking the corpus is W2's job; this constant is a
distinct, standing, agreed ceiling.

Anti-scope 20, both halves (both are hard, per this chunk's own dispatch brief):
  1. This gate lives at `coordinator_core/tests/`, OUTSIDE the corpus it measures
     (`state/review-trail/chain-ancestry-waivers/`) — never colocated with it.
  2. `_assert_gate_outside_corpus` is the re-entrancy sentinel: it raises `RuntimeError`
     LOUDLY (never silently skips/passes) if this gate's own file were ever found nested
     inside the corpus root it counts — mirroring G1's `_assert_not_self_scanned` shape
     exactly (a positive control that proves the sentinel actually fires, plus a negative
     control proving the real, correct placement does not trip it).

REMOVE-ONLY BOUNDARY (repeated from this chunk's brief, load-bearing): this module ONLY
ever reads (`Path.iterdir()`, `Path.is_dir()`, `Path.suffix`) under
`state/review-trail/chain-ancestry-waivers/`. It never deletes, moves, or edits anything
there, and never imports or calls `coordinator_core.ops.reap_chain_ancestry_waivers`'s
`reap_chain_ancestry_waivers`/`_chain_ancestry_waivers_reap` — counting and reaping are
kept in two entirely separate modules on purpose, so a bug in this gate can only ever
misreport a count, never mutate the tracked corpus it is reporting on.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md
§ Tasks, row W3.

Negative-spec — what this module deliberately does NOT do:
  - Does not invoke `chain_ancestry_waivers.reap`/`reap_chain_ancestry_waivers` against the
    live tree, or any tree — no reap call anywhere in this file.
  - Does not assert anything about the reapable subset beyond reporting it — reaping is W2's
    job, not this gate's, and nothing here shrinks the corpus automatically.
  - Does not touch, edit, or extend `coordinator_core/chain_ancestry_waivers.py` or
    `coordinator_core/ops/reap_chain_ancestry_waivers.py` — read-only imports only.
"""

from __future__ import annotations

import pathlib
from typing import NamedTuple

from coordinator_core.chain_ancestry_waivers import (
    _CHAIN_ID_RE,
    chain_reached_terminal_close,
    chain_root_dir,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()

#: See module docstring's "BOUND" section for the full justification and the fresh count
#: (959 files / 69 chains, observed 2026-08-08) this was chosen against.
_BOUND = 1500


class _CorpusCounts(NamedTuple):
    total: int
    reapable: int
    chains: int


def _collect_corpus_counts(cwd: str, *, classify: bool = True) -> _CorpusCounts:
    """Single read-only walk of `chain_root_dir(cwd)`'s immediate subdirectories: for every
    validly-named (`_CHAIN_ID_RE`) chain subdirectory, count its `*.json` waiver files, and
    additionally count those files as `reapable` when `chain_reached_terminal_close` reports
    that chain's own `chain_id` has reached a terminal `closed` disposition (W1's predicate,
    reused unmodified — never re-derived here).

    `classify=False` skips the `chain_reached_terminal_close` call per chain and leaves
    `reapable` at 0. That predicate spawns git per chain to classify an archived handoff's
    disposition, so on the live corpus the classifying walk costs ~180s against a
    filesystem-only walk's milliseconds. The STANDING bound assertion needs `total` alone —
    paying 69 git classifications on every green run purely to decorate a failure message
    that green runs never emit would make this gate an amplification site of exactly the
    kind the plan it ships under exists to remove. The classifying form is for the
    visibility report and for a failing bound, where the reapable count is the operator's
    next action.

    Mirrors `reap_chain_ancestry_waivers`'s own traversal shape (immediate children only,
    `_CHAIN_ID_RE`-gated, one chain directory at a time) so the two counts stay directly
    comparable to what a real reap run would remove — but this function never calls that
    reaper and never mutates anything it walks.

    An absent corpus root, or an `OSError` listing it, is a fresh/empty-tree no-op:
    `_CorpusCounts(0, 0, 0)`, never a raise — matching every other reader in
    `chain_ancestry_waivers.py`'s own best-effort-read posture.
    """
    root = chain_root_dir(cwd)
    try:
        if not root.is_dir():
            return _CorpusCounts(total=0, reapable=0, chains=0)
        entries = sorted(root.iterdir())
    except OSError:
        return _CorpusCounts(total=0, reapable=0, chains=0)

    total = 0
    reapable = 0
    chains = 0
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        chain_id = entry.name
        if not _CHAIN_ID_RE.match(chain_id):
            continue
        try:
            waiver_files = [
                p for p in entry.iterdir() if p.suffix == ".json" and p.is_file()
            ]
        except OSError:
            continue
        if not waiver_files:
            continue
        chains += 1
        count = len(waiver_files)
        total += count
        if classify and chain_reached_terminal_close(cwd, chain_id):
            reapable += count

    return _CorpusCounts(total=total, reapable=reapable, chains=chains)


def _assert_gate_outside_corpus(gate_file: pathlib.Path, corpus_root: pathlib.Path) -> None:
    """Anti-scope 20's loud re-entrancy sentinel, mirroring
    `test_no_unbatched_per_item_git_spawn.py::_assert_not_self_scanned`'s discipline exactly:
    raises `RuntimeError` — never returns a silently-clean/empty result — if `gate_file`
    resolves to a path nested inside `corpus_root`. A gate that measured a corpus it was
    itself physically inside of would be liable to count/miscount its own presence and pass
    vacuously; this check exists so that placement is verified every run, not merely trusted
    once at review time.
    """
    try:
        gate_file.resolve().relative_to(corpus_root.resolve())
    except ValueError:
        return
    raise RuntimeError(
        f"re-entrancy: this bound gate ({gate_file}) resolves INSIDE the corpus root it "
        f"measures ({corpus_root}) — this would make the standing bound assertion count "
        "(or miscount) its own presence and risk passing vacuously. Anti-scope 20 requires "
        "the gate live OUTSIDE the corpus it measures."
    )


def _live_corpus_root() -> pathlib.Path:
    return chain_root_dir(str(_REPO_ROOT))


def test_gate_lives_outside_the_corpus_it_measures():
    """Positive control for the real, correct placement: this file's own real path must
    NOT trip the sentinel."""
    _assert_gate_outside_corpus(_THIS_FILE, _live_corpus_root())


def test_reentrancy_sentinel_raises_loudly_if_gate_is_inside_corpus():
    """Negative control proving the sentinel actually fires (not merely trusted), mirroring
    `test_no_unbatched_per_item_git_spawn.py::test_reentrancy_sentinel_raises_loudly_if_self_scanned`:
    simulates a gate file that (wrongly) resolves inside the corpus root it measures."""
    corpus_root = _live_corpus_root()
    poisoned_gate_file = corpus_root / "some-chain-id" / "this_gate.py"
    try:
        _assert_gate_outside_corpus(poisoned_gate_file, corpus_root)
    except RuntimeError as exc:
        assert "re-entrancy" in str(exc)
    else:
        raise AssertionError("sentinel did not raise for a gate file nested in the corpus root")


def test_chain_ancestry_waiver_corpus_stays_bounded():
    """STANDING gate (W3): the live corpus under
    `state/review-trail/chain-ancestry-waivers/` must not exceed `_BOUND` waiver files.
    Green at land (959 observed vs. 1500 bound, see module docstring) and fires loudly —
    an `AssertionError` naming the observed total, the reapable subset, and the chain
    count — the moment the corpus grows past the agreed ceiling with nothing having reaped
    it. This is the sole gating assertion in this module; see module docstring's
    "ONE collector, TWO outputs" section for why the reapable count is reported, not
    separately gated."""
    _assert_gate_outside_corpus(_THIS_FILE, _live_corpus_root())
    # Filesystem-only walk: the bound is on TOTAL, and classification costs a git spawn per
    # chain (~180s live). The reapable subset is resolved below only when the bound has
    # actually been breached, where it is the operator's next action rather than decoration
    # on a message no green run prints.
    counts = _collect_corpus_counts(str(_REPO_ROOT), classify=False)
    if counts.total > _BOUND:
        classified = _collect_corpus_counts(str(_REPO_ROOT))
        raise AssertionError(
            f"chain-ancestry-waiver corpus grew past its agreed bound: "
            f"{classified.total} waiver files across {classified.chains} chain directories "
            f"(bound: {_BOUND}; {classified.reapable} of those files are already reapable per "
            "chain_reached_terminal_close — run `coordinator_core.ops."
            "reap_chain_ancestry_waivers.reap_chain_ancestry_waivers` against this repo root, "
            "or raise the bound with a named justification if this growth is expected)."
        )


def test_chain_ancestry_waiver_corpus_reapable_count_is_visible():
    """Visibility test (this chunk's brief: 'a logged/reported total for visibility'):
    exercises the SAME collector as the standing gate above, and prints the observed
    total/reapable/chains split so a `-s`/`-v` run surfaces the reapable count even though
    it is not itself gated. Never fails on its own (the shape check below is intentionally
    weak — reapable can legitimately be 0 on a corpus with no terminal-closed chains yet);
    the standing bound assertion above is what fires loudly, this test is what makes the
    number visible."""
    counts = _collect_corpus_counts(str(_REPO_ROOT))
    print(
        f"chain-ancestry-waiver corpus: total={counts.total} reapable={counts.reapable} "
        f"chains={counts.chains} bound={_BOUND}"
    )
    assert counts.reapable <= counts.total


def test_collector_counts_only_valid_chain_id_directories(tmp_path):
    """Unit control on the collector itself (fixtures, not the live tree): a stray file
    directly under the corpus root, and a directory whose name fails `_CHAIN_ID_RE`, must
    both be excluded from the count — matching W2's own `reap_chain_ancestry_waivers`
    scoping discipline for the same directory shape."""
    root = tmp_path / "state" / "review-trail" / "chain-ancestry-waivers"
    root.mkdir(parents=True)
    (root / "not-a-directory.json").write_text("{}", encoding="utf-8")
    valid_chain = root / "abc123"
    valid_chain.mkdir()
    (valid_chain / "deadbeef.json").write_text("{}", encoding="utf-8")
    invalid_chain = root / "not_a_valid_chain_id!!"
    invalid_chain.mkdir()
    (invalid_chain / "deadbeef.json").write_text("{}", encoding="utf-8")

    counts = _collect_corpus_counts(str(tmp_path))
    assert counts.total == 1
    assert counts.chains == 1


def test_collector_empty_corpus_root_is_a_noop(tmp_path):
    """No corpus root at all (fresh clone, no chain has ever HALTed and minted here) must
    return zeroed counts, never raise."""
    counts = _collect_corpus_counts(str(tmp_path))
    assert counts == _CorpusCounts(total=0, reapable=0, chains=0)


if __name__ == "__main__":
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__, "-v", *sys.argv[1:]]))
