# PreToolUse advisory audit (C0)

Measured by `python -m coordinator_core.advisory_census --replay coordinator_core/tests/fixtures/pretooluse_advisory_corpus.jsonl`
at `baseline_ref` 38ea5f40af52bcabeb271d1168db4211b526995a (the tree exported with `git archive`, the census
module and corpus copied in; the live worktree carries peer edits, e.g. `block_topic_branch` measures 306 B
there against 564 B at the ref). One session, 182 payloads, bytes per site are the sum over its fires.
Baseline: 27 sites, 55 fires, 14,445 B total, 189 B median. Pinned in `pretooluse_advisory_baseline.json`.
The replay sandbox is hermetic: a synthetic doctrine content root (three-agent roster), Windows host verdict
forced for the bash chain, the sandbox path scrubbed to `<repo>`; the figures are the same on every machine
and say nothing about what a given box has installed.

C1-C3 read the `verdict` column and nothing else decides their write set; an advisory not in this table is
not touched (gated criterion: sites deleted plus sites gated equals this table's verdicts).

Rules applied (plan `## Anti-scope`): a site whose module or tests name a prevented failure (DR, lesson,
incident, regression, measured-live audit) is gated, never deleted; a site naming none is deleted; a site
that already fires once per scope, or is a DoE-requested steering nudge, is kept and pinned by the ratchet.

Gate warrant: DR-475 (`docs/decisions/DR-475-guards-deny-only-the-pm-allowlist-every.md`), "every other
guard nudges once"; and `docs/wiki/guard-messaging.md` "A repeat of a non-blocking advisory is silent".

Verdict `gate` means: first fire per (session, shape) emits, repeats are silent. For `bash:*` rows that is
`silence_repeat_advisory` in `bash_guards/dispatch.py`, which already exists; the work there is the key (see
"Why Bash repeats escape"). For every other row it is C1's `gate_once_per_session` at the dispatcher.

## Sites

| site | bytes | fires | file | failure it names | verdict |
|---|---|---|---|---|---|
| agent:block-unenumerated-agent-type | 190 | 1 | hooks/block_unenumerated_agent_type.py | measured-live unenumerated `hookprobe-named` dispatch, docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md | gate |
| bash:block-approval-sentinel-creation | 168 | 1 | bash_guards/block_approval_sentinel_creation.py | regression test, DR-260 (tests) | gate |
| bash:block-drive-root-new-dir-bash-write | 145 | 1 | bash_guards/block_drive_root_new_dir_bash_write.py | DR-258, DR-344 (tests) | gate |
| bash:cat-heredoc-write-advise | 645 | 3 | bash_guards/_dc_rewrites.py :: check_cat_heredoc_write_advise | DR-258 Amendment 2026-08-30 (record/commit consequence) | gate |
| bash:find-exec-rewrite | 299 | 1 | bash_guards/find_exec_rewrite.py | 879-process stall incident on Windows; regression test | gate |
| bash:git-commit-safe-commit-advise | 349 | 2 | bash_guards/_dc_commit.py :: check_git_commit_safe_commit_advise | DR-020, DR-008, regression (tests) | gate |
| bash:git-no-optional-locks | 129 | 1 | bash_guards/guard_no_optional_locks.py | docs/plans/2026-08-07-git-index-lock-contention-campaign | gate |
| bash:grep-via-bash-rewrite | 185 | 1 | bash_guards/_dc_rewrites.py :: check_grep_via_bash_rewrite | 879-process stall incident (file header), DR-290 (test) | gate |
| bash:inprocess-search | 485 | 3 | bash_guards/guard_inprocess_search.py | DR-009, incident, regression | gate |
| bash:plumbing-and-loops | 817 | 2 | bash_guards/guard_plumbing_and_loops.py | DR-280, incident, regression tests | gate |
| bash:sed-range-read-advise | 146 | 1 | bash_guards/_dc_rewrites.py :: check_sed_range_read_advise | none in the function; DESCOPED by docs/plans/2026-08-21-the-advisory-band-gets-smaller-cheaper-and-honest.md (CONTESTED E3.o2, per-call leg kept) | gate (never delete) |
| hooks.block_ungranted_opus_subagent:check | 342 | 2 | hooks/block_ungranted_opus_subagent.py | state/audits/2026-09-18-why-the-opus-subagent-guard-did-not-fire.md, measured live | gate |
| hooks.enforce_agent_dispatch_mode:_handler | 154 | 1 | hooks/enforce_agent_dispatch_mode.py | none; one fire per dispatch, a steering nudge | keep |
| hooks.nudge_workflow_authoring_trampoline:_handler | 201 | 1 | hooks/nudge_workflow_authoring_trampoline.py | none; already once per session by its own sentinel | keep |
| hooks.preuse_search_dispatch:_handler | 535 | 5 | hooks/preuse_search_dispatch.py | none; already once per (session, agent, shape) by sentinel; DoE-requested adoption nudge (state/cross-repo/inbox/2026-09-25-coordinator-content-repo-em-example-retrieval-repo-adoption-hooks.md), a delete is the APM's call | keep |
| hooks.preuse_sendmessage_dispatch:_handler | 76 | 1 | hooks/preuse_sendmessage_dispatch.py | none; already once per agent | keep |
| write:block_drive_root_new_dir_write | 143 | 1 | write_guards/block_drive_root_new_dir_write.py | DR-258 | gate |
| write:block_hand_authored_handoff_creation | 123 | 1 | write_guards/block_hand_authored_handoff_creation.py | hand-made handoff skips scaffold frontmatter, lineage and chain bookkeeping (module purpose; no DR) | gate |
| write:block_sizing_object_schema_violation | 820 | 2 | write_guards/block_sizing_object_schema_violation.py | schema failure surfacing a session later as a plan-scaffold failure (module purpose; no DR) | gate |
| write:guard_doctrine_surface_edits | 429 | 3 | write_guards/guard_doctrine_surface_edits.py | DR-277, DR-235, DR-088 | gate |
| write:nudge_new_sh_file_naked_python | 260 | 2 | write_guards/nudge_new_sh_file_naked_python.py | none (a doctrine statement, no failure) | delete |
| write:nudge_shell_shaped_spawn | 283 | 1 | write_guards/nudge_shell_shaped_spawn.py | DR-077; docs/plans/2026-08-06-shell-spawn-regrowth-gate | gate |
| write:nudge_windows_subprocess_popup | 820 | 2 | write_guards/nudge_windows_subprocess_popup.py | DR-077, DR-345; docs/plans/2026-06-19-windows-console-popup-coordinator-doctrine | gate |
| write:validate_frontmatter_schema_advisory | 4110 | 5 | write_guards/validate_frontmatter_schema_advisory.py | DR-277, DR-156; docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-schemas | gate |
| write:validate_frontmatter_schema_deny | 456 | 2 | write_guards/validate_frontmatter_schema_deny.py (its advisory-shaped offers; the deny path is out of scope) | DR-277, docs/plans/2026-08-06-apply-guard-class-census, regression | gate |

Totals by verdict: delete 1 site, gate 20, keep 4.

Not measured, verdict keep: `mise_autofire`, `pickup_autofire` and `handoff_segment_inject` (Skill legs).
They shell out to the installed bin forwarders, so their bytes depend on the box and cannot be replayed
hermetically (a handoff or mise invocation measured ~17 KB and ~11 KB on the authoring box). They inject the
content the invoked skill needs, once per invocation; they are not advisories and are out of the ratchet.

## Why Bash repeats escape `silence_repeat_advisory` today

`advisory_dedupe_key(guard, envelope)` = `<guard>__sha256(additionalContext minus Command: lines minus the
terse-alternative span)[:16]` (`bash_guards/_advisory_dedupe.py`). The call in `bash_guards/dispatch.py`
(`_session_advisory_already_fired`) is the only consumer. A repeat escapes when per-invocation tokens sit in
the prose outside those two stripped spans, so each firing mints a new key.

| bash site | fires in corpus | cause |
|---|---|---|
| bash:git-no-optional-locks | 1 | text is invariant: collapses as designed (the control case) |
| bash:block-topic-branch | 3 | the ref name is echoed in backticks in the prose (`topic`, `work/x`, `topic/x`) |
| bash:block-venv-creation | 2 | the offending command is echoed in backticks (`python -m venv`, `uv sync`) |
| bash:cat-heredoc-write-advise | 3 | the target file name is echoed in backticks (`f.py`, `g.py`, `h.py`) |
| bash:inprocess-search | 3 | the in-process result listing is part of the advisory text and varies per query |
| bash:git-commit-safe-commit-advise | 2 | two distinct shapes (no scope, amend): legitimate separate keys |
| bash:plumbing-and-loops | 2 | two distinct shapes (for-loop, while-read): legitimate separate keys |
| bash:block-approval-sentinel-creation, block-drive-root-new-dir-bash-write, find-exec-rewrite, grep-via-bash-rewrite, sed-range-read-advise | 1 each | one firing in the corpus; the echoed token is in the prose, so a repeat with a different token would escape |

C1 fixes the key, not the store: collapse backtick-quoted spans (and, for `inprocess-search`, the listing
tail) to a placeholder before hashing, so shape-identical firings share one key. Distinct shapes keep
distinct keys.

## Cross-check against the 2026-08-21 advisory-band plan

That plan retired `probe-spray`, `branch-set-precedence` and `longlived-branch-naming`, and descoped
`sed-range-read-advise` (kept, per-call leg stays). None of the three retired ids is in the registry or in
this table; `sed-range-read-advise` is verdict `gate`, never `delete`, so no site it retired is resurrected
and none it kept is removed.

## Not in the table

The corpus fires the 27 sites above; the other registered guards did not fire on it and are out of C1-C3 scope.
`python -m coordinator_core.advisory_census --static` lists the 16 PreToolUse builder call sites and the 130
registered guard-chain members. A site C2 or C3 finds emitting outside this table is a corpus gap: add a
payload to the corpus and re-pin, do not edit the advisory.
