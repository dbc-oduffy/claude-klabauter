"""
coordinator_core.benchmarks.baselines -- the two-partition baseline store's
on-disk root (see coordinator_core/benchmarks/baseline_store.py for the
partition split: `runs/<machine>.jsonl` ignored run history vs.
`tracked-<machine>.jsonl` curated, git-tracked baseline).

This package holds no store logic of its own -- `baseline_store.py` (one
level up) owns every read/write path. It holds `refresh.py`, the named,
runnable refresh action that populates/re-populates the tracked partition
(qsub-01 C8).

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C8.
"""
