"""Shrinking-debt ledger for `test_home_resolution_lint.py` -- NOT an
approved-patterns list.

Every entry below is a KNOWN, LIVE violation of the home-resolution
defect class (`os.access(..., os.X_OK)`, a literal `":"` PATH-list
join/split, a forward-slash-only path split in the resolution-code family,
or a `CLAUDE_HOME`/`HOME` `or`-chain with no `USERPROFILE` rung) that
existed in the tree on 2026-07-28, the day this gate was authored. The gate
found ~30+ sites this class occurs at (the design blueprint's own count),
not the ~6 originally believed from the machine-a friction log -- this file
is the retrofit worklist that discovery produced, not a set of exceptions
anyone should be adding to.

**Do not add a new entry to this file to make a new site pass.** A NEW
violation is exactly what `test_home_resolution_lint.py` exists to catch;
fix the site instead. The only correct way this file's total count moves
is DOWN: fix a site, delete its baseline row, and if the file's own
`test_*_baseline_has_no_stale_entries` test in the lint module doesn't
already fail and name it for you, delete it yourself.

Per-rule current counts, as measured 2026-07-28 (re-run the four
`find_*` functions in `test_home_resolution_lint.py` to get a live count --
these numbers are a snapshot, not re-derived automatically):

  - X_OK_BASELINE (os.access(..., os.X_OK)):           0 (fully paid down
    2026-08-01 -- see the note directly above X_OK_BASELINE's definition)
  - COLON_JOIN_BASELINE (literal ':' PATH split):       1
  - FORWARD_SLASH_BASELINE (fwd-slash-only split):      5 unique lines
    (2026-07-29: was 9 total / 8 unique; 3 fixed via a backslash-fold before
    the endswith/rstrip check, 5 remain -- 1 not a live OS path
    (scaffold_structure.py, manifest-authored) and 4 in a security guard
    (trusted_root_guard.py) whose Windows-safety is already proven by a
    separate mechanism -- see the note directly above
    FORWARD_SLASH_BASELINE's definition)
  - BARE_OR_BASELINE (CLAUDE_HOME/HOME `or`-chain, no USERPROFILE rung): 4
    (2026-07-29: was 18; 13 fixed. 2026-08-01: wsc_commit.py pruned, the site
    is gone. The 4 remaining are confirmed AST-window false positives -- see
    the note directly above BARE_OR_BASELINE's definition)

Total: 93 known sites (across 4 rules; the 5th shape -- lying docstrings --
has its own, separately-baselined gate in `test_docstring_shell_paste_hazard.py`
and is not counted here; X_OK_BASELINE and COLON_JOIN_BASELINE counts above
predate later waves' paydowns and are stale -- count the live list literals
directly rather than trusting this total).

2026-07-28 update: two sites fixed via `coordinator_core.win_portability
.is_executable` -- `verify_dist_publish_repo_sync.py`'s `candidate` X_OK
check (baseline entry removed, stale text `home_binary` from a since-renamed
variable) and `verify_ue_overrides.py`'s `settings_bin` X_OK check (never
baselined -- added after the original 98-site count, fixed on discovery
rather than added as a 71st debt row). X_OK_BASELINE: 70 -> 69.

2026-07-28 update 2: `coordinator_core/install/_shared.py`'s `elif
os.access(shim, os.X_OK):` baseline row removed -- not a code edit, an
engine fix. The X_OK rule (`home_resolution_lint.py`) gained a guard-shape
exemption (`if os.name == "nt": ... elif <this>:` provably runs the `elif`
branch only when NOT on Windows), and this site was already correctly
mutually-exclusive with the `os.name == "nt"` branch above it -- the old
engine just could not see that. Removing a genuinely-safe site from this
ledger, not a debt paydown.

Keyed on (relpath, exact stripped source line text) -- text-keyed, not
line-number-keyed, matching the convention in
`test_docstring_shell_paste_hazard.py`'s own `_BASELINE`. Text keys move
with their line when unrelated code shifts around them; the tradeoff is
that *editing* the flagged line itself drops it out of the baseline and
re-fails the gate -- which is the correct direction to fail, since editing
the line is exactly when it should be fixed.

Every X_OK entry in a `coordinator*/tests/` or `bin/tests/` path is a real
test asserting an installed artifact's executable bit on POSIX CI -- still
a genuine finding (the assertion is meaningless on a Windows CI runner,
exactly as the production sites are), not a "test file constructing a bad
input" false positive (see `test_home_resolution_lint.py`'s AC-6 discussion
for that distinct exemption class, which this baseline does not use: no
site here was filtered out as a false positive; every one is a real,
uncorrected instance of the banned shape).
"""

from __future__ import annotations

# KEY-SHAPE HAZARD: each row below is keyed on (relpath, exact stripped

# 2026-08-01 update: X_OK_BASELINE reached ZERO. The last 28 rows were pruned
# `test_x_ok_baseline_has_no_stale_entries`. X_OK_BASELINE is empty again.
X_OK_BASELINE: list[tuple[str, int, str]] = []

COLON_JOIN_BASELINE: list[tuple[str, int, str]] = []

# 2026-07-29 update: 3 of the original 8 unique FORWARD_SLASH_BASELINE lines
# endswith/rstrip check so an operator-set CLAUDE_HOME spelled with
# for two DIFFERENT reasons -- see the dispatch's own report for the
#   coordinator_core/trusted_root_guard.py:210,270,445,453 -- a SECURITY
#     Windows case is asserted by a DIFFERENT test,
FORWARD_SLASH_BASELINE: list[tuple[str, int, str]] = [
    ("coordinator_core/install/scaffold_structure.py", 99, 'return self.path.endswith("/")'),
    ("coordinator_core/trusted_root_guard.py", 210, 'if content.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 270, 'if content.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 445, 'if os.name == "nt" and doe_root.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 453, 'if os.name == "nt" and claude_klabauter_root.endswith("/"):'),
    ("coordinator_core/install/check_install_singularity.py", 184, 'p = raw[:-1] if raw.endswith("/") else raw'),
    ("coordinator_core/install/check_install_singularity.py", 187, 'basename = p.rsplit("/", 1)[-1]'),
    (
        "coordinator_core/install/check_install_singularity.py",
        192,
        'if basename == "coordinator" and p.endswith("/coordinator"):',
    ),
    (
        "coordinator_core/install/check_install_singularity.py",
        386,
        'if claude_home_cmp.endswith("/.claude") or claude_home_cmp.endswith("/.claude/"):',
    ),
]

# 2026-07-29 update: 13 of the original 18 BARE_OR_BASELINE sites fixed --
BARE_OR_BASELINE: list[tuple[str, int, str]] = [
    # Shape A -- an OPTIONAL EXTRA root, not a resolution chain: a lone
    # `os.environ.get("CLAUDE_HOME", "")` / `os.environ.get("HOME", "")`
    # whose result, if non-empty, is APPENDED to a list already populated
    (
        "coordinator_core/ops/check_auto_memory_drained.py",
        180,
        'claude_home = os.environ.get("CLAUDE_HOME", "")',
    ),
    (
        "coordinator_core/write_guards/block_derived_global_doctrine_write.py",
        194,
        'claude_home = os.environ.get("CLAUDE_HOME", "")',
    ),
    (
        "coordinator_core/write_guards/block_home_dir_memo_delivery.py",
        128,
        'claude_home = os.environ.get("CLAUDE_HOME", "")',
    ),
    (
        "coordinator_core/write_guards/guard_memory_store_cap.py",
        167,
        'claude_home = os.environ.get("CLAUDE_HOME", "")',
    ),
    # integration note -- that `claude_home` feeds a USERPROFILE-guarded
    # line above (a local var, not a nearby comment mentioning USERPROFILE,
    # os.environ.get("USERPROFILE") or resolved_home`, an explicit
    # USERPROFILE rung one line up).
    (
        "coordinator_core/install/uninstall_legs.py",
        817,
        'claude_home = os.environ.get("CLAUDE_HOME") or home',
    ),
    # "baselined in RUNG_ORDER_BASELINE" -- false even at the time it was
    # written: `RUNG_ORDER_BASELINE` in `test_home_resolution_lint.py` is an
    # `expanduser` already honours USERPROFILE (verified both permutations
    # USERPROFILE under stock Windows, so both realistic environments
    (
        "coordinator_core/ops/check_posix_exec_assumptions.py",
        1667,
        'return os.environ.get("HOME") or os.path.expanduser("~")',
    ),
]

# run, all one shape -- `os.environ.get("CLAUDE_HOME"|"HOME"[, ...]) or
#     F4-reviewed -- see this file's RUNG_ORDER_BASELINE removal note)
# `USERPROFILE` rung or delegate to `Path.home()`) is out of this chunk's
