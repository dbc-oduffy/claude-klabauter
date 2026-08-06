# POSIX-portability fix-vs-carve-out discriminator (`path_separator` / `posix_mode_bits`)

> Written by C7 (2026-07-31-exec-cli-posix-leg-convergence). Gates C8-C11, which apply the
> per-file dispositions below. Read-only analysis — no code, test, or guard edits happen here.
> Oracle: `python3 coordinator_core/ops/check_posix_exec_assumptions.py`. Do not trust a count in
> prose (including in this doc, past today) — re-run it.

## Why this exists

PM ruling 2026-07-31 pulled `path_separator` and `posix_mode_bits` into scope as genuinely
Windows-hostile — not ratchet noise. But both classes are AST pattern-matches on a *construct*
(`.replace("/", "\\")`, `os.access(path, os.X_OK)`, …), and a construct is not automatically a
defect. Some flagged files manipulate Windows path *syntax* by definition — a literal `\\` is the
correct code there, and mechanically rewriting it to `os.sep` would break the very thing the file
does while making the gate green. That's the hazard: a blind sweep that trades a real gate for a
false one.

## The two-way test

Ask **what the literal separator/mode-bit construct is FOR**, not just where it appears.

**FIX** — the construct reasons about *this host's own* filesystem behavior, and a literal breaks
that reasoning off from the platform it's actually running on:
- Building or joining a path this process will hand to `open()`/`os.path`/`Path` I/O, using a
  hardcoded separator instead of `os.sep`/`os.path.join`/`pathlib`.
- Querying or setting a POSIX permission bit (`os.access(path, os.X_OK)`, `os.chmod` with an exec
  bit) as a *decision input* that Windows silently lies about (`os.access(..., os.X_OK)` returns
  True for any readable file on Windows, exec bit or not) — i.e. the code trusts the bit's answer
  on both platforms when only one platform's answer is meaningful.
- Remedy: `os.sep` / `os.path.join` / `pathlib.Path` for separators; a structural `os.name`/
  `sys.platform` guard (recognized by `_is_windows_guarded()`) around the POSIX-only permission
  check, with the Windows branch using its own (non-lying) signal, for mode bits.

**CARVE-OUT** — the construct's job is independent of which platform it runs on, and `os.sep`/an
`os.name` guard would be *wrong*, not merely unnecessary:
- Canonicalizing a path **string** (not a live filesystem path) to a fixed form — e.g.
  `.replace("\\", "/")` on a value that may have been *written* on a different platform than the
  one now reading it (a recorded frontmatter field, a `tool_input.file_path` from an editor
  payload, a git-diff line, an env var) — so it compares/stores consistently regardless of host.
  `os.sep` is the *wrong* fix here: `os.sep` reflects the host running the code, not the platform
  the string's author was on, and on POSIX it's a no-op that silently leaves stray backslashes
  unnormalized.
- Parsing Windows path **syntax** appearing as text being scanned (a UNC prefix `\\host\share` or
  a drive letter in a doc/prose/citation being linted) — the backslash is the thing being
  recognized, not a filesystem operation.
- Emitting a literal backslash for an unrelated grammar (e.g. a regex engine writing `\` as an
  *escape character* in translated output — nothing to do with paths at all).
- A permission-bit construct whose Windows behavior is a platform semantic gap no code change can
  close (e.g. `os.chmod` cannot make a *directory* actually write-blocked on Windows regardless of
  which bits are passed — this is an OS limitation, not a POSIX assumption in the code).
- Remedy: an `EXEMPTIONS` entry stating *why the literal is correct*, never "removing it would be
  disruptive" or "it makes the gate pass."

**Known residual, not a false alarm to route around:** `_is_windows_guarded()` recognizes a nested
`If` and the short-circuit `and`-chain shape, but not a bare early-return guard with no `else:`
(`if sys.platform.startswith('win'): return p` followed by the guarded code as the next sibling
statement — see that function's own docstring). If a real hit is in that exact shape, it still
gets an `EXEMPTIONS` entry (citing the gap), not a cosmetic `else:` added purely to satisfy the
detector. **None of the 28 live files below are in this shape** — checked individually; every hit
either reasons about a host-independent string/syntax (carve-out) or has no platform guard at all
(the one real fix, `_alternative_liveness.py`).

## The m8-frozen-snapshot precedent (commit `65d406fc`)

That commit carved three `state/review-trail/diffs/m8-baseline/*.py` files — verbatim
point-in-time snapshots of this engine's own guard modules, kept so a landed review's subject can
be re-read later. Editing them to satisfy the ratchet would falsify review evidence. Its reason
text named a structural follow-up: a directory-prefix exclusion for frozen snapshot trees, instead
of hand-listing files one at a time.

**BUILT 2026-08-03 — `EXEMPT_PREFIXES`.** The recommendation below was executed: the scanner now
carries `EXEMPT_PREFIXES` (`repo_key -> prefix -> reason`) alongside the per-file `EXEMPTIONS`, and
the three m8-baseline entries were migrated onto the single prefix
`state/review-trail/diffs/m8-baseline/`. Read the module docstring's *EXEMPT_PREFIXES* section
before adding one — the admission test is two-part (verbatim foreign/frozen bytes AND this repo
owns none of the runtime), it suppresses every class **except Tier A** (a checkout-breaker inside a
snapshot tree still breaks the repo's Windows clone), and
`check_no_stale_exempt_prefixes()` fails a prefix that matches no tracked file. Note what the
discriminator is not: *shipping* is fine — `dist/mirror-native/` is published verbatim to a mirror
and still qualifies, because the runtime is the destination's. Vendored-but-imported code never
qualifies.

**Original disposition, for the record: ADOPT the recommendation, not executed here.** None of the 24 `path_separator`
/ 4 `posix_mode_bits` live violations fall under `state/review-trail/` or any other frozen-snapshot
tree, so nothing in this wave needs it — but the next snapshot that lands (m9, m10, …) will hit
the same three hand-listed entries growing by however many files it contains, unless the scanner
gains a directory-prefix exclusion for review-trail snapshot trees. That's a real structural fix
to `check_posix_exec_assumptions.py`'s scope logic, is unrelated to C8-C11's file-level work, and
should be filed as its own backlog item rather than folded into this convergence.

## Disposition table

Grouped by wave-2 owner. `os.sep`/`pathlib` reasons are abbreviated per row; full `EXEMPTIONS`
reason text (ready to paste, verbatim) follows the table for each named reason-id.

### C8 — `coordinator_core/write_guards/`

| file | class | construct (enclosing function) | disposition | reason |
|---|---|---|---|---|
| `write_guards/_case_fold_path.py` | path_separator | `casefold_path()`: `raw.replace("\\", "/").casefold()` | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/block_derived_global_doctrine_write.py` | path_separator | `_normalize()`: `p.replace("\\", "/").casefold()`; `_authoring_path()`: `root.replace("\\", "/")...` | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/block_home_dir_memo_delivery.py` | path_separator | `_receiver_inbox()`: `root.replace("\\", "/")...` | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/block_oss_mirror_memo_delivery.py` | path_separator | `_casefold_path()`: `raw.replace("\\", "/").casefold()` | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/guard_memory_store_cap.py` | path_separator | inline in scan loop: `suffix.replace("\\", "/").split("/")` on `suffix`, sliced from `str(resolved)` (the raw, non-casefolded resolved path -- casefolding is done separately, on `resolved_cf`/`root_cf`, for the containment check, not for `suffix`) | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/nudge_new_sh_file_naked_python.py` | path_separator | `check()`: `normalized = file_path.replace("\\", "/")` on `tool_input.file_path` | CARVE-OUT | `REASON_TOOL_INPUT_PATH` |
| `write_guards/nudge_prose_queue_append.py` | path_separator | `check()`: `normalized = file_path.replace("\\", "/")` on `tool_input.file_path` | CARVE-OUT | `REASON_TOOL_INPUT_PATH` |
| `write_guards/nudge_prose_queue_creation.py` | path_separator | `check()`: `normalized = file_path.replace("\\", "/")` on `tool_input.file_path` | CARVE-OUT | `REASON_TOOL_INPUT_PATH` |
| `write_guards/validate_frontmatter_schema_advisory.py` | path_separator | `to_repo_relative()`, `is_memo_path_mislocated()`, `derive_sidecar_plan_stem()`, inline routing check: repeated `repo_rel.replace("\\", "/")` / `abs_path.replace("\\", "/")` | CARVE-OUT | `REASON_CANON_STRING` |
| `write_guards/validate_frontmatter_schema_deny.py` | path_separator | same shape as the `_advisory` twin above (paired module) | CARVE-OUT | `REASON_CANON_STRING` |

### C9 — `coordinator_core/bash_guards/` + `coordinator_core/ops/` + `coordinator_core/ops/session/`

| file | class | construct (enclosing function) | disposition | reason |
|---|---|---|---|---|
| `bash_guards/guard_offer_git_c.py` | path_separator | `_offer_normalize_path()`: `rest = m.group(2).replace("\\", "/")` rewrites a regex-matched Windows drive-letter path to MSYS/git-bash convention, then the function calls `Path(p).resolve()` -- a real filesystem stat, compared against the guard's own process `cwd` on the same host | CARVE-OUT (re-adjudicated post-review, see below) | `REASON_HOST_NATIVE_DRIVE_RESOLVE` |
| `bash_guards/tests/test_advisory_value_registry.py` | path_separator | `test_advisory_value_member_references_confined_to_two_files()`: `"/tests/" in str(path).replace("\\", "/")` over `rglob()` results | CARVE-OUT | `REASON_CANON_STRING` |
| `ops/append_integrator_dispositions.py` | path_separator | `_normalize()`: `value.replace("\\", "/")` then collapse `//` | CARVE-OUT | `REASON_CANON_STRING` |
| `ops/check_auto_memory_drained.py` | path_separator | `_slugify_repo_root()`: `str(root).replace("\\", "/")` before the `-` collapse (own docstring already states this is the encode-direction mirror of a decode helper) | CARVE-OUT | `REASON_CANON_STRING` |
| `ops/session/fix_concrete_path_citations.py` | path_separator | `_replacement_for()`: `rest.lstrip("/\\").replace("\\", "/")`; `_raw_hits_in_line()`: `m.group(0).lstrip("\\").split("\\")[0]` extracting a UNC host from scanned text | CARVE-OUT | `REASON_WIN_SYNTAX_IN_TEXT` |
| `ops/session/guard_concrete_path_citations.py` | path_separator | same UNC-host-extraction shape as its `fix_` twin; `_CHEAP_MARKERS` pre-filter tuple containing `"\\\\"` as a substring marker for UNC-shaped text | CARVE-OUT | `REASON_WIN_SYNTAX_IN_TEXT` |
| `ops/session/guard_foreign_platform_paths.py` (named candidate) | path_separator | `_suggest_corrected()`: `value.replace("\\", "/")`, `local_coordinator_root.replace("\\", "/")` — deliberately re-deriving a *foreign*-platform path's tail, independent of the host running the guard | CARVE-OUT | `REASON_CANON_STRING` |
| `ops/session/safe_commit_offer.py` | path_separator | inline: `entry = entry.replace("\\", "/")` after an already-platform-agnostic absolute-path pre-check (own comment: "three shapes multi-OS demands") | CARVE-OUT | `REASON_CANON_STRING` |

*(8 files: bash_guards 2, ops 2, ops/session 4.)*

### C10 — `coordinator_core/install/` + `baton_assemble` + `diff_scoped_tests` + `search/regex_translate` + `frontmatter/tests/`

| file | class | construct (enclosing function) | disposition | reason |
|---|---|---|---|---|
| `install/ensure_venv.py` | path_separator | inline CLAUDE_HOME check: `claude_home.replace("\\", "/").rstrip("/").endswith("/.claude")` — own comment already states the Windows-backslash rationale | CARVE-OUT | `REASON_CANON_STRING` |
| `install/uninstall_legs.py` | path_separator | inline HOME derivation: `_folded_claude_home = claude_home.replace("\\", "/")` — own comment states the same rationale | CARVE-OUT | `REASON_CANON_STRING` |
| `baton_assemble/__init__.py` | path_separator | 6 sites across `_supersede_continued`'s predecessor-lookup helpers and a path-relativization helper: all `.replace("\\", "/")` on `continued_into`/`predecessor` frontmatter field values or resolved-path fallbacks — one site's comment explicitly names the failure this avoids ("backslash silently produces an EMPTY intersection on Windows … never an error") | CARVE-OUT | `REASON_CANON_STRING` |
| `diff_scoped_tests.py` | path_separator | scoped-test resolution loop: `posix_path = raw.replace("\\", "/").strip()` on `git diff --name-only` / `git ls-files` output | CARVE-OUT | `REASON_CANON_STRING` |
| `search/regex_translate.py` | path_separator | `_escape_class_member()`: `return "\\" + c` — emitting a **regex escape character** for a Python `re` character class; not a path at all | CARVE-OUT | `REASON_NOT_A_PATH` |
| `frontmatter/tests/test_handoff_lineage_corpus_dangling_refs.py` | path_separator | both `test_no_dangling_handoff_lineage_references_in_corpus()` and `test_no_dangling_handoff_id_references_in_corpus()`: `repo_rel = os.path.relpath(abs_path, repo_root).replace('\\', '/')`, matching the forward-slash convention this repo's own frontmatter fields are recorded in | CARVE-OUT | `REASON_CANON_STRING` |

### C11 — all four `posix_mode_bits` files

| file | class | construct (enclosing function) | disposition | reason |
|---|---|---|---|---|
| `bash_guards/_alternative_liveness.py` (named candidate) | posix_mode_bits | `_resolve_on_path_or_settings_home()`: `if os.path.isfile(candidate) and os.access(candidate, os.X_OK): return candidate` runs UNGUARDED before the `if os.name == "nt":` `.cmd`-fallback branch that follows it | **FIX** | Add `os.name != "nt" and` to the `os.access(candidate, os.X_OK)` check (or reorder: try the `.cmd` twin first on Windows). Real defect: `os.access(path, os.X_OK)` returns True for *any readable file* on Windows regardless of actual executability (module docstring: "meaningless-to-lying"). If a bare-named, non-`.cmd` file happens to exist at `candidate` on a Windows box, this returns it as the resolved executable *before* the `.cmd` fallback ever runs — wrong candidate, not a hypothetical. |
| `coordinator/bin/tests/file-attribution/test_derive_file_attribution.py` | posix_mode_bits | `test_unwritable_cache_dir_does_not_crash_and_still_produces_rows()`: `os.chmod(self.cache_dir, stat.S_IREAD \| stat.S_IEXEC)` to simulate a write-blocked cache dir | CARVE-OUT | `REASON_CHMOD_DIR_GAP` |
| `hooks/test_em_report_altitude.py` | posix_mode_bits | `test_sentinel_write_failure_does_not_change_return_value()`: `os.chmod(os.path.dirname(path), 0o500)` — same write-blocked-directory simulation | CARVE-OUT | `REASON_CHMOD_DIR_GAP` |
| `ops/test_install_meta_repo_precommit_hook_install_all.py` | posix_mode_bits | `_write_stub_gates()` / inline in `test_preexisting_foreign_hooks_preserved_across_all_three()`: `os.chmod(script or hook_path, 0o755)` setting the exec bit on generated git-hook scripts. (Note: the file's *other* mode-bit check, `if os.name != "nt": assert os.access(hook_path, os.X_OK)`, is already structurally guarded and does not fire.) | CARVE-OUT | `REASON_CHMOD_EXEC_FOR_SH` |

## `EXEMPTIONS` reason text (verbatim, ready to paste)

Follow the existing pattern: one module-level `_SCREAMING_SNAKE_REASON` constant per distinct
rationale, referenced by every `EXEMPTIONS["path_separator"][...]` / `["posix_mode_bits"][...]`
key that shares it — do not inline-duplicate the string per file (see `_M8_REVIEW_TRAIL_SNAPSHOT_REASON`
being reused across 3 keys in the precedent commit).

**`REASON_CANON_STRING`** — the majority pattern (17 of 24 `path_separator` files):

> "This normalizes a path **string** — a recorded frontmatter field, a `tool_input.file_path`
> payload, a git-diff/`ls-files` line, an env var, or a resolved-path fallback — to a canonical
> forward-slash form for comparison or storage, independent of which platform the string's author
> or its current reader is on. `os.sep` is the wrong fix here: `os.sep` reflects the *host running
> this code*, not the platform the string came from, and is a no-op on POSIX that would leave a
> backslash written on Windows (or embedded in test fixture/frontmatter data) unnormalized. This
> is the correct, portable idiom for that job, not a POSIX assumption."

**`REASON_TOOL_INPUT_PATH`** — the three write-guard/hook `check()` functions normalizing a raw
`tool_input.file_path` string before pattern-matching it (same idiom as `REASON_CANON_STRING`,
called out separately only because the caller is a hook payload rather than a frontmatter field):

> "Normalizes `tool_input.file_path` — a string supplied by the editing tool, which may already
> contain either separator depending on the invoking platform — to forward-slash form before this
> hook's own carve-out/suffix matching runs. `os.sep` would only recognize this host's native
> separator, missing a payload written with the other one; the literal `.replace()` is the fix,
> not the debt."

**`REASON_WIN_SYNTAX_IN_TEXT`** — UNC/drive-letter parsing over scanned prose or command text (not
a live filesystem path):

> "Parses Windows path **syntax** (a UNC `\\host\share` prefix or a drive-letter form) appearing as
> literal text being scanned — prose, a citation, or a bash-command argument — not a filesystem
> path this process will open. The backslash is the syntax being recognized, correct on every host
> regardless of what `os.sep` is there; rewriting it to `os.sep` would stop recognizing the exact
> Windows-path shape this code exists to detect."

**`REASON_HOST_NATIVE_DRIVE_RESOLVE`** — `guard_offer_git_c.py`'s
`_offer_normalize_path()`, re-adjudicated post-review (was originally filed under
`REASON_WIN_SYNTAX_IN_TEXT`; see "Re-adjudication" below):

> "Review: code-reviewer flagged `_REASON_WIN_SYNTAX_IN_TEXT` as a false premise for this site —
> it claims the string is never opened, but this function ends in `Path(p).resolve()`, which does
> stat the real filesystem. The construct is still a carve-out, on different grounds:
> `_offer_normalize_path()` first rewrites a Windows drive-letter prefix to MSYS/git-bash's
> forward-slash drive convention, THEN resolves it. Both the target and the `cwd` it's compared
> against are native to the SAME host this guard process is running on — `cwd` is the guard's own
> process `cwd`, passed in by the caller, not a citation of a foreign machine's path. On a Windows
> box running git-bash, a `cd`-target written in native drive-letter form and the bash-reported
> `cwd` (in MSYS form) can differ only in drive-letter convention while naming the same real
> directory; without the rewrite step, `resolve()` would compare a Windows-form path against an
> MSYS-form one and never match even when they are the same place. The drive-letter rewrite is
> what makes the subsequent host-filesystem `resolve()` correct, not something in tension with it
> — `os.sep` is still the wrong fix, since it does not know the MSYS convention either. This
> differs from `REASON_CANON_STRING`'s territory: that class covers a string whose resolve is
> deliberately never taken (comparison/storage only), while this one's whole point is a correct
> host-filesystem `resolve()`."

**Re-adjudication (2026-07-31, post-review):** the original disposition filed this site under
`REASON_WIN_SYNTAX_IN_TEXT` on the theory that the construct only parses Windows path *syntax*
appearing as text, never opening a real path. A code-reviewer pass (Finding 1,
`state/subagent-share/4503453c-41d2-43b9-a415-c330007e0c55/coordinatorcode-reviewer-714b90ac.md`)
correctly caught that this is false: `_offer_normalize_path()` ends in `Path(p).resolve()`, a real
filesystem stat, so the site does not fit `REASON_WIN_SYNTAX_IN_TEXT`'s own stated bar ("not a
filesystem path this process will open"). It also doesn't cleanly fit `REASON_CANON_STRING`,
because that class's remedy logic assumes the string's `resolve()` is never taken — here the
`resolve()` is the entire point. `REASON_HOST_NATIVE_DRIVE_RESOLVE` is a new, narrower bucket
(currently one file) for this shape: a drive-letter-to-MSYS rewrite whose sole purpose is enabling
a correct SAME-host filesystem resolve/comparison.

**`REASON_NOT_A_PATH`** — `regex_translate.py`'s escape-character emission:

> "`\"\\\\\" + c` emits a **regex escape character** for a translated Python `re` character class
> — this function has no relationship to filesystem paths at all; the AST shape (`BinOp` `Add`
> with a literal backslash constant) is structurally identical to a path-concatenation hack but the
> semantic content is unrelated. Renaming or restructuring this to satisfy a path-shaped detector
> would not make the code more portable — it isn't path code to begin with."

**`REASON_CHMOD_DIR_GAP`** — `os.chmod` used to simulate an unwritable *directory* in a test:

> "Simulates a write-blocked directory via `os.chmod` to exercise the corresponding error-handling
> path. On Windows, the read-only attribute `os.chmod` can toggle for a *directory* does not
> block writes into it the way a POSIX permission bit does — this is a platform semantic gap in
> what `chmod` means for directories on NTFS, not a code defect fixable by passing different mode
> bits. No portable equivalent exists in the standard library; a genuine Windows-native
> write-block simulation (e.g. an ACL deny entry) is a separate mechanism, out of this
> convergence's scope. The test does not crash on Windows — it degrades to weaker coverage there,
> which is a known, named limitation, not silently-passing debt."

**`REASON_CHMOD_EXEC_FOR_SH`** — `os.chmod(path, 0o755)` on generated git-hook scripts this same
test file then runs via a hardcoded `/bin/sh`:

> "Sets the POSIX exec bit on a generated git-hook script that this same test file immediately
> executes via a hardcoded `subprocess.run(['/bin/sh', str(hook)], ...)` — the exec bit is
> necessary and correct for that POSIX-only invocation path. The test's dependency on `/bin/sh`
> (not itself a `posix_mode_bits`-scanned construct) already makes this test POSIX-only end to
> end; making the isolated `chmod` call portable would not make the test runnable on Windows and
> would be cosmetic. A genuine Windows-parity leg for this test (installed `.cmd` hooks, no `/bin/sh`
> dependency) is a real follow-up but a materially larger change than this convergence's scope —
> not taken here."

## Known widening of risk: file-level `EXEMPTIONS` granularity (accepted, not fixed here)

`EXEMPTIONS` keys by relpath, not by line or AST node — an exempted file is invisible to the
`path_separator`/`posix_mode_bits` scan wholesale, not just at the specific construct identified
in this convergence's review. Before this wave, that mechanism covered 3 curated, single-site
entrypoints. This wave takes it to 27 files, several of them multi-site
(`baton_assemble/__init__.py` alone has 6 separate `.replace("\\", "/")` sites under one entry;
`validate_frontmatter_schema_advisory.py`/`_deny.py` have 4-5 each).

**What this costs:** a genuinely new, unrelated backslash-path defect — a hardcoded Windows
separator built for real `open()`/`os.path` I/O, the exact FIX-class construct this gate exists to
catch — landing later in one of these 27 files will be silently invisible to
`check_posix_exec_assumptions.py`, because the whole file is now exempt from that class, not just
today's identified construct. This is a real, deliberate widening of blast radius, accepted as the
cost of this convergence rather than solved by it.

**Not addressed here:** line-level or AST-node-level exemption granularity, which would close this
gap without giving up the carve-outs. Related to, but distinct from, the m8 frozen-snapshot
directory-prefix-exclusion follow-up named above — both are structural gaps in this same module's
scope logic, and both are deferred as separate backlog items rather than folded into this
convergence.

## Notes for C8-C11

- Every FIX/CARVE-OUT call above was made by reading the enclosing function, not the AST hit
  alone — re-verify against a fresh gate run before applying; the live violation set can drift.
- The one real fix (`_alternative_liveness.py`) needs a test pinning the old-vs-new behavior per
  the plan's C8 body's general instruction ("a rewrite that silently changes matching behavior…is
  worse than the violation") even though it lives in C11's scope, not C8's — same discipline
  applies.
- Do not widen `state/posix-exec-baseline.json` for any of these — carve-outs go in `EXEMPTIONS`,
  never the baseline (per the module's own fix-instruction text).
