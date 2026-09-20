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
# source line text) -- see the module docstring above. Only leading/trailing
# whitespace is stripped; internal whitespace is NOT normalized. A pure
# reformat that changes internal spacing on an already-baselined line (e.g.
# a formatter touching operator spacing) changes the stripped text, so the
# live finding no longer matches its baseline row and mints a phantom NEW
# finding -- `test_*_baseline_has_no_stale_entries` will simultaneously
# report the OLD row as stale. If a rule test goes red immediately after a
# pure reformat with no logic change: re-seed the affected row(s) with the
# new stripped text: do not treat the "NEW violation" as a real finding.

# 2026-08-01 update: X_OK_BASELINE reached ZERO. The last 28 rows were pruned
# together -- the fast-tier baseline-red sweep found every one of them stale
# (`test_x_ok_baseline_has_no_stale_entries` named them). 27 were real paydowns
# landed across earlier waves (guarded behind `if os.name != "nt":`, or routed
# through `coordinator_core.win_portability.is_executable`); the 28th,
# `_alternative_liveness.py`'s `if os.name != "nt" and ... and os.access(...)`,
# was always Windows-safe and became visible as such when the engine learned the
# short-circuit-operand guard shape (see `find_x_ok_checks`'s docstring). An
# empty list is the correct terminal state for a shrinking-debt ledger, NOT an
# invitation to refill it: a new X_OK site now fails `test_no_x_ok_access_check`
# outright, which is the whole point.
#
# 2026-08-08 (C8 re-seed, discovery widened per C1): ONE genuine false positive
# newly surfaced. `coordinator/bin/machine-local:96` -- `if
# os.path.isfile(base) and os.access(base, os.X_OK):` sits directly below an
# `if os.name == "nt":` block that ALREADY returns when `os.path.isfile(base)`
# is True (two return paths: the suffix loop, and the bare
# `if os.path.isfile(base): return base`). So on Windows, control only
# reaches line 96 when `os.path.isfile(base)` is already known False --
# `and` short-circuits and `os.access` never executes with a truthy first
# operand on native Windows. On POSIX (no `os.name == "nt"` branch taken at
# all) this is the real, intended X_OK check. Not the same shape as the
# `elif`-exemption the engine already understands (this is two sequential
# `if`s, not `if`/`elif`), so the engine's static pattern-match cannot see
# the mutual exclusivity -- verified by hand-tracing both branches, not by
# widening the engine.
#
# A second finding at this same run, `coordinator/bin/claude-doe:283`
# (`if ml_argv == [ml_bin] and not (os.path.isfile(ml_bin) and
# os.access(ml_bin, os.X_OK)):`), is a REAL defect -- no `os.name` guard
# anywhere nearby, unconditional on every platform. `coordinator/bin/**` is
# outside this chunk's write scope; deferred to a named successor rather
# than baselined (see this chunk's own run-report for the full reasoning).
#
# 2026-08-17: `coordinator/bin/machine-local:96` -- the site the note above
# names as the sole remaining false positive -- no longer matches a live
# finding (fixed or moved upstream). Row removed per
# `test_x_ok_baseline_has_no_stale_entries`. X_OK_BASELINE is empty again.
X_OK_BASELINE: list[tuple[str, int, str]] = []

COLON_JOIN_BASELINE: list[tuple[str, int, str]] = []

# 2026-07-29 update: 3 of the original 8 unique FORWARD_SLASH_BASELINE lines
# fixed (check_install_singularity.py:362, ensure_venv.py:372,
# uninstall_legs.py:823 -- all three fold "\\" to "/" before the
# endswith/rstrip check so an operator-set CLAUDE_HOME spelled with
# backslashes on native Windows shells is still recognized). 5 remain,
# for two DIFFERENT reasons -- see the dispatch's own report for the
# full per-site reasoning:
#   coordinator_core/install/scaffold_structure.py:99 -- `self.path` is a
#     manifest-authored string from `canonical-structure.yaml`
#     (DoE-owned, forward-slash by schema convention), never a live
#     OS-supplied filesystem path. Folding backslash here would be
#     inert (the manifest never contains one) at best, and would
#     misinterpret a literal backslash in a future template name as a
#     directory marker at worst -- not a home-resolution defect at all.
#   coordinator_core/trusted_root_guard.py:210,270,445,453 -- a SECURITY
#     GUARD whose Windows-safety is already proven by a separate
#     mechanism, not these four lines. `_doe_root`/`_claude_klabauter_root` (210,
#     270) deliberately strip only a raw POSIX "/" for byte-exact
#     bash-oracle parity (see `test_doe_root_only_single_trailing_slash_
#     stripped`, explicitly `skipif(os.name == "nt", ...)` because the
#     Windows case is asserted by a DIFFERENT test,
#     `test_windows_separator_and_case_normalization`); a raw
#     backslash-terminated sentinel survives this strip on purpose and is
#     re-stripped downstream in `is_trusted` (445, 453) AFTER `_norm()`
#     has already folded "\\" to "/" and lowercased. Folding backslash at
#     210/270 would strip the trailing separator too early, before
#     `_norm()` runs, changing the POSIX-parity contract that
#     `test_doe_root_only_single_trailing_slash_stripped` pins byte-for-
#     byte. 445/453 already operate on `_norm()`-folded strings (guarded
#     by `os.name == "nt"`), so they are correct exactly as written --
#     the lint's textual pattern-match cannot see that the value it's
#     inspecting was already normalized upstream. Widening any of these
#     four risks the exact "changed a security guard's behaviour
#     silently" outcome the dispatch brief calls out as the worst case.
FORWARD_SLASH_BASELINE: list[tuple[str, int, str]] = [
    ("coordinator_core/install/scaffold_structure.py", 99, 'return self.path.endswith("/")'),
    ("coordinator_core/trusted_root_guard.py", 210, 'if content.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 270, 'if content.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 445, 'if os.name == "nt" and doe_root.endswith("/"):'),
    ("coordinator_core/trusted_root_guard.py", 453, 'if os.name == "nt" and claude_klabauter_root.endswith("/"):'),
    # 2026-08-06 (post F8-merge, gen-doe-root-pointer/check-install-singularity
    # reconciliation): 4 new sites, same false-positive shape as
    # trusted_root_guard.py:445/453 above -- the lint's textual pattern-match
    # cannot see that the value it inspects was already normalized upstream.
    #   check_install_singularity.py:184/187/192 (`_to_plugin_root`) -- `raw`
    #     is folded via `_norm_sep(raw)` ONLY when `os.name == "nt"` (fixed
    #     2026-08-06: unconditional folding corrupted a literal POSIX
    #     backslash filename component, see
    #     `test_to_plugin_root_posix_backslash_left_untouched`). On Windows
    #     the value reaching these three lines is already forward-slashed;
    #     on POSIX a backslash is never a separator, so a forward-slash-only
    #     split is correct as written on both platforms.
    #   check_install_singularity.py:386 (`_check1_claude_home_suffix_guard`)
    #     -- inspects `claude_home_cmp`, not `claude_home`: the line directly
    #     above unconditionally sets `claude_home_cmp = _norm_sep(claude_home)`,
    #     so the value has already been backslash-folded before this endswith
    #     check runs on any platform.
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
# see cross-repo memo / DoE-claude judgment-half dispatch for the home-
# resolution defect class (bare_home_or_chain rule).
#
# 2026-09-11 (C7, engine_version=2026-09-19.1): the 4 entries that stood
# here -- check_install_singularity.py:550, gen_claude_doe_shim.py:145,
# install_health_run.py:222, block_dev_side_mirror_wiki.py:80 -- were the
# old engine's +/-3-line "nearby window" false positives: each "delegates to
# expanduser" one line outside the window the old detector scanned. The new
# engine's window reaches the delegation site directly, so none of these are
# live findings any more; rows deleted per
# `test_bare_or_baseline_has_no_stale_entries`, not re-baselined. The
# "delegates to expanduser" rationale that justified keeping them is stale
# under the new engine and does not describe any surviving row below.
BARE_OR_BASELINE: list[tuple[str, int, str]] = [
    # 2026-08-08 (C8 re-seed, discovery widened per C1/C4): genuine false
    # positives newly surfaced, two shapes:
    #
    # Shape A -- an OPTIONAL EXTRA root, not a resolution chain: a lone
    # `os.environ.get("CLAUDE_HOME", "")` / `os.environ.get("HOME", "")`
    # whose result, if non-empty, is APPENDED to a list already populated
    # from a separate (already Windows-safe) source, or used only as a
    # literal string for a containment/suffix check. No fallback path is
    # ever constructed from it -- an unset var means "skip", not "silently
    # resolve to a broken relative path" (the actual defect class this gate
    # exists to catch).
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
    (
        "coordinator_core/install/sandbox_check.py",
        837,
        'home_literal = os.environ.get("HOME", "")',
    ),
    # 2026-09-11 (C7, engine_version=2026-09-19.1): the Shape-A row that
    # stood here, `probe_onboarding_currency.py:175`, no longer matches a
    # live finding under the new engine's window and was deleted per
    # `test_bare_or_baseline_has_no_stale_entries` (its own review-
    # integration note -- that `claude_home` feeds a USERPROFILE-guarded
    # fallback chain 21 lines later -- is moot now that the new engine's
    # window reaches that chain directly instead of relying on this note).
    #
    # Shape B -- delegates to a value already resolved Windows-safely one
    # line above (a local var, not a nearby comment mentioning USERPROFILE,
    # since the rule's own docstring says a nearby mention no longer exempts
    # anything -- correctly, since a comment can lie): `uninstall_legs.py:817`'s
    # `home` is computed at line 816 (`os.environ.get("HOME") or
    # os.environ.get("USERPROFILE") or resolved_home`, an explicit
    # USERPROFILE rung one line up).
    (
        "coordinator_core/install/uninstall_legs.py",
        817,
        'claude_home = os.environ.get("CLAUDE_HOME") or home',
    ),
    # 2026-09-11 (C7, engine_version=2026-09-19.1): the row that stood here,
    # `test_resolvers_resolve_context.py:91`, no longer matches a live
    # finding under the new engine and was deleted per
    # `test_bare_or_baseline_has_no_stale_entries`. Its comment claimed the
    # chain it asserts against (`resolvers.py`'s `resolve_context()`) was
    # "baselined in RUNG_ORDER_BASELINE" -- false even at the time it was
    # written: `RUNG_ORDER_BASELINE` in `test_home_resolution_lint.py` is an
    # empty tuple, not a home for this or any other site.
    # 2026-09-11 (C7, engine_version=2026-09-19.1): the row that stood here,
    # `coordinator/bin/check-machine-path-leak.py:327`, no longer matches a
    # live finding under the new engine and was deleted per
    # `test_bare_or_baseline_has_no_stale_entries`. Its "correct construct,
    # not deferred debt" reasoning -- this chain reads HOME at its FIRST
    # rung, so the two realistic environments both resolve correctly and the
    # `expanduser` terminal is unreachable in either: under git-bash HOME is
    # set and wins at rung 1; under stock Windows HOME is unset and
    # `expanduser` already honours USERPROFILE (verified both permutations
    # 2026-08-08) -- still applies; it just no longer needs a baseline row
    # of its own to say so. The entry directly below,
    # `check_posix_exec_assumptions.py:1667`, deliberately mirrors this same
    # shape and is unaffected.
    #
    # 2026-08-17: `check_posix_exec_assumptions.py::_current_machine_home`
    # deliberately MIRRORS the shape that stood immediately above as
    # `coordinator/bin/check-machine-path-leak.py:327` (see the 2026-09-11
    # note directly above) -- its own docstring says so explicitly
    # ("Deliberately mirrors -- does not reinvent -- the discrimination
    # coordinator/bin/check-machine-path-leak.py's main() already draws").
    # Same construct, same reasoning, same verified-correct outcome: HOME
    # wins at rung 1 under git-bash, and `expanduser` already honours
    # USERPROFILE under stock Windows, so both realistic environments
    # resolve correctly and the chain is not the F10/F15 defect shape this
    # rule exists to catch. Not deferred debt -- a documented instance of
    # the same already-accepted correct construct, not a new site created
    # to dodge the gate.
    (
        "coordinator_core/ops/check_posix_exec_assumptions.py",
        1667,
        'return os.environ.get("HOME") or os.path.expanduser("~")',
    ),
]

# 2026-08-08 (C8 re-seed): 6 NEW `bare_home_or_chain` findings surfaced this
# run, all one shape -- `os.environ.get("CLAUDE_HOME"|"HOME"[, ...]) or
# os.path.expanduser("~")` (sometimes nested one call deeper, e.g. inside
# `os.path.join(...)`), in:
#   coordinator_core/ops/bootstrap_orchestrate.py:184 (_resolve_coordinator_root)
#   coordinator_core/ops/install_shell_init_guard_seam.py:150 (_resolve_rc_path)
#   coordinator_core/ops/migrate_state_to_claude_klabauter.py:313 (main)
#   coordinator/bin/check-machine-path-leak.py:327 (already independently
#     F4-reviewed -- see this file's RUNG_ORDER_BASELINE removal note)
#   coordinator/bin/gen-claude-klabauter-root-pointer.py:78 (_settings_home)
#   coordinator/bin/tests/test_claude_machine_local.py:54 (_default_settings_home)
#
# Deliberately NOT baselined here. `find_bare_home_or_chains`'s own
# docstring is explicit that an unguarded `expanduser` rung does not exempt
# a bare_or chain either way: "an unguarded os.path.expanduser call is the
# vulnerable site itself, not evidence the chain already guards against
# it." That is a considered design choice of the rule (distinct from rule 5
# / rung_order, which downgrades the same shape to a warning) -- reading it
# as a false positive here would be exactly the "reason says why the gate
# should pass, not why the construct is correct" shape this ledger's own
# bar forbids. These 6 are real, live findings of this rule and are
# reported as such in this chunk's own run-report rather than silenced
# here; fixing them (swap the bare `expanduser` rung for an explicit
# `USERPROFILE` rung or delegate to `Path.home()`) is out of this chunk's
# write scope.
