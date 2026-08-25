"""Ratchet baseline for `test_casefold_bypass_lint.py` -- ceiling, not floor.

This is DEBT, not an approved-patterns list (same convention as
`coordinator_core/tests/_home_resolution_lint_baseline.py`, which this file
mirrors deliberately -- see that module for the property this baseline
enforces: shrink-only, text-keyed, never a bare count).

Every entry below is a LIVE, UNFIXED instance of the exact defect class
`d9617436521c` fixed in three other guards: a path-derived string compared
for containment or equality (`startswith`/`==`/`in`) without routing
through `coordinator_core.write_guards._case_fold_path.casefold_path`
first, which a case-insensitive-but-case-preserving filesystem (macOS
APFS, Windows) can walk around with a differently-cased path. Discovered
by `test_casefold_bypass_lint.py` while building that gate (2026-08-05) --
see its module docstring for the full defect description and the
mutation test proving the detector fires on the original bug shape.

These are NOT sanctioned exemptions (contrast `EXEMPTIONS` in
`coordinator_core/ops/check_posix_exec_assumptions.py`, which requires "a
real reason a file is a genuine carve-out"): there is no reason any of
these eight sites should stay unfixed, only that fixing production guards
was out of scope for the session that wrote the gate (constraint: "adding
a gate, not changing behavior"). Each entry is a fix-me, tracked so it
cannot silently multiply while also not silently blocking the gate that
found it. **Reported to the dispatching EM as break-class findings needing
a fast-follow fix** -- see that session's report for the full list; three
guards are CLASS = "hard-deny" (`guard_settings_json_write.py`,
`block_dev_side_mirror_wiki.py`, and `validate_frontmatter_schema_deny.py`
itself), meaning the bypass is a real security-boundary gap, not merely an
advisory miss.

Two distinct sub-shapes, both the same underlying defect:
  - `startswith`/`in` containment: a resolved/normalized path checked
    against a prefix or an allow-list without folding either side
    (`block_dev_side_mirror_wiki.py`, `guard_settings_json_write.py`,
    both `validate_frontmatter_schema_*.py`'s `to_repo_relative`).
  - `==` equality between two independently-`.resolve()`'d paths, again
    with no fold on either side (`validate_frontmatter_schema_*.py`'s
    `this_repo_is_central`/`landing_repo_is_central` central-repo identity
    check) -- the same shape `guard_memory_store_cap.py` gets right.

Deleting an entry (because the site was fixed to route through
`casefold_path`) is a pure win -- the paired
`test_baseline_has_no_stale_entries` test will fail and name it for
deletion, never allowing a stale row to be replaced by a fresh violation at
the same coordinates.

All eight original entries were fixed 2026-08-05 (fast-follow to the gate
landing minutes earlier): each site now routes its comparison operands
through `casefold_path` (I/O-bearing values -- `file_path_expanded` in
`block_dev_side_mirror_wiki.py`, `abs_file_path` throughout the two
`validate_frontmatter_schema_*.py` modules -- are left in original case;
only local copies used for the containment/equality check are folded).
The baseline is empty; any future row here is a fresh violation, not
inherited debt.
"""

from __future__ import annotations

# (relpath, line, stripped source line at time of baselining)
KNOWN_BYPASS_BASELINE: list[tuple[str, int, str]] = []
