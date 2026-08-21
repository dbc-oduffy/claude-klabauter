"""
coordinator_core.op_scopes — op-to-scope keying table (dependency-free).

Purpose: Home for _OP_KEY_SCOPE / OP_KEY_SCOPE / WORKTREE_SCOPED_OPS, split out of
coordinator_core.ipc so that `import coordinator_core` (which re-exports OP_KEY_SCOPE /
WORKTREE_SCOPED_OPS as a cross-repo parity surface) does not transitively pull in ipc.py's
`import asyncio` — a stdlib-only op such as coordinator_core.ops.mint_deliverable_id has no
business loading the asyncio stack just to read a plain dict. ipc.py re-exports these names
for backward compatibility (`from coordinator_core.ipc import OP_KEY_SCOPE` keeps working).

Negative-spec: this module MUST remain stdlib-only (types, nothing else) — do not import
ipc.py, asyncio, or anything else that would reintroduce the cold-import cost this split
exists to avoid.

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-python-bin-cold-invocation-minutes-per-call.md
"""

from __future__ import annotations

import types as _types
from typing import Dict


# ---------------------------------------------------------------------------
# AC-1b op-keying table (C1c)
#
# Maps each registered op to the scope used to derive its per-request repo key:
#
#   "common_dir" — key is git_common_dir(resolved_worktree); shared across all
#                  linked worktrees of the same repo (correct for ops that read
#                  from the main-worktree-rooted state/ tree, e.g. handoffs,
#                  emit ops).
#   "show_top"   — key is the resolved _origin_worktree itself (per-worktree);
#                  correct for ops whose state is genuinely per-worktree
#                  (e.g. coverage.gate which reads per-worktree git log).
#   "none"       — op accesses no repo-specific state; _origin_worktree not
#                  required.  Handler receives None as repo_root.
#
# NOTE: "central" scope has been RETIRED as of 2026-07-07. Emit ops
# (artifact.emit, backlog.record, goal.append) were reclassified from
# "central" to "common_dir" by the per-repo-emission-cutover plan
# (docs/plans/2026-07-07-per-repo-emission-cutover.md, chunk C3).
# "central" meaning "route via coordinator_state_root --central, bypass
# per-request key" was a conflation of store-less-ness with single-central-
# emission; per-repo emission corrects it. The "central" block is now empty
# — any new op should use "common_dir", "show_top", or "none".
#
# Default for an op NOT in this table: "none" — unclassified/test-only ops
# are not required to supply _origin_worktree.  All production ops are
# listed explicitly; a missing entry is an oversight, not a silent promotion
# to working-tree-scoped (which would require a key).
#
# Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C1c
# DR:            docs/decisions/2026-07-04-coordinator-core-global-multiplex-topology.md § AC-1b
# Amendment:     docs/plans/2026-07-07-per-repo-emission-cutover.md § C3
# ---------------------------------------------------------------------------
_OP_KEY_SCOPE: Dict[str, str] = {
    # Working-tree, keyed on git_common_dir (shared across linked worktrees)
    # Emit ops (artifact.emit, backlog.record, goal.append): per-repo emission —
    # each emitting repo supplies _origin_worktree so the handler resolves
    # main_worktree_root(common_dir) and attributes the snapshot to the emitting repo.
    # Reclassified from "central" → "common_dir" per 2026-07-07 per-repo-emission-cutover.
    "artifact.emit":                         "common_dir",
    "backlog.record":                        "common_dir",
    "goal.append":                           "common_dir",
    "orientation.regenerate_cache":          "common_dir",
    # emit.cadence — sequences backlog.record then artifact.emit over the same common_dir
    # key (derives main_worktree_root(common_dir) exactly like its two sub-ops).
    "emit.cadence":                          "common_dir",
    # emission.publish — TRANSPORTS the artifact artifact.emit produced, so it must key on
    # the same scope that produced it: it resolves main_worktree_root(repo_root) and reads
    # state/cockpit-emission.json off the main-worktree-rooted state/ tree, exactly as
    # artifact_emit._artifact_emit does. "show_top" would key per-worktree and let a linked
    # worktree publish a different (or absent) artifact than the one the emit ops wrote;
    # "none" would deny the handler the repo_root it needs to find the artifact at all.
    # It is a WRITE (outbound, to cockpit's sink) but writes nothing repo-local, so the
    # scope question is purely about which tree it READS from — that is the common dir.
    "emission.publish":                      "common_dir",
    # workflow.fire / workflow.fire_status — the fire registry and its logs live
    # under <git-common-dir>/coordinator-sessions/workflow-fires (fire.py::_registry_dir
    # walks from the envelope's repo_root), so every linked worktree of a repo must
    # see the same live-fire population — the concurrency cap is counted over it.
    "workflow.fire":                         "common_dir",
    "workflow.fire_status":                  "common_dir",
    # Working-tree, keyed on git_common_dir (shared across linked worktrees)
    "handoff.has_live_children":             "common_dir",
    # Same class as handoff.has_live_children above — reads from the main-
    # worktree-rooted state/handoffs + archive/handoffs subtrees.
    "handoff.blocked_by_dependents":         "common_dir",
    # Reference implementation only — deregistered from DoE's Agent PreToolUse matcher
    # (2026-07-31 parallel-emitter race); the live reroute is DoE's
    # _foreground_dispatch_strip.py port. A scope entry here is not a liveness claim.
    "hooks.nudge_foreground_agent_dispatch": "common_dir",
    "hooks.nudge_em_code_dispatch":          "common_dir",
    "hooks.track_touched_files":             "common_dir",
    "hooks.session_heartbeat":               "common_dir",
    # hooks.receiver_state_sensor — common_dir, same reason as session_heartbeat
    # immediately above: repo_root resolves the session dir under
    # .git/coordinator-sessions/ for the receiver-state sibling-file write.
    "hooks.receiver_state_sensor":           "common_dir",
    "hooks.agent_completion_log":            "common_dir",
    "hooks.track_dispatched_agents":         "common_dir",
    "hooks.subagent_zero_tool_use":          "common_dir",
    "hooks.subagent_zero_tool_use_surface":  "common_dir",
    "hooks.subagent_zero_tool_use_resolve":  "common_dir",
    # hooks.subagent_review_mark — common_dir, same reason as
    # subagent_zero_tool_use directly above (same SubagentStop event, same
    # gitdir-internal store): repo_root resolves <git_common_dir>/
    # coordinator-sessions/.commit-ledger/ for the review-mark append.
    "hooks.subagent_review_mark":            "common_dir",
    # hooks.subagent_fabrication_check — common_dir: repo_root is used to
    # resolve the worktree root for a live `git status --porcelain` probe on
    # the agent's declared target path(s), same reason subagent_zero_tool_use_resolve
    # is common_dir (needs git_common_dir to locate its per-session store).
    "hooks.subagent_fabrication_check":      "common_dir",
    # hooks.subagent_sidecar_fill_check — common_dir: repo_root arrives as the
    # gitdir (git_common_dir(request_repo), NOT the worktree root — see
    # ipc.resolve_op_repo_key). The handler derives the worktree root via
    # main_worktree_root(git_common_dir(repo_root)) before joining
    # state/subagent-share/<session_id>/ (the scan target), same idiom as
    # subagent_fabrication_check; repo_root itself (already the gitdir) is
    # used directly as the advisory-dedupe marker root, same reason
    # subagent_zero_tool_use_resolve is common_dir.
    "hooks.subagent_sidecar_fill_check":     "common_dir",
    # Working-tree, keyed on show-toplevel (per-worktree)
    "coverage.gate":                         "show_top",
    # cutover.gate — common_dir (NOT show_top like coverage.gate above): cutover
    # records live in the main-worktree-rooted state/ tree (state/roadmap/**/cutovers/),
    # the documented common_dir case, whereas coverage.gate is show_top because it reads
    # per-worktree git log (op_scopes.py:33-35 above). Do not "fix" this to show_top by
    # analogy with the coverage.gate precedent immediately above it — that is the exact
    # copy-error misreading this comment exists to head off (Review: the Director of Engineering-cutover-review F17).
    "cutover.gate":                          "common_dir",
    # cutover.advance — common_dir, same reasoning as cutover.gate immediately above:
    # it calls cutover.gate internally against the same main-worktree-rooted
    # state/roadmap/**/cutovers/ record and must resolve the identical worktree.
    "cutover.advance":                       "common_dir",
    # Review: code-reviewer (F13) — show_top is the KEYING scope (which worktree's daemon
    # partition handles the request), not the memo location. Memo may live in any registered
    # git repo's cross-repo/; containment gate (memo_transition.py:_containment_check) governs
    # reachability. The prior comment "memo lives in the caller worktree" was misleading for
    # the primary cross-repo use case (memo in sibling repo's cross-repo/inbox/).
    "memo.transition":                       "show_top",   # worktree-scoped per caller (memo may be in any registered git repo's cross-repo/ — containment governs reachability)
    # No repo state accessed — _origin_worktree not required
    "ping":                                  "none",
    # invoke.from_argv — the served warm-door entrypoint (coordinator_core.
    # ops.invoke_from_argv). Its own params carry an explicit `cwd`; any
    # repo_root resolution the dispatched argv itself needs happens INSIDE
    # its own re-entrant call to _dispatch_argv, from that explicit `cwd`,
    # never from this op's repo_root (always None) or from _origin_worktree.
    "invoke.from_argv":                      "none",
    "hooks.suggest_sonnet_research":         "none",
    # hooks.subagent_arrival_check — no repo state accessed: derives and reads
    # ONLY the subagent transcript path built from the caller-supplied
    # transcript_path + agent_id; repo_root is unused (always None). Same
    # "none" class as ping / the other repo_root-less hooks.* ops here.
    "hooks.subagent_arrival_check":          "none",
    "hooks.nudge_unauthorized_handoff":      "none",
    # hooks.nudge_named_agent_report_delivery — no repo state accessed at all: reads
    # only the caller-supplied tool_name/tool_input params and returns an advisory
    # envelope. No file I/O of any kind; repo_root is unused (always None).
    "hooks.nudge_named_agent_report_delivery": "none",
    "hooks.postuse_advisory_dispatch":       "none",
    # hooks.context_pressure_precompact — no repo state accessed: writes its
    # sentinel + state-snapshot only to tempfile.gettempdir(), keyed by
    # session_id, never to a repo/worktree path; repo_root is unused (always
    # None). Same "none" class as ping / the other repo_root-less hooks.* ops
    # above, not common_dir like session_heartbeat (its "follows the
    # session_heartbeat.py shape" note in the module docstring is about the
    # async-bookkeeping/write-side-effect PATTERN, not repo-key scoping).
    "hooks.context_pressure_precompact":     "none",
    # percolate.run / percolate.validate_store — no repo state accessed; all paths
    # (store_path, target_root) are explicit caller-supplied params, not resolved
    # relative to a repo/worktree root. Spec: docs/plans/2026-07-10-percolation-engine-claude-klabauter.md § Tasks C15.
    "percolate.run":                         "none",
    "percolate.validate_store":              "none",
    # cruft_sweep.run — no repo_root-derived state access; every path
    # (projects_root, file_history_root, handoffs_dir, log_path, repo_root_override,
    # parent_roots) is an explicit caller-supplied param, matching the bash oracle's
    # own flag-driven config (no implicit machine-local/registry read inside this
    # module).
    "cruft_sweep.run":                       "none",
    # engine.drift — no repo state accessed: read-only drift probe that inspects the
    # engine's OWN checkout (Path(__file__)), not the caller's repo; _origin_worktree
    # not required (same "none" class as ping / percolate.validate_store).
    "engine.drift":                          "none",
    # plugin_health.drift — no repo state accessed: read-only drift probe that inspects
    # the OPERATOR's OWN machine-local plugin registry (settings-home resolved via
    # coordinator_core._settings_home), not the caller's repo; _origin_worktree not
    # required (same "none" class as engine.drift / percolate.validate_store).
    "plugin_health.drift":                   "none",
    # app_session.launch / .census / .teardown — the launch target is a CONSUMING
    # repo supplied by the caller, and the spawned-process handles persist under
    # that repo's git common dir (never <root>/.git, which is a FILE in a linked
    # worktree). "common_dir" so every linked worktree of one repo censuses and
    # tears down the same set of processes: a launch from one worktree and a
    # teardown from a sibling must not see disjoint state, or teardown silently
    # leaves the process running.
    "app_session.launch":                    "common_dir",
    "app_session.census":                    "common_dir",
    "app_session.teardown":                  "common_dir",
    # plugin_health.scan — no repo state accessed: read-only reader of the
    # OPERATOR's OWN machine-local plugin/consumer sentinel roots
    # (COORDINATOR_PLUGINS_ROOT / COORDINATOR_CONSUMER_HEALTH_ROOT env-resolved,
    # default ~/.claude/plugins and ~/.claude), not the caller's repo;
    # _origin_worktree not required (same "none" class as plugin_health.drift).
    "plugin_health.scan":                    "none",
    # plugin_health.sentinel — no repo state accessed: fires the coordinator-doctor
    # probe suite against the OPERATOR's OWN machine (CLAUDE_HOME, settings-home,
    # machine-local registry, resolved Python interpreter), not the caller's repo;
    # _origin_worktree not required (same "none" class as plugin_health.drift /
    # plugin_health.scan).
    "plugin_health.sentinel":                "none",
    # cartography.* — fleet-generic, COMPUTE_ONLY ops (coordinator-core-op-target-
    # resolution-model: explicit `target_root` wire param, any repo, NOT the caller's
    # own dispatching tree). No repo-specific state is accessed via repo_root/
    # _origin_worktree — every handler ignores the repo_root argument entirely and
    # resolves solely from the caller-supplied target_root param (path-guarded).
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2/C3/C4.
    "cartography.tree":                      "none",
    "cartography.file_index":                "none",
    "cartography.churn":                     "none",
    "cartography.symbols":                   "none",
    "cartography.edges":                     "none",
    # cartography.op_edges -- "none": same cartography.* target-resolution
    # model (explicit target_root wire param, ANY repo, never the caller's
    # own dispatching tree); registry-dispatch producer/consumer edge graph
    # over a caller-supplied `files` list, no repo_root involvement.
    # Spec: cross-repo memo, 2026-08-06 architecture survey.
    "cartography.op_edges":                  "none",
    # goals.reassess_krs — no repo_root-derived state access, all paths (goals_dir,
    # bin_dir, signal_repo_root) are explicit caller-supplied params from the DoE-side
    # trampoline, which resolves them itself exactly as the original bash script
    # derived SCRIPT_DIR/REPO_ROOT from its own BASH_SOURCE location.
    "goals.reassess_krs":                    "none",
    # goal.set_kr_status — no repo_root-derived state access: goal_file is an
    # explicit caller-supplied absolute path (same class as goals.reassess_krs
    # above); the optional repo_root param, when supplied, only steers the
    # locked_write.py lock-sidecar location, not any repo-scoped state read.
    "goal.set_kr_status":                    "none",
    # workflow.validate — fleet-generic, COMPUTE_ONLY op (mirrors the
    # cartography target-resolution model): explicit `script_path` wire
    # param, any repo, NOT the caller's own dispatching tree. No repo-
    # specific state is accessed via repo_root/_origin_worktree — the
    # handler ignores the repo_root argument entirely and resolves solely
    # from the caller-supplied script_path (+ optional target_root),
    # path-guarded. Spec: docs/plans/2026-07-12-workflow-skeleton-stamper-
    # claude-klabauter-engine.md § C2.
    "workflow.validate":                     "none",
    # distill.curate_clusters — pure structural gate over a caller-supplied
    # {system_tag: count} map: no file is read, no repo_root/_origin_worktree
    # state is accessed at all (not even a path read, one level purer than
    # workflow.validate above). See coordinator_core/ops/distill_curate_clusters.py.
    "distill.curate_clusters":               "none",
    # distill.workflow_input — pure translation from a caller-supplied
    # distill.scope scope-manifest dict to the distill-harvest Workflow
    # script's consumer shape (runId/batches[]/wikiSlugs/repoRoot); no file
    # is read, no repo_root/_origin_worktree state is accessed (repoRoot
    # comes from the plain params key, not the engine-injected common_dir).
    # See coordinator_core/ops/distill_workflow_input.py.
    "distill.workflow_input":                "none",
    # workflow.scaffold — fleet-generic, COMPUTE_ONLY op: pure generation
    # from caller-supplied name/description/phases/pattern params, no repo
    # state accessed at all (not even a path read) — the handler ignores
    # repo_root/_origin_worktree entirely. Spec: docs/plans/2026-07-12-
    # workflow-skeleton-stamper-claude-klabauter-engine.md § C3.
    "workflow.scaffold":                     "none",
    # compute_layer.scaffold — fleet-generic, COMPUTE_ONLY op with two modes,
    # neither of which touches repo state through repo_root/_origin_worktree:
    # `emit` composes producer module TEXT from caller-supplied
    # skill_name/verbs and returns it (the caller writes), and `check` scores
    # the Sub-shape B producers read-only. Listed explicitly rather than
    # relying on this table's absent-entry default, for the same reason
    # dispatch.emit below is: that default covers unclassified and test-only
    # ops, and this is neither. Spec: docs/plans/2026-08-13-compute-layer-
    # scaffolder.md § C4.
    "compute_layer.scaffold":                "none",
    # dispatch.emit — fleet-generic, MUTATING op: reads a caller-supplied plan
    # path and writes an emitted Workflow script, with containment resolved from
    # the `output_path`/`target_root` wire params rather than from
    # repo_root/_origin_worktree — the same target-resolution model as
    # workflow.validate. Listed explicitly rather than relying on this table's
    # absent-entry default: that default is documented for unclassified and
    # test-only ops, and dispatch.emit is neither. Both its siblings above are
    # explicit for the same reason.
    "dispatch.emit":                         "none",
    # review.mint_workflow — fleet-generic, MUTATING op: reads a caller-
    # supplied plan path plus the DoE-owned review-roster fragment (resolved
    # via read_doe_root_pointer(), not repo_root/_origin_worktree) and writes
    # an emitted review Workflow script, with containment resolved from the
    # `output_path`/`target_root` wire params -- the same target-resolution
    # model dispatch.emit above uses. Listed explicitly for the same reason
    # dispatch.emit is: the absent-entry default is documented for
    # unclassified and test-only ops, and this is neither.
    "review.mint_workflow":                  "none",
    # strategic.generate — fleet-generic, MUTATING op (mirrors the cartography/
    # workflow target-resolution model): explicit REQUIRED `target_root` wire
    # param, any repo, NOT the caller's own dispatching tree. No repo-specific
    # state is accessed via repo_root/_origin_worktree — the handler ignores
    # the repo_root argument entirely and resolves solely from the caller-
    # supplied target_root, path-guarded. Spec: docs/plans/2026-07-11-claude-klabauter-
    # strategic-self-description-generation-leg.md § C1.
    "strategic.generate":                    "none",
    # strategic.emit — fleet-generic, MUTATING op (mirrors strategic.generate's target-
    # resolution model): explicit REQUIRED `target_root` wire param, any repo, NOT the
    # caller's own dispatching tree. The handler ignores the repo_root argument entirely and
    # resolves solely from the caller-supplied target_root, path-guarded. Spec:
    # tasks/strategic-feed-emission/stub.md
    "strategic.emit":                        "none",
    # testing.full_runner — fleet-generic, COMPUTE_ONLY op (mirrors the cartography/
    # workflow/strategic.generate target-resolution model): explicit REQUIRED
    # `target_root` wire param, any repo, NOT the caller's own dispatching tree. No
    # repo-specific state is accessed via repo_root/_origin_worktree — the handler
    # ignores the repo_root argument entirely and resolves solely from the
    # caller-supplied target_root, path-guarded. Spec: docs/plans/2026-07-19-
    # claude-klabauter-doe-full-test-runner.md § C5.
    "testing.full_runner":                   "none",
    # fleet.* archival-writer ops — keyed on git_common_dir (shared across linked worktrees);
    # handlers derive worktree via main_worktree_root(common_dir), never repo_root directly.
    # DR-211 D4 async mandate + plan Key Decision 5.
    "fleet.archive_completed_plans":         "common_dir",
    "fleet.archive_completed_handoffs":      "common_dir",
    "fleet.prune_closed_bugs":               "common_dir",
    "fleet.aggregate_capability_index":      "common_dir",
    "fleet.reap_unintegrated_findings":      "common_dir",
    "fleet.reap_integrated_findings":        "common_dir",
    # fleet.handoffs_for_plan — COMPUTE_ONLY read op, but still "common_dir": unlike
    # memo.list (registry-only, scope "none"), this op enumerates the CALLING repo's
    # own state/handoffs/ + archive/handoffs/ trees for a given origin_plan_id, so it
    # needs the caller's own worktree resolved via main_worktree_root(common_dir),
    # same keying class as queue.cluster/queue.age_ping's own-worktree reads. Without
    # this entry dispatch resolves repo_root=None and the handler returns a setup-error
    # envelope rather than silently reading the wrong (or no) repo.
    # Spec: cross-repo memo from claude-central-em, 2026-07-26.
    "fleet.handoffs_for_plan":               "common_dir",
    # commit.anchors — keyed on git_common_dir: reads main-worktree-rooted state/handoffs/
    # + the staged diff; handler derives worktree via main_worktree_root(common_dir), same as
    # the fleet ops. Spec: docs/plans/2026-07-04-claude-klabauter-commit-anchor-stamper.md § D2/AC2.
    "commit.anchors":                        "common_dir",
    # handoff.* lifecycle ops — keyed on git_common_dir: in-place frontmatter mutation of
    # state/handoffs/*.md files, which live in the main-worktree-rooted state/ tree.
    # DR-212 sanctioned carve-out; spec:
    # docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md
    "handoff.transition":                    "common_dir",
    "handoff.stamp":                         "common_dir",
    # handoff.correct_body — keyed on git_common_dir: single-file body-correction
    # op (DR-247, amended by docs/plans/2026-08-06-executing-session-can-
    # discharge-criteria.md's DR), same key-scope class as handoff.stamp/
    # handoff.transition — the target resolves under EITHER a
    # state/handoffs/*.md file (live) OR an archive/handoffs/YYYY-MM/*.md
    # file (archive-follow, AC12), both in the main-worktree-rooted tree, and
    # the handler derives worktree via main_worktree_root(common_dir), same
    # as handoff_stamp.py's own _handler. A terminal (shipped/continued/
    # closed) archived target is refused (AC14) — the archive-follow widens
    # WHERE this op looks, not what it may touch once it finds a file.
    # Without this entry dispatch resolves repo_root=None and the op fails
    # outright — the exact unreachable-escape-hatch defect
    # docs/plans/2026-07-31-claimed-baton-body-correction-route.md exists to
    # close, reproduced by omission (see that plan's chunk C2 body and the
    # records.query backfill comment below for the shape of what happens
    # when a registered op is left without this entry).
    "handoff.correct_body":                  "common_dir",
    # handoff.discharge_criteria — keyed on git_common_dir, inheriting
    # handoff.correct_body's key-scope class by construction: it is a bounded
    # wrapper that delegates its entire write to that op's handler (DR-274 D3),
    # so it resolves the identical target set — a live state/handoffs/*.md file
    # or an archive-follow archive/handoffs/YYYY-MM/*.md one — and derives
    # worktree the same way. Without this entry dispatch resolves repo_root=None
    # and the op fails outright, exactly as the comment above describes.
    "handoff.discharge_criteria":            "common_dir",
    # handoff.propagate — keyed on git_common_dir, same class as
    # handoff.correct_body/handoff.stamp: single-file target under the
    # main-worktree-rooted state/handoffs/ tree, handler derives worktree via
    # main_worktree_root(common_dir). Without this entry dispatch resolves
    # repo_root=None and the op fails outright.
    "handoff.propagate":                     "common_dir",
    # plan.propagate — keyed on git_common_dir, same class as handoff.propagate
    # (B2, AC8): same module, same single-file target shape, just rooted under
    # docs/plans/ instead of state/handoffs/; handler derives worktree via
    # main_worktree_root(common_dir) exactly as handoff.propagate does. Without
    # this entry dispatch resolves repo_root=None and the op fails outright.
    "plan.propagate":                        "common_dir",
    # handoff.stamp_phase — keyed on git_common_dir, same class as handoff.stamp:
    # in-place frontmatter mutation of a single state/handoffs/*.md file
    # (handoff_phase + execution-only four-field stamp). Also reads plan_path
    # under main-worktree-rooted docs/plans/ (read-only; no write). Spec:
    # docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md § C4
    "handoff.stamp_phase":                   "common_dir",
    # handoff.ship_and_archive — event-driven single-handoff ship+archive composite;
    # derives worktree via main_worktree_root(common_dir), same as handoff.stamp/ship
    # and the fleet.archive_shipped_handoffs act path it delegates to.
    "handoff.ship_and_archive":              "common_dir",
    # handoff.archive_transition — the 4-mode (chain/stamp_shipped/stamp_only/
    # supersede) archive-ceremony op;
    # derives worktree via main_worktree_root(common_dir), same as handoff.stamp/
    # handoff.transition/handoff.ship_and_archive. Spec: cross-repo DoE 7-bug
    # route item 7. Position A (PM-ratified 2026-07-15): no branch-tip fallback,
    # no Session-Id correction walk — item-7 eliminated at source, not patched.
    "handoff.archive_transition":            "common_dir",
    # handoff.reconcile_close_terminal — composed close (DR-084 human/session-only
    # terminal) + archive (handoff.archive_transition mode="chain") for the
    # "reconcile concluded terminal, no successor" shape; derives worktree via
    # main_worktree_root(common_dir), same class as handoff.ship_and_archive/
    # handoff.archive_transition. Spec: cross-repo/inbox/2026-08-04-market-
    # intelligence-em-baton-terminal-state-not-cleared-programmatically.md.
    "handoff.reconcile_close_terminal":      "common_dir",
    # handoff.backfill_claim_stamp — reconstructs a missing claim stamp
    # (claimed_at/claimed_by) from caller-supplied, git-verified evidence.
    # Derives worktree via main_worktree_root(common_dir), same class as
    # handoff.reconcile_close_terminal directly above. Spec:
    # docs/plans/2026-08-11-a-claim-stamp-backfill-verb-and-the-lega.md.
    "handoff.backfill_claim_stamp":          "common_dir",
    # handoff.repoint_origin — sanctioned hook-exempt writer for the
    # origin_handoff provenance edge; repoints/nulls origin_handoff on an
    # existing state/handoffs/*.md stub. Derives worktree via
    # main_worktree_root(common_dir), same as handoff.normalize/handoff.stamp.
    "handoff.repoint_origin":                "common_dir",
    "handoff.close_origin_stub":             "common_dir",
    "handoff.normalize":                     "common_dir",
    # handoff.reconcile_open — keyed on git_common_dir: enumerates+decides over
    # main-worktree-rooted state/handoffs/*.md, then delegates per-handoff mutation to
    # handoff.ship_and_archive / handoff.transition gate-cascade-clear (both above,
    # same "common_dir" scope). Orchestrating op, not a batch-mutation carve-out — see
    # coordinator_core/ops/handoff_reconcile.py module docstring for the DR-212
    # compliance argument. Spec:
    # docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C4
    "handoff.reconcile_open":                "common_dir",
    # initiative.serve_set + roadmap.serve — keyed on git_common_dir: both read from
    # main-worktree-rooted paths (state/initiatives/, state/handoffs/, archive/handoffs/);
    # handlers derive the worktree root via main_worktree_root(common_dir), mirroring
    # handoff_children.py. Spec: docs/plans/2026-07-05-claude-klabauter-served-initiative-roadmap-read-model.md § C2/C5
    "initiative.serve_set":                  "common_dir",
    "roadmap.serve":                         "common_dir",
    # roadmap.link_stubs — keyed on git_common_dir: writes into main-worktree-
    # rooted state/handoffs/*.md (two files, the reciprocal blocked_by/blocks
    # edge), same key-scope class as handoff.transition/handoff.stamp above —
    # the handler derives the worktree via main_worktree_root(common_dir),
    # exactly as those verbs do. DR-264, spec:
    # docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md § C4
    "roadmap.link_stubs":                    "common_dir",
    # goal.match_candidates — keyed on git_common_dir: reads state/goals/ under
    # main_worktree_root(common_dir), same key-scope as initiative.serve_set/roadmap.serve.
    # Spec: docs/plans/2026-07-06-goal-setting-okr-legibility-system.md § C3
    "goal.match_candidates":                 "common_dir",
    # goal.close_day — keyed on git_common_dir: reads the collapsed goals-log wire
    # under main_worktree_root(common_dir)-resolved central_state_root, same
    # key-scope class as goal.match_candidates.
    # Spec: docs/plans/2026-07-25-day-goal-close-out-lifecycle.md § C2
    "goal.close_day":                        "common_dir",
    # goal.close_day_apply — keyed on git_common_dir: writes the per-machine
    # goals-log shard under main_worktree_root(common_dir)-resolved
    # central_state_root, same key-scope class as goal.append.
    # Spec: docs/plans/2026-07-25-day-goal-close-out-lifecycle.md § C3
    "goal.close_day_apply":                  "common_dir",
    # plan.match_candidates — keyed on git_common_dir: reads docs/plans/ under
    # main_worktree_root(common_dir), same key-scope as goal.match_candidates.
    # Without this entry dispatch resolves repo_root=None and the op returns empty.
    # Spec: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C2
    "plan.match_candidates":                 "common_dir",
    # handoff.match_candidates — keyed on git_common_dir: reads state/handoffs/ under
    # main_worktree_root(common_dir), same key-scope as goal.match_candidates.
    # Without this entry dispatch resolves repo_root=None and the op returns empty.
    # Spec: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C2
    "handoff.match_candidates":              "common_dir",
    # handoff.lineage_ancestry — keyed on git_common_dir: reads state/handoffs/ under
    # main_worktree_root(common_dir), same key-scope as handoff.match_candidates.
    # Without this entry dispatch resolves repo_root=None and the op returns empty ancestry.
    # Spec: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C4
    "handoff.lineage_ancestry":               "common_dir",
    # deliverable.rollup — keyed on git_common_dir: resolves DR-207 deliverable-spine
    # forward-edges by scanning main-worktree-rooted docs/plans/*.md + state/handoffs/ +
    # archive/handoffs/ + state/initiatives/; handler derives the worktree via
    # main_worktree_root(common_dir), same as roadmap.serve / initiative.serve_set. Without this
    # entry dispatch resolves repo_root=None and the op returns empty for every deliverable.
    # Spec: docs/plans/2026-07-06-claude-klabauter-deliverable-spine-factsupply-op.md § C2/C3.
    "deliverable.rollup":                    "common_dir",
    # spec_backlink.resolve / spec_backlink.rewrite — keyed on git_common_dir: both
    # read the same corpus shape (docs/plans/*.md, archive/specs/**, state/sizings/**)
    # via main_worktree_root(common_dir), same class as deliverable.rollup above.
    # Spec: pln-spec-backlinks-cite-a-stable-d-451b3e § C1
    "spec_backlink.resolve":                 "common_dir",
    "spec_backlink.rewrite":                 "common_dir",
    # queue.* write ops — keyed on git_common_dir: handlers derive caller worktree via
    # main_worktree_root(common_dir) for project-scope path resolution (F1/AC13).
    # DR-213 sanctioned carve-out; spec: docs/plans/2026-07-05-strang-08-queue-append-strangle.md § C1/C2
    "queue.append":                          "common_dir",
    # peer_notice.send / peer_notice.check — same-repo peer-contention notice
    # channel: keyed on git_common_dir (state/peer-notices/ is main-worktree-
    # rooted, same class as queue.append above).
    "peer_notice.send":                      "common_dir",
    "peer_notice.check":                     "common_dir",
    # queue.close — same keying class as queue.append: it stamps and commits one
    # entry under the caller's own state/improvement-queue/, then hands that
    # already-committed path to fleet.archive_queue_entry (DR-270).
    "queue.close":                           "common_dir",
    "queue.promote":                         "common_dir",
    # queue.cluster / queue.age_ping — keyed on git_common_dir: both cluster/scan the
    # CALLER's own main-worktree-rooted queue-family directories (state/debt-backlog/,
    # state/bug-backlog/, state/improvement-queue/) via
    # queue_family.load_family_records(family, repo_root), same *keying* class as
    # queue.append's write scope (both need the caller's own worktree resolved via
    # common_dir), and the same read pattern as deliverable.rollup/memo.triage's
    # own-worktree reads. Without this entry dispatch resolves repo_root=None and each op raises
    # (queue.cluster) or returns an error envelope (queue.age_ping) rather than
    # silently reading nothing / the wrong repo.
    # Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C3/C4
    "queue.cluster":                         "common_dir",
    "queue.age_ping":                        "common_dir",
    # handoff.scaffold_from_queue — keyed on git_common_dir: writes a new
    # state/handoffs/*.md baton under the CALLER's own main-worktree-rooted tree
    # (handler derives worktree via main_worktree_root(repo_root)), same scope class
    # as handoff.author_fork. Without this entry dispatch resolves repo_root=None and
    # the handler returns an explicit error rather than silently writing into (or
    # failing to write into) the wrong repo.
    # Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C5
    "handoff.scaffold_from_queue":           "common_dir",
    # memo.send — cross-tree write op — keyed on git_common_dir: handler derives sender
    # worktree via main_worktree_root(common_dir) (Key Decision 5 precedent; fleet.* pattern).
    # Receiver inbox path is always registry-derived, never wire-derived (C2 containment spec).
    # DR-214-send-class sanctioned carve-out; spec:
    # docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C3
    # HTTP/UDS gating vacated by DR-215 (command-type execution model): no gate chain in
    # dispatch.py; MUTATING ops are serial-by-construction in the in-process model.
    "memo.send":                             "common_dir",
    # memo.list — "none": repo_root is accepted per the standard handler signature
    # but unused. Receiver enumeration/resolution reads only the machine-local
    # registry (not sender-worktree-scoped substrate); unlike memo.send there is
    # no own-inbox check to make here, since memo.list never writes. Listed
    # explicitly (rather than left to the "none" default) so the wiring is
    # documented alongside its memo.draft/memo.compose siblings, not implicit.
    # Spec: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C2/C7
    "memo.list":                              "none",
    # memo.check_addressee — "common_dir", diverging from memo.list's "none":
    # unlike memo.list, this op NEEDS the caller's own repo to compute
    # self_root (the addressee verdict is self_root vs to_root), so it must be
    # common_dir-scoped — same precedent as memo.send/memo.draft. Without this
    # entry dispatch resolves repo_root=None and the handler fails loud on the
    # missing-repo_root guard.
    # Spec: state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md
    "memo.check_addressee":                   "common_dir",
    # memo.draft / memo.compose — keyed on git_common_dir: both stage into the
    # CALLING repo's OWN cross-repo/outbox/ (never a receiver's inbox — that
    # crossing is memo.send's alone), so the handler derives the sender's own
    # worktree via main_worktree_root(common_dir), same precedent as memo.send /
    # fleet.* / deliverable.rollup. Without this entry dispatch resolves
    # repo_root=None and both handlers fail loud on the missing-repo_root guard.
    # Spec: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5/C7
    "memo.draft":                             "common_dir",
    "memo.compose":                           "common_dir",
    # memo.list_outbox — keyed on git_common_dir: enumerates the CALLING
    # repo's OWN state/memo-outbox/ tree (same worktree the sender's own
    # memo.draft/memo.compose calls stage into), so the handler derives the
    # caller's own worktree via main_worktree_root(common_dir), mirroring
    # memo.draft/memo.compose's own scoping. Diverges from memo.list's "none"
    # because memo.list reads the machine-local registry (not worktree-
    # scoped substrate), while memo.list_outbox's whole purpose is reading
    # THIS repo's own outbox directory. Without this entry dispatch resolves
    # repo_root=None and the handler fails loud on the missing-repo_root guard.
    # Spec: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md
    "memo.list_outbox":                       "common_dir",
    # memo.blitz_buckets — keyed on git_common_dir for the same reason
    # memo.list_outbox is: it reads the CALLING repo's own cross-repo/inbox/
    # (worktree-scoped substrate), not the machine-local registry, so it needs
    # the caller's worktree derived via main_worktree_root(common_dir).
    # Diverges from memo.list's "none" on exactly that axis. The op's optional
    # `inbox_dir` override is the one path that does not need repo_root; the
    # handler fails loud when neither is available rather than guessing a cwd.
    # Spec: cross-repo/inbox/2026-07-28-example-retrieval-repo-em-inbox-blitz-proven-pattern.md
    "memo.blitz_buckets":                     "common_dir",
    # ceremony.wsc_tail — the C9 single-pass rebuild orchestrator; keyed on git_common_dir.
    # Supersedes the two-phase ceremony.wsc_resolve / ceremony.wsc_commit pipeline (both
    # ops' registrations were removed 2026-07-29, kill-list op removal — wsc_resolve.py's
    # engine survives as coordinator_core/ops/ceremony/branch_resolution.py, still imported
    # live by ceremony.session_instructions; wsc_commit.py was deleted outright). Resolves
    # + runs the full pre-commit tail + the locked commit critical section + the C5
    # post-commit consumed-handoff stamp + AC17 follow-up commit, all in ONE in-process
    # pass (no receipt round-trip between resolve and commit — AC6).
    # Handler derives worktree via main_worktree_root(common_dir) per the fleet.* /
    # handoff.* precedent.
    # Spec: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C9
    "ceremony.wsc_tail":                     "common_dir",
    # ceremony.post_commit_tail — keyed on git_common_dir, same as wsc_tail (the
    # only in-process caller): the C3a (2026-07-23 wsc-tail-slim-down) extraction
    # of wsc_tail's steps 5c (C5 post-commit consumed-handoff stamp+ship) and 5d
    # (origin-stub close) into one standalone op. Handler derives worktree via
    # main_worktree_root(common_dir), same as wsc_tail.
    # Spec: docs/plans/2026-07-23-wsc-tail-slim-down.md § C3a
    "ceremony.post_commit_tail":             "common_dir",
    # ceremony.session_instructions — read-mostly render op; keyed on git_common_dir.
    # It reuses the SAME _resolve_branches call (branch_resolution.py, the surviving
    # engine of the retired ceremony.wsc_resolve op) over session-shape.json under
    # <common_dir>/coordinator-sessions/<sid>/ and renders the pruned instruction set.
    # No durable write of its own (renders off an already-resolved context; does not
    # emit a receipt). Handler derives worktree via main_worktree_root(common_dir).
    # Spec: docs/plans/2026-07-08-session-specific-instruction-set-emitter.op-spec.md
    "ceremony.session_instructions":         "common_dir",
    # strang-10 A+B residual writer strangle — changelog / completion / review-trail write ops.
    # Keyed on git_common_dir: changelog.* + review_trail.write write main-worktree-rooted state/
    # (handler derives worktree via main_worktree_root(common_dir), never repo_root/'state' directly);
    # completion.* mutate a caller-supplied docs/plans/*.md path. All MUTATING, sanctioned by DR-216.
    # Spec: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md
    "changelog.append_day":                  "common_dir",
    "changelog.backfill_gaps":               "common_dir",
    "changelog.inject_anchor":               "common_dir",
    # changelog.compute_day_fields — COMPUTE_ONLY sibling of changelog.append_day
    # (Zone-A build #4): keyed identically since it reads the same worktree-rooted
    # git history / state/handoffs / state/review-trail / state/week-changelog
    # substrate append_day writes to (handler derives worktree via
    # main_worktree_root(common_dir), same as append_day).
    "changelog.compute_day_fields":          "common_dir",
    # changelog.upsert_reviewed — surgical single-field **Reviewed:** upsert (2026-07-21
    # ask: cross-repo/inbox/2026-07-21-claude-central-em-reviewed-line-surgical-upsert.md).
    # Keyed identically to append_day/backfill_gaps: handler derives worktree via
    # main_worktree_root(common_dir) and writes the same state/week-changelog/{date}.md noun.
    "changelog.upsert_reviewed":             "common_dir",
    "completion.reconcile_commits":          "common_dir",
    "plan.append_session":                   "common_dir",
    "review_trail.write":                    "common_dir",
    # Backfill: records.query (strang-11 C1a COMPUTE_ONLY read op) was registered without an
    # _OP_KEY_SCOPE entry by a concurrent session, leaving the key-scope coverage gate RED on HEAD.
    # Reads main-worktree-rooted project records → common_dir (matches deliverable.rollup precedent).
    "records.query":                         "common_dir",
    # records.history — file-set resolution + single-pass `git log -p -U0` derivation over
    # record-type directory pathspecs (C1a/C1b), walked across every registered ACTIVE sibling
    # root (C5), not just the calling repo's own tree. Follows fleet.work_state's own
    # registration triple verbatim (closest sibling in shape and blast radius) — same
    # cross-repo target-resolution shape → "none".
    # Spec: docs/plans/2026-08-20-a-time-axis-for-any-record-type.md, chunk C2.
    "records.history":                        "none",
    # handoff.columns — keyed on git_common_dir, matching records.query, whose collectors it
    # reuses verbatim: the handler derives main_worktree_root(repo_root) and reads
    # main-worktree-rooted state/handoffs/ plus (opt-in) archive/handoffs/. Without this entry
    # dispatch resolves repo_root=None and the op returns an EMPTY payload rather than failing —
    # the caller sees a working surface serving nothing, which is the exact shape this op exists
    # to stop a cross-repo consumer from being handed.
    # Spec: docs/plans/2026-08-11-pull-surface-four-columns-and-the-archive.md § C3.
    "handoff.columns":                       "common_dir",
    # memo.triage — keyed on git_common_dir: handler resolves the caller's worktree via
    # main_worktree_root(repo_root) and reads main-worktree-rooted cross-repo/archive/*.md +
    # docs/decisions/*.md + CLAUDE.md (deliverable.rollup / records.query precedent). Without
    # this entry dispatch resolves repo_root=None and the op returns the all-zero empty outcome.
    # Spec: docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C5.
    "memo.triage":                            "common_dir",
    # updatedocs.gates — keyed on git_common_dir: the gate battery reads
    # main-worktree-rooted docs/, state/ and the bundled plugin wiki, the same
    # surfaces records.query and memo.triage resolve through. show_top would be
    # wrong — none of the eight gates reads per-worktree git state the way
    # coverage.gate does. Memo: doe-claude-em updatedocs-gates-structured-verdicts.
    "updatedocs.gates":                       "common_dir",
    # distill.scope — keyed on git_common_dir: composes harvest_debt/ripe_filter/
    # sidecar_sweep over the CALLER's own main-worktree-rooted archive/specs,
    # archive/handoffs, cross-repo/archive, docs/wiki trees (handler derives worktree
    # via main_worktree_root(common_dir), mirroring memo.triage/deliverable.rollup).
    # DR-228 § D6 scratch-tier writer; writes only into its own run-id subdirectory
    # under state/scratch/artifact-distillation/. Without this entry dispatch resolves
    # repo_root=None and the handler fails loud (matches artifact.emit's AC5 precedent).
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C10
    "distill.scope":                          "common_dir",
    # memo.fate_partition (C16, DR-228 § D6 scratch-tier) — keyed on git_common_dir:
    # handler resolves the caller's worktree via main_worktree_root(repo_root) and
    # reads main-worktree-rooted cross-repo/archive/*.md, writing its shard under
    # main-worktree-rooted state/scratch/artifact-distillation/<run_id>/ — same
    # scope class as memo.triage. Without this entry dispatch resolves repo_root=None
    # and the op raises rather than silently no-op'ing (this op is MUTATING, not
    # COMPUTE_ONLY — a resolved write target is mandatory).
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C16
    "memo.fate_partition":                    "common_dir",
    # memo.fate_backfill (2026-08-06, cross-repo ask item 1(a)) — keyed on
    # git_common_dir: handler resolves the caller's worktree via
    # main_worktree_root(repo_root) and reads main-worktree-rooted
    # cross-repo/archive/*.md, same read surface as memo.fate_partition.
    # COMPUTE_ONLY (no shard write, no memo.transition call) — without this
    # entry dispatch resolves repo_root=None and the handler returns the
    # all-zero empty outcome rather than reading the caller's actual corpus.
    # Spec: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-reader.md § 1(a)
    "memo.fate_backfill":                      "common_dir",
    # distill.curation_status (C11, DR-228 § D6 scratch-tier) — keyed on git_common_dir:
    # handler resolves the caller's worktree via main_worktree_root(repo_root), reads
    # main-worktree-rooted docs/plans + archive/specs + state/distillation-log.md +
    # state/improvement-queue, and (only when params["emit"] is truthy) writes the
    # single fixed main-worktree-rooted state/ceremony/curation-status.json artifact —
    # same scope class as memo.fate_partition/artifact.emit. Without this entry
    # dispatch resolves repo_root=None and the handler raises rather than silently
    # deriving against the wrong worktree.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C11
    "distill.curation_status":                "common_dir",
    # distill.assemble_disposal_manifest (C12, DR-228 § D1 disposal tier) —
    # keyed on git_common_dir: handler resolves the caller's worktree via
    # main_worktree_root(repo_root), reads candidate paths relative to that
    # worktree, and writes the disposal-manifest under main-worktree-rooted
    # state/scratch/artifact-distillation/<run_id>/ — same scope class as
    # distill.scope/distill.curation_status/memo.fate_partition. Without this
    # entry dispatch resolves repo_root=None and the handler raises rather
    # than silently deriving against the wrong worktree.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C12
    "distill.assemble_disposal_manifest":     "common_dir",
    # distill.stamp_disposal (C13, DR-228 § D2b(vi)/D3) — keyed on git_common_dir:
    # handler resolves the caller's worktree via main_worktree_root(repo_root) and
    # reads/in-place-rewrites C12's own disposal-manifest under main-worktree-rooted
    # state/scratch/artifact-distillation/<run_id>/ — same scope class as
    # distill.assemble_disposal_manifest/distill.curation_status/memo.fate_partition.
    # Without this entry dispatch resolves repo_root=None and the handler raises
    # rather than silently deriving against the wrong worktree.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C13
    "distill.stamp_disposal":                  "common_dir",
    # distill.apply_disposal (C14, DR-228 § D2a/D2b/D3/D4 disposal tier —
    # Phase 5 delete) — keyed on git_common_dir: handler resolves the
    # caller's worktree via main_worktree_root(repo_root), reads C12/C13's
    # own stamped disposal-manifest under main-worktree-rooted
    # state/scratch/artifact-distillation/<run_id>/, and (the one deleting
    # member of the disposal tier) git-rm's/unlinks eligible paths plus
    # appends+commits state/distillation-log.md — all resolved against that
    # SAME worktree. Without this entry dispatch resolves repo_root=None and
    # the handler raises rather than silently deriving against (or deleting
    # from) the wrong worktree.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C14
    "distill.apply_disposal":                  "common_dir",
    "crossrepo.closure_status":               "common_dir",
    # ceremony.update_docs_scan (C17) — keyed on git_common_dir: handler resolves the
    # caller's worktree via main_worktree_root(repo_root) and reads main-worktree-rooted
    # coordinator_core/DIRECTORY.md, docs/plans/*.md, cross-repo/archive/*.md, tasks/*.md,
    # plus a bounded `git log` window scoped to that worktree — same read-only scope
    # class as handoff.has_live_children. Without this entry dispatch resolves
    # repo_root=None and the handler raises rather than silently deriving against the
    # wrong worktree. Never writes.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C17
    "ceremony.update_docs_scan":               "common_dir",
    # deliverable.cascade_terminal / cascade_retract / cascade_backstop_sweep —
    # keyed on git_common_dir: handlers read/write main-worktree-rooted
    # state/handoffs/ + docs/plans/, same scope class as handoff.has_live_children.
    # Both documented triggers (plan_status_transition._run_cascade,
    # post_commit_tail._run_deliverable_cascade) call the handler function
    # directly with an explicit repo_root=, bypassing dispatch — but any call
    # routed through the registered-op JSON-RPC surface without this entry
    # resolves repo_root=None and the handler raises rather than silently
    # deriving against the wrong worktree.
    # Review: coordinator:code-reviewer — op-registration quad completeness gap
    "deliverable.cascade_terminal":            "common_dir",
    "deliverable.cascade_retract":              "common_dir",
    "deliverable.cascade_backstop_sweep":       "common_dir",
    # deliverable.fork_detect — keyed on git_common_dir for the same reason as the
    # three cascade ops above: the detector walks the main-worktree-rooted corpus
    # (state/handoffs/, docs/plans/) through seed_deliverable_ledger_rows. Read-only —
    # it reports slug-prefix fork families and writes nothing, the equivalence map
    # included.
    # Spec: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md § AC12
    "deliverable.fork_detect":                  "common_dir",
    # sizing.decline — keyed on git_common_dir, same scope class as
    # deliverable.cascade_terminal above: the handler reads/writes a
    # main-worktree-rooted state/sizings/ file, derived via
    # main_worktree_root(common_dir). Without this entry dispatch resolves
    # repo_root=None and the handler raises rather than silently deriving
    # against the wrong worktree.
    # Spec: docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md § C2
    "sizing.decline":                           "common_dir",
    # sizing.ship — same scope class as sizing.decline above: the handler
    # reads/writes a main-worktree-rooted state/sizings/ file, derived via
    # main_worktree_root(common_dir). Without this entry dispatch resolves
    # repo_root=None and the handler raises rather than silently deriving
    # against the wrong worktree.
    # Spec: PM ruling 2026-08-13 (see coordinator_core/ops/sizing_ship.py docstring)
    "sizing.ship":                               "common_dir",
    # sizing.record_spike_verdict — same scope class as sizing.ship/sizing.decline
    # above: the handler reads/writes a main-worktree-rooted state/sizings/ file
    # and reads a docs/research/spike-verdicts/ file, both derived via
    # main_worktree_root(common_dir). Without this entry dispatch resolves
    # repo_root=None and the handler raises rather than silently deriving
    # against the wrong worktree.
    # Spec: coordinator_core/ops/sizing_spike_verdict.py docstring
    "sizing.record_spike_verdict":               "common_dir",
    # strang-11 B8 new ops — all keyed on git_common_dir: handlers derive worktree via
    # main_worktree_root(common_dir), matching the fleet.*/handoff.*/ceremony.* precedent.
    # Class-A ops (fleet.*) use archive_and_commit and read/write main-worktree-rooted paths.
    # Class-B/composite ops (session.*) read/write .git/coordinator-sessions/ and state/ paths,
    # both derived from the main worktree root. Spec: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C5
    "fleet.archive_shipped_handoffs":        "common_dir",
    "fleet.archive_actioned_memos":          "common_dir",
    # fleet.backfill_dispositionless_memos — same "common_dir" keying as the other
    # fleet.* ops: the handler derives the worktree via main_worktree_root(common_dir)
    # to resolve cross-repo/archive/<filename> for each of the 34 backfill-table
    # entries. Spec: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C5
    "fleet.backfill_dispositionless_memos":  "common_dir",
    # fleet.backfill_reference_edges — same "common_dir" keying: the handler
    # derives the worktree via main_worktree_root(common_dir) to walk
    # docs/plans/ and state/handoffs/ and stamp `references:` edges.
    # Spec: docs/plans/2026-08-18-a-session-always-has-a-baton.md § D-D, chunk C7
    "fleet.backfill_reference_edges":        "common_dir",
    # session.commits — keyed on git_common_dir (worktree derived via
    # main_worktree_root(common_dir), same convention as ceremony.wsc_tail /
    # resolve_session_branches): resolves a session_id's attributed commits
    # via one `git log --numstat` over the caller's own repo. Unlisted
    # default is "none", which would be wrong here — this op reads the
    # caller's own git history, not an explicit caller-supplied repo path.
    # Spec: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C4
    "session.commits":                       "common_dir",
    # session_baton.mint / session_baton.promote -- "none", mirroring
    # session.record_pickup: both take an explicit session_id and resolve
    # their own store path under the git common dir themselves rather than
    # reading a caller-supplied repo. Spec:
    # docs/plans/2026-08-18-a-session-always-has-a-baton.md C2/C3.
    # session.warm_start — none. DECISION, not a default: this op is
    # machine-scoped, not repo-scoped, and says so in its own handler
    # signature ("this op is machine-scoped, not repo-scoped — it warms
    # THIS machine's engine"). Its whole effect is a debounced detached
    # spawn gated on `warm.settings.is_warm_enabled` plus the
    # `warm.breadcrumb` pre-check; it reads machine-local settings and a
    # breadcrumb, never a caller worktree, and writes no repo substrate.
    # Threading a common_dir in would imply a per-repo warm engine, which
    # is the opposite of the one-engine-per-machine rule it implements.
    # Spec: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C25.
    "session.warm_start":                    "none",
    "session_baton.mint":                    "none",
    "session_baton.promote":                 "none",
    "session.reap":                          "common_dir",
    "session.boot_sweep":                    "common_dir",
    # session.sweep_consumed_handoffs — same scope as session.boot_sweep, and for the same
    # reason: it wraps that op's own consumed-handoff leg (`_sweep_consumed_handoffs`), so it
    # reads/writes state/handoffs/ and archive/ paths derived from the main worktree root and
    # git-commits each archival. Repo-touching ops must be listed explicitly — the unlisted
    # default is "none", which would be wrong here. Spec: docs/plans/2026-07-23-wsc-tail-slim-down.md § C21
    "session.sweep_consumed_handoffs":       "common_dir",
    # session.reap_claims_for_repos — fleet-generic per-repo claim-reap primitive: takes an
    # explicit target_roots[] param and reaps each, so it derives NO per-request repo key from
    # the caller's own tree → scope "none" (claude-klabauter's target-resolution convention: fleet-generic
    # ops pass target_root explicitly rather than resolving common_dir). Spec:
    # docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md § C5.
    "session.reap_claims_for_repos":         "none",
    # session.record_pickup — DR-059 defense-in-depth port of the bash
    # cs_session_shape_set pickup write: takes an explicit repo_root wire param
    # (a /pickup may consume a handoff in a repo other than claude-klabauter's own tree,
    # mirroring the foreign-repo case bash's _hp_rec already guards) → scope
    # "none" (same target-resolution convention as session.reap_claims_for_repos).
    # Spec: session-shape-pickup-port-proposal.md (2026-07-14 DR-059 port dispatch).
    "session.record_pickup":                 "none",
    # session.scope_report — read-only reporter over THIS session's own scope
    # (compute_offer composition, C4a): takes optional session_id/cwd wire
    # params, resolves the calling session via
    # coordinator_core.session.core.resolve_session_id(cwd) when session_id
    # is absent, never derives a target from the engine-supplied repo_root →
    # scope "none" (same target-resolution convention as
    # session.reap_claims_for_repos / session.record_pickup). Spec:
    # docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md § C4a.
    "session.scope_report":                  "none",
    # session.guard_settings_integrity — MUTATING (classification.py) but scope "none":
    # handler resolves config_dir from an explicit params["config_dir"] override or
    # falls back to CLAUDE_CONFIG_DIR/$HOME/.claude env resolution — never from
    # repo_root. Missing from this table was an oversight (per the table's own
    # docstring), not an intentional default; behaviorally inert today since "none"
    # is the correct scope, but undetected by any test. Review: code-reviewer.
    "session.guard_settings_integrity":      "none",
    # session.guard_hooks_kill_switch_detail — same module and same resolution story as
    # session.guard_settings_integrity above: a read-only reporter over the settings-home
    # kill-switch marker, resolved from CLAUDE_CONFIG_DIR/$HOME/.claude, never from
    # repo_root. Absent from this table until 2026-07-31, when the registration-quad
    # check surfaced it alongside handoff.correct_body; behaviorally inert, same as its
    # sibling, but an incomplete quad is exactly what that check exists to catch.
    "session.guard_hooks_kill_switch_detail": "none",
    # session.resolve_address — read-only resolver mapping a session UUID to the live
    # SendMessage address, reading <claude-config>/sessions/<pid>.json via
    # coordinator_core.session.harness_registry.registry_dir(), which resolves through
    # claude_config_dir() (CLAUDE_CONFIG_DIR/$HOME/.claude) and never from repo_root →
    # scope "none", same resolution story as session.guard_settings_integrity above.
    # The harness peer registry is machine-global, not per-worktree: two linked
    # worktrees of one repo see the identical peer set, so neither "common_dir" nor
    # "show_top" would key anything meaningful.
    # Spec: state/handoffs/2026-08-13-session-owner-reachability-registry.md § 1.
    "session.resolve_address":                "none",
    # session.peer_roster — read-only cwd-filtered live peer roster, over the
    # SAME machine-global harness registry as session.resolve_address just
    # above (identical resolution story, identical reasoning for "none").
    # Its wire-level repo_root filter param is read from params, never from
    # this engine-injected repo_root kwarg.
    # Spec: state/handoffs/2026-08-13-live-peer-roster.md § 1-2.
    "session.peer_roster":                    "none",
    # session.work_state — read-only held/unclaimed corpus read over
    # state/handoffs/, which is main-worktree-rooted repo state -- exactly
    # the case this table's own header comment names for "common_dir"
    # (git_common_dir(resolved_worktree), shared across linked worktrees).
    # Deliberately DIFFERENT from session.peer_roster's "none" just above:
    # the harness peer registry is machine-global, but this op's own
    # state/handoffs/ scan is not. Takes the engine-injected repo_root
    # kwarg verbatim; no wire param of the same name.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C3.
    "session.work_state":                     "common_dir",
    # fleet.work_state — fleet-generic aggregation of session.work_state's
    # per-repo held/unclaimed core across every registered ACTIVE sibling
    # (C5). Deliberately DIFFERENT from session.work_state's "common_dir"
    # just above: this op is not scoped to the calling repo's own tree at
    # all — it reaches across every registered sibling via
    # _memo_resolver.read_registry_repos(), the same fleet-generic
    # target-resolution convention session.reap_claims_for_repos /
    # session.record_pickup / session.scope_report already use → "none".
    # repo_root arrives as None (unused) for this op.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C5.
    "fleet.work_state":                       "none",
    # fleet.record_history — fleet-generic aggregation of records.history's own
    # cross-repo derivation (C2), fanned out across every registered ACTIVE
    # sibling the same way fleet.work_state does. Follows fleet.work_state's
    # own registration triple verbatim → "none".
    # Spec: state/dispatch-briefs/2026-08-20-a-counted-fleet-answer-for-record-history/C2.md.
    "fleet.record_history":                   "none",
    # session.artifact_owner — read-only "who's on this?" read, keyed on an
    # artifact path rather than a UUID or a repo. Its own file read is the
    # caller-supplied artifact_path param, never repo_root-prefixed, and its
    # owner-id resolution is a pass-through to session.resolve_address's own
    # machine-global harness registry — same "none" story as both siblings
    # above.
    # Spec: state/handoffs/2026-08-13-live-peer-roster.md § "What this
    # covers" amendment (L52-62).
    "session.artifact_owner":                 "none",
    # handoff.author_fork — keyed on git_common_dir: creates a new fork handoff under
    # main-worktree-rooted state/handoffs/; handler derives worktree via
    # main_worktree_root(common_dir), matching the fleet.*/handoff.* precedent.
    # Without this entry dispatch resolves repo_root=None and the op returns an error.
    # Spec: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
    "handoff.author_fork":                   "common_dir",
    # plan.persist_capture — keyed on git_common_dir: writes a new plan under
    # main-worktree-rooted docs/plans/, same scope class as handoff.author_fork
    # (handler derives worktree via main_worktree_root(common_dir)). Without this
    # entry dispatch resolves repo_root=None and the op returns an error.
    # Spec: state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md § Part 2
    "plan.persist_capture":                  "common_dir",
    # plan.tasks.mutate — keyed on git_common_dir: in-place mutation of a
    # docs/plans/*.md file's '## Tasks' fenced body block, which lives in the
    # main-worktree-rooted docs/plans/ tree — same scope class as handoff.transition.
    # Spec: docs/plans/2026-07-10-pcli-need-1-plan-tasks-engine-plane.md § C3
    "plan.tasks.mutate":                     "common_dir",
    # plan.tasks.grouping_digest — common_dir, same key as plan.tasks.mutate above: it
    # reads the same main-worktree-rooted docs/plans/*.md '## Tasks' block, read-only.
    # Explicit, NOT left to the "none" default: the handler requires repo_root (it derives
    # main_worktree_root from it) and returns an error when handed None, so the default
    # would silently break every call. Same shape as handoff.author_fork's note above.
    "plan.tasks.grouping_digest":             "common_dir",
    # session_ledger.aggregate_chain_loe — common_dir-scoped: reads the calling
    # repo's own state/handoffs + archive/handoffs. Same class as handoff.has_live_children.
    # Spec: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3d
    "session_ledger.aggregate_chain_loe":    "common_dir",
    # session_hierarchy.derive — no per-caller repo state accessed: this op always
    # targets claude-klabauter's OWN checkout (Path(__file__) + git rev-parse --show-toplevel),
    # not the caller's repo_root — post-migration, handoffs live in claude-klabauter regardless
    # of which repo dispatches the call. Same "none" class as percolate.run /
    # cartography.* / engine.drift.
    # Spec: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3c
    "session_hierarchy.derive":              "none",
    # deferral.detect_orphan_memo / deferral.detect_partial_strangle — keyed on
    # git_common_dir: both are read-only hidden-deferral detectors that read
    # main-worktree-rooted cross-repo/inbox/, docs/plans/, and docs/decisions/,
    # same key-scope class as memo.triage / deliverable.rollup. Without an entry
    # here dispatch resolves repo_root=None and each detector scans nothing.
    # Spec: tasks/hidden-deferral-detectors/design.md
    "deferral.detect_orphan_memo":           "common_dir",
    "deferral.detect_partial_strangle":      "common_dir",
    # schema.describe / schema.validate — "none": repo_root is unused, both always
    # read claude-klabauter's own fixed vendored coordinator_core/frontmatter/schemas/ tree,
    # never a per-repo path. Same "none" class as ping / percolate.run / cartography.*.
    "schema.describe":                       "none",
    "schema.validate":                       "none",
    # fleet.* archival-writer ops (fence-inventory buildout wave 1) — keyed on
    # git_common_dir: each writes into the CALLER's own archive/ tree (release-notes,
    # research/archive, improvement-queue), same class as the existing fleet.* archival
    # writers above; must resolve main_worktree_root(common_dir) or the archive lands
    # in claude-klabauter's own clone instead of the caller's.
    # Spec: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
    "fleet.archive_release_accumulator":     "common_dir",
    "fleet.archive_paper_trail":              "common_dir",
    "fleet.archive_queue_entry":              "common_dir",
    "fleet.migrate_handoff_vocabulary":       "common_dir",
    # fleet.archive_terminal_sizings — common_dir: git-mv's terminal
    # sizing entries into archive/sizings/YYYY-MM/ under the caller's own
    # repo tree, same archival-writer class as the fleet.* ops above.
    # repo_root is the git common dir per the handler's own docstring;
    # main_worktree_root(common_dir) resolves the worktree, mirroring
    # archive_plans.py's Key Decision 5.
    "fleet.archive_terminal_sizings":         "common_dir",
    # git_branch.* — orphan-branch-sweep ops. compute_descendant_tip/list_unmerged_work/
    # verify_commit_in_review_window read shared object-database/ref state (identical
    # across any linked worktree) but still need the caller's injected repo root, so
    # common_dir; detect_unpushed_commits is inherently per-checkout (local HEAD vs
    # origin for THIS worktree's branch), so show_top.
    "git_branch.compute_descendant_tip":      "common_dir",
    "git_branch.detect_unpushed_commits":     "show_top",
    "git_branch.list_unmerged_work":          "common_dir",
    "git_branch.verify_commit_in_review_window": "common_dir",
    # scratchpad.sweep — "none", MUTATING (dry-run by default; `reclaim: true`
    # is the sole destructive opt-in — see module docstring's two-gate
    # deletion contract). The handler (_handler in scratchpad_sweep.py)
    # accepts `repo_root` for dispatch-signature parity only and NEVER passes
    # it to `sweep_scratchpads` — confirmed by its own docstring ("Scope:
    # none ... repo_root is accepted ... but unused") and by reading the
    # `_handler` body, which forwards only temp_root/project_slugs/ttl_days/
    # reclaim. The op is fleet-generic by construction: it walks
    # `<temp_root>/claude/` across EVERY discovered project-slug directory
    # (via `_build_slug_to_root_map`'s Tier A + Tier A.5 discovery), not the
    # caller's own dispatching tree — same "any repo, never the caller's own
    # tree" target-resolution model as cartography.*/workflow.validate,
    # confirmed by test_scratchpad_sweep.py's cross-repo-isolation tests
    # (test_two_project_slugs_resolve_independent_liveness), which sweep
    # multiple project slugs in one call, none derived from repo_root.
    # Spec: this module's own docstring (Two-gate deletion contract /
    # Liveness-scope fix, 2026-08-10).
    "scratchpad.sweep":                       "none",
    # cartography.count_references — "none": mirrors cartography.edges' own existing
    # scope (target_root is an explicit caller-supplied param, not injected repo_root),
    # same target-resolution convention as the rest of the cartography.* family.
    "cartography.count_references":           "none",
    # doctrine.assert_cross_reference_counts — common_dir: reads the CALLER's own
    # skills/**/*.md + docs/wiki/**/*.md doctrine tree; must resolve
    # main_worktree_root(common_dir) or the count assertion silently reads claude-klabauter's
    # own tree instead of the caller's (coordinator-claude/DoE).
    "doctrine.assert_cross_reference_counts": "common_dir",
    # percolate.check_inverse_drift — "none": dest_dir/marker_path are explicit
    # caller-supplied params (the publish target), matching the percolate.run/
    # percolate.validate_store "no repo state accessed" precedent.
    "percolate.check_inverse_drift":          "none",
    # repo.clone_and_register — "none": operates on a sibling repo path and the
    # OPERATOR's own machine-local registry, neither of which is the caller's own
    # dispatching tree; matches the plugin_health.* operator-machine-scoped class.
    "repo.clone_and_register":                "none",
    # release.* — common_dir: tags/releases are repo-wide ref objects stored in the
    # shared common gitdir, identical across every linked worktree of the same repo.
    "release.cut_tag":                        "common_dir",
    "release.cut_tag_and_publish":             "common_dir",
    # dependency.detect_changed_manifests — show_top: the tracked-file enumeration
    # reflects the calling worktree's checked-out tree; a common_dir key risks
    # answering for the wrong linked worktree's checkout.
    "dependency.detect_changed_manifests":    "show_top",
    # detect.plugin_layout / detect.primary_languages / cartography.stack — "none":
    # target_root/plugin_root is an explicit caller-supplied param (any repo/plugin
    # dir under research, not necessarily the caller's own dispatching worktree),
    # mirroring the cartography.*/workflow.validate target-resolution model.
    "detect.plugin_layout":                   "none",
    "detect.primary_languages":                "none",
    "cartography.stack":                       "none",
    # cartography.chunk_table — "none": same cartography.* target-resolution
    # model as its siblings above (explicit target_root wire param, any repo,
    # NOT the caller's own dispatching tree); repo_root is ignored entirely.
    # MUTATING (unlike its COMPUTE_ONLY siblings) because emit=true writes
    # one scratch-tier JSON artifact under target_root/state/scratch/
    # cartography-chunk-table/<run_id>/ — DR-228 § D6 (amended) sanctions the
    # write; scope classification is unaffected by that (still "none", since
    # no repo-specific _origin_worktree state is read).
    # Spec: cross-repo/inbox/2026-08-06-doe-claude-em-cartography-chunk-table-producer-seam.md
    "cartography.chunk_table":                 "none",
    # ceremony.chunk_commits — "none": same cartography.* / workflow.validate
    # target-resolution model (explicit caller-supplied plan_path, any repo,
    # NEVER the caller's own dispatching tree); the op derives its git repo
    # from wherever the resolved path lands and ignores repo_root entirely.
    # COMPUTE_ONLY — two read-only `git log` invocations, no writes.
    #
    # Registered EXPLICITLY rather than defaulting to "none" by omission,
    # which is what this entry's absence was: the op shipped relying on the
    # table's default, so its "scope is deliberately none" claim lived only in
    # a docstring. That claim is load-bearing — it is the reason the
    # caller-cwd defect (d4d429d21) was fixed in the bin/ forwarder rather
    # than by resolving against a forwarded --repo, which would have made the
    # op's answer depend on where it was dispatched from.
    # Spec: cross-repo/inbox/2026-08-10-doe-claude-em-chunk-commits-forwarder-relative-path.md
    "ceremony.chunk_commits":                  "none",
    # install.detect_python3_appx_stub — "none": inspects the operator's own machine
    # (PATH-resolved python3 interpreter), not any repo state; mirrors engine.drift/
    # plugin_health.drift's own-machine-probe class.
    "install.detect_python3_appx_stub":        "none",
    # lessons.filter_undated_universal — "none": operates on a caller-supplied YAML
    # payload (params, not a file path); no repo state accessed at all.
    "lessons.filter_undated_universal":        "none",
    # lessons.reject_orphan_strip_entries — common_dir: records.yaml and the
    # strip-list live under this repo's lessons/state tree, main-worktree-rooted
    # like handoffs/emit-ops, shared across linked worktrees.
    "lessons.reject_orphan_strip_entries":     "common_dir",
    # completion.flip_to_released — common_dir: mutates a caller-supplied
    # docs/plans/*.md or archive/completed/**/*.md entry under the caller's repo,
    # matching completion.reconcile_commits' existing common_dir classification in
    # this same module.
    "completion.flip_to_released":             "common_dir",
    # install.clone_idempotent — "none": repo_url/target_dir are explicit
    # caller-supplied install-time params (a sibling-repo clone location on the
    # operator's own machine), not the caller's own dispatching worktree.
    "install.clone_idempotent":                "none",
    # coverage.halt_on_uncovered — show_top: same per-worktree keying as the
    # coverage.gate op it wraps (reads per-worktree git log state), not common_dir.
    "coverage.halt_on_uncovered":              "show_top",
    # ceremony.init_anchor_injection_state — "none": resolves coordinator-claude/
    # DoE root via coordinator_doe_root(), a fixed cross-repo target, not the
    # caller's own worktree.
    "ceremony.init_anchor_injection_state":    "none",
    # install.write_shell_rc_guard_block / install.wrapper_onto_path — "none": both
    # write to the operator's own $HOME (shell rc file / bin dir), not the caller's
    # repo.
    "install.write_shell_rc_guard_block":      "none",
    "install.wrapper_onto_path":               "none",
    # install.detect_cmd_autorun_coverage / install.write_cmd_autorun_guard /
    # install.strip_cmd_autorun_guard — "none": own-machine probe/write
    # against the operator's own HKCU hive, not the caller's repo; matches
    # install.detect_python3_appx_stub / install.write_shell_rc_guard_block's
    # own scope verdicts per the handlers' own docstrings.
    "install.detect_cmd_autorun_coverage":     "none",
    "install.write_cmd_autorun_guard":         "none",
    "install.strip_cmd_autorun_guard":         "none",
    # percolate.list_files_newer_than_marker — "none": mirrors percolate.run/
    # percolate.validate_store's existing precedent — source_dir is resolved from
    # the TARGETS entry and passed as an explicit param, never derived from the
    # caller's worktree.
    "percolate.list_files_newer_than_marker":  "none",
    # plan.list_stale_executing — common_dir: scans the CALLER's own
    # docs/plans/*.md; an absent scope entry would silently scan claude-klabauter's own
    # docs/plans instead of the caller's repo.
    "plan.list_stale_executing":               "common_dir",
    # plan.list_orphaned — common_dir, same key and same rationale as
    # plan.list_stale_executing directly above: scans the CALLER's own
    # docs/plans/*.md and state/handoffs/*.md; an absent entry here would
    # silently default to "none" and un-scope the op, scanning claude-klabauter's own
    # tree instead of the caller's.
    "plan.list_orphaned":                      "common_dir",
    # plan.suggest_completion_steps — common_dir: scans the CALLER's own
    # docs/plans/*.md and state/review-trail/*.json (+ archive/review-trail/)
    # for the plan-completion assist surface (Part 3, state/handoffs/
    # 2026-08-13-vanilla-plan-mode-capture-safety-net.md). Same rationale as
    # plan.list_stale_executing/plan.list_orphaned directly above: an absent
    # entry here would silently scan claude-klabauter's own tree instead of the
    # caller's.
    "plan.suggest_completion_steps":           "common_dir",
    # cli.parse_flag / cli.parse_date_flags — "none": parse a literal arguments
    # string handed in as a param; touch no filesystem or repo state.
    "cli.parse_flag":                          "none",
    "cli.parse_date_flags":                    "none",
    # merge.quiet_activity_gate — show_top: reads the CALLER's own current-checkout
    # git commit history (most recent commit on the branch being merged), which is
    # per-worktree state, same class as coverage.gate.
    "merge.quiet_activity_gate":               "show_top",
    # schema.drift_gate — "none": resolves the DoE-claude sibling clone (env var /
    # registry pointer, same ladder as the advisory probe) and claude-klabauter's own fixed
    # vendored-schemas dir; touches no per-worktree/per-repo caller state.
    "schema.drift_gate":                       "none",
    # update_docs.probe_fresh_repo_noop — common_dir: DIRECTORY.md / archive/ /
    # tasks/ are main-worktree-rooted paths, shared across linked worktrees of the
    # same repo per this table's own common_dir definition.
    "update_docs.probe_fresh_repo_noop":       "common_dir",
    # install.probe_skill_frontmatter_valid / install.probe_windows_terminal_presence
    # — "none": both inspect the OPERATOR's own machine (coordinator-claude
    # installation tree / PATH+winget packages), same "none" class as engine.drift /
    # plugin_health.scan.
    "install.probe_skill_frontmatter_valid":   "none",
    "install.probe_windows_terminal_presence": "none",
    # mcp.resolve_server_cli_path — "none": reads a global per-user home file
    # (~/.claude.json), never touches caller-repo state.
    "mcp.resolve_server_cli_path":              "none",
    # baton.resolve_swept_in_archive — common_dir: explicitly named in the plan's
    # § Registration is four surfaces list as one of the repo-touching ops needing
    # worktree scope; searches archive dirs under the caller's repo root, same
    # class as handoff.match_candidates/session.boot_sweep.
    "baton.resolve_swept_in_archive":          "common_dir",
    # percolate.run_ci_smoke_check — "none": matches percolate.run/
    # percolate.validate_store's existing none entries — dest is passed explicitly
    # as a param, not derived from repo_root/worktree context.
    "percolate.run_ci_smoke_check":            "none",
    # percolate.run_identity_check — "none": matches percolate.run_ci_smoke_check's
    # own entry immediately above — dest is passed explicitly as a param, not
    # derived from repo_root/worktree context.
    "percolate.run_identity_check":            "none",
    # ci.run_pip_audit / ci.run_semgrep_scan / ci.run_shellcheck_sweep — show_top:
    # each audits/scans the caller's own worktree-specific state (lock file, diff
    # scope, tracked files), not shared main-worktree state.
    "ci.run_pip_audit":                        "show_top",
    "ci.run_semgrep_scan":                     "show_top",
    "ci.run_shellcheck_sweep":                 "show_top",
    # review_trail.scan_unresolved_ubt — common_dir: state/review-trail is
    # main-worktree-rooted per list_review_trail_records.py's own resolution, same
    # class as the existing review_trail.write op.
    "review_trail.scan_unresolved_ubt":        "common_dir",
    # review_trail.readjudication_report — show_top, NOT common_dir like the two
    # review_trail.* rows above it. Read-only C7 re-adjudication diagnostic
    # (review_trail_readjudication_report.py). This op is a genuine HYBRID: its
    # corpus reads are main-worktree-rooted (like its two review_trail.* siblings)
    # but its git reads are per-worktree (like coverage.gate). show_top is the only
    # scope that can serve both, because it is strictly the more informative key:
    #   (1) show_top hands the handler the CALLER's worktree, from which the op
    #       derives the corpus's main worktree itself — it calls the same single
    #       reviewed derivation the siblings use, main_worktree_root() over
    #       lifecycle.git_common_dir(), in its own `_corpus_root()`. So the
    #       "state/ is main-worktree-rooted" requirement that earns
    #       review_trail.write / scan_unresolved_ubt their common_dir key is
    #       satisfied here WITHOUT the common_dir key.
    #   (2) The reverse does not hold: common_dir hands a handler
    #       <main-worktree>/.git, and the caller's worktree is NOT recoverable
    #       from it (many linked worktrees share one common dir). That would
    #       misroot the git half — `git rev-list <sha_range>` and the Session-Id
    #       trailer lookup — onto the main checkout. Not academic: a record's
    #       sha_range may name HEAD, which is per-worktree state (4 of the 29
    #       chain/workstream-close-auto records on disk at authoring do), so those
    #       ranges would resolve against the wrong checkout's HEAD.
    #   Net: common_dir trades a derivable fact (the main worktree) for an
    #   underivable one (the caller's worktree). Do not "fix" this row to
    #   common_dir by analogy with the two rows above — the analogy covers only
    #   half of what this op reads.
    # History: this row's earlier justification argued show_top on the grounds that
    # the op lacked the main_worktree_root derivation entirely — true when written
    # (2fa1c4cf), and a real silent-wrong-output defect: from a linked worktree the
    # op globbed an absent state/review-trail and reported records_scanned=0,
    # flips=0, which reads as a clean bill of health because "no surface re-opens"
    # is a legitimate result. The derivation now exists; the verdict is unchanged
    # but the reasoning is not.
    # Spec: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C8, AC11
    "review_trail.readjudication_report":      "show_top",
    # ceremony.scoped_git_commit — show_top: commits land in a specific worktree's
    # index/HEAD; § Registration is four surfaces, not one names this row
    # explicitly as repo-touching, so it must not default to "none".
    "ceremony.scoped_git_commit":              "show_top",
    # findings.self_persist_fallback — "none": target_path is caller-supplied; the
    # op does not resolve relative paths against a repo root itself.
    "findings.self_persist_fallback":          "none",
    # review.snapshot_diff_and_head — show_top: `git diff origin/main...HEAD` and
    # `rev-parse HEAD` read the CALLER's checked-out branch tip, which differs per
    # linked worktree; a common_dir key would collapse distinct worktrees' snapshots
    # onto the same slot.
    "review.snapshot_diff_and_head":           "show_top",
    # review.freeze_diff — show_top: same reasoning as review.snapshot_diff_and_head
    # above — `git diff <range>`/`rev-parse HEAD` read the CALLER's checked-out
    # worktree tip, which differs per linked worktree; a common_dir key would
    # collapse distinct worktrees' frozen diffs onto the same slot.
    "review.freeze_diff":                      "show_top",
    # workday.stitch_sidecar_into_summary — common_dir: the daily summary and its
    # sidecar live in the CALLER's main-worktree-rooted state/week-changelog tree,
    # shared across any linked worktree of that repo.
    "workday.stitch_sidecar_into_summary":     "common_dir",
    # repo_setup.validate_target_root — "none": target_root is an explicit
    # caller-supplied path (the repo being onboarded during repo-setup, not
    # necessarily the invoking cwd/worktree); never needs _origin_worktree.
    "repo_setup.validate_target_root":         "none",
    # research.verify_scout_inventory_completeness — common_dir: expected scout
    # inventory files live under the CALLER's main-worktree-rooted tasks//scratch
    # tree, shared across any linked worktree.
    "research.verify_scout_inventory_completeness": "common_dir",
    # install.write_identity_file — "none": writes to CLAUDE_HOME/.claude/
    # coordinator-identity.yaml, a fixed per-machine operator-config path, not the
    # caller's repo.
    "install.write_identity_file":              "none",
    # baton.resolve_path_and_repo — "none": takes an explicit baton_path param and
    # self-discovers its owning repo dynamically via git rev-parse on that path; it
    # does not read any state relative to the caller's _origin_worktree/repo_root.
    "baton.resolve_path_and_repo":              "none",
    # branch.merge_into_workstream — show_top: git merge mutates the currently
    # checked-out worktree's index/HEAD; per-worktree state, mirrors
    # coverage.gate's show_top rationale, not shared common_dir state.
    "branch.merge_into_workstream":             "show_top",
    # bug_sweep.verify_fix_files_changed — show_top: `git diff --name-only` reads
    # the CALLER's working-tree state, which differs per linked worktree of the
    # same repo; a common_dir key would answer for the wrong worktree's
    # uncommitted diff.
    "bug_sweep.verify_fix_files_changed":       "show_top",
    # commit.exec_bit_change — common_dir: stages and commits within the CALLER's
    # own working tree/index; must resolve main_worktree_root(common_dir), matching
    # commit.anchors' precedent.
    "commit.exec_bit_change":                   "common_dir",
    # fanout.poll_scratch_dir — "none": scratch_dir is an explicit caller-supplied
    # param (same class as cartography.*'s target_root), not derived from
    # repo_root/_origin_worktree.
    "fanout.poll_scratch_dir":                  "none",
    # machine.hibernate — "none": pure OS power-state action, touches no repo
    # state at all, no repo_root needed.
    "machine.hibernate":                        "none",
    # percolate.run_pre_ci_hooks — "none": hooks_root and dest are explicit
    # caller-supplied params mirroring percolate.run's target_root precedent (no
    # repo state read via injected worktree).
    "percolate.run_pre_ci_hooks":               "none",
    # percolate.scan_content_leakage_tiers — "none": target_root is an explicit
    # caller-supplied param (the about-to-publish tree), mirrors percolate.run's
    # target_root precedent; no injected worktree read.
    "percolate.scan_content_leakage_tiers":     "none",
    # repo.create_and_push_remote — common_dir: creating the remote and writing
    # the resulting origin entry write into .git/config, which lives in the shared
    # common gitdir across all linked worktrees of the same repo, not per-worktree
    # state.
    "repo.create_and_push_remote":              "common_dir",
    # repo_setup.copy_console_subprocess_tripwire — show_top: both the copy
    # destination and the pytest run land in the specific worktree's checked-out
    # files; a common_dir key would let a copy destined for one linked worktree
    # register against a different worktree's key.
    "repo_setup.copy_console_subprocess_tripwire": "show_top",
    # research.archive_workdir — common_dir: writes into the CALLER's own
    # docs/research/archive/; must resolve main_worktree_root(common_dir), or the
    # workdir archives against claude-klabauter's own clone instead of the pipeline caller's.
    "research.archive_workdir":                 "common_dir",
    # research.restructure_for_repeat_topic — common_dir: mutates docs/research/
    # under the caller's repo root (a top-level doc dir, shared across linked
    # worktrees like state/); omitting scope would restructure claude-klabauter's own
    # docs/research instead of the caller's when invoked cross-repo.
    "research.restructure_for_repeat_topic":    "common_dir",
    # session.resolve_chain_terminal_disposition — common_dir: reads
    # state/handoffs/ (live claimed_by/consumed_by consume-stamp) and
    # git-provenance under the caller's repo git_common_dir, same class as
    # handoff.match_candidates and session.boot_sweep; omitting this would
    # classify claude-klabauter's own handoffs instead of the caller's when invoked
    # cross-repo from a DoE fence.
    "session.resolve_chain_terminal_disposition": "common_dir",
    # session.rotate_orphan_sweep_log — common_dir: must match
    # session.boot_sweep's existing common_dir entry for the SAME
    # tasks/orphan-sweep-notes.md write target (GIT_ROOT-worktree-rooted per
    # boot_sweep.py's own documented convention); a mismatched scope would
    # truncate a different repo's copy of the file than the one boot_sweep wrote
    # to.
    "session.rotate_orphan_sweep_log":          "common_dir",
    # workday.surface_auto_push_failure_stats — show_top: the handler ignores
    # the ipc-injected repo_root entirely (it uses only the explicit
    # `repo_root` param, per the manifest contract) and resolves the git
    # COMMON dir itself via `coordinator_core.git.git_dir.resolve_git_common_dir`
    # (2026-08-01, replacing the writer's former literal `<repo_root>/.git`
    # join, which mis-resolved in a linked worktree/`--separate-git-dir`
    # clone/submodule). The scope key below is therefore inert for this op's
    # own file targeting; left at show_top (its 2026-07-22 value, review
    # W3-familyA F3) rather than re-litigated here since no caller currently
    # depends on the ipc-injected value.
    "workday.surface_auto_push_failure_stats":  "show_top",
    # git.push_failure_verdict — show_top: every signal it classifies on
    # (staged/unstaged diff, MERGE_HEAD, upstream ahead/behind) is
    # per-worktree state, same class as coverage.gate and merge.
    # quiet_activity_gate above; only the push-failures.log read inside the
    # handler itself resolves through the git COMMON dir (writer's own
    # keying), independent of this scope key. See the op module's own
    # docstring for the full rationale.
    "git.push_failure_verdict":                 "show_top",
    # workday.drain_pending_push — show_top: same rationale as its read-half
    # sibling above — the handler ignores the ipc-injected repo_root kwarg
    # entirely and uses only the explicit `repo_root` param (see that op's own
    # module docstring, coordinator_core/ops/workday_drain_pending_push.py).
    "workday.drain_pending_push":                "show_top",
    # tracker.advance_status — common_dir: tracker_path is a caller-supplied
    # path resolved against main_worktree_root(common_dir) and path-contained
    # to it (coordinator_core/ops/_path_guard.contained_path). See
    # coordinator_core/ops/tracker/advance_status.py module docstring's
    # "Provisional classification" section for why this op has no ratified DR
    # carve-out yet.
    "tracker.advance_status":                   "common_dir",
    # tracker.fold_observed_set — common_dir, the SAME scope
    # session.boot_sweep itself uses (this op is actuated FROM boot_sweep,
    # per docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Tasks
    # C5). DECISION, not a default: common_dir scoping means one
    # sovereign-tracker store shared across all LINKED WORKTREES of the same
    # repo — matching every other session.*/fleet.* op's scoping — not a
    # per-worktree store. An op missing from this table silently degrades to
    # repo_root=None, which would break the DEC-11 confinement DR-241's
    # Amendment affirms; do not omit this entry.
    "tracker.fold_observed_set":                "common_dir",
    # tracker.mint_person — common_dir, the SAME scope tracker.fold_observed_set
    # and session.boot_sweep use. DECISION, not a default: this op's WRITE
    # BOUND (PM ruling 2026-08-12, see coordinator_core/ops/tracker/
    # mint_person.py module docstring) confines every write to the LOCAL
    # repo's own worktree root, derived via main_worktree_root(common_dir) —
    # never a holder/peer repo's root. An op missing from this table silently
    # degrades to repo_root=None, which would break that per-repo
    # confinement bound silently; do not omit this entry.
    "tracker.mint_person":                      "common_dir",
    # tracker.assign — common_dir, the SAME scope tracker.mint_person and
    # tracker.fold_observed_set use. DECISION, not a default: this op's
    # writes are confined to the LOCAL repo's own worktree root, derived via
    # main_worktree_root(common_dir) — never a holder/peer repo's root. An
    # op missing from this table silently degrades to repo_root=None, which
    # would break that per-repo confinement bound silently; do not omit
    # this entry.
    "tracker.assign":                           "common_dir",
    # tracker.render_status — common_dir, the SAME scope every other tracker.*
    # op uses. DECISION, not a default: render_status reads the LOCAL repo's
    # own event shard via tracker_projection, resolved from
    # main_worktree_root(common_dir) — never a holder/peer repo's root. An op
    # missing from this table silently degrades to repo_root=None, which for a
    # read op means projecting the CENTRAL scope's events instead of the
    # caller's and returning a confidently wrong status
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.render_status":                    "common_dir",
    # tracker.assert_code_complete — common_dir, the SAME scope every other
    # tracker.* op uses (C11). DECISION, not a default: this op writes into
    # the LOCAL repo's own event shard via tracker_completion_policy.
    # emit_code_complete_assert, resolved from main_worktree_root(common_dir)
    # — never a holder/peer repo's root. An op missing from this table
    # silently degrades to repo_root=None, which would mis-scope this write
    # to the CENTRAL scope instead of the caller's own repo
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.assert_code_complete":              "common_dir",
    # tracker.push_suggestion — common_dir, the SAME scope every other
    # tracker.* op uses (C4). DECISION, not a default: repo_root is always
    # the CALLING repo's own worktree, derived from main_worktree_root(
    # common_dir) — ownership resolution (local-vs-peer routing through
    # tracker_holder.write_root_for) happens INSIDE the handler, not via a
    # different repo_root scope. An op missing from this table silently
    # degrades to repo_root=None, which would break the D3 consistency check
    # and the local-write worktree derivation silently
    # (state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml);
    # do not omit this entry.
    "tracker.push_suggestion":                   "common_dir",
    # tracker.fold_ownership — common_dir, the SAME scope every other
    # tracker.* op uses. DECISION, not a default: this op reads the LOCAL
    # repo's own worktree store, derived via main_worktree_root(common_dir) —
    # never a holder/peer repo's root. An op missing from this table
    # silently degrades to repo_root=None, which would break that per-repo
    # confinement bound silently; do not omit this entry.
    "tracker.fold_ownership":                   "common_dir",
    # priority.set — none: the priority-ledger root is resolved centrally via
    # coordinator_state_root(central=True), never from a caller repo_root
    # (same class as ping / goal.set_kr_status). See
    # coordinator_core/ops/priority_set.py module docstring.
    "priority.set":                             "none",
    # priority.drain — none: both the priority-intent inbox and the
    # priority-ledger root are resolved centrally via
    # coordinator_state_root(central=True), never from a caller repo_root
    # (same class as priority.set). See
    # coordinator_core/ops/priority_drain.py module docstring.
    "priority.drain":                           "none",
    # probes.fork_census — "none": base_dir defaults to the OPERATOR's own live
    # Claude Code transcript root (home_dir()/".claude"/"projects") and is
    # otherwise an explicit caller-supplied override (tests); no path is ever
    # derived from repo_root/_origin_worktree. Same "none" class as
    # plugin_health.drift/engine.drift (operator-machine-scoped probes).
    "probes.fork_census":                       "none",
    # plugin_health.forwarder_drift — "none": inspects the operator's OWN
    # settings-home/claude-klabauter install state (settings-home bin/, the retired
    # ~/.claude/bin compat mirror, coordinator/bin/ via the claude-klabauter-root
    # resolver, DoE-claude's own prompt-surface trees) — never the caller's
    # repo_root. The handler's own docstring says so explicitly: "repo_root is
    # accepted for handler-signature parity but IGNORED". Same "none" class as
    # plugin_health.drift/plugin_health.scan.
    "plugin_health.forwarder_drift":            "none",
    # write_surface.emit_manifest — "none": the manifest describes CLAUDE-KLABAUTER'S OWN
    # source tree (the package this handler ships inside), never a caller's repo,
    # so there is no per-caller state to key on. `_repo_root_fallback` walks up
    # from `__file__` precisely because the answer does not depend on who asked.
    # Listed explicitly rather than left to the "none" default, per this table's
    # own header: "All production ops are listed explicitly; a missing entry is an
    # oversight, not a silent promotion."
    "write_surface.emit_manifest":              "none",
    # diagnostics.* — "none", and here that value is exact rather than merely
    # closest-fitting: these three probes read no state at all (not a path, not an
    # env var, not a param), so there is genuinely no per-request repo key to
    # derive and _origin_worktree is not required. Same class as ping, one step
    # purer — ping is the nearest precedent and the honest analogy.
    # Scope-table caveat, stated so a reader does not over-read this row: "none"
    # keys REPO STATE, not write-freedom (install.write_identity_file is "none"
    # and writes a file). The write-free-by-construction property these ops exist
    # for is carried by OP_CLASSIFICATION=COMPUTE_ONLY plus the module's own
    # empty import surface, not by this table.
    # Spec: docs/plans/2026-08-07-safe-target-for-transport-failure-probes.md § C1
    "diagnostics.always_succeeds":              "none",
    "diagnostics.always_refuses":               "none",
    "diagnostics.always_structural_pin":        "none",
    # gate.validate_invocable — "none": takes an explicit `changed_files`
    # wire param (any repo's diff/changed-file set), accesses no
    # per-session implicit repo state (no common_dir/show_top resolution).
    # Spec: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1
    "gate.validate_invocable":                  "none",
    # eol.census / eol.audit_producers / eol.repair — "none": DECISION, not the
    # closest-fitting default. All three take an explicit `target_root` wire
    # param naming the tree to scan/repair; the injected repo_root/caller's own
    # tree is ignored entirely (this plan's anti-scope: "Do not resolve the
    # target root from the environment"). An op key missing from this table
    # silently degrades to repo_root=None — see this module's own scope-table
    # caveat above for why that would be a silent behavior change, not a no-op.
    # Spec: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C5
    "eol.census":                               "none",
    "eol.audit_producers":                      "none",
    "eol.repair":                               "none",
}

# ---------------------------------------------------------------------------
# Public parity surface (cross-repo contract) — DR § AC-1b
#
# OP_KEY_SCOPE       — read-only view of the op→scope keying table. A consumer
#                      (e.g. DoE's coordinator-core-shim) imports this to keep its
#                      _origin_worktree-injection allowlist in lock-step with the
#                      engine's registered scopes, instead of hand-mirroring it
#                      (which drifts silently the next time an op's scope changes).
# WORKTREE_SCOPED_OPS — the frozenset of ops that REQUIRE a top-level
#                      _origin_worktree envelope field (scope ∈ {common_dir, show_top}).
#                      Ops NOT in this set (central / none) ignore _origin_worktree.
#
# Parity-check contract (RAG-bait — the consumer test SHAPE, not raw set-equality):
#   A consumer allowlist A is CONSISTENT iff:
#     (1) A ⊆ WORKTREE_SCOPED_OPS                 — never inject on a non-scoped op
#     (2) (ops the consumer invokes) ∩ WORKTREE_SCOPED_OPS ⊆ A
#                                                 — never OMIT on a scoped op it calls
#   Raw set-equality (A == WORKTREE_SCOPED_OPS) is WRONG when the consumer invokes
#   only a subset of engine ops. See DR-208 / global-multiplex DR § AC-1b.
# ---------------------------------------------------------------------------
OP_KEY_SCOPE = _types.MappingProxyType(dict(_OP_KEY_SCOPE))
WORKTREE_SCOPED_OPS = frozenset(
    op for op, scope in _OP_KEY_SCOPE.items() if scope in ("common_dir", "show_top")
)
