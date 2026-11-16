"""
coordinator_core.tests.test_raw_writes_have_a_disposition — the gate (D2):
every non-test ``coordinator_core`` module that carries a raw file-write
primitive must carry a recorded disposition, or the gate goes RED.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C8, § D2. Register seeded from state/audits/2026-09-11-state-writer-census.md
(C3) at this chunk's commit.

Why this exists
================
Before this gate, "does this module's raw write have an owner?" was a
question nobody was forced to answer. A module could write to ``state/``
outside the seam (``coordinator_core/session/claimed_write.py``) and nothing
noticed, because the census that would have caught it (state/audits/
2026-09-11-state-writer-census.md, C3) is a point-in-time document, not a
standing check. The remembered claim — "we'll migrate it later" — failed by
construction: there was no artifact forcing the remembering. This gate is
that artifact. From this commit forward, a peer's new raw writer cannot land
undecided while the migration batches (C5-C7) run, because the gate is live
and its register is the ONLY thing that turns it green.

Why bytes, not AST
==================
Census row 3 measured an AST-parse gate at ~1.8s of process time to parse
just the pre-filtered hits, before any analysis — over 3.5x this repo's
500ms brightline before the gate does any work. The bytes-level walk (census
row 2) measured 297ms for the whole ``coordinator_core`` package. That is
still over CLAUDE.md's "one process over 200ms needs a fix, not a
rationale" line, so this gate's own walk (below) compiles the same
vocabulary into one ``(``-anchored regex with a fixed-width lookbehind per
``(``-terminated literal and for the ``open(`` shape, plus one ``in`` pass
per remaining literal and the ``mode=`` prefilter — each file is walked by
three searches instead of one per term, and read with one unbuffered
``os.read``. ``_is_raw_writer_by_definition`` is the vocabulary read
literally; two tests pin the compiled form to it, on samples and on the
live population. Measured (``time.process_time``, ~1570 files, ~35MB):

  - census row 2's single alternation regex: ~275ms.
  - one ``in`` pass per literal plus prefiltered regexes: ~200-240ms.
  - this gate's anchored walk: ~100-130ms, about half of it the reads.
  - a `git grep --untracked -lE` spawn over the same vocabulary, measured
    for comparison: ~1469ms of child process time with a naive pathspec
    that (unlike the walk) does not exclude ``tests``/``testing``/
    ``benchmarks``/``__pycache__`` or ``test_*.py``/``conftest.py`` — a
    `git grep` variant that replicated the walk's exclusions was not
    pursued further once the python-side walk alone beat the 200ms bar,
    since census row 3's own finding is that a spawned process is a cost that "justifies itself per use" (CLAUDE.md) and this walk
    needs no spawn at all. The kept scan is therefore the anchored
    bytes-level Python walk, never a `git grep` spawn. `git grep` qualifies
    only if its module set equals the walk's; it was not re-measured with
    matching exclusions because the walk already cleared the budget.

The asserted budget is 200ms flat (never the repo's 500ms brightline), the
AC6 ceiling ("at or below 200ms"). If a future change pushes the live scan
over 200ms, the fix is to cut the walk's cost further (fewer files touched twice, a cheaper prefilter) — a looser
assertion is not a fix. The walk is one ``os.scandir`` pass (size and mtime come
from the directory entry, no per-file stat). The live scan memoises per-file verdicts by (size, mtime_ns); two
tests hold the 200ms budget, one on the warm (stat-only) path and one on the cold path
with the memo forced empty (a fresh export, a reboot): there a committed ``_SEED`` of
per-module verdicts, keyed by path and size, spares the read, and the seed test pins it
against a full read of the live tree. A rewrite
that preserves size and mtime (``cp -p``, ``touch -r``) outside the racy window is served stale.

The vocabulary (``_RAW_LITERALS`` / ``_RAW_OPEN_RE`` / ``_RAW_MODE_RE``
below) is census row 2's ``RAW`` pattern, decomposed into a literal-substring
prefilter plus two narrower regexes covering only the ``open(...)`` and
``mode=...`` call shapes that need real pattern matching. It is stated once,
here, and is the same vocabulary the census used to build the population
this gate's register covers.

What the gate checks, and what it does not
============================================
The gate never decides whether a raw write actually targets ``state/``. It
requires only that a decision — a disposition — exists for every module its
scan flags. That is the "prove, don't decide" shape of
docs/plans/2026-09-11-invert-the-write-detector-prove-read-only.md, applied
at module grain instead of call-site grain.

A flagged module passes only if ONE of the following covers it:

  - a **rule**: it is the seam module itself
    (``coordinator_core/session/claimed_write.py``), or one of the two
    primitives the seam delegates to (``coordinator_core/atomic_append.py``,
    ``coordinator_core/atomic_replace.py``); or it sits under
    ``coordinator_core/install/`` and declares a module-level
    ``WRITE_SURFACE``. The install-prefix scoping matters: a module outside
    ``coordinator_core/install/`` that happens to declare a variable named
    ``WRITE_SURFACE`` is NOT covered by this rule — it goes through the
    register like any other module. Six ``coordinator_core/ops/`` modules
    declare ``WRITE_SURFACE`` and are registered as ``claims-explicitly``
    below, not rule-covered, for exactly this reason.
  - a **register entry** in ``_DISPOSITIONS: dict[str, tuple[str, str]]``,
    keyed by repo-relative POSIX path, valued ``(category, reason)``. The
    category set is closed to exactly six members: ``claims-explicitly``,
    ``outside-repo``, ``git-internal``, ``ignored-target``,
    ``in-repo-non-state``, ``to-fix``.

Only two of the six are mechanically enforced. ``claims-explicitly`` is
TOKEN-checked: the gate greps the module's own bytes for one of the seam's
claim tokens (``declare_write(``, ``_scope_touch_paths``,
``touch_written_path(``, ``_SCOPE_TOUCH_PATHS_KEY``) and fails the entry if
none is present. Importing the seam is not itself a claim token — a module
that imports ``claimed_write`` but never calls ``declare_write`` could still
hold an unclaimed raw ``state/`` write elsewhere in the same module, so the
token check looks for an actual call/reference, not an import line.
``to-fix`` is CEILING-checked against ``_TO_FIX_CEILING``, a frozenset that
can only shrink (see "Ceiling shrink is review-time, not mechanical" below).
The other four labels (``outside-repo``, ``git-internal``, ``ignored-target``,
``in-repo-non-state``) are UNCHECKED — the gate verifies only that the
category is one of the closed six and that the reason is non-blank. The
label itself is C3's evidence, carried into the register; it is not an
assertion this gate makes about the module's actual write target.

A disposition describes a module's RESIDUAL raw write, never its seam
writes. A module that has moved its ``state/`` writes onto the seam but
still holds one raw write elsewhere (a tempfile, a write outside the repo)
is still flagged by the scan, and its register entry names that residual's
category (typically ``outside-repo`` or ``in-repo-non-state``), not
``claims-explicitly`` — because ``claims-explicitly``'s token check would
pass (the seam call is present) while the residual raw write stays
unaccounted for. This is deliberate: ``claims-explicitly`` means the
module's remaining raw-shaped bytes ARE the claim call itself, not that the
module also happens to import the seam elsewhere.

Ceiling shrink is review-time, not mechanical
================================================
``_TO_FIX_CEILING`` sits in this same file as ``_DISPOSITIONS`` and nothing
in this gate mechanically stops a future edit from WIDENING it — that
property is enforced by review (a peer checking a diff to this file
confirms the ceiling only shrank), not by a runtime assertion. What the gate
DOES enforce mechanically: every ``to-fix`` register entry's module must be
a ceiling member (an entry outside the ceiling is RED), and every ceiling
member must have a ``to-fix`` register entry naming a module that still
carries a raw token (a ceiling member with no entry, or whose module has
lost its raw token without the entry being deleted, is a STALE ceiling
member and is RED). That symmetry is what forces a migration batch (C5-C7)
to delete a module's register entry AND its ceiling membership in the same
commit — the stale-check on one side and the stale-ceiling check on the
other leave no way to land only one half.

The register-shape and stale-check precedent is
``coordinator_core/tests/test_session_dir_has_one_constructor.py``
(``_EXEMPT_SITES``, ``test_named_exemption_still_describes_a_real_site``): a
named exemption that stops describing a real site is deleted, not carried
forward, so a future refactor re-trips the gate instead of riding a dead
entry forever. The same discipline applies to ``_DISPOSITIONS`` entries
here: a stale entry (module gone, or the module no longer carries a raw
token) is RED, not silently ignored.

Named residuals (negative spec) — what this gate does NOT prove
===================================================================
1. **A dispositioned module can still add a second, unclaimed raw write and
   stay green.** ``claims-explicitly`` is a TOKEN check, not a per-site
   coverage proof: a module holding one claimed write and one brand-new
   unclaimed write passes as long as the claim token is present anywhere in
   its bytes. This gate proves that every raw-writing module has a recorded
   decision; it does NOT prove that every raw-write site inside a
   dispositioned module is covered. The runtime instrument (D3,
   ``coordinator_core/testing/state_write_audit.py``) is the on-demand,
   single-run re-check that answers that narrower question when it is
   asked — it is opt-in and never a standing gate, because an audit hook
   cannot be uninstalled once loaded.
2. **The raw-primitive vocabulary itself has a gap.** ``os.rename``,
   ``Path.rename``, ``Path.touch``, ``shutil.*``, ``sqlite3.connect`` and
   ``zipfile.ZipFile`` are not in ``_RAW_LITERALS``/``_RAW_OPEN_RE``/
   ``_RAW_MODE_RE``. A module whose only raw write goes through one of
   these calls is invisible to this gate's scan and needs no disposition to
   pass, even though it may write to ``state/``. Widening the vocabulary is
   a deliberate future change (it changes the scanned population and
   therefore the register), not a defect in this gate as authored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import zlib
from typing import Dict, FrozenSet, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.complete

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCAN_ROOT = "coordinator_core"
_EXCLUDED_DIRS = frozenset({"tests", "testing", "benchmarks", "__pycache__"})

_RAW_LITERALS: Tuple[bytes, ...] = (
    b"os.replace(",
    b".write_text(",
    b"mkstemp(",
    b"os.fdopen(",
    b"json.dump(",
    b"O_CREAT",
    b".write_bytes(",
    b"append_line(",
)
_RAW_OPEN_RE = re.compile(rb"""open\([^)\n]*['"][wax]b?\+?['"]""")
_RAW_MODE_RE = re.compile(rb"""mode\s*=\s*['"][wax]""")

#: The scan's compiled form of the vocabulary above, identical in what it
#: matches. Every ``(``-terminated literal and the ``open(`` shape share one
#: anchor byte, so one regex anchored on ``(`` with a fixed-width lookbehind
#: per literal replaces one full ``in`` pass per literal: the search walks the
#: file once, and a literal costs only a lookbehind probe at each ``(``.
_PAREN_LITERALS: Tuple[bytes, ...] = tuple(lit for lit in _RAW_LITERALS if lit.endswith(b"("))
_BARE_LITERALS: Tuple[bytes, ...] = tuple(lit for lit in _RAW_LITERALS if not lit.endswith(b"("))
_PAREN_ANCHORED_RE = re.compile(
    rb"\((?:"
    + b"|".join(rb"(?<=" + re.escape(lit) + rb")" for lit in _PAREN_LITERALS)
    + rb"""|(?<=open\()(?=[^)\n]*['"][wax]b?\+?['"])"""
    + rb")"
)

_CLAIM_TOKEN_RE = re.compile(
    rb"declare_write\(|_scope_touch_paths|touch_written_path\(|_SCOPE_TOUCH_PATHS_KEY"
)

#: A module-level ``WRITE_SURFACE = ...`` / ``WRITE_SURFACE: ... = ...``
#: declaration, scoped by the caller to ``coordinator_core/install/`` only.
_WRITE_SURFACE_RE = re.compile(rb"^WRITE_SURFACE\s*[:=]", re.M)

_CATEGORIES = frozenset(
    {
        "claims-explicitly",
        "outside-repo",
        "git-internal",
        "ignored-target",
        "in-repo-non-state",
        "to-fix",
    }
)

_RULE_SEAM_MODULES = frozenset(
    {
        "coordinator_core/session/claimed_write.py",
        "coordinator_core/atomic_append.py",
        "coordinator_core/atomic_replace.py",
    }
)

#: The install-prefix a module must sit under for the ``WRITE_SURFACE`` rule
#: to apply. A module outside this prefix that declares ``WRITE_SURFACE`` is
#: NOT rule-covered — see the module docstring.
_INSTALL_PREFIX = "coordinator_core/install/"

#: ============================================================================
#: The register. Seeded from state/audits/2026-09-11-state-writer-census.md
#: (C3) at this chunk's commit — 281 modules total: 3 rule-covered
#: (seam/primitive), 15 rule-covered (WRITE_SURFACE/install), and 263 with an
#: explicit register entry below (47 claims-explicitly, 28 outside-repo, 3
#: git-internal, 129 in-repo-non-state, 56 to-fix; 0 ignored-target at this
#: commit — no census row needed it). First re-run of the scan at this
#: chunk's commit reconciled with C3's ref: no new member landed since C3, so
#: this register and ceiling are C3's table verbatim.
#: C5 (migration batch A) moved coordinator_core/ops/goal_append.py's state
#: write onto the seam (session/claimed_write.py::append_claimed_line); the
#: module's raw-write bytes are gone, so it no longer matches the scan
#: vocabulary at all and its entry/ceiling membership are deleted rather than
#: recategorized — 262 explicit entries, 55 to-fix, population 280.
#: C6 (migration batch B) moved
#: coordinator_core/bash_guards/block_subagent_destructive_action.py's
#: `_log_fail_open` append onto the seam (session/claimed_write.py::
#: append_claimed_line, entry wrapped once at bash_guards/dispatch.py::main
#: via cli_entry.recording_declared_writes). The module keeps a residual raw
#: write (`_rotate_fail_open_log_if_oversized`'s `os.replace(` rotation of
#: the same settings-home-rooted log), so per this batch's body its entry is
#: RECATEGORIZED (never deleted) to the residual's own category —
#: outside-repo, matching `_alternative_liveness.py`'s identical
#: settings-home-rotation shape — rather than swapped to a category that
#: describes the seam write: 262 explicit entries, 54 to-fix.
#: C6 also moved
#: coordinator_core/write_guards/validate_frontmatter_schema_deny.py's
#: `_capture_guard_forensics` write onto the seam
#: (session/claimed_write.py::replace_text, entry wrapped once at
#: write_guards/engine.py::evaluate_payload_json via cli_entry.
#: recording_declared_writes -- the one entry both `__main__.main()` and the
#: out-of-repo PreToolUse dispatchers converge on). No residual raw-write
#: bytes remain, so its entry/ceiling membership are deleted rather than
#: recategorized: 261 explicit entries, 53 to-fix, population 280.
#: C7 (migration batch C: every other package's to-fix writers onto the
#: seam) moved ten modules' state writes onto the seam
#: (session/claimed_write.py::append_claimed_line/replace_text/
#: create_exclusive) — coordinator_core/backlog_grind_assemble/apply.py,
#: coordinator_core/distill/wiki_log_migrate.py,
#: coordinator_core/engine_provenance_counter.py,
#: coordinator_core/fact_contract_gate/engine_gap_ratchet.py,
#: coordinator_core/group_em/send_pass.py,
#: coordinator_core/roadmap/blitz_land.py,
#: coordinator_core/telemetry/cost_census.py,
#: coordinator_core/tracker_store.py, and
#: coordinator_core/workstream_complete/directives_commit_tail.py — plus
#: coordinator_core/baton_assemble/__init__.py, whose only "raw-write"
#: bytes were two docstring mentions of `open(out_path, "w", ...)` (a scan
#: false positive, not a real write site); all ten no longer match the scan
#: vocabulary and their entries/ceiling membership are deleted. Eight more
#: to-fix members held no actual `state/` write and are RECATEGORIZED
#: (never deleted) to the category their real write target names:
#: coordinator_core/ceremony_common/_phantom_sweep_providers.py and
#: coordinator_core/percolate/engine.py + percolate/inject.py write only
#: under a tmp_path fixture / the percolate publish destination tree (never
#: a tracked path in this repo) — outside-repo;
#: coordinator_core/commit_ledger/store.py writes to
#: .coordinator-local/coordinator-sessions/.commit-ledger/, and
#: coordinator_core/contract/emit_memo_schema.py,
#: coordinator_core/frontmatter/author_dependence.py, and
#: coordinator_core/orientation/expired_grant_signal.py write a generated
#: schema file, a committed golden fixture, and a best-effort
#: .coordinator-local/cache/ index respectively — none of these five is
#: `state/`, so in-repo-non-state; coordinator_core/session/claims.py's
#: claim-directory bookkeeping is session-internal (routing it through the
#: seam would recurse through the very claim machinery the seam feeds, per
#: this batch's row body) — git-internal. Population unchanged by a
#: recategorization: 251 explicit entries, 35 to-fix, population 270.
#: ============================================================================

_DISPOSITIONS: Dict[str, Tuple[str, str]] = {
    'coordinator_core/post_run/h1_normalize.py': ('in-repo-non-state', 'docs/wiki page rewrites normalising the H1, never state/: normalize'),
    'coordinator_core/post_run/linkfix.py': ('in-repo-non-state', 'docs/wiki and docs/decisions page rewrites repairing links, never state/: fix'),
    'coordinator_core/post_run/nav_index.py': ('in-repo-non-state', 'docs/wiki nav and _index page rewrites, never state/: rebuild'),
    'coordinator_core/post_run/p5_audit.py': ('in-repo-non-state', 'evidence files under the post-run record home written for the scoped commit: run'),
    'coordinator_core/ops/backlog_pass_ledger.py': ('in-repo-non-state', 'mkdir of the backlog-pass record home; ledger rows go through atomic_append.append_line: record_run'),
    'coordinator_core/ops/weekly_doc_files.py': ('ignored-target', 'wiki-tokens.json page-token cache via tmp + os.replace under the gitignored .coordinator-local/cache/plan-wiki-map/ (or the machine-local override): _wiki_index'),
    'coordinator_core/hooks/preuse_search_dispatch.py': ('git-internal', 'per-agent grep-after-nudge counter inside the session dir under .coordinator-local/coordinator-sessions: _count_grep_after_nudge'),
    'coordinator_core/ops/dispatch_emit/compose_size_probe.py': ('outside-repo', 'probe sizing yaml under temp_layout.managed_temp_dir, never a clone'),
    'coordinator_core/distill/launch_args.py': ('ignored-target', 'launch-args.json under the distill run dir (RUN_DIR_REL, gitignored): build'),
    'coordinator_core/ops/repo_setup.py': ('outside-repo', 'rendered template files (open "w") and the .gitignore append (open "a") under the caller-supplied target root, never the engine checkout state/: _write_new, .gitignore block'),
    'coordinator_core/agent_pipe/__main__.py': ('outside-repo', 'relay worker result.json (tmp + os.replace), argv.json and agent log under <settings-home>/agent-pipe/runs/<run_id>, never the repo: _relay'),
    'coordinator_core/agent_pipe/mailbox.py': ('outside-repo', 'O_APPEND mailbox lines, lock file and drain os.replace under <settings-home>/agent-pipe/mailboxes, never the repo: append, drain'),
    'coordinator_core/agent_pipe/bridge.py': ('outside-repo', 'bridge inbox and log O_APPEND lines, routes.json (tmp + os.replace) and inbox claim rename under <settings-home>/agent-pipe, never the repo: _append_line, _store_routes, _claim_inbox'),
    'coordinator_core/agent_pipe/roster.py': ('outside-repo', 'roster.json via tmp + os.replace under <settings-home>/agent-pipe, never the repo: _store'),
    'coordinator_core/warm/dispatch_fence.py': ('outside-repo', 'empty <key>.killed fence files (os.open O_CREAT) and their stale sweep under the warm claim dir, breadcrumb.svc_dir(engine_root)/claims, a per-user runtime dir outside every clone: write, _sweep_stale'),
    'coordinator_core/group_em/gem_home_path_guard.py': ('git-internal', 'pre-commit hook written into <group-em home>/.git/hooks (the home is outside every checkout), never state/: install'),
    'coordinator_core/hooks/block_ungranted_opus_subagent.py': ('outside-repo', 'override log lines appended via atomic_append under <settings-home>/state/agent-model-overrides.jsonl, never the repo; the open(..., "rb") is a read-only pointer-file read: _record_override'),
    'coordinator_core/ops/agent_pipe.py': ('outside-repo', 'run dir and request.json under <settings-home>/agent-pipe/runs/<run_id>, never the repo: agent_pipe.start/send'),
    'coordinator_core/codex_runner/__main__.py': ('outside-repo', 'worker result.json (tmp + os.replace) under <settings-home>/codex-runs/<run_id>, never the repo: _write_result'),
    'coordinator_core/codex_runner/agy.py': ('outside-repo', 'agy conversation events and stderr logs under <settings-home>/codex-runs/<run_id>, never the repo: AgyConversation.open'),
    'coordinator_core/codex_runner/execute.py': ('outside-repo', 'codex stderr and events.jsonl under <settings-home>/codex-runs/<run_id>, never the repo: run'),
    'coordinator_core/codex_runner/sandbox_preflight.py': ('ignored-target', 'codex-sandbox-proven-roots.json and codex-sandbox-warmup.json via tmp + os.replace under the gitignored repo .coordinator-local/: _atomic_write_json'),
    'coordinator_core/codex_runner/review.py': ('outside-repo', 'codex events and stderr logs under <settings-home>/codex-runs/<run_id>, never the repo: run'),
    'coordinator_core/codex_runner/grok.py': ('outside-repo', 'grok events and stderr logs under <settings-home>/codex-runs/<run_id>, never the repo: review_run, _read_stream'),
    'coordinator_core/codex_runner/roster.py': ('outside-repo', 'roster record json via tmp + os.replace under <settings-home>/codex-runs, never the repo: write_record'),
    'coordinator_core/codex_runner/persona.py': ('outside-repo', 'persona provider stdout captured to persona.stdout inside the codex-runs run_dir under <settings-home>/codex-runs/<run_id>, never the repo: run'),
    'coordinator_core/codex_runner/worker.py': ('outside-repo', 'worker provider stdout captured to worker.stdout inside the codex-runs run_dir under <settings-home>/codex-runs/<run_id>, never the repo: run'),
    'coordinator_core/codex_runner/workflow.py': ('git-internal', 'workflow agent admission.json, prompt, schema, stdout/stderr, process.json and result.json under the per-agent artifact_dir inside .coordinator-local/coordinator-sessions/workflow-fires (the identity observer refuses any artifact_dir outside repo .git), plus the codex-usage ledger line appended under <settings-home>/state/codex-usage: run_agent, _record_usage'),
    'coordinator_core/git/run.py': ('outside-repo', 'anonymous tempfile.TemporaryFile(mode="w+b") spool files for child stdout, stderr and fed stdin on Windows, deleted on close and never a named path: run_git'),
    'coordinator_core/ops/session_ceremonies.py': ('outside-repo', 'ceremony worker request.json, admitted marker and worker.log under <settings-home>/ceremony-runs/<run_id>, never the repo: _launch_admitted'),
    'coordinator_core/session_ceremonies.py': ('outside-repo', 'ceremony result json tmp-then-replace and the notify.lock claim (O_CREAT|O_EXCL) under <settings-home>/ceremony-runs/<run_id>, never the repo: _write_json, _notify'),
    'coordinator_core/ops/workflow_fire/acceptance.py': ('outside-repo', 'acceptance harness seeds README.md and acceptance-request/submitted json in a throwaway git repo created under the caller-supplied --workspace, never this repo: main'),
    'coordinator_core/ops/workflow_fire/cold_operation.py': ('outside-repo', 'cold-operation reply json written to the caller-supplied response path, a per-fire file under the workflow artifact_dir inside .coordinator-local/coordinator-sessions, never state/: main'),
    'coordinator_core/ops/workflow_fire/finalizer.py': ('git-internal', 'terminal-action.json, terminal-finalizer-state.json (exclusive create) and receipt via tmp + os.replace beside the protected roster in the workflow artifact_dir under .coordinator-local/coordinator-sessions/workflow-fires, never state/: _write, run'),
    'coordinator_core/ops/workflow_fire/headless.py': ('git-internal', 'headless fire record tmp + os.replace, worker.log, request/stdout/stderr, execution.guard (exclusive create) and journal.jsonl under registry/headless/<fire_id> inside .coordinator-local/coordinator-sessions/workflow-fires, never state/: _save, fire_headless, execute'),
    'coordinator_core/ops/workflow_fire/headless_agents.py': ('git-internal', 'headless agent admission.json, prompt, schema, stdout/stderr, process.json and result.json under the per-agent artifact_dir inside .coordinator-local/coordinator-sessions/workflow-fires, never state/: run_agent'),
    'coordinator_core/ops/workflow_fire/private_engine.py': ('git-internal', 'worker-bindings.json (tmp + os.replace) and private-server output logs under the private-engine artifact_dir inside .coordinator-local/coordinator-sessions/workflow-fires, never state/: _atomic_json, start'),
    'coordinator_core/warm/door/private_required.py': ('git-internal', 'door.private.json binding created exclusively beside door.exe in the private-bin alias directory under the workflow artifact_dir inside .coordinator-local/coordinator-sessions, never state/: write_binding'),
    'coordinator_core/worker_contracts/worker.py': ('outside-repo', 'false positive: the only hit is the keyword mode="worker" passed to a provider launch, no file is written; the stdout handle is supplied by the caller: supervise'),
    'coordinator_core/ops/codex_runner.py': ('outside-repo', 'request.json, result and worker log under <settings-home>/codex-runs/<run_id>, never the repo: _codex_runner_start'),
    'coordinator_core/contract/emit_op_catalog.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: generated contract artifacts (op-catalog.json, mcp-tools.json, docs/reference/op-catalog.md)'),
    'coordinator_core/ops/rea_ledger_write.py': ('in-repo-non-state', 'the rea-ledger writer op: claim and evidence files via atomic_write_bytes and unknown-revision lines raw-appended ("ab", one write, fsync) under the tracked state/rea-ledger/, the only module naming that root outside coordinator_core/rea_ledger: _rea_ledger_write'),
    'coordinator_core/launch_records.py': ('outside-repo', 'workflow launch records move from the flat managed-temp name into <managed temp>/workflow-runs/<session>/ via os.replace, never the repo: migrate_flat_records'),
    'coordinator_core/ops/review_partition_slices.py': ('claims-explicitly', 'claim token in _write_slice'),
    'coordinator_core/index_distribution/newer_index_marker.py': ('in-repo-non-state', 'newer-index marker json via tmp + os.replace under the repo index-distribution dir, never state/: write_marker'),
    'coordinator_core/backlog_grind_assemble/grind_fix_paths.py': ('in-repo-non-state', 'per-row fix-paths json via tmp + os.replace under the caller-supplied <run_dir>/fix-paths, a run-artifact dir, never state/: write'),
    'coordinator_core/cartography/atlas_catchup.py': ('ignored-target', 'watermark and pending json via tmp + os.replace under the gitignored .coordinator-local/freshness/: _write_json'),
    'coordinator_core/cartography/atlas_freshness.py': ('in-repo-non-state', 'files: frontmatter line rewrite of docs/architecture/systems pages (write_bytes) and acted-fact JSON lines appended to the gitignored .coordinator-local/freshness/atlas-acted.jsonl, never state/: apply_repair, record_acted'),
    'coordinator_core/hooks/project_orientation.py': ('ignored-target', 'empty once-per-hour claim stamp .coordinator-local/scratch-hygiene-trigger.stamp (gitignored) before launching fleet.scratch_hygiene: _claim_scratch_trigger'),
    'coordinator_core/hooks/commit_resolution.py': ('git-internal', 'staged-set cache json via tmp + os.replace beside the index inside the git dir, never the worktree: _save'),
    'coordinator_core/ops/rag_freshness.py': ('outside-repo', 'refresh-queue marker json under <settings-home>/rag-freshness-queue, never the repo: queue_refresh'),
    'coordinator_core/ops/reverify_base_measure.py': ('in-repo-non-state', 'baseline-measure result file at the caller-supplied out path (a run scratch artifact), never state/: main'),
    'coordinator_core/ops/_anchor_premise.py': ('outside-repo', 'baseline blobs from git show written into a tempfile.TemporaryDirectory, discarded on exit: the anchor-baseline evaluator'),
    'coordinator_core/publish/dry_refire_gate.py': ('outside-repo', 'built-in sample sizing, plan and halted run written into the caller-given throwaway root, never the repo: write_sample'),
    'coordinator_core/ops/claude_home_census.py': ('outside-repo', 'falsifier fixture of sparse files under a caller-given scratch root, never the repo: _sparse'),
    'coordinator_core/ops/dispatch_emit/ledger_recovery.py': ('claims-explicitly', 'live-tree snapshot json at the caller-named --out under the run record dir state/warp/<run_id>/ (declare_write), plus loose pre-leg blob objects in the git common dir: snap_before, snap_after'),
    'coordinator_core/ops/dispatch_emit/live_write_guard.py': ('in-repo-non-state', 'restores a foreign live hunk a fix leg dropped: writes the merge_or_refuse result back to the one live-tree path the snapshot measured the leg writing: restore'),
    'coordinator_core/ops/dispatch_emit/in_run_widen.py': ('in-repo-non-state', 'widen record appended under the tracked run record dir state/warp/<run_id>/: record_widen'),
    'coordinator_core/ops/dispatch_emit/run_write_prints.py': ('in-repo-non-state', 'write_prints.json (tmp + os.replace) under the run record dir state/warp/<run_id>/, plus loose blob objects in the git common dir: record'),
    'coordinator_core/ops/dispatch_emit/minted_records.py': ('in-repo-non-state', 'minted-record manifest appended under the run record dir state/warp/<run_id>/minted.jsonl: record'),
    'coordinator_core/ops/retention_run.py': ('outside-repo', 'retention lock file under the Claude home dir, O_EXCL create and stale unlink: _acquire'),
    'coordinator_core/write_guards/_memory_evict.py': ('outside-repo', 'evicts a body file, rewrites MEMORY.md and appends memory-evictions.jsonl in a Claude-home project memory dir: evict_oldest'),
    'coordinator_core/session/deferred_release.py': ('git-internal', 'deferred claim-release record json via tmp + os.replace under .coordinator-local/coordinator-sessions/deferred-release/<sid-hash>/: record'),
    'coordinator_core/git/ref_lock.py': ('git-internal', 'ref .lock completion/quarantine, owner records and the recovery log, all under <gitdir> (<gitdir>/refs, <gitdir>/coordinator-ref-locks, <gitdir>/coordinator-stale-ref-locks): register_holder, _append_record, _claim_owner, _restore_owner, _resolve_lock'),
    'coordinator_core/git/store_hygiene.py': ('git-internal', 'daily-tier claim file created exclusively under <git-common-dir>/coordinator/store-hygiene.claim: _claim'),
    'coordinator_core/install/runtime_ignore_apply.py': ('in-repo-non-state', 'runtime-ignore rule block appended to the repo .gitignore, never state/: _append_rules'),
    'coordinator_core/ops/dispatch_emit/ask_stage_receipt.py': ('in-repo-non-state', 'stage claim exclusive-create and stage-receipt.json under the tracked record dir state/warp/<run_id>/: claim, write'),
    'coordinator_core/grind_triage/checkpoint.py': ('in-repo-non-state', 'grind checkpoint lines appended via atomic_append under <run_dir>/progress.jsonl, the run record home: emit_checkpoints'),
    'coordinator_core/ops/dispatch_emit/grind_compose.py': ('in-repo-non-state', 'one triage verify-evidence file per closed row under state/queue-grind/<profile>/evidence/<run_id>/, the grind record home: close_triaged_rows'),
    'coordinator_core/ops/dispatch_emit/run_artifacts.py': ('in-repo-non-state', 'one outcome.json per run via tmp + os.replace under the tracked state/run-evidence/<run_id>/: write_tracked_outcome'),
    'coordinator_core/ops/dispatch_emit/clean_overlay_verify.py': ('in-repo-non-state', 'overlay-verdict.json beside measured-rev.json under the run record dir state/warp/<run_id>/, one derived verdict per run: write_verdict'),
    'coordinator_core/ops/dispatch_emit/ask_stage_blocks.py':('in-repo-non-state', 'ask-stage block text written under the run scratch/record dir by _write'),
    'coordinator_core/ops/dispatch_emit/checkpoint_reds.py': ('in-repo-non-state', 'introduced-red fix row written under the tracked bug backlog dir, committed with the run: mint_fix_rows'),
    'coordinator_core/ops/dispatch_emit/terminal_commit_closeout.py': ('in-repo-non-state', 'mkdir of the sizing-ledger and run-record dirs; bytes go through atomic_append.append_line and replace_text'),
    'coordinator_core/ops/dispatch_emit/terminal_commit_ledger.py': ('in-repo-non-state', 'verdicts section appended to the run note, committed with the run'),
    'coordinator_core/ops/dispatch_emit/terminal_commit_stamp.py': ('in-repo-non-state', 'regenerated publish allowlist under coordinator/, committed with the run; move rollback via os.replace'),
    'coordinator_core/ops/dispatch_emit/halt_resume_record.py': ('in-repo-non-state', 'resume.json and the resume.building claim under the tracked run record dir state/warp/<run_id>/, the halted-run hint under .coordinator-local/halt-resume-hint/: write, claim_recompose, write_hint'),
    'coordinator_core/backlog_grind_assemble/grind_split.py': ('claims-explicitly', 'parent and per-item child backlog rows replaced atomically (tmp + os.replace), declared through declare_write: _write_atomic'),
    'coordinator_core/codex_runner/codex_spawn_env.py': ('ignored-target', 'codex auth.json/config.toml copied via .part + os.replace and a write probe under <writable_root>/scratch/codex-home, gitignored scratch, never state/'),
    'coordinator_core/gem_inbox_apply.py': ('claims-explicitly', 'manifest writes (batons under state/handoffs) declared through declare_write; pending ledger json and moves stay under the gitignored .coordinator-local/gem-inbox-pending: apply_manifest'),
    'coordinator_core/git/worktree_sync.py': ('git-internal', 'materializes a landed committed blob into the worktree via tmp + os.replace, the same bytes a git checkout writes; no new content: _sync_one'),
    'coordinator_core/ops/fleet/_memo_push_pending.py': ('ignored-target', 'push-pending record json via tmp + os.replace under the gitignored .coordinator-local/memo-outbox/push-pending, never state/: _write_atomic'),
    'coordinator_core/session/op_dispatch_nag.py': ('ignored-target', 'throttle stamp beside the session lane file in the coordinator-sessions hub, never state/: _throttled'),
    'coordinator_core/plan_completion_ledger.py': ('claims-explicitly', 'plan-completion row appended to state/plan-completions.jsonl via atomic append_line, declared through declare_write: append_row'),
    'coordinator_core/ops/dispatch_emit/held_revisions.py': ('claims-explicitly', 'held-revision store jsonl appended under the run record dir, declared through declare_write: _append'),
    'coordinator_core/ops/dispatch_emit/run_accept_partial.py': ('claims-explicitly', 'accept_partial.jsonl appended under the run dir, declared through declare_write: record'),
    'coordinator_core/ops/dispatch_emit/tree_write_fence.py': ('claims-explicitly', 'restore-refusal list under the run record dir via tmp + os.replace (a corrupt list is renamed aside), declared through declare_write: record_refusal'),
    'coordinator_core/warm/client.py': ('outside-repo', 'unanswered.json run counter (write_text) beside the discovery record under the warm runtime dir, a per-user runtime dir outside any repo: _count_unanswered, _clear_unanswered'),
    'coordinator_core/ops/dispatch_emit/land_resume.py': ('outside-repo', 'land-resume queue entries (tmp + os.replace, rename claims) and the resume log under the warm runtime dir, breadcrumb.svc_dir(engine_root)/land-resume, outside any repo: _write, enqueue, next_due, finish'),
    'coordinator_core/architecture_survey/mechanical_report.py': ('ignored-target', 'atlas mechanical report json under gitignored scratch/atlas-mechanical: produce'),
    'coordinator_core/ops/dispatch_emit/land_blobs.py': ('git-internal', 'index.lock exclusive-create and the spliced index bytes inside the gitdir, retired by the ref CAS or abort: the in-process commit writer'),
    'coordinator_core/ops/dispatch_emit/land_blobs_halt.py': ('in-repo-non-state', 'halt record json (tmp + os.replace) under the run record dir: write_halt_record'),
    'coordinator_core/ops/dispatch_emit/stranded_sibling.py': ('in-repo-non-state', 'touched-blobs.json under the run record dir: write'),
    'coordinator_core/ops/dispatch_emit/run_credit.py': ('claims-explicitly', 'credit.jsonl appended under the run dir, declared through declare_write: record'),
    'coordinator_core/ops/dispatch_emit/pipeline_run.py': ('claims-explicitly', 'revisions.md appended under the run scratch dir, declared through declare_write: append_revision'),
    'coordinator_core/warm/paused_runs.py': ('ignored-target', 'paused-run records and pause/handover markers (tmp + os.replace, unlink) under the gitignored .coordinator-local/paused-runs/: _atomic_write, clear_pause_request, clear_handover_complete'),
    'coordinator_core/ops/fleet/scratch_hygiene.py': ('ignored-target', 'sweep cursor tmp + os.replace under the gitignored scratch/ dir: _write_cursor'),
    'coordinator_core/usage/session_cost.py': ('outside-repo', 'per-session cost cache tmp + os.replace under <settings-home>/state/session-cost: _write_cache'),
    'coordinator_core/git/sshsig.py': ('outside-repo', 'request frames written to the ssh-agent socket, never a file: _agent_requests'),
    'coordinator_core/hooks/submit_engine_ask.py': ('in-repo-non-state', 'engine-friction row appended to .coordinator-local/engine-friction.jsonl, never state/: _record_skipped_overlap_check; the refusal log line is appended under .coordinator-local likewise'),
    'coordinator_core/hooks/nudge_hand_written_plan.py': ('outside-repo', 'exclusive-create one-shot sentinel under tempfile.gettempdir()'),
    'coordinator_core/hooks/nudge_em_research_scoping.py': ('outside-repo', 'per-session once-per-turn sentinel json under tempfile.gettempdir(): mark_signalled'),
    'coordinator_core/arrival_record.py': ('git-internal', 'subagent-arrivals.jsonl appended under .coordinator-local/coordinator-sessions/<sid>: append_arrival'),
    'coordinator_core/ops/baton_pm_turns.py': ('git-internal', 'pm-turns log appended under .coordinator-local/coordinator-sessions/<sid>: append_turn'),
    'coordinator_core/ops/dispatch_emit/landing_export.py': ('ignored-target', 'blob export and its staging dir under the gitignored <worktree_root>/scratch/: write_bytes, os.replace'),
    'coordinator_core/ops/dispatch_emit/ask_brief.py': ('ignored-target', 'over-cap brief full text spilled to the gitignored scratch/em-brief-spill/<digest>.md: load_em_brief'),
    'coordinator_core/ops/dispatch_emit/research_emit.py': ('ignored-target', 'ask.md and brief.md under the caller-named run scratch dir (gitignored scratch/): write_ask, bind_context'),
    'coordinator_core/ops/dispatch_emit/clean_overlay_verify.py': ('in-repo-non-state', 'overlay verdict json (write_text) beside measured-rev.json under the tracked run record dir state/warp/<run_id>/, not an ownership-claimed state file; the overlay clone dir is reaped via rmtree: write_verdict'),
    'coordinator_core/ops/dispatch_emit/owed_leg_queue.py': ('in-repo-non-state', 'owed-leg records written with open(..., "w") as state/owed-legs/<run_id>--<id>.yaml, one file per leg, idempotent rewrite keeping `created`; the record is a run-halt artifact committed with the run, not a claimed state file: write_owed_legs'),
    'coordinator_core/ops/review_reachability.py': ('in-repo-non-state', 'no write: a read-only reachability classifier; the write_text/write_bytes and open(..., "w") tokens are docstring prose and regexes that detect those calls in a reviewed range'),
    'coordinator_core/ops/dispatch_emit/measured_rev.py': ('in-repo-non-state', 'measured-rev.json under the tracked record dir state/warp/<run_id>/, beside manifest.json; git objects via write_object: write_text'),
    'coordinator_core/ops/dispatch_emit/resume_snapshot.py': ('in-repo-non-state', 'resume-snapshot.json in the gitignored run scratch dir scratch/warp/<run_id>/: write'),
    'coordinator_core/bug_blitz/mint.py': ('claims-explicitly', 'in-place tag rewrite of a state/bug-backlog row via tmp + os.replace, declared through declare_write: _write_reuse'),
    'coordinator_core/ops/dispatch_emit/kept_work.py': ('claims-explicitly', 'kept.json under the run record dir, declared through declare_write: write'),
    'coordinator_core/ops/dispatch_emit/row_returns.py': ('claims-explicitly', 'returns.json under the run record dir, declared through declare_write: write'),
    'coordinator_core/git/commit.py': ('in-repo-non-state', 'published-tree classification file rewritten in place with the refused inclusions and added to the commit path list: write_text; the post-landing worktree sync is the sync_landed_paths call, whose writes are listed under coordinator_core/git/worktree_sync.py'),
    'coordinator_core/git/worktree_sync.py': ('in-repo-non-state', 'post-landing 3-way sync of landed paths into the worktree via tmp + os.replace, re-read guarded: _sync_one'),
    'coordinator_core/idle_compact/watcher.py': ('outside-repo', 'injections.jsonl append and armed.json tmp + os.replace under <settings>/state/idle-compact: append_log, save_armed'),
    'coordinator_core/ops/dispatch_emit/ask_fanout.py': ('claims-explicitly', 'ask records staged exclusive-create under .coordinator-local/fanout-staging, published by os.replace with ledger.claim and declare_write: _stage_text, the publish loop'),
    'coordinator_core/ops/dispatch_emit/tranche_chain.py': ('in-repo-non-state', 'zero-byte O_EXCL tranche-chain.claim marker in the run record dir (ask_contract.record_dir) so a re-fired terminal commit spawns the next part once: _claim_chain'),
    'coordinator_core/warm/dispatch_ack.py': ('outside-repo', 'per-key <key>.outcome.json (tmp + os.replace, first writer wins) under the ack-journal dir beside the engine service files (journal_dir_under), never the repo: record_outcome'),
    'coordinator_core/ops/dispatch_emit/live_run_index.py': ('outside-repo', 'O_EXCL claim markers (os.open, os.write) under the settings-home engine record dir coordinator-warp-live-claims, never the repo; the index json goes through halt_resume_record.bounded_write: _claim_markers'),
    'coordinator_core/ops/dispatch_emit/land_run_grind.py': ('in-repo-non-state', 'undo of grind_rows close for an unlanded row: restores the source row byte-identical to its HEAD blob (write_bytes) and unlinks the untracked archive copy, so the tree returns to HEAD for that row rather than recording new state: _reopen_held_rows'),
    'coordinator_core/ops/dispatch_emit/land_leg_record.py': ('in-repo-non-state', 'land-legs.json via tmp + os.replace under the tracked record dir state/warp/<run_id>/: write'),
    'coordinator_core/ops/dispatch_emit/landing_integrity.py': ('git-internal', 'parse-verdict cache json via tmp + os.replace under <git-common-dir>/coordinator-parse-verdicts: _write_verdict'),
    'coordinator_core/ops/fleet/terminal_sizing_drain.py': ('in-repo-non-state', 'sizing files moved by os.replace between tracked state/sizings dirs then committed through the head spine; commit message via a managed tempfile: drain'),
    'coordinator_core/ops/dispatch_emit/run_withhold.py':('claims-explicitly', 'withhold.jsonl appended under the run dir, declared through declare_write: record'),
    'coordinator_core/ops/dispatch_emit/halt_tree_evict.py': ('claims-explicitly', 'evicted blobs and index under the run record dir, declared through declare_write; tree restored to HEAD bytes: write_bytes, write_text'),
    'coordinator_core/ops/dispatch_emit/objective_calibrate.py': ('in-repo-non-state', 'objective.json, base file and loop log under the tracked record dir state/warp/<run_id>/, calibration checkouts under gitignored scratch/: _atomic_write, append'),
    'coordinator_core/ops/dispatch_emit/objective_check.py': ('in-repo-non-state', 'loop log appended under the tracked record dir state/warp/<run_id>/: _append_line'),
    'coordinator_core/ops/dispatch_emit/objective_write.py': ('in-repo-non-state', 'objective.json and base file under the tracked record dir state/warp/<run_id>/ via atomic_write_bytes: write'),
    'coordinator_core/ops/dispatch_emit/verdict_supersession.py': ('claims-explicitly', 'tests-reverify record created exclusively under state/, declared through declare_write'),
    'coordinator_core/ops/review_prep.py': ('claims-explicitly', 'prep-freeze sidecar under the session share dir, declared through declare_write: _write_sidecar'),
    'coordinator_core/ops/dispatch_emit/terminal_commit_params_channel.py': ('ignored-target', 'params.json under the gitignored run scratch dir (RUN_DIR_ROOT/<run_id>/): materialize'),
    'coordinator_core/roadmap/blitz_stage.py': ('in-repo-non-state', 'OVERVIEW.md frontmatter stamp in the roadmap run dir, committed by _commit_paths in the same call; state writes go through create_exclusive'),
    'coordinator_core/group_em/gem_home.py': ('outside-repo', 'group-em home dir under <settings-home>/group-em (resolve_home refuses any path inside a checkout): .gitignore lines, seeded CLAUDE.md and state/ in ensure_home, _ensure_ignored'),
    'coordinator_core/ops/dispatch_emit/process_artifact_policy.py': ('git-internal', 'run-artifact path lines appended to <git-common-dir>/info/exclude, never a tracked path: exclude_locally'),
    'coordinator_core/ops/plan_chain/contract.py': ('claims-explicitly', 'chain manifest under the caller-named trail dir, declared through declare_write'),
    'coordinator_core/ops/plan_chain/digest.py': ('claims-explicitly', 'final digest under the trail dir, declared through declare_write: write_final_digest'),
    'coordinator_core/ops/plan_chain/phase1_checks.py': ('ignored-target', 'completeness sidecar under .coordinator-local/plan-sidecars (gitignored)'),
    'coordinator_core/ops/plan_chain/plan_stage.py': ('claims-explicitly', 'bound plan script under the trail dir, declared through declare_write'),
    'coordinator_core/ops/research_close.py': ('in-repo-non-state', 'scouts digest.md in the gitignored run scratch dir (_build_digest); archived copies and the docs/research/trail record are written in-repo and committed by _close_committed'),
    'coordinator_core/ops/seam_baton_mint.py': ('claims-explicitly', 'exclusive-create staged batons under state/handoffs/ and state/seam-baton-index.json, declared through declare_write'),
    'coordinator_core/percolate/guard_cache.py': ('git-internal', 'publish parse cache under the destination repo git dir'),
    'coordinator_core/percolate/gate_result_cache.py': ('git-internal', 'per-gate verdict cache json via tmp + os.replace under the publish destination repo git dir (.git/claude-klabauter-gate-result): GateResultCache.flush'),
    'coordinator_core/workstream_complete/apply.py': ('in-repo-non-state', 'review-notes section appended to the completion entry under archive/completed/'),
    'coordinator_core/content_root.py': ('outside-repo', 'content-root pointer under settings_home()/machine-local, outside every repo: migrate'),
    'coordinator_core/sizing_assemble/__init__.py': ('claims-explicitly', 'exclusive-create sizing scaffold under state/sizings/, declared through declare_write: _scaffold_missing'),
    'coordinator_core/completion_receipts/store.py': ('claims-explicitly', 'exclusive-create receipt under state/completion-receipts/, declared through declare_write: write_receipt'),
    'coordinator_core/contract/cockpit_schema/entities/completion_receipt.py': ('in-repo-non-state', 'no write: the vocabulary hit is pydantic\'s model_validator(mode="after")'),
    'coordinator_core/git/reconcile_manifest.py': ('git-internal', 'conflict manifest tmp-then-replace under <git-dir>/coordinator-reconcile/conflict-manifest.json: write'),
    'coordinator_core/git/hook_dispositions.py': ('git-internal', 'hook backup and in-place hook rewrite under <git-common-dir>/hooks: _write_backup, _replace_file'),
    'coordinator_core/hooks/postuse_subagent_compaction_warning.py': ('outside-repo', 'per-agent band marker under settings_home()/state/compaction-warned, outside every repo'),
    'coordinator_core/percolate/payload_parity.py': ('ignored-target', 'parity verdict memo index-*.json via tmp + os.replace under machinery_paths.cache_dir (gitignored .coordinator-local/cache/payload-parity-memo): _ParityMemo.save'),
    'coordinator_core/ops/discover_working_repos.py': ('outside-repo', 'tmp-then-replace of <home>/.claude/working-repos.yaml: write_working_repos_yaml'),
    'coordinator_core/ops/dispatch_emit/reverify_delivery.py': ('claims-explicitly', 'exclusive-create delivery-verdict record declared through declare_write: record_delivery_verdict; emit_reverify writes only the operator-named --out script'),
    'coordinator_core/ops/dispatch_emit/requirements_ledger.py': ('in-repo-non-state', 'run.json, the revision inbox and the requirements view under the tracked record dir state/warp/<run_id>/: _write_run, _write_view, revise; PM rows go through requirement_register (tracked, not gitignored scratch)'),
    'coordinator_core/ops/dispatch_emit/stage_receipts.py': ('in-repo-non-state', 'stage receipt under the tracked record dir state/warp/<run_id>/receipts: write_receipt, atomic tmp then replace'),
    'coordinator_core/ops/dispatch_emit/run_close.py': ('ignored-target', 'close-job record under .coordinator-local/close-jobs (gitignored): _write_atomic'),
    'coordinator_core/ops/ceremony/close_background.py': ('in-repo-non-state', 'no write: the vocabulary hit is dataclasses.replace; the legs write through their own seams'),
    'coordinator_core/ops/dispatch_emit/retest_targeted.py': ('ignored-target', 'fresh test-verdict record and the launcher log beside it under the retest dir (landing_integrity.RETEST_DIR): the two path.write_text calls and the _run_argv log open; --isolated-export also builds the gitignored scratch/warp/<run>/export via landing_export.build_head_export and removes it on pass'),
    'coordinator_core/ops/dispatch_emit/launcher_staged_twin.py': ('ignored-target', 'staged launcher twin under the gitignored scratch/warp/<run_id>/, contained by contained_path; carries the PLAINTEXT single-op run token inlined as runToken, so it must stay gitignored: ensure_twin tmp write_bytes, os.replace'),
    'coordinator_core/ops/dispatch_emit/reply_sidecar.py': ('in-repo-non-state', 'content-addressed stage readers under scratch/stage-readers and reply pages under the run record dir state/warp/<run_id>/: _ensure_reader, write_reply'),
    'coordinator_core/ops/plan_premise_carry.py': ('ignored-target', 'premise-carry record under .coordinator-local/plan-sidecars (gitignored): _write_atomic'),
    'coordinator_core/ops/clean_clone_proof.py': ('in-repo-non-state', 'one verdict json per sha via tmp + os.replace under the tracked state/warp/clean-clone-proofs/: _write_verdict'),
    'coordinator_core/ops/dispatch_emit/run_stage_lookup.py': ('outside-repo', 'run-stage-lookup.json via tmp + replace in the Workflow run dir under the Claude projects dir, never the repo: record'),
    'coordinator_core/ops/dispatch_emit/run_close_lessons.py': ('outside-repo', 'front-matter stamp on a candidate queue_promote just wrote into the central lessons outbox, which is not this repo: _stamp'),
    'coordinator_core/ops/dispatch_emit/stage_wait.py': ('outside-repo', 'stage wait records, their O_EXCL lock and started markers under run_token._record_home (<settings home>/machine-local/engine-records/<key>)/coordinator-warp-stage-waits: _write, _write_quiet, _record_lock, _claim_start'),
    'coordinator_core/ops/dispatch_emit/run_token.py': ('git-internal', 'sha256-only run-token records under <settings home>/machine-local/engine-records/<key>/coordinator-warp-run-tokens (reads fall back to the legacy <git common dir> copy): mint, admits and redeem claim by rename, bind_sizing rewrites in place'),
    'coordinator_core/bug_blitz/targeted_run.py': ('in-repo-non-state', 'runner record JSON at the caller-named --record path, no state/ component: write_record'),
    'coordinator_core/bug_blitz/foreign_edits.py': ('outside-repo', 'HEAD-tree cache under tempfile.gettempdir(), never the repo: head_tree'),
    'coordinator_core/ops/workflow_fire/headless_em_grant.py': ('outside-repo', 'headless-EM grant record under <settings home>/state/headless-em-grants/<sha256(session)>.json, outside every checkout (tmp write + os.replace): write_headless_em_grant'),
    'coordinator_core/group_em/admission_post.py': ('outside-repo', 'append-only admission line under settings_home()/state/group-em/box, outside every repo: post'),
    'coordinator_core/hooks/submit_bug_blitz.py': ('in-repo-non-state', 'decisions record in the bug-blitz run dir, not state/: _write_decisions'),
    'coordinator_core/ops/dispatch_emit/ask_gate.py': ('in-repo-non-state', 'minted M-route spine at the path mint_m_spine names, written only when absent: the spine-direct mint'),
    'coordinator_core/ops/dispatch_emit/cross_repo_write_refusal.py': ('git-internal', 'sibling-write grant ledger under <repo>/.coordinator-local/coordinator-sessions/<sid>: record_grants'),
    'coordinator_core/ops/dispatch_emit/ask_stage.py': ('in-repo-non-state', 'spine, manifest and pm-intent under the tracked record dir state/warp/<run_id>/, written by the named writer _write; briefs and the commit-request marker stay gitignored scratch/warp/'),
    'coordinator_core/ops/dispatch_emit/plan_route.py': ('in-repo-non-state', 'the emitted dispatch workflow script at the caller-named --out path, contained under target_root by contained_path (PathEscapeError otherwise), no state/ component: emit_dispatch write_bytes'),
    'coordinator_core/session/handoff_claim_index.py': ('git-internal', 'derived handoff-claim signature cache under <git common dir>/coordinator-housekeeping, never committed: _save writes a same-directory temp file then os.replace'),
    'coordinator_core/workweek_complete/release/merge.py': ('in-repo-non-state', 'transient pr-body-<sha>.md in the caller-supplied run_dir, handed to gh pr create --body-file: merge_pinned'),
    'coordinator_core/ops/review_mint/supersede.py': ('claims-explicitly', 'exclusive-create superseding record under state/superseding-reviews/, declared through declare_write: record_superseding_review'),
    'coordinator_core/percolate/_entrypoint_depth_probe/sitecustomize.py': ('outside-repo', 'per-pid depth record in the env-named dir the percolate gate makes with tempfile.TemporaryDirectory'),
    'coordinator_core/codex_runner/codex_spawn_env.py': ('ignored-target', 'isolated codex child home (auth.json, config.toml re-keyed, hooks.json) via .part + os.replace under the gitignored <writable_root>/scratch/codex-home, plus the scratch/codex-tmp mkdir: _isolate_codex_home, _home_writable'),
    'coordinator_core/session/xs_lane_budget.py': ('git-internal', "the session's XS lane record under .coordinator-local/coordinator-sessions/<sid>/, never committed: _write writes a temp file then os.replaces it; _create_exclusive writes a temp file then os.links it"),
    'coordinator_core/session/em_turn_count.py': ('git-internal', "the session's em-turns.json beside xs-lane.json under .coordinator-local/coordinator-sessions/<sid>/, never committed: save_state writes a same-dir temp file then os.replaces it"),
    'coordinator_core/session/migrate_hub.py': ('ignored-target', 'one-shot merge-copy of the legacy .git/coordinator-sessions hub into the gitignored .coordinator-local/coordinator-sessions hub: migrate_common'),
    'coordinator_core/resource_lifecycle/lease.py': ('in-repo-non-state', 'POSIX-only <socket>.lock flock target in the caller-supplied lease directory; the module picks no target and has no caller yet: Lease.acquire'),
    'coordinator_core/resource_lifecycle/store.py': ('in-repo-non-state', 'append-only JSONL at the caller-supplied path; the module picks no target and has no caller yet, so the first caller re-dispositions it: BoundedStore.append'),
    'coordinator_core/session/incident_claims.py': ('git-internal', 'incident-claim holder fields under .coordinator-local/coordinator-sessions: _write_field'),
    'coordinator_core/hooks/support/turn_intent.py': ('git-internal', 'per-session turn-intent record, block counter and role marker inside the session dir under .coordinator-local/coordinator-sessions: _atomic_write'),
    'coordinator_core/hooks/guard_manufactured_blocker.py': ('git-internal', 'per-session has-fired marker inside the session dir under .coordinator-local/coordinator-sessions: _mark_fired_this_session'),
    'coordinator_core/ops/handoff_discharge_landed.py': ('git-internal', 'O_EXCL batch lock under the git common dir: _acquire_lock; the handoff stamp write itself goes through claimed_write.replace_text'),
    'coordinator_core/telemetry/traffic_manifest.py': ('in-repo-non-state', 'operator-named --out file for a generated traffic manifest; no state/ target'),
    'coordinator_core/hooks/stop_dispatch.py': ('outside-repo', 'drive-root baseline tmp-then-replace under settings_home()/state/drive-root-baseline.json, outside every repo: _drive_baseline_path; the "rb" open is a transcript tail read'),
    'coordinator_core/hooks/preuse_sendmessage_dispatch.py': ('outside-repo', 'once-per-agent advised marker beside the harness transcript under <transcript>/subagents/workflows/<run>: _claim_once'),
    'coordinator_core/ops/migrate_completion_log_legacy.py': ('outside-repo', 'git --pathspec-from-file list under tempfile.mkstemp(), removed after the commit: _commit_moved'),
    'coordinator_core/hooks/flag_em_poll_in_flight.py':('outside-repo', 'per-session poll counter under tempfile.gettempdir(): _save_poll_state'),
    'coordinator_core/warp_fire.py': ('outside-repo', 'per-session engine-ask nag counter under tempfile.gettempdir()/coordinator-engine-ask-nag: _bump'),
    'coordinator_core/warp_live.py': ('ignored-target', 'run submission record beside the run briefs under scratch/warp/<run_id>/ (gitignored scratch/): record_submission'),
    'coordinator_core/hooks/guard_doctrine_surface_ratio_precommit.py': ('outside-repo', 'accumulator json under machine_local_dir() (settings home): _save_accumulator'),
    'coordinator_core/hooks/nudge_multiwave_workflow.py': ('git-internal', 'dispatch log inside the session dir under .coordinator-local/coordinator-sessions'),
    'coordinator_core/hooks/observe_config_change.py': ('git-internal', 'hook-observation jsonl under .coordinator-local/coordinator-sessions/hook-observations'),
    'coordinator_core/hooks/observe_post_compact.py': ('git-internal', 'hook-observation jsonl under .coordinator-local/coordinator-sessions/hook-observations'),
    'coordinator_core/hooks/offer_exploration_tier_dispatch.py': ('git-internal', 'exclusive-create fire marker inside the session dir under .coordinator-local/coordinator-sessions'),
    'coordinator_core/hooks/pickup_autofire.py': ('git-internal', 'decision files under <repo>/.coordinator-local/coordinator-sessions/decisions and a probe log under tempfile.gettempdir()'),
    'coordinator_core/hooks/repin_cloud_engine_root.py': ('outside-repo', 'engine-root symlink repoint in the cloud install location, outside every repo'),
    'coordinator_core/hooks/runtime_tripwire_stop_watcher.py': ('git-internal', 'stop-watcher pid lock under .coordinator-local/coordinator-sessions/<session>'),
    'coordinator_core/hooks/session_start_announce_job_mode.py': ('outside-repo', 'job-mode log under <settings-home>/state'),
    'coordinator_core/hooks/session_start_repair_prepare_commit_msg_hook.py': ('git-internal', 'rewrites the installed .git/hooks/prepare-commit-msg shim'),
    'coordinator_core/hooks/session_start_write_plugin_root_breadcrumb.py': ('outside-repo', 'breadcrumb file under the user home directory'),
    'coordinator_core/hooks/support/bin_impl_drift.py': ('outside-repo', 'refreshes <settings-home>/bin files and the daily stamp file'),
    'coordinator_core/hooks/support/next_move_ledger.py': ('ignored-target', 'ledger under .coordinator-local/subagent-share (gitignored) plus drain/claim files beside it: append and drain use O_APPEND/O_EXCL on box-local bookkeeping'),
    'coordinator_core/ops/grind_ops.py': ('outside-repo', 'NamedTemporaryFile scratch json passed to a verifier subprocess'),
    'coordinator_core/archive_stamp.py': ('outside-repo', 'NamedTemporaryFile holding the batch commit message handed to commit_scoped, unlinked after: cs_resolve_memos'),
    'coordinator_core/ops/session/emit_effective_delivery.py': ('in-repo-non-state', 'generated effective-delivery manifest under the plugin content root, temp file then replace; no state/ target'),
    'coordinator_core/warm/http_hook_forwarder.py': ('outside-repo', 'dial-count file and degrade log under CLAUDE_HOME, machine-global'),
    'coordinator_core/ask_spool/drain.py': ('ignored-target', 'spool entry rewrite (tmp + os.replace), session notice O_APPEND lines and the submission-record spool_id stamp, all under the gitignored .coordinator-local/ask-spool and scratch/warp, never state/: _rewrite, _notify, _stamp'),
    'coordinator_core/ask_spool/notice.py': ('ignored-target', 'minted-notice O_APPEND lines under the gitignored .coordinator-local/ask-spool/notices, never state/: append_notice'),
    'coordinator_core/ask_spool/record.py': ('ignored-target', 'exclusive O_CREAT spool entry under the gitignored .coordinator-local/ask-spool, never state/: spool_ask'),
    'coordinator_core/remote_engine/background.py': ('outside-repo', 'pending/outcome handle json (tmp + os.replace) under <settings-home>/state/remote-engine/requests, never the repo: _write_atomic'),
    'coordinator_core/remote_engine/fallback.py': ('outside-repo', 'best-effort telemetry.jsonl append under <settings-home>/state/remote-engine, never the repo: _record'),
    'coordinator_core/warm/paused_runs.py': ('ignored-target', 'paused-run records (tmp + os.replace) under the gitignored .coordinator-local/paused-runs, never state/: _atomic_write'),
    'coordinator_core/warm/hang_restart.py': ('outside-repo', 'hang-restart log appended under the warm runtime dir: maybe_restart'),
    'coordinator_core/warm/idle.py': ('outside-repo', 'ladder-gone tombstone inside the ladder engine copy root: make_ladder_gone'),
    'coordinator_core/async_hook_status.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_failure'),
    'coordinator_core/authz/classification.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: <module-level>'),
    'coordinator_core/authz/token.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tokens'),
    'coordinator_core/backlog_grind_assemble/grind_rows.py': ('claims-explicitly', 'claim token in _declare_under_repo_root: rows, run records and the ledger under state/queue-grind stay raw (temp file + os.replace, one appended line) because the claim must anchor on the verb\'s --repo-root via a nested recording scope; the seam claims on the ambient cwd, which a dispatched agent rarely shares with its target repo'),
    'coordinator_core/bash_guards/_alternative_liveness.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: <module-level>, _trigger_destructive_git_revert, _trigger_destructive_git_revert_advisory, _trigger_host_subagent_policy_guard'),
    'coordinator_core/bash_guards/agent_process_ownership.py': ('outside-repo', 'append-only per-session agent command ledger under settings_home() state: record_agent_command (os.open O_APPEND, 0o600); never raises'),
    'coordinator_core/bash_guards/_dialect.py': ('to-fix', 'raw-write site(s): _log_dialect_parser_unavailable; runtime observed=yes (n=24), sample=/tmp/pytest-of-root/pytest-716/home-quarantine21712/.coordinator-claude-settings/state/dialect-parser-unavailable.log'),
    'coordinator_core/bash_guards/_rewrite_support.py': ('outside-repo', 'best-effort interpreter-resolution cache (atomic tmp+os.replace) under state/cache with an OS-tempdir fallback: _bt_python3_invocation; fail-open, no cross-session claim semantics'),
    'coordinator_core/bash_guards/_heavy_lease_store.py': ('outside-repo', 'lease records under settings_home()/heavy-admission/leases, outside every repo: write_lease'),
    'coordinator_core/bash_guards/guard_heavy_command_admission.py': ('outside-repo', 'would-deny and override rows under settings_home()/heavy-admission, outside every repo: _record, _log_override'),
    'coordinator_core/ops/dispatch_emit/cross_plan_write_overlap.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _log_override appends the session overrides.log'),
    'coordinator_core/bash_guards/_taskstop_launch_store.py': ('outside-repo', 'launch records and reaper.jsonl under settings_home()/taskstop-reaper, outside every repo: write_record, append_row'),
    'coordinator_core/bash_guards/_write_bump_session_start.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_session_start_record'),
    'coordinator_core/bash_guards/_write_bump_sink_shapes.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _bound_literal_paths, _strip_comments_and_docstrings'),
    'coordinator_core/bash_guards/_write_bump_stand_down.py': ('to-fix', 'raw-write site(s): _mirror_to_durable_sink, log_environment_stand_down; runtime observed=yes (n=15), sample=/tmp/pytest-of-root/pytest-705/test_deny_grant_allow_consumed0/anchor/state/stand-downs/foreign-repo-write.log'),
    'coordinator_core/_engine_landing.py': ('git-internal', 'swap landing marker (tmp + os.replace) under <engine-root>/.git: begin_swap'),
    'coordinator_core/bash_guards/_heavy_lease_store.py': ('outside-repo', 'lease JSON (tmp + os.replace) under settings_home()/heavy-admission/leases, outside every repo: write_lease'),
    'coordinator_core/bash_guards/guard_heavy_command_admission.py': ('outside-repo', 'would-deny.jsonl under settings_home()/heavy-admission; bypass lines to <repo>/.coordinator-local/coordinator-sessions/<sid>/overrides.log: _log_bypass'),
    'coordinator_core/ops/dispatch_emit/cross_plan_write_overlap.py': ('git-internal', 'admitted-overlap line appended to <repo>/.coordinator-local/coordinator-sessions/<sid>/overrides.log'),
    'coordinator_core/bash_guards/bump_outside_repo_write.py': ('in-repo-non-state', 'no write: the raw-write vocabulary appears only in a docstring describing the interpreter payload sinks the guard detects'),
    'coordinator_core/bash_guards/block_approval_sentinel_creation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/bash_guards/guard_post_executor_verification.py': ('ignored-target', 'run counter under <repo>/.coordinator-local/subagent-share/<sid>, the gitignored machinery root: _bump_counter; fail-open'),
    'coordinator_core/bash_guards/block_subagent_commit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _open_call_is_read_only'),
    'coordinator_core/bash_guards/block_subagent_destructive_action.py': ('outside-repo', 'C6-migrated: _log_fail_open now routes through session/claimed_write.py::append_claimed_line (entry wrapped at bash_guards/dispatch.py::main); residual raw-write site(s) near tempdir/home/settings-home construct: _rotate_fail_open_log_if_oversized'),
    'coordinator_core/bash_guards/block_subagent_plan_body_bash_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log'),
    'coordinator_core/bash_guards/chain_arrival_ledger.py': ('to-fix', 'raw-write site(s): <module-level>, _rotate_if_oversize, record_chain_arrival; runtime observed=yes (n=1462), sample=/tmp/pytest-of-root/pytest-705/test_deny_grant_allow_consumed0/isolated-settings-home/state/chain-arrival-ledger/em-grant-e2e-f3ffc92aee9c/chain-arrival-ledger.jsonl'),
    'coordinator_core/bash_guards/commit_tripwires.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _log_pathspec_divergence_override'),
    'coordinator_core/bash_guards/_dc_git_probe.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _persist_probe_fail_open'),
    'coordinator_core/bash_guards/_dc_commit.py': ('claims-explicitly', 'claim token in check_validate_commit'),
    'coordinator_core/bash_guards/_dc_blanket_add.py': ('git-internal', 'override line appended under <git-root>/.coordinator-local/coordinator-sessions/<sid>/overrides.log: check_blanket_git_add'),
    'coordinator_core/bash_guards/_dc_rewrites.py': ('in-repo-non-state', 'no write: the raw-write vocabulary appears only in strings and comments describing the sinks the heredoc advisory detects'),
    'coordinator_core/bash_guards/guard_doctrine_surface_bash_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _fold_literal_joins, _interpreter_write_sinks_are_ungoverned'),
    'coordinator_core/block_discharge.py': ('outside-repo', 'block-discharge spool under settings_home()/machine-local, outside every repo'),
    'coordinator_core/ceremony_common/_phantom_sweep_providers.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: sweep_review_assemble writes its fixture .md files under a pytest tmp_path, never a tracked path in this repo'),
    'coordinator_core/claims_emit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_atomic_pair'),
    'coordinator_core/comment_strip/engine.py': ('in-repo-non-state', 'rewrites the source files being comment-stripped (and reverts them) plus an operator-named report path; no state/ target'),
    'coordinator_core/contract/grind_vocab.py': ('in-repo-non-state', 'emits grind-vocab.json and the handback schema into the schema directory; no state/ target'),
    'coordinator_core/commit_ledger/store.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: append_entry._append, record_predecessor_pointer -- target is .coordinator-local/coordinator-sessions/.commit-ledger/<handoff_id>.jsonl, not state/'),
    'coordinator_core/contract/cockpit_schema/emit_conformance_fixture.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: generate'),
    'coordinator_core/contract/cockpit_schema/emit_schema.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: emit_schemas'),
    'coordinator_core/contract/cockpit_schema/entities/competitor_summary.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/contract/cockpit_schema/provenance.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/contract/emit_memo_schema.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: emit_schemas -- generated schema file, matching contract/cockpit_schema/emit_schema.py and emit_conformance_fixture.py'),
    'coordinator_core/daily_branch.py': ('git-internal', 'appends coordinator.dayBranch to the git common-dir config file'),
    'coordinator_core/diagnostics/contained_run.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_spawn_script'),
    'coordinator_core/distill/_common.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: active_reference_guard_many'),
    'coordinator_core/distill/log_append.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: append_row, append_rows'),
    'coordinator_core/distill/log_normalize.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: normalize_arrow_dialects_log, normalize_log'),
    'coordinator_core/distill/scan_digest.py': ('in-repo-non-state', 'raw-write site(s) confined to the distill run dir digest/ (mkstemp + os.replace), no state/-component signal: write_digests'),
    'coordinator_core/engine_root_census.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, record_fallback_read'),
    'coordinator_core/execute_plan_assemble/close_out_and_stamp.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _dry_run_scratch_plan, _stamp_plan_landed, apply_tracker_reconciliation, close_out_and_stamp'),
    'coordinator_core/frontmatter/author_dependence.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main -- writes only to the committed golden fixture coordinator_core/frontmatter/tests/_goldens/author_dependence_labels.json, gated behind --write'),
    'coordinator_core/git/commit_trailers.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: apply_missing_trailers'),
    'coordinator_core/git/eol_declared.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: repair_declared_eol_drift'),
    'coordinator_core/git/git_dir.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/git/git_objects.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _replace_with_retry, append_reflog, cas_ref, write_object'),
    'coordinator_core/git/index_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: splice_index'),
    'coordinator_core/goals/reassess_krs.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_goal_file_atomic'),
    'coordinator_core/group_em/atomic_record.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: holder_lock, write_json_atomic'),
    'coordinator_core/group_em/baseline.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_atomic'),
    'coordinator_core/group_em/box_transport.py': ('outside-repo', 'exclusive O_CREAT per-box lock record under <settings-home>/group-em/box-locks (box_registry.lock_dir), outside every repo: _acquire'),
    'coordinator_core/group_em/nomination.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_json_atomic'),
    'coordinator_core/group_em/obligations.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record'),
    'coordinator_core/group_em/suite_grant.py': ('outside-repo', 'grant records and the append-only receive audit under <settings-home>/state/group-em/suite-grants, outside every repo: issue, receive_relayed_grant'),
    'coordinator_core/group_em/watch_heartbeat.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_atomic'),
    'coordinator_core/group_em/watch_spool.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: prune'),
    'coordinator_core/guard_advisory_counter.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_advisory_fire, record_deny_fire'),
    'coordinator_core/handoff_creation_guard.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: assert_no_archived_twin'),
    'coordinator_core/hooks/agent_completion_log.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_audit_entry'),
    'coordinator_core/hooks/dispatch_escape.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _log_escape (bounded append into the subagent-share dir)'),
    'coordinator_core/hooks/auto_push.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: log_failure'),
    'coordinator_core/hooks/cater_subagent_start.py': ('claims-explicitly', 'claim token in _write_miss_sentinel'),
    'coordinator_core/hooks/context_pressure_precompact.py': ('to-fix', "raw-write site(s): _write_sentinel, _write_state_snapshot; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/hooks/em_report_altitude.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _mark_fired'),
    'coordinator_core/hooks/nudge_harness_directive_dispatch.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _claim_fire'),
    'coordinator_core/hooks/nudge_unrouted_sizing.py': ('to-fix', "raw-write site(s): _claim_fire; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/hooks/plan_persistence_check.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_readme_row, _handler'),
    'coordinator_core/hooks/platform_localize.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: atomic_write'),
    'coordinator_core/hooks/postuse_advisory_dispatch.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _check_group_em_watch_arm_sync, _check_group_em_watch_arm_sync._touch_checked_sentinel, _check_runtime_tripwire_sync, _remove_workflow_run_record, _save_advisory_state, _write_workflow_run_record'),
    'coordinator_core/hooks/subagent_zero_tool_use.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_record_sync, _persist_final_report_sync'),
    'coordinator_core/hooks/taskstop_reaper.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_stopped_marker'),
    'coordinator_core/hooks/track_dispatched_agents.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _process_dispatched_sync, _write_backpointer_sync'),
    'coordinator_core/hooks/ue_knowledge_distrust.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _run_bootstrap'),
    'coordinator_core/hooks/watchdog_undischarged_next_move.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_records'),
    'coordinator_core/housekeeping/archive_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_index'),
    'coordinator_core/learn_lessons_pipeline/run_stamp.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: stamp_run_complete -- target is <claude_home>/tasks/learn-lessons-<date>/COMPLETE via ops/learn_lessons_cutoff.resolve_runs_dir, outside every repo'),
    'coordinator_core/install/_shared.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: settings_hook_identity_inverse_strip'),
    'coordinator_core/install/door_install.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _replace_possibly_running_image, install_named_forwarder'),
    'coordinator_core/install/door_uninstall.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_uninstall_fallback_cmd_forwarder'),
    'coordinator_core/install/host_sampler_scheduler.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: register_host_sampler_task'),
    'coordinator_core/install/plugin_cache_sessions.py': ('outside-repo', 'per-session binding files under the claude config dir, never a repo path: write_binding, prune_dead_bindings'),
    'coordinator_core/install/sandbox_check.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _tier1_filesystem_shape, _tier1b_mirror_and_cold_tier, _tier1b_pointer_and_shim, _tier1c_publish_repo_parity, _write_claude_author_argv_stub'),
    'coordinator_core/locked_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: held_lock, locked_rmw, replace_with_retry'),
    'coordinator_core/machine_profile.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: per-session advisory-event claim files under share_dir'),
    'coordinator_core/op_census/module_summary.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_index'),
    'coordinator_core/op_census/spawn_bearing_ops.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: save_spawn_index'),
    'coordinator_core/ops/app_session.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_handle'),
    'coordinator_core/ops/_app_session_build_guard.py': ('git-internal', 'per-worktree validate-<digest>.lock and its stale/tmp siblings under the app_session handle dir (<git-common-dir>): acquire_tree_lock, transfer_tree_lock'),
    'coordinator_core/ops/_app_session_validate_contract.py': ('ignored-target', 'spec.json written by write_run_spec into the run dir, default gitignored scratch/app-session-validate/<key>/...; a caller-supplied out_dir is the caller\'s own target'),
    'coordinator_core/ops/_app_session_validate_run.py': ('ignored-target', 'build/server logs, result.json, report.json and screenshots under the gitignored <root>/scratch/app-session-validate/<key> run dir; lock and handle under the git common dir'),
    'coordinator_core/ops/archive_auto_memory_rows.py': ('to-fix', 'raw-write site(s): main; runtime observed=yes (n=5), sample=/tmp/pytest-of-root/pytest-716/test_own_body_and_index_row_la0/repo/state/auto-memory-archive/2026-09-19-self-session.md'),
    'coordinator_core/ops/assert_no_dangling_plan_backlinks.py': ('claims-explicitly', "claim token in _fix_file [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/backfill_deliverable_spine.py': ('claims-explicitly', 'claim token in _stamp_file, _stamp_yaml_document'),
    'coordinator_core/ops/backfill_initiative_fk.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _acquire_lock, _attach_batch'),
    'coordinator_core/ops/bootstrap_orchestrate.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _coordinator_currency_write'),
    'coordinator_core/ops/cartography_chunk_table.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: write_chunk_table [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/cartography_symbols.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_symbols_artifact'),
    'coordinator_core/ops/cartography_tree.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/ceremony/detached_spawn.py': ('to-fix', 'raw-write site(s): _append_to_failures_archive, _log_spawn_failure, advance_failures_cursor, clear_failures_log, record_child_failure; runtime observed=yes (n=158), sample=/tmp/pytest-of-root/pytest-705/test_exhausts_retries_and_logs0/state/housekeeping-failures.log'),
    'coordinator_core/ops/ceremony/git_native.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _apply_trailers, _write_pathspec_file'),
    'coordinator_core/ops/ceremony/housekeeping_liveness.py': ('to-fix', 'raw-write site(s): stamp_liveness; runtime observed=yes (n=24), sample=/tmp/pytest-of-root/pytest-705/test_stamp_liveness_writes_par0/state/housekeeping-liveness.json'),
    'coordinator_core/ops/ceremony/post_commit_tail.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_and_push_origin_stub_close, _fold_sha_into_entry_on_disk, _run_completion_entry_fold [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/ceremony/receipt_emit.py': ('claims-explicitly', 'claim token in emit_receipt'),
    'coordinator_core/ops/ceremony/renderers.py': ('to-fix', 'raw-write site(s): _process_file, refresh_roadmap_callout; runtime observed=yes (n=12), sample=/tmp/pytest-of-root/pytest-705/test_matches_real_node_output0/state/roadmap/testroadmap/STUB-INDEX.md'),
    'coordinator_core/ops/ceremony/snapshot_diff_and_head.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _handler'),
    'coordinator_core/ops/changelog_ops.py': ('claims-explicitly', 'claim token in append_day, backfill_gaps'),
    'coordinator_core/ops/check_competitor_positioning_nudge.py': ('claims-explicitly', 'claim token in _record_decline'),
    'coordinator_core/ops/check_posix_exec_assumptions.py': ('to-fix', "raw-write site(s): <module-level>; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/completion_ops.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: append_plan_session [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/coordinator_complete_entry.py': ('claims-explicitly', 'claim token in _write_entry'),
    'coordinator_core/ops/coordinator_setup_state.py': ('claims-explicitly', 'claim token in _atomic_write'),
    'coordinator_core/ops/cruft_sweep.py': ('claims-explicitly', "claim token in _append_log_row [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/decision_record_mint.py': ('to-fix', 'raw-write site(s): <module-level>, mint_next_dr_id; runtime observed=yes (n=9), sample=/tmp/pytest-of-root/pytest-716/test_mint_first_number_in_empt0/state/decision-record-reservations/DR-1.reserved'),
    'coordinator_core/ops/deliverable_equivalence.py': ('to-fix', "raw-write site(s): _is_immutable_path_local; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/deliverable_ledger_write.py': ('to-fix', 'raw-write site(s): _restore_original_content, upsert_deliverable_ledger_rows; runtime observed=yes (n=93), sample=/tmp/pytest-of-root/pytest-716/test_header_bytes_preserved_ve0/state/deliverable-equivalence.yaml.ledger-write.tmp.24772'),
    'coordinator_core/ops/dev_sync.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _sync_plugin'),
    'coordinator_core/ops/dispatch_emit/queue_select.py': ('ignored-target', 'raw-write site(s): _materialise_source_rows writes source-op rows under the run dir (state/scratch/..., gitignored scratch/)'),
    'coordinator_core/ops/dispatch_emit/emission_receipt.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: the emission receipt, extracted from op.py'),
    'coordinator_core/ops/dispatch_emit/stranded_part_rebuild.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: the emission receipt and part spine of a stranded split part, caller-named'),
    'coordinator_core/ops/dispatch_emit/op.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _dispatch_emit, _write_emission_receipt, restamp'),
    'coordinator_core/ops/distill_apply_disposal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _delete_tracked_and_append_log, _write_denormalizations, write_apply_receipt'),
    'coordinator_core/ops/distill_disposal_manifest.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_disposal_manifest'),
    'coordinator_core/ops/distill_scope.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_scope_manifest; scope cache json via tmp + os.replace under the registry paths.distill_scope_cache dir, or the gitignored .coordinator-local/cache fallback, never state/: _store_cache'),
    'coordinator_core/ops/distill_stamp_disposal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: write_stamped_manifest'),
    'coordinator_core/ops/distill_write_curated_clusters.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _write_atomic'),
    'coordinator_core/ops/docindex_emit.py': ('to-fix', "raw-write site(s): _docindex_emit; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/doctor.py': ('claims-explicitly', 'claim token in _fix_bare_hook_commands'),
    'coordinator_core/ops/dod_floor_ratchet.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_floor'),
    'coordinator_core/ops/edit_live_hook.py': ('claims-explicitly', 'claim token in cmd_commit'),
    'coordinator_core/ops/emit/lma_cache.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _store'),
    'coordinator_core/ops/emit_artifact_shape_contract.py': ('claims-explicitly', 'claim token in _emit'),
    'coordinator_core/ops/emit_withheld_knobs.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/ensure_vscode_readonly.py': ('claims-explicitly', 'claim token in _merge_settings'),
    'coordinator_core/ops/extract_cited_sidecars.py': ('to-fix', 'raw-write site(s): main; runtime observed=yes (n=6), sample=/tmp/pytest-of-root/pytest-716/test_main_writes_both_audit_fi0/state/audits/2026-09-02-cited-subagent-share-sidecars.md'),
    'coordinator_core/ops/fleet/_common.py': ('claims-explicitly', 'claim token in declare_move_claims'),
    'coordinator_core/ops/fleet/_receiver_materialize.py': ('outside-repo', 'O_EXCL memo write into a receiver repo worktree (not this repo) then a hook-free commit via git_native.commit_authored_new_file, plus a commit-message tempfile under managed_tempdir: materialize_memo, write_msg_file'),
    'coordinator_core/ops/fleet/_sweep_receipt.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _truncate_if_oversized, record_sweep_outcome'),
    'coordinator_core/ops/fleet/archive_actioned_memos.py': ('to-fix', "INDIRECTION (D3 caught, static missed): os.open(...O_CREAT|O_EXCL...) lock-file create in _acquire lock helper (~L693/708); state write missed by static regex because the target is a Path variable, not a literal 'state/'/STATE_ROOT token nearby"),
    'coordinator_core/ops/fleet/archive_plans.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _acquire_sweep_lock, _apply_untracked_sidecar_moves'),
    'coordinator_core/ops/fleet/archive_terminal_handoffs.py': ('to-fix', "raw-write site(s): _acquire_sweep_lock, apply_sweep; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/backlog_grind_assemble/grind_size_chain.py': ('in-repo-non-state', 'one-shot .size.md fail-evidence file under state/queue-grind/<profile>/evidence/<stamp>/ in record_size_fail; the size/fail ledger line that follows claims through grind_rows.cmd_append, this evidence file itself is a run artifact left unclaimed'),
    'coordinator_core/ops/fleet/memo_migrate_group_em_inbox.py': ('outside-repo', 'moves memos out of the group-em home and delivers them into the receiver repo like memo.send: _remove_home_copy restore, _deliver'),
    'coordinator_core/ops/research_reground.py': ('ignored-target', 'brief, carry, basis, script and stale-report copy under the run scratch dir (gitignored scratch/): _reground_sync'),
    'coordinator_core/ops/sibling_repo_provision.py': ('outside-repo', 'provisions the sibling repo checkout named by the manifest: marker under its .git and wanted files in its tree, never this repo'),
    'coordinator_core/ops/workflow_fire/receipt.py': ('ignored-target', 'rewrites the <script>.emitted.json receipt beside the emitted workflow script under gitignored scratch/: stamp_fire_ownership'),
    'coordinator_core/ops/fleet/memo_compose.py': ('claims-explicitly', 'claim token in _memo_compose'),
    'coordinator_core/ops/fleet/memo_draft.py': ('claims-explicitly', 'claim token in _memo_draft'),
    'coordinator_core/ops/fleet/memo_heal.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _restore_one, _write_msg_file'),
    'coordinator_core/ops/fleet/memo_reconcile_outbox.py': ('claims-explicitly', 'claim token in _memo_reconcile_outbox, _reconcile'),
    'coordinator_core/ops/fleet/memo_sent_records.py': ('in-repo-non-state', 'write-once JSON send records under .coordinator-local/memo-outbox/sent-records/ (tmp + os.replace) and the legacy-ledger move into retired-ledgers/: write_record, migrate_legacy_ledgers'),
    'coordinator_core/ops/fleet/memo_send.py': ('claims-explicitly', "claim token in _memo_send [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/fleet/migrate_handoff_vocabulary.py': ('to-fix', 'raw-write site(s): apply_migration; runtime observed=yes (n=8), sample=/tmp/pytest-of-root/pytest-705/test_idempotent_second_run_is_1/state/handoffs/a.md'),
    'coordinator_core/ops/fleet_machinery_sweep.py': ('to-fix', "raw-write site(s): _write_ignore_block, append_audit, main; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/gen_claude_author_launcher.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/gen_claude_author_shim.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/generate_exec_summary.py': ('claims-explicitly', "claim token in main [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/guard_message_audit.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/ops/handoff_archive_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_retained_supersede_flip [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/handoff_repoint_origin.py': ('to-fix', "raw-write site(s): _handler; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/install_meta_repo_precommit_hook.py': ('claims-explicitly', 'claim token in _atomic_write'),
    'coordinator_core/ops/install_publish_repo_precommit_hook.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/list_reverse_drift_cmds.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _run'),
    'coordinator_core/ops/memo_fate_partition.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _atomic_write_json'),
    'coordinator_core/ops/memo_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_terminal_write [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/name_personas.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/ops/new_project_scaffold.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/normalize_claimed_frontmatter.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/normalize_env.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _ne_darwin_bash_profile_repair, _ne_write_backup_file'),
    'coordinator_core/ops/peer_notice_send.py': ('to-fix', 'raw-write site(s): _peer_notice_send; runtime observed=yes (n=48), sample=/tmp/pytest-of-root/pytest-716/test_send_writes_notice_file0/state/peer-notices/peer-abc/.438833fec0d44a83ab3ac1525b682cdf.json.tmp'),
    'coordinator_core/ops/percolate_build_token_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_cursor'),
    'coordinator_core/ops/percolate_preflight_scratch_publish.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: run_scratch_publish, self_test'),
    'coordinator_core/ops/plan_capture_persist.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: persist_captured_plan'),
    'coordinator_core/ops/plan_status_transition.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _commit_plan_flip, _stamp_implemented, _stamp_reopened, _stamp_review_verified, _stamp_rung, _stamp_superseded [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/propagate_body.py': ('to-fix', 'raw-write site(s): _rollback_delivery; runtime observed=yes (n=2), sample=/tmp/pytest-of-root/pytest-716/test_commit_delivery_failure_r0/repo/state/handoffs/2026-08-01-rollback.md'),
    'coordinator_core/ops/prune_resolved_queue_entries.py': ('claims-explicitly', 'claim token in _atomic_replace'),
    'coordinator_core/ops/queue_append.py': ('claims-explicitly', 'claim token in _queue_append_handler'),
    'coordinator_core/ops/queue_promote.py': ('claims-explicitly', "claim token in _queue_promote_handler [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/reap_orphaned_agent_dirs.py': ('to-fix', "raw-write site(s): _write_audit; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/reap_stale_locks.py': ('claims-explicitly', 'claim token in _append_log'),
    'coordinator_core/ops/register_coordinator_mirror.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: register'),
    'coordinator_core/ops/release_tagging.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _publish_release'),
    'coordinator_core/ops/render_posture_overlay.py': ('claims-explicitly', 'claim token in run'),
    'coordinator_core/ops/render_template.py': ('claims-explicitly', 'claim token in _render_and_write_in_place, main'),
    'coordinator_core/ops/review_mint/wave_bookkeeping.py': ('ignored-target', 'reviewer wave sidecars, the bookkeeping record and the plan_id stamp land under .coordinator-local/subagent-share (gitignored) box-local run sidecars: stamp_plan_id, bookkeep_wave'),
    'coordinator_core/ops/review_freeze_diff.py': ('claims-explicitly', 'claim token in freeze_diff'),
    'coordinator_core/ops/review_reverify.py': ('ignored-target', 'move-aside os.replace of a removed review edit into the gitignored session share dir <slice>.pre-review/removed/ (kept_at); restores via atomic_write_bytes are declared through declare_write: review_reverify'),
    'coordinator_core/ops/rewrite_spec_backlinks.py': ('claims-explicitly', 'claim token in rewrite_file'),
    'coordinator_core/ops/roadmap_link_stubs.py': ('to-fix', "raw-write site(s): _roadmap_id_lock; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/run_shellcheck_sweep.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _lint_files'),
    'coordinator_core/ops/scope_soak_enable.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: enable'),
    'coordinator_core/ops/scope_warning_resolve.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: resolve'),
    'coordinator_core/ops/self_persist_findings.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/session/fix_concrete_path_citations.py': ('claims-explicitly', "claim token in _write_preserving_newlines [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session/guard_hook_generation_self_probe.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_write_text, run_self_probe'),
    'coordinator_core/ops/session/guard_settings_integrity.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_copy, evaluate_settings_integrity'),
    'coordinator_core/ops/session/legacy_touch_corpus_migrate.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/session/record_pickup.py': ('outside-repo', "raw-write site(s) near tempdir/home/settings-home construct: _record_pickup_sync, _try_claim_lock_dir [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session_baton_promote.py': ('to-fix', 'INDIRECTION (D3 caught, static missed): handoff_path.write_text(...) writes the promoted baton handoff (~L215/239); target built from a Path variable, static regex missed the nearby state-signal token'),
    'coordinator_core/ops/session_hierarchy_derive.py': ('claims-explicitly', "claim token in _atomic_write_json [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/session_hierarchy_query.py': ('to-fix', "raw-write site(s): main; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/setup_rag_decision.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_tripwire'),
    'coordinator_core/ops/setup_seed_health_ledger.py': ('to-fix', 'raw-write site(s): seed_health_ledger; runtime observed=yes (n=4), sample=/tmp/pytest-of-root/pytest-716/test_fresh_repo_seeds_ledger0/state/health-ledger.md'),
    'coordinator_core/ops/shim_usage_census.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, record_invocation'),
    'coordinator_core/ops/strategic/draft_writer.py': ('to-fix', "raw-write site(s): write_draft; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/strategic/emit_writer.py': ('to-fix', "raw-write site(s): emit_strategic_feed; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/tracker/advance_status.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, advance_status'),
    'coordinator_core/ops/tracker/push_suggestion.py': ('to-fix', "INDIRECTION (D3 caught, static missed): _write_envelope_file: os.open(...O_CREAT|O_EXCL...)+os.fdopen (~L456-457) creates the tracker envelope file under state/; static regex missed the state-signal token near this indirection"),
    'coordinator_core/ops/verify_subagent_sandbox_preamble_sync.py': ('claims-explicitly', 'claim token in insert_block, rewrite_block'),
    'coordinator_core/ops/workday_complete_step2_5_dirty_tree.py': ('claims-explicitly', "claim token in _append_to_gitignore [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/workflow_fire/fire.py': ('to-fix', "raw-write site(s): _acquire_cap_lock, _write_record, fire_workflow; runtime observed=no (n=0), sample=n/a [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/ops/workweek_trail_scope.py': ('claims-explicitly', 'claim token in main'),
    'coordinator_core/ops/write_identity_file.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>'),
    'coordinator_core/ops/write_workday_start_marker.py': ('claims-explicitly', "claim token in write_marker [static-only: flagged by census row 2, not exercised in C2's one run — gap stays visible, per C3 body]"),
    'coordinator_core/orientation/expired_grant_signal.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_index_atomically -- target is .coordinator-local/cache/queue-grants-index.json, a best-effort orientation cache, not state/'),
    'coordinator_core/roadmap/probe_store.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: ProbeStore.save, _save_graph -- targets are .coordinator-local/cache/probe-store.json and probe-import-graph.json, derived caches, not state/'),
    'coordinator_core/orientation/regenerate_cache.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _atomic_replace'),
    'coordinator_core/p4/register.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _author_gitignore, _ensure_p4ignore, _pin_gitattributes, _upsert_local_md_keys'),
    'coordinator_core/percolate/engine.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _build_inject_scrub_callback._scrub_one_file, run_content_transform_sweep -- target_root is the percolate publish destination tree, never a tracked path in this repo'),
    'coordinator_core/percolate/inject.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: backup_destination_native -- target is the publish destination / consumer_state_home(), never a tracked path in this repo'),
    'coordinator_core/percolate/land_notify.py': ('outside-repo', 'subscriber registry json via tmp + os.replace under settings_home()/state/publish: _save; mailbox append targets the agent_pipe mailbox dir'),
    'coordinator_core/percolate/manifest.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_manifest'),
    'coordinator_core/percolate/prune_dead_registrations.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _prune_registry_map, prune_dead_registrations'),
    'coordinator_core/percolate/rewrite_basename.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_write_text, write_rename_ledger'),
    'coordinator_core/percolate/round.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: stamp_engine_row'),
    'coordinator_core/percolate/token_index.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: serialize_index'),
    'coordinator_core/plugin_health/sentinel.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_sentinel'),
    'coordinator_core/publish/time_transform.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: rename_codename_basenames, rewrite_plugin_paths, run_fix'),
    'coordinator_core/registry_fallback_counter.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: record_registry_fallback'),
    'coordinator_core/review_trail/reviewed_set.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_lines'),
    'coordinator_core/review_trail/sweep_tombstone.py': ('ignored-target', 'tombstone files under the gitignored .coordinator-local/sweep-tombstones/: record_swept (os.makedirs plus O_APPEND lines) and expire_tombstones (os.unlink)'),
    'coordinator_core/session/claims.py': ('git-internal', 'raw-write site(s) within the claim-directory bookkeeping (pid, session_id, claimed_at, stage files) under the coordinator-sessions hub -- session-internal, per this batch\'s row body: routing session/claims.py through the seam would recurse through the very claim machinery the seam feeds'),
    'coordinator_core/session/claude_md_grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_claude_md_write_grant'),
    'coordinator_core/session/context_usage_sidecar.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/session/core.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/session/day_branch_cut_lock.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_create'),
    'coordinator_core/session/em_guard_grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_em_guard_grant'),
    'coordinator_core/session/fleet_delegation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_fleet_delegation'),
    'coordinator_core/session/fleet_mode.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_fleet_mode'),
    'coordinator_core/session/grant.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tier_u_grant'),
    'coordinator_core/session/grant_scope.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_tier_u_grant_scope'),
    'coordinator_core/session/receiver_state.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_receiver_state'),
    'coordinator_core/session/scope.py': ('claims-explicitly', 'claim token in _restate_single_path_claim, _restate_tree_claims, touch, touch_written_path'),
    'coordinator_core/session/shape.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_claim_lock, session_shape_set'),
    'coordinator_core/session/touch_record.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _rotate_oversized, append_event, compact_record, note_claim_in_index, append_index_names -- the claim-index files sit beside the sessions dir under <git-common-dir>, never state/'),
    'coordinator_core/session_baton/store.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: merge_baton, write_baton'),
    'coordinator_core/shared_read.py': ('in-repo-non-state', 'read-only open of the caller-named path (read_bytes_shared); writes nothing'),
    'coordinator_core/snippet_sync/verify.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _insert_block, _rewrite_block, run'),
    'coordinator_core/subagent_sandbox/provision_report.py': ('claims-explicitly', 'claim token in _provision, _provision_plan_derivable_doc'),
    'coordinator_core/telemetry/host_sampler.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: sample_once'),
    'coordinator_core/telemetry/log_rotation.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: rotate_if_needed'),
    'coordinator_core/telemetry/op_latency.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _append_line, _write_entry'),
    'coordinator_core/text/refresh_queries.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: process_file'),
    'coordinator_core/warm/breadcrumb.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: try_claim_boot, write_breadcrumb'),
    'coordinator_core/warm/cookie.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_private_atomically'),
    'coordinator_core/warm/door/build.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: build, write_provenance, write_sidecar'),
    'coordinator_core/warm/door/build_posix.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_provenance'),
    'coordinator_core/warm/election.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _acquire_election_lock'),
    'coordinator_core/warm/front_door.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: flush, write_discovery'),
    'coordinator_core/warm/front_door_routing.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_routing_table'),
    'coordinator_core/warm/polite_io.py': ('in-repo-non-state', 'os.open(O_RDONLY) in open_bulk is a read-only bulk open; no file is written'),
    'coordinator_core/warm/push_cadence.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _try_create_sweep_lock'),
    'coordinator_core/warm/server.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: <module-level>, _bind_null_std_streams, _wrap_handle'),
    'coordinator_core/warm/skew.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_currency_cache, write_engine_stamp'),
    'coordinator_core/warm/supervisor.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: write_discovery'),
    'coordinator_core/warm/telemetry.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: flush, record_client_boot_wait, record_client_cold_fallback, record_degrade, record_election_lost, record_publish_warm_attempt, record_server_boot, record_worker_pool_depth'),
    'coordinator_core/architecture_survey/attestation.py': ('in-repo-non-state', 'rewrites the attestation key line in an atlas page under docs/architecture/'),
    'coordinator_core/architecture_survey/health_ledger.py': ('in-repo-non-state', 'restamps the full-audit date in the atlas health ledger under docs/architecture/'),
    'coordinator_core/architecture_survey/render.py': ('in-repo-non-state', 'atlas matrix and file-index pages under docs/architecture/'),
    'coordinator_core/architecture_survey/structure.py': ('ignored-target', 'structure.json into the caller-named survey run dir, default scratch/survey-runs/<run_id> (gitignored): fetch'),
    'coordinator_core/dag.py': ('git-internal', 'history-paths cache <git-dir>/claude-klabauter-history-paths.json'),
    'coordinator_core/frontmatter/lint_tree.py': ('git-internal', 'lint verdict store under <git-dir>'),
    'coordinator_core/housekeeping/cycle.py': ('in-repo-non-state', 'rollback only: restores the exact pre-release bytes of a record whose gate clear did not land in a commit, under the record lock'),
    'coordinator_core/ops/records_query.py': ('git-internal', 'per-record-type cache under <git-dir>'),
    'coordinator_core/sidecar_reap.py': ('outside-repo', 'reap pathspec file in managed_tempdir()'),
    'coordinator_core/updatedocs/readme_wikis.py': ('in-repo-non-state', 'rewrites the Wikis table block of the tracked docs/README.md in place (open "w"), never state/: sync_wikis'),
    'coordinator_core/machine_local_spawn.py': ('outside-repo', 'cross-process memo cache (tmp + os.replace) under the settings home, never the repo: _shared_write'),
    'coordinator_core/prior_art_recall/benchmark.py': ('outside-repo', 'report.json and run dirs under the caller-supplied out_dir, never state/: run_benchmark'),
    'coordinator_core/updatedocs/_stamp_cache.py': ('git-internal', 'update-docs stamp cache <git-dir>/claude-klabauter-updatedocs-<name>.json'),
    'coordinator_core/ceremony_common/run/decide.py': ('git-internal', 'decisions.jsonl in the run directory under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/ceremony_common/run/driver.py': ('git-internal', 'run files and briefing in the run directory under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/ceremony_common/run/emit.py': ('git-internal', 'bound workflow.mjs and its receipt in the run directory under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/ceremony_common/run/ledger.py': ('git-internal', 'run directory under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/ceremony_common/apply_checkpoint.py': ('git-internal', 'day-keyed apply checkpoint under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/ceremony_common/run/runner.py': ('outside-repo', 'per-step stdin/stdout/stderr files in a managed temp dir (temp_layout.make_managed_temp_dir)'),
    'coordinator_core/workday_start/rules.py': ('in-repo-non-state', 'stale-branch deferral line in tasks/.deferred-branches.md'),
    'coordinator_core/ceremony_common/run/single_flight.py': ('git-internal', 'single-flight lock.json under .coordinator-local/coordinator-sessions/'),
    'coordinator_core/workday_complete/autonomous_verb.py':('in-repo-non-state', 'raw-write site(s), no state/-component signal: main'),
    'coordinator_core/workflow_watch/run_report.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _atomic_write (run-cost.json beside the journal, cost ratchet)'),
    'coordinator_core/workflow_watch/stamp.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: stamp_terminal'),
    'coordinator_core/workstream_complete/directives_lessons_plan.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: _spool_body_to_file'),
    'coordinator_core/workstream_complete/directives_review.py': ('outside-repo', 'raw-write site(s) near tempdir/home/settings-home construct: record_gate_memo'),
    'coordinator_core/write_guards/block_subagent_archive_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log'),
    'coordinator_core/write_guards/block_subagent_plan_body_write.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_block_log'),
    'coordinator_core/write_guards/guard_concrete_path_citations.py': ('git-internal', 'raw-write site within 3 lines of a coordinator-sessions/meta.json reference'),
    'coordinator_core/write_guards/guard_doctrine_surface_edits.py': ('in-repo-non-state', 'raw-write site(s), no state/-component signal: _write_repo_identity_advisory_log'),
    'coordinator_core/advisory_census.py': ('outside-repo', 'sandbox git dir, content root and roster written under a caller-given scratch root, never the repo: _make_sandbox, _provision_content_root, _provision_roster'),
    'coordinator_core/ops/dispatch_emit/land_remainder_verify.py': ('ignored-target', 'land-remainder-verdict.json under the gitignored run scratch dir scratch/warp/<run_id>/: _write_verdict'),
    'coordinator_core/install/engine_interpreter.py': ('outside-repo', 'engine-site venv and its stamp file under <settings-home>/engine-site, never the repo: ensure'),
    'coordinator_core/install/klabauter_freshness_apply.py': ('outside-repo', 'freshness record json via tmp + os.replace under the settings home, never the repo: _write_record'),
    'coordinator_core/ops/dispatch_emit/deferred_emit_script.py': ('ignored-target', 'deferred-emit script and params/brief spills under the gitignored scratch/warp/: _spill_long_brief, write_deferred_script'),
    'coordinator_core/ops/dispatch_emit/owed_leg_queue.py': ('in-repo-non-state', 'owed-leg yaml records under the tracked state/owed-legs/ via open(..., "w"), no declare_write: write_owed_legs'),
    'coordinator_core/ops/review_reachability.py': ('in-repo-non-state', 'no file write: the write_/open( tokens are the pipeline-output scan patterns it matches in a reviewed diff: _pipeline_outputs'),
    'coordinator_core/ops/pm_directive_map.py': ('outside-repo', 'disposition and unmapped sidecars beside pm_turns.jsonl in the per-session baton directory, never the repo: append_disposition, the unmapped-file write'),
    'coordinator_core/bug_blitz/reap_green.py': ('claims-explicitly', 'closed backlog row files and the reap record state/scratch/bug-blitz/reap/latest.json, each declared through declare_write before the write: _close_row, reap'),
    'coordinator_core/ops/ceremony/update_docs_worklist.py': ('outside-repo', 'worklist cache json via tmp + os.replace under the registry paths.update_docs_worklist_cache dir, or the gitignored .coordinator-local/cache fallback, never state/: _store_cache'),
    'coordinator_core/learn_lessons_pipeline/mechanical_close.py': ('to-fix', 'lesson row rewrites (tmp + os.replace) and the archive move under state/lessons and archive/lessons, plus docs/wiki appends, with no claim declared: _atomic_write, apply_verdicts, _append_to_wiki'),
    'coordinator_core/ops/dispatch_emit/run_landing.py': ('in-repo-non-state', 'regenerated op-catalog files (contract/op-catalog.json, mcp-tools.json, docs/reference/op-catalog.md) written and restored on a non-commit, plus the git index.lock splice of the landing commit, never state/: _render_op_catalog, _restore_unlanded, _commit'),
    'coordinator_core/session/claim_index.py': ('git-internal', 'claim-index BUILDING token and stream directories beside the sessions hub under .coordinator-local; _begin_index renames the previous directory aside with os.replace and the daemon _sweep_retired deletes it'),
    'coordinator_core/session/op_dispatch_nag.py': ('git-internal', "the DIY-nag window and check stamps (op-dispatch-nag.stamp, op-dispatch-nag.checked) beside xs-lane.json under .coordinator-local/coordinator-sessions/<sid>/, never committed: _mark"),
    'coordinator_core/gem_inbox_apply.py': ('to-fix', 'manifest-declared writes (batons under state/handoffs, memos, the commit ledger under .coordinator-local) via write_text and os.replace moves, with no claim declared: apply_manifest'),
}

_TO_FIX_CEILING: FrozenSet[str] = frozenset({
    'coordinator_core/bash_guards/_dialect.py',
    'coordinator_core/bash_guards/_write_bump_stand_down.py',
    'coordinator_core/bash_guards/chain_arrival_ledger.py',
    'coordinator_core/gem_inbox_apply.py',
    'coordinator_core/hooks/context_pressure_precompact.py',
    'coordinator_core/hooks/nudge_unrouted_sizing.py',
    'coordinator_core/learn_lessons_pipeline/mechanical_close.py',
    'coordinator_core/ops/archive_auto_memory_rows.py',
    'coordinator_core/ops/ceremony/detached_spawn.py',
    'coordinator_core/ops/ceremony/housekeeping_liveness.py',
    'coordinator_core/ops/ceremony/renderers.py',
    'coordinator_core/ops/check_posix_exec_assumptions.py',
    'coordinator_core/ops/decision_record_mint.py',
    'coordinator_core/ops/deliverable_equivalence.py',
    'coordinator_core/ops/deliverable_ledger_write.py',
    'coordinator_core/ops/docindex_emit.py',
    'coordinator_core/ops/extract_cited_sidecars.py',
    'coordinator_core/ops/fleet/archive_actioned_memos.py',
    'coordinator_core/ops/fleet/archive_terminal_handoffs.py',
    'coordinator_core/ops/fleet/migrate_handoff_vocabulary.py',
    'coordinator_core/ops/fleet_machinery_sweep.py',
    'coordinator_core/ops/handoff_repoint_origin.py',
    'coordinator_core/ops/peer_notice_send.py',
    'coordinator_core/ops/propagate_body.py',
    'coordinator_core/ops/reap_orphaned_agent_dirs.py',
    'coordinator_core/ops/roadmap_link_stubs.py',
    'coordinator_core/ops/session_baton_promote.py',
    'coordinator_core/ops/session_hierarchy_query.py',
    'coordinator_core/ops/setup_seed_health_ledger.py',
    'coordinator_core/ops/strategic/draft_writer.py',
    'coordinator_core/ops/strategic/emit_writer.py',
    'coordinator_core/ops/tracker/push_suggestion.py',
    'coordinator_core/ops/workflow_fire/fire.py',
})


_SCAN_MS_BUDGET = 200.0


def _is_raw_writer(content: bytes) -> bool:
    if _PAREN_ANCHORED_RE.search(content) is not None:
        return True
    if any(lit in content for lit in _BARE_LITERALS):
        return True
    return _RAW_MODE_RE.search(content) is not None


def _is_raw_writer_by_definition(content: bytes) -> bool:
    """The vocabulary read literally, one pass per term — the reference
    ``_is_raw_writer``'s compiled form must agree with."""
    return (
        any(lit in content for lit in _RAW_LITERALS)
        or _RAW_OPEN_RE.search(content) is not None
        or _RAW_MODE_RE.search(content) is not None
    )


_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)

#: A file modified within this window of the memo's write is never memoised:
#: a same-tick rewrite could keep (size, mtime_ns) and hide behind a stale verdict.
_MEMO_RACY_NS = 2_000_000_000


def _memo_digest() -> str:
    """Identity of the classifier; any edit to this file (vocabulary or the
    ``_is_raw_writer*`` logic) discards every memoised verdict."""
    return hashlib.sha1(pathlib.Path(__file__).read_bytes()).hexdigest()


def _memo_path(root: pathlib.Path) -> Optional[pathlib.Path]:
    """The live-tree memo under the gitignored pytest cache; a synthetic root has none."""
    if pathlib.Path(root).resolve() != _REPO_ROOT:
        return None
    return _REPO_ROOT / ".pytest_cache" / "d" / "scan-memo" / "raw-writes.json"


def _load_memo(path: Optional[pathlib.Path], digest: str) -> Dict[str, list]:
    if path is None:
        return {}
    try:
        body = json.loads(path.read_bytes())
        if body["digest"] == digest and isinstance(body["files"], dict):
            return {
                k: v for k, v in body["files"].items()
                if isinstance(v, list) and len(v) == 3 and isinstance(v[2], bool)
            }
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def _save_memo(path: Optional[pathlib.Path], files: Dict[str, list], digest: str) -> None:
    if path is None:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"digest": digest, "files": files}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


#: ``rel<TAB>lf_size<TAB>crlf_size<TAB>verdict`` lines, zlib + base64: the verdict of every scanned module
#: at the recorded size. A fresh export keeps no mtime the memo could match, so
#: this is what spares it the full read; a size mismatch falls back to reading.
#: Regenerate with ``python <this file> --regen`` after a landing that moved
#: many module sizes (the cold-cost pin goes red as the seed ages).
_SEED_BLOB = b"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"

_SEED: Optional[Dict[str, Tuple[int, int, bool]]] = None


def _seed() -> Dict[str, Tuple[int, int, bool]]:
    global _SEED
    if _SEED is None:
        _SEED = {}
        if _SEED_BLOB:
            for line in zlib.decompress(base64.b64decode(_SEED_BLOB)).decode().split("\n"):
                rel, lf, crlf, verdict = line.split("\t")
                _SEED[rel] = (int(lf), int(crlf), verdict == "1")
    return _SEED


def _walk(root: pathlib.Path):
    """Yield ``(rel, full_path, stat)`` for every non-test ``.py`` module under
    ``root / "coordinator_core"``; size and mtime come from the directory
    entry, never a per-file stat."""
    scan_root_str = os.path.join(str(root), _SCAN_ROOT)
    root_str = str(root)
    prefix_len = len(root_str) + 1 if not root_str.endswith(os.sep) else len(root_str)
    pending = [scan_root_str]
    while pending:
        try:
            it = os.scandir(pending.pop())
        except OSError:
            continue
        with it:
            for entry in it:
                name = entry.name
                if name.endswith(".py") and not entry.is_dir(follow_symlinks=False):
                    if name.startswith("test_") or name == "conftest.py":
                        continue
                elif name not in _EXCLUDED_DIRS and entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                else:
                    continue
                full = entry.path
                try:
                    st = entry.stat()
                except OSError:
                    continue
                yield full[prefix_len:].replace(os.sep, "/"), full, st


def _read_verdict(full: str, size: int) -> Optional[bool]:
    try:
        fd = os.open(full, _READ_FLAGS)
    except OSError:
        return None
    try:
        return _is_raw_writer(os.read(fd, size + 1))
    except OSError:
        return None
    finally:
        os.close(fd)


def scan_raw_writers(root: pathlib.Path) -> List[str]:
    """Return sorted POSIX paths (rooted at ``coordinator_core/...``, matching
    the register's keying) of every non-test ``.py`` module under ``root /
    "coordinator_core"`` whose bytes match the raw-write vocabulary. ``root``
    is the repo root (real ``_REPO_ROOT``) or a synthetic ``tmp_path`` holding
    its own ``coordinator_core/`` subtree. Exposed (not private) so the AC6
    proof tests can drive it against a synthetic tree.

    The live tree keeps a ``(size, mtime_ns) -> verdict`` memo under
    ``.pytest_cache``: an unchanged file is stat-ed, never opened. A file the
    memo misses but whose size matches ``_SEED`` is served from the seed
    (``test_the_seed_agrees_with_a_full_read_of_the_live_tree`` pins that a
    same-size edit cannot hide behind it); any other file is read in full. A
    synthetic root uses neither memo nor seed."""
    if not os.path.isdir(os.path.join(str(root), _SCAN_ROOT)):
        return []
    memo_path = _memo_path(root)
    live = pathlib.Path(root).resolve() == _REPO_ROOT
    seed = _seed() if live else {}
    digest = _memo_digest() if memo_path is not None else ""
    old = _load_memo(memo_path, digest)
    fresh: Dict[str, list] = {}
    horizon_ns = time.time_ns() - _MEMO_RACY_NS
    found: List[str] = []
    for rel, full, st in _walk(root):
        hit = old.get(rel)
        seeded = seed.get(rel)
        if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            verdict = hit[2]
        elif seeded is not None and st.st_size in seeded[:2]:
            verdict = seeded[2]
        else:
            verdict = _read_verdict(full, st.st_size)
            if verdict is None:
                continue
        if memo_path is not None and st.st_mtime_ns < horizon_ns:
            fresh[rel] = [st.st_size, st.st_mtime_ns, verdict]
        if verdict:
            found.append(rel)
    if memo_path is not None and fresh != old:
        _save_memo(memo_path, fresh, digest)
    return sorted(found)


def _rule_covers(rel: str, content: bytes) -> Optional[str]:
    """Return the rule name covering ``rel``, or ``None``. The two
    structural rules from D2: the seam/primitive modules by exact path, and
    a ``WRITE_SURFACE``-declaring module scoped to ``coordinator_core/
    install/`` only — a module outside that prefix declaring the same
    variable is NOT covered (see module docstring)."""
    if rel in _RULE_SEAM_MODULES:
        return "seam/primitive"
    if rel.startswith(_INSTALL_PREFIX) and _WRITE_SURFACE_RE.search(content):
        return "WRITE_SURFACE/install"
    return None


def _evaluate(
    root: pathlib.Path,
    register: Dict[str, Tuple[str, str]],
    ceiling: FrozenSet[str],
    head: Optional[Tuple[FrozenSet[str], FrozenSet[str]]] = None,
) -> List[str]:
    """`head` is (paths tracked at HEAD, paths dirty vs HEAD). A register row whose
    path is absent at HEAD, or dirty vs HEAD and no longer matching, is not a violation:
    both the HEAD export and the shared tree stay green. None means strict."""
    violations: List[str] = []
    found = scan_raw_writers(root)
    found_set = set(found)

    for rel in found:
        content = (root / rel).read_bytes()
        if _rule_covers(rel, content) is not None:
            continue
        if rel not in register:
            violations.append(f"undispositioned: {rel}")

    for rel, (category, reason) in register.items():
        if category not in _CATEGORIES:
            violations.append(f"unknown category {category!r}: {rel}")
        if not reason.strip():
            violations.append(f"blank reason: {rel}")

    for rel in register:
        if head is not None and (rel not in head[0] or rel in head[1]):
            continue
        target = root / rel
        if not target.is_file():
            violations.append(f"stale entry (file gone): {rel}")
            continue
        if rel not in found_set:
            violations.append(f"stale entry (no raw token): {rel}")

    for rel, (category, _reason) in register.items():
        if category != "claims-explicitly":
            continue
        if head is not None and (rel not in head[0] or rel in head[1]):
            continue
        target = root / rel
        if not target.is_file():
            continue
        content = target.read_bytes()
        if not _CLAIM_TOKEN_RE.search(content):
            violations.append(f"claims-explicitly without claim token: {rel}")

    to_fix_entries = {rel for rel, (cat, _r) in register.items() if cat == "to-fix"}
    for rel in to_fix_entries:
        if rel not in ceiling:
            violations.append(f"to-fix entry outside ceiling: {rel}")

    for rel in ceiling:
        if rel not in to_fix_entries:
            violations.append(f"stale ceiling member (no to-fix entry): {rel}")

    return violations


def _head_state(root: pathlib.Path) -> Optional[Tuple[FrozenSet[str], FrozenSet[str]]]:
    import subprocess

    def _git(*args: str) -> Optional[List[str]]:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "--no-optional-locks", *args],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.split("\n") if r.returncode == 0 else None

    tracked = _git("ls-tree", "-r", "--name-only", "HEAD", "--", "coordinator_core")
    dirty = _git("diff", "--name-only", "HEAD", "--", "coordinator_core")
    if tracked is None or dirty is None:
        return None
    return frozenset(tracked), frozenset(dirty)


@pytest.mark.spawns_process
def test_the_live_gate_is_green():
    violations = _evaluate(_REPO_ROOT, _DISPOSITIONS, _TO_FIX_CEILING, _head_state(_REPO_ROOT))
    assert violations == [], (
        "The disposition gate is RED. Every raw-writing module needs a rule "
        "or a _DISPOSITIONS entry (category, reason). See this file's module "
        "docstring for the closed category set and how to add an entry.\n  "
        + "\n  ".join(violations)
    )


def test_to_fix_ceiling_is_an_exact_mirror_of_the_to_fix_set():
    to_fix_entries = {rel for rel, (cat, _r) in _DISPOSITIONS.items() if cat == "to-fix"}
    assert to_fix_entries == _TO_FIX_CEILING


def test_at_close_out_the_ceiling_is_empty():
    """AC4: 'At close-out `_TO_FIX_CEILING` and the register's `to-fix` set
    are both empty.' This chunk (C8) seeds the ceiling with C3's full
    migration list; C5-C7 shrink it. It is NOT empty yet — that assertion
    belongs to C9's close-out, not this gate. This test is deliberately the
    inverse: it documents, and pins, that the ceiling is non-empty here (its
    size shrinks as each migration batch lands -- C5 moved
    coordinator_core/ops/goal_append.py onto the seam, dropping C3's seeded
    56 to 55; C6 moved coordinator_core/bash_guards/block_subagent_
    destructive_action.py's append onto the seam and recategorized its
    residual-rotation entry to outside-repo (55 to 54), and moved
    coordinator_core/write_guards/validate_frontmatter_schema_deny.py's
    forensics write onto the seam with no residual raw-write bytes left, so
    that entry was deleted outright (54 to 53); C7 moved ten more modules'
    writes onto the seam (entries/ceiling membership deleted) and
    recategorized eight more to-fix members that held no actual `state/`
    write to the category their real write target names (53 to 35 -- see
    the register's C7 comment block for the full per-module breakdown); retiring the
    backlog-history shard deleted one to-fix module outright (35 to 34); deleting generator_scan_cache.py took it 34 to 33; the bug-sweep atomic-write pass left
    runtime_tripwire_em_check.py with no raw token (33 to 32); mechanical_close lost its claim, 31 to 32; gem_inbox_apply landed undispositioned, 32 to 33), so
    a reader of this file at this commit does not mistake a non-empty
    ceiling for a defect."""
    assert len(_TO_FIX_CEILING) == 33


@pytest.mark.parametrize(
    "content",
    [
        b"os.replace(a, b)", b"x.write_text(t)", b"p.write_bytes(b)", b"fd, p = mkstemp()",
        b"os.fdopen(fd)", b"json.dump(o, fh)", b"os.O_CREAT", b"append_line(p, l)",
        b"open(p, 'w')", b'open(p, "ab+")', b"open(p, 'x')", b"open(p, mode='w')",
        b"mode = 'a'", b"model_validator(mode=\"after\")",
        b"open(p)", b"open(p, 'r')", b"open(\n 'w')", b"json.dumps(o)", b"str.replace(a, b)",
        b"os.replace (a, b)", b"write_text(t)", b"mode", b"",
    ],
)
def test_the_compiled_scan_matches_the_vocabulary_as_written(content):
    assert _is_raw_writer(content) is _is_raw_writer_by_definition(content)


def test_the_compiled_scan_flags_exactly_the_live_population_the_vocabulary_does():
    scan_root = _REPO_ROOT / _SCAN_ROOT
    for path in scan_root.rglob("*.py"):
        content = path.read_bytes()
        assert _is_raw_writer(content) is _is_raw_writer_by_definition(content), path


def test_scan_cost_is_at_or_below_the_budget():
    scan_raw_writers(_REPO_ROOT)  # untimed warm-up fills the memo: the budget is the warm scan
    t0 = time.process_time()
    scan_raw_writers(_REPO_ROOT)
    elapsed_ms = (time.process_time() - t0) * 1000.0
    assert elapsed_ms <= _SCAN_MS_BUDGET, (
        f"Live scan took {elapsed_ms:.1f}ms, over the {_SCAN_MS_BUDGET}ms budget "
        "(measured figure + headroom, per this file's module docstring). Cut "
        "the walk's cost; do not loosen this assertion."
    )


def test_the_seed_agrees_with_a_full_read_of_the_live_tree():
    """A seed entry is trusted on size alone, so a stale verdict at an unchanged
    size must go red here, on the tree that edited it."""
    seed = _seed()
    assert seed, "regenerate the seed: python <this file> --regen"
    served = 0
    for rel, full, st in _walk(_REPO_ROOT):
        entry = seed.get(rel)
        if entry is not None and st.st_size in entry[:2]:
            served += 1
            assert _read_verdict(full, st.st_size) is entry[2], rel
    assert served, "no live module is served by the seed"


def test_cold_scan_cost_is_at_or_below_the_budget(monkeypatch):
    """The cold path (empty memo: a fresh export, a reboot) reads every file."""
    monkeypatch.setattr(f"{__name__}._load_memo", lambda path, digest: {})
    monkeypatch.setattr(f"{__name__}._save_memo", lambda path, files, digest: None)
    monkeypatch.setattr(f"{__name__}._SEED", None)
    t0 = time.process_time()
    found = scan_raw_writers(_REPO_ROOT)
    elapsed_ms = (time.process_time() - t0) * 1000.0
    assert found, "an empty memo must still classify the live tree"
    assert elapsed_ms <= _SCAN_MS_BUDGET, (
        f"Cold live scan took {elapsed_ms:.1f}ms, over the {_SCAN_MS_BUDGET}ms budget. "
        "Cut the walk's cost; do not loosen this assertion."
    )


def test_cold_scan_with_no_memo_and_no_seed_reads_every_file_under_the_brightline(monkeypatch):
    """The floor of the cold path: no memo and no seed, every module read in full."""
    monkeypatch.setattr(f"{__name__}._load_memo", lambda path, digest: {})
    monkeypatch.setattr(f"{__name__}._save_memo", lambda path, files, digest: None)
    monkeypatch.setattr(f"{__name__}._seed", lambda: {})
    t0 = time.process_time()
    found = scan_raw_writers(_REPO_ROOT)
    elapsed_ms = (time.process_time() - t0) * 1000.0
    assert found, "an empty memo must still classify the live tree"
    assert elapsed_ms <= 500.0, (
        f"Unseeded cold scan took {elapsed_ms:.1f}ms, over the 500ms brightline."
    )


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _scan_with_memo(monkeypatch, root: pathlib.Path, memo: pathlib.Path):
    """Drive scan_raw_writers on a synthetic root with a memo file; returns
    (found, classifier_call_count, digest_call_count)."""
    calls = {"classify": 0, "digest": 0}
    real_classify = _is_raw_writer
    real_digest = _memo_digest

    def counting_classify(content):
        calls["classify"] += 1
        return real_classify(content)

    def counting_digest():
        calls["digest"] += 1
        return real_digest()

    monkeypatch.setattr(f"{__name__}._memo_path", lambda r: memo)
    monkeypatch.setattr(f"{__name__}._is_raw_writer", counting_classify)
    monkeypatch.setattr(f"{__name__}._memo_digest", counting_digest)
    found = scan_raw_writers(root)
    return found, calls["classify"], calls["digest"]


def _age(path: pathlib.Path, seconds_ago: int) -> None:
    t = time.time_ns() - seconds_ago * 1_000_000_000
    os.utime(path, ns=(t, t))


def test_memo_is_invalidated_by_a_same_size_rewrite_with_a_new_mtime(tmp_path, monkeypatch):
    memo = tmp_path / "memo" / "raw-writes.json"
    writer = "def f(p):\n    open(p, 'w').write('x')\n"
    reader = "def f(p):\n    open(p, 'r').read( 'x')\n"
    assert len(writer) == len(reader)
    _write(tmp_path, "coordinator_core/m.py", writer)
    target = tmp_path / "coordinator_core" / "m.py"
    _age(target, 100)

    found, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert found == ["coordinator_core/m.py"] and classified == 1 and memo.exists()

    found, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert found == ["coordinator_core/m.py"] and classified == 0, "unchanged file must hit the memo"

    target.write_text(reader, encoding="utf-8")
    _age(target, 50)
    found, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert found == [] and classified == 1, "same size + new mtime must re-read and change the verdict"


def test_memo_is_discarded_when_the_classifier_digest_changes(tmp_path, monkeypatch):
    memo = tmp_path / "memo" / "raw-writes.json"
    _write(tmp_path, "coordinator_core/m.py", "def f(p):\n    open(p, 'w').write('x')\n")
    _age(tmp_path / "coordinator_core" / "m.py", 100)

    _scan_with_memo(monkeypatch, tmp_path, memo)
    _, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert classified == 0

    monkeypatch.setattr(f"{__name__}._memo_digest", lambda: "different-classifier")
    monkeypatch.setattr(f"{__name__}._memo_path", lambda r: memo)
    calls = []
    real = _is_raw_writer
    monkeypatch.setattr(
        f"{__name__}._is_raw_writer", lambda c: calls.append(1) or real(c)
    )
    assert scan_raw_writers(tmp_path) == ["coordinator_core/m.py"]
    assert len(calls) == 1, "a changed digest must force a rescan"


def test_memo_digest_is_computed_once_per_scan(tmp_path, monkeypatch):
    memo = tmp_path / "memo" / "raw-writes.json"
    for name in ("a", "b", "c"):
        _write(tmp_path, f"coordinator_core/{name}.py", "def f(p):\n    open(p, 'w').write('x')\n")
        _age(tmp_path / "coordinator_core" / f"{name}.py", 100)
    _, _, digests = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert digests == 1


def test_a_file_modified_inside_the_racy_window_is_never_memoised(tmp_path, monkeypatch):
    memo = tmp_path / "memo" / "raw-writes.json"
    _write(tmp_path, "coordinator_core/m.py", "def f(p):\n    open(p, 'w').write('x')\n")
    _, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert classified == 1
    _, classified, _ = _scan_with_memo(monkeypatch, tmp_path, memo)
    assert classified == 1, "a just-written file must be re-read, not served from the memo"


def test_an_undispositioned_raw_writer_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/newwriter.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert any("undispositioned: coordinator_core/newwriter.py" in v for v in violations)


def test_a_seam_only_writer_goes_green(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/claimer.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/claimer.py": ("claims-explicitly", "claim token in write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert violations == []


def test_a_stale_entry_goes_red_when_the_file_is_gone(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True)
    register = {"coordinator_core/ghost.py": ("in-repo-non-state", "no longer exists")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("stale entry (file gone): coordinator_core/ghost.py" in v for v in violations)


def test_a_stale_entry_goes_red_when_the_raw_token_is_gone(tmp_path):
    _write(tmp_path, "coordinator_core/migrated.py", "def x():\n    return 1\n")
    register = {"coordinator_core/migrated.py": ("in-repo-non-state", "no longer a raw writer")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("stale entry (no raw token): coordinator_core/migrated.py" in v for v in violations)


def test_a_claims_explicitly_entry_without_a_claim_token_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/unclaimed.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/unclaimed.py": ("claims-explicitly", "claim token in write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("claims-explicitly without claim token: coordinator_core/unclaimed.py" in v for v in violations)


def test_a_to_fix_entry_outside_the_ceiling_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/migrate_me.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/migrate_me.py": ("to-fix", "raw-write site(s): write_something")}
    violations = _evaluate(tmp_path, register, frozenset())
    assert any("to-fix entry outside ceiling: coordinator_core/migrate_me.py" in v for v in violations)


def test_a_ceiling_member_with_no_to_fix_entry_goes_red(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True)
    violations = _evaluate(tmp_path, {}, frozenset({"coordinator_core/nothing_here.py"}))
    assert any(
        "stale ceiling member (no to-fix entry): coordinator_core/nothing_here.py" in v
        for v in violations
    )


def test_a_to_fix_entry_inside_the_ceiling_is_fine(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/migrate_me.py",
        "def write_something(p):\n    open(p, 'w').write('x')\n",
    )
    register = {"coordinator_core/migrate_me.py": ("to-fix", "raw-write site(s): write_something")}
    ceiling = frozenset({"coordinator_core/migrate_me.py"})
    violations = _evaluate(tmp_path, register, ceiling)
    assert violations == []


def test_a_write_surface_module_under_install_passes_by_rule(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/install/some_installer.py",
        "WRITE_SURFACE = True\n\ndef install(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert violations == []


def test_the_same_write_surface_module_outside_install_goes_red(tmp_path):
    """The install-prefix scoping matters (Review F2 in the plan): a module
    outside ``coordinator_core/install/`` that declares ``WRITE_SURFACE`` is
    NOT rule-covered — it needs a register entry like any other module."""
    _write(
        tmp_path,
        "coordinator_core/ops/some_op.py",
        "WRITE_SURFACE = True\n\ndef run(p):\n    open(p, 'w').write('x')\n",
    )
    violations = _evaluate(tmp_path, {}, frozenset())
    assert any("undispositioned: coordinator_core/ops/some_op.py" in v for v in violations)


def test_a_migrated_module_that_regains_a_raw_write_goes_red(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/session/claimed_write.py",
        "def declare_write(p):\n    pass\n",
    )
    _write(
        tmp_path,
        "coordinator_core/migrated_then_regressed.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n",
    )
    violations_before = _evaluate(tmp_path, {}, frozenset())
    assert violations_before == []
    assert "coordinator_core/migrated_then_regressed.py" not in scan_raw_writers(tmp_path)

    _write(
        tmp_path,
        "coordinator_core/migrated_then_regressed.py",
        "from coordinator_core.session.claimed_write import declare_write\n"
        "def write_something(p):\n    declare_write(p)\n"
        "def new_raw_write(p):\n    open(p, 'w').write('y')\n",
    )
    violations_after = _evaluate(tmp_path, {}, frozenset())
    assert any(
        "undispositioned: coordinator_core/migrated_then_regressed.py" in v
        for v in violations_after
    )


def test_a_test_module_is_never_scanned(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/tests/test_something.py",
        "def test_x():\n    open('/tmp/x', 'w').write('y')\n",
    )
    assert scan_raw_writers(tmp_path) == []


def test_a_conftest_is_never_scanned(tmp_path):
    _write(tmp_path, "coordinator_core/conftest.py", "open('/tmp/x', 'w').write('y')\n")
    assert scan_raw_writers(tmp_path) == []


def test_a_register_row_absent_at_head_or_dirty_is_not_a_violation(tmp_path):
    _write(tmp_path, "coordinator_core/migrated.py", "def x():\n    return 1\n")
    register = {
        "coordinator_core/migrated.py": ("in-repo-non-state", "no longer a raw writer"),
        "coordinator_core/ghost.py": ("in-repo-non-state", "absent at HEAD"),
    }
    assert _evaluate(tmp_path, register, frozenset(), (frozenset({"coordinator_core/migrated.py"}), frozenset({"coordinator_core/migrated.py"}))) == []
    clean = _evaluate(tmp_path, register, frozenset(), (frozenset({"coordinator_core/migrated.py"}), frozenset()))
    assert any("stale entry (no raw token): coordinator_core/migrated.py" in v for v in clean)


def _build_seed_blob() -> bytes:
    """Both the LF and the CRLF size are recorded: an export may be checked out
    with either line ending."""
    lines = []
    for rel, full, _st in sorted(_walk(_REPO_ROOT)):
        try:
            content = pathlib.Path(full).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue
        crlf = len(content) + content.count(b"\n")
        lines.append(f"{rel}\t{len(content)}\t{crlf}\t{int(_is_raw_writer(content))}")
    return base64.b64encode(zlib.compress("\n".join(lines).encode(), 9))


def _regen_seed() -> None:
    src = pathlib.Path(__file__)
    text = src.read_bytes().decode("utf-8")
    blob = _build_seed_blob().decode()
    text = re.sub(r'(?m)^_SEED_BLOB = b"[^"]*"$', lambda _m: f'_SEED_BLOB = b"{blob}"', text)
    src.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen_seed()
    else:
        raise SystemExit(pytest.main([__file__, "-v"]))
