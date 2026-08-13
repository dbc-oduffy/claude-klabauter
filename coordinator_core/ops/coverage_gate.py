"""
coordinator_core.ops.coverage_gate — JSON-RPC "coverage.gate" operation.

Purpose: Thin RPC wrapper over coordinator_core.coverage.run_coverage_gate.
Accepts JSON-RPC params, delegates to the coverage algorithm, returns
the frozen verdict-line + diagnostic notes + exit_code for the veneer to render,
and WRITES the computed result to a versioned disk artifact so cockpit can
read disk-truth without a live engine call.

Self-registration: importing this module calls register_op("coverage.gate", ...)
as a side-effect — same pattern as ops/ping.py. Import this module (or add it to
coordinator_core.ops.__init__) to activate the op.

Wire params (all optional):
    range              (str)       — git rev-range; flat mode; defaults to merge-base..HEAD.
    from_handoff       (str)       — closing handoff absolute path; enables DAG mode.
    scope_paths        (list[str]) — path-scope for flat-mode chain_set only (never applied to
                                     reviewed_set — asymmetric scope, mirrors review-coverage-gate.sh
                                     (example-doctrine-repo d9ac8232, 2026-07-19)).
    closing_session_id (str)       — active Claude Code session ID of the closing handoff;
                                     enables D3 case 3 (unpublished closing handoff attribution).
                                     Maps to $CLAUDE_CODE_SESSION_ID in the bash original.
                                     When absent, the op falls through to git-log add-commit lookup.
    verbose             (bool)     — forwarded to coverage.run_coverage_gate's `verbose`
                                     kwarg; reachable from coordinator/bin/review-coverage-gate.py's
                                     `--verbose` flag. Default False.
    mint_chain_waivers  (bool)     — default False. Ceremony-close-only side effect (plan
                                     docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md
                                     § C2b — moved here from ``coverage.halt_on_uncovered``,
                                     which has NO production caller; the ceremony-close path
                                     actually reaches THIS op via
                                     ``coordinator/bin/review-coverage-gate.py``'s
                                     ``cc_invoke.route("coverage.gate", ...)`` call, invoked in
                                     turn by ``coordinator/bin/wsc-coverage-gate-runner.py``'s
                                     ``coverage-gate`` subcommand): when True AND the derivation
                                     is DAG-mode (``from_handoff`` set) AND the verdict is
                                     UNCOVERED, mint one ``coordinator_core.chain_ancestry_waivers``
                                     waiver per uncovered chain commit (``result.uncovered_shas`` —
                                     already bounded to this call's own ``chain_set``, never
                                     widened), keyed under the SAME ``closing_session_id`` value
                                     this call threaded to ``run_coverage_gate`` — that identity
                                     match is what lets the chain-ancestry-waiver READ side
                                     (``coverage.py``'s ``_chain_ancestry_waived_shas``) credit it
                                     back later. Only ``wsc-coverage-gate-runner.py``'s
                                     ceremony-close paths may pass True (via
                                     ``review-coverage-gate.py``'s ``--mint-chain-waivers`` flag);
                                     every diagnostic invocation — the ad-hoc EM CLI
                                     (``review-coverage-gate.py`` bare/``--from-handoff``/
                                     ``--verbose``) and ``ops/review_brightline_gate.py`` (which
                                     does not call this op at all) — MUST keep defaulting to
                                     False, so a human poking at the gate never mints evidence as
                                     a side effect of looking. As of 2026-08-07
                                     (state/audits/2026-08-07-review-gate-scoping-predecessor-and-
                                     planning-artifacts.md), TWO ceremony-close subcommands of
                                     ``wsc-coverage-gate-runner.py`` pass True: ``coverage-gate``
                                     (unconditionally) and ``brightline-gate`` (from its own
                                     PARTITION-MANDATORY discharge check, so it is self-sufficient
                                     without requiring ``coverage-gate`` to have run first). This is
                                     safe because minting is keyed per (sha, chain_id) and
                                     idempotent — see the next sentence — so either subcommand
                                     alone, or both in sequence in either order, mint the same set
                                     of waivers exactly once each. Minting is best-effort (never
                                     raises — see ``record_chain_ancestry_waiver``'s own
                                     contract) and gates on ``result.uncovered_shas`` being
                                     non-empty, NOT on ``result.verdict`` (2026-08-07 fix,
                                     state/audits/2026-08-07-review-gate-scoping-predecessor-and-
                                     planning-artifacts.md — the verdict answers a CODE-coverage
                                     question, the mint answers a foreign-session-narrowing
                                     question; a chain whose only uncovered commits are planning
                                     artifacts can net verdict COVERED while still needing the
                                     mint). It therefore CAN fire on a COVERED verdict, provided
                                     uncovered_shas is non-empty; it never fires when
                                     uncovered_shas is empty (the fabrication-hazard guard — see
                                     the mint call site's own comment) and never fires on
                                     INDETERMINATE (an indeterminate DAG derivation returns early
                                     with ``verdict="INDETERMINATE"`` before this op ever sees a
                                     verdict to key on — see ``coverage.py``'s
                                     ``run_coverage_gate``/``_derive_dag_chain_set`` — and that
                                     early return also means uncovered_shas is never populated
                                     for it).

Reply fields (result object in JSON-RPC response):
    verdict_line (str)      — frozen CLI contract line (AC11):
                              'range=<r> chain_commits=N covered=M uncovered=K VERDICT=...'
    notes        (list[str]) — diagnostic messages (INDETERMINATE reasons, uncovered SHAs).
    exit_code    (int)       — 0 = COVERED/UNCOVERED, 2 = INDETERMINATE, 1 = error.

Idempotency-hazard update (op-classification.tsv, "halt-on-coverage-gate-uncovered" row moved
to whichever row covers this op, per C2b): this op is NO LONGER unconditionally side-effect-free
with respect to the coverage-review-trail waiver artifact. With ``mint_chain_waivers``
absent/False (every existing and every diagnostic caller) it remains the pure disk-artifact-write
behaviour already described above. Only a caller passing ``mint_chain_waivers=True``
(ceremony-close) takes on the additional mint side effect, and that side effect is itself
idempotent per (sha, chain_id) — see ``record_chain_ancestry_waiver``.

Exit-code contract (mirrors review-coverage-gate.sh, example-doctrine-repo d9ac8232 2026-07-19, and AC11):
    0 — COVERED or WARN verdict (veneer prints verdict_line and exits 0; C10 —
        WARN is the pre-C10 UNCOVERED token's replacement and never halts).
    2 — INDETERMINATE (veneer exits 2; calling skill treats exit 2 as a halt).
    1 — Usage / setup error (unsafe range, unresolvable origin/main, git failure).

Disk artifact — coverage gate result (cockpit read contract):
    Path (repo-relative):  state/coverage/gate-result.json
    Written:               atomically (temp-file + os.replace) after every successful
                           gate evaluation.  Write errors are non-fatal — the op still
                           returns its JSON-RPC result.
    Schema version:        "1"

    JSON schema (schema_version "1"):
    {
        "schema_version": "1",          // str  — bump on breaking field changes
        "verdict":        str,          // "COVERED" | "WARN" | "INDETERMINATE" | "UNKNOWN"
                                        //   extracted from VERDICT=<token> in verdict_line;
                                        //   "UNKNOWN" is `_extract_verdict`'s own fallback
                                        //   token, not "ERROR" -- see its docstring. C10
                                        //   (docs/plans/2026-08-05-coverage-gate-planning-
                                        //   artifact-class.md, AC14): the pre-C10 binary
                                        //   "UNCOVERED" token is retired -- below the
                                        //   code-partition coverage ratio threshold the
                                        //   underlying gate (coordinator_core.coverage.
                                        //   run_coverage_gate) now reports "WARN", which
                                        //   never halts (see that module's own hard-block
                                        //   decision note).
        "verdict_line":   str,          // frozen CLI contract line (AC11):
                                        //   "range=<r> chain_commits=N covered=M uncovered=K VERDICT=..."
        "coverage_ratio": float,        // code-partition coverage ratio (C10) — same value
                                        //   rendered into verdict_line's coverage_ratio=R.RR
                                        //   token, exposed as a first-class field so cockpit
                                        //   doesn't have to regex-parse verdict_line for it.
                                        //   Additive field, schema_version NOT bumped.
        "exit_code":      int,          // 0 (COVERED/UNCOVERED) | 1 (error) | 2 (INDETERMINATE)
        "chain_commits":  int,          // total commits in the evaluated chain
        "covered":        int,          // commits with at least one covering trail record
                                        //   OR classified as ceremony bookkeeping (see
                                        //   bookkeeping_shas below) — both count as covered
        "uncovered":      int,          // commits with no covering trail record AND not
                                        //   ceremony bookkeeping
        "uncovered_shas": [str, ...],   // list of uncovered commit SHAs (UNCOVERED verdict)
        "bookkeeping_shas": [str, ...], // subset of the raw uncovered set classified as
                                        //   ceremony bookkeeping (state/, archive/, tasks/
                                        //   touched paths only — see coverage.py's
                                        //   _classify_bookkeeping_shas) and excluded from
                                        //   uncovered_shas/the verdict. Never silently
                                        //   dropped — additive field, schema_version NOT
                                        //   bumped (see module docstring's own "bump on
                                        //   breaking field changes" policy: this is a
                                        //   backward-compatible addition, existing readers
                                        //   that ignore unknown keys are unaffected).
        "planning_shas":  [str, ...],   // SUBSET of uncovered_shas (never excluded from it —
                                        //   AC9, docs/plans/2026-08-05-coverage-gate-
                                        //   planning-artifact-class.md) classified PLANNING
                                        //   (docs/plans/, docs/research/, docs/problems/,
                                        //   state/plan-sidecars/ — see coverage.py's
                                        //   _classify_bookkeeping_shas). Unlike
                                        //   bookkeeping_shas, membership here does NOT mean
                                        //   "excluded from the verdict" — a planning commit
                                        //   still owes a plan review and stays inside
                                        //   uncovered_shas/the VERDICT count until a plan
                                        //   review credits it. Exposed so a consumer (e.g.
                                        //   the C10 code-partition coverage ratio) can derive
                                        //   the CODE-only uncovered count
                                        //   (len(uncovered_shas) - len(planning_shas)) and the
                                        //   code-partition denominator
                                        //   (chain_commits - len(bookkeeping_shas) -
                                        //   len(planning_shas)) without re-running the
                                        //   classifier. Additive field, schema_version NOT
                                        //   bumped (same backward-compatible-addition
                                        //   rationale as bookkeeping_shas above).
        "unrecordable_shas": [str, ...],// SUBSET of uncovered_shas (never excluded from
                                        //   it — same subset-not-excluded contract as
                                        //   planning_shas above) classified structurally
                                        //   UNRECORDABLE for this chain: a
                                        //   foreign-attributed commit (own Session-Id
                                        //   trailer names a different session) seen only
                                        //   when the DAG walk collapsed to a genuine
                                        //   single-node predecessor: none (schema rule
                                        //   C2-4, every spinoff baton, by construction —
                                        //   len(coverage.py's _DagChainResult.
                                        //   ordered_ancestry) == 1 AND
                                        //   _DagChainResult.terminated_early == ""). Membership means
                                        //   "cannot be recorded for this chain, ever" — no
                                        //   chain-ancestry waiver can be minted over a
                                        //   single-node walk (nothing to mint ancestry
                                        //   over) and _guard_foreign_session_range refuses
                                        //   the record permanently — ruled:
                                        //   docs/decisions/DR-294-pickup-claim-as-guard-
                                        //   evidence-is-declin.md. Deliberately NOT
                                        //   populated on a 'missing-link' collapse — a
                                        //   broken predecessor pointer is a bug, not a
                                        //   ruled limit. Exposed so a consumer can derive
                                        //   the ACTIONABLE uncovered count
                                        //   (len(uncovered_shas) -
                                        //   len(unrecordable_shas)) without re-running the
                                        //   classifier. Additive field, schema_version NOT
                                        //   bumped (same backward-compatible-addition
                                        //   rationale as bookkeeping_shas above).
        "notes":          [str, ...],   // diagnostic messages + "uncovered: <sha>" lines
        "generated_at":   str           // ISO 8601 commit timestamp of HEAD at write time
                                        //   (git log -1 --format=%cI HEAD); OMITTED if
                                        //   repo_root has no commits or git is unavailable.
    }

Negative-spec (C7, docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md § C7,
AC12 — genuinely incomplete, not silently claimed done):
  - A ``mint_chain_waivers=True`` mint (see that param's doc above) `git add`-stages the
    minted waiver directory (best-effort — see ``_stage_chain_ancestry_waivers``) so a
    LATER ceremony's dirty-tree gate (coordinator_core.ops.ceremony.commit_gates
    .dirty_tree_gate) never flags these files as an "unattributable" dirty path (a staged
    path is unconditionally excluded from that classification, regardless of pathspec
    scoping).
  - Staging is NOT committing: a `git commit -- <pathspec>` scoped commit
    (coordinator_core.ops.ceremony.commit_pipeline / git_native.commit_scoped, which this
    codebase deliberately never calls bare/`-A`/`.`) only commits paths named in ITS OWN
    pathspec, staged or not. The waiver directory reaching an actual commit on the
    close path requires the ceremony's own bookkeeping-commit pathspec
    (`coordinator_core.ops.ceremony.wsc_tail._run_precommit_tail`'s `extra_stage_paths`,
    mirroring its existing handoff-tracker/roadmap-stub-index entries) to include it —
    that file is OUTSIDE this plan's declared scope and is NOT touched here. Do not read
    this module's staging step as having discharged AC12 in full.
  - Also verified (C7): `coordinator/bin/wsc-coverage-gate-runner.py` has NO bookkeeping
    commit of its own at all — it is a thin subcommand CLI (claim-plan / coverage-gate /
    write-trail / brightline-gate) with no `git commit` call anywhere in it. The actual
    ceremony bookkeeping commit lives in `ceremony.wsc_tail`, invoked later in the same
    ceremony via a separate CLI (`wsc-tail.py`). A HALT at Step 2.9 (where minting fires)
    aborts the ceremony before Step 3 (`ceremony.wsc_tail`) ever runs in that same
    invocation — the mint therefore always precedes any possible commit on this
    invocation's own path, it does not follow one.

Negative-spec (hard-won):
  - An UNCOVERED verdict means only "the gate found no covering trail record for
    this commit" -- it is AGNOSTIC to WHY no trail record exists. It does NOT
    distinguish "genuinely unreviewed" from "the caller never supplied review
    metadata this pass" -- ``review_trail.write`` skips cleanly with
    ``no-review-metadata`` when metadata is absent/incomplete (see
    ``ceremony/tail_ops.write_review_trail``), and this gate has no visibility
    into that skip. Do NOT read UNCOVERED as "review did not happen" -- check
    the sibling ``review_trail.write`` result (or the receipt-layer
    ``review_metadata_supplied`` annotation ``ceremony/wsc_tail`` adds
    alongside it) for that signal before concluding review
    was skipped. Originating incident: 2026-07-22 sibling-repo dogfood run read
    a ceremony receipt's UNCOVERED verdict as "review did not happen" when the
    true fact was "no review metadata was supplied that pass."
  - Deliberately does NOT couple to ``review_trail.write``'s skip reason to
    produce this distinction -- this op is git/trail-only by design; the
    receipt-assembly layer (which already holds both ops' results) is the
    correct home for reconciling them, not this gate.

Spec backlink: docs/plans/2026-07-05-coordinator-core-execution-model-retirem.md § C6

---

coordinator_core.ops.coverage_gate — JSON-RPC "coverage.halt_on_uncovered" operation.

Purpose: the caller-side halt/override decision that used to live in
review-coverage-gate.sh's shell veneer — parse the gate's VERDICT token and
decide whether the calling ceremony should halt, replacing a
``grep VERDICT=UNCOVERED ... && exit 1`` shell fence with a structured verdict
dict.  Re-derives the verdict by calling ``coordinator_core.coverage.run_coverage_gate``
directly (the same algorithm ``coverage.gate`` wraps) rather than composing
over that op's handler, so this op stays a pure read with no disk-artifact
side effect of its own (op-classification.tsv idempotency-hazard: "none").

Self-registration: importing this module calls
``register_op("coverage.halt_on_uncovered", ...)`` as a side-effect — same
pattern as ``coverage.gate`` above and ``ops/ping.py``.

Wire params (all optional):
    range              (str)  — git rev-range; flat mode; defaults to merge-base..HEAD.
    from_handoff       (str)  — closing handoff absolute path; enables DAG mode.
    closing_session_id (str)  — active Claude Code session ID of the closing handoff;
                                enables D3 case 3 (unpublished closing handoff attribution)
                                identically to coverage.gate. When absent, DAG mode falls
                                through to the git-log add-commit lookup fallback.
    override           (bool) — explicit warn-and-continue override. When absent,
                                the op ALSO honors the ``COORDINATOR_OVERRIDE_COVERAGE_GATE``
                                environment variable (truthy iff its value is exactly "1"),
                                mirroring the fence's env-var escape hatch
                                (``distinct-ops-new.tsv`` row: "exit 1 unless
                                COORDINATOR_OVERRIDE_COVERAGE_GATE=1 is set (then warn and
                                continue)"). An explicit ``override`` param always wins over
                                the env var.

C10 (docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md, AC14): the pre-C10
binary ``UNCOVERED`` token this op's ``halted``/override logic below was written against
is retired — ``coordinator_core.coverage.run_coverage_gate`` now reports ``WARN`` below
the code-partition coverage ratio threshold, and WARN never halts (see that module's own
hard-block decision note; the ruling is deliberately scoped to ORDINARY coverage only, not
the partition-mandatory chain-verdict case). This op's own comparison
(``verdict != "UNCOVERED"``) already falls through to the non-halting generic branch for
any verdict other than the literal string "UNCOVERED" — since that token can no longer be
produced by ``run_coverage_gate``, the ``halted=True`` branch below (and ``_HALT_MESSAGE``)
is effectively unreachable in production today. Left in place, not deleted, because this op
has NO production caller (see the negative-spec below) and pruning unreachable-but-harmless
defensive code is out of this change's scope — do not read its continued presence as this
op still gating anything live.

Reply fields (result object in JSON-RPC response):
    halted          (bool) — True iff the caller should treat this as a halt (the retired
                             UNCOVERED verdict and no override in effect — see the C10 note
                             above; unreachable with today's vocabulary).
    verdict         (str)  — "COVERED" | "WARN" | "INDETERMINATE" | "UNKNOWN",
                             extracted the same way ``coverage.gate``'s disk artifact does.
    override_applied (bool) — True iff the (now-unreachable) UNCOVERED verdict was hit AND
                             an override (param or env var) was in effect, i.e. the override
                             actually changed the outcome. False for every other verdict,
                             even if an override flag was set — there was nothing to
                             override (WARN never halts either, per C10).
    message         (str)  — halt/remediation guidance (UNCOVERED, no override — unreachable
                             today), a warn-and-continue notice (UNCOVERED, override applied
                             — unreachable today), or a pass-through summary
                             (COVERED/WARN/INDETERMINATE/UNKNOWN).

Negative-spec: this op does NOT itself halt anything — it is a pure decision read.
The caller (ceremony skill / veneer) is responsible for acting on ``halted`` (e.g.
raising / exiting non-zero); this mirrors ``coverage.gate`` returning ``exit_code``
for the veneer to render rather than the op process exiting on the daemon's behalf.

Negative-spec (C2b): this op has NO production caller anywhere in the tree (verified by
grep — only op-registration plumbing and its own test file reference it) and therefore
never carries the ``mint_chain_waivers`` side effect that a first draft of C2 attached here.
Minting lives exclusively on ``coverage.gate`` above, the op the ceremony-close path
actually reaches. This op remains unconditionally side-effect-free
(op-classification.tsv idempotency-hazard: "none").

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § C1e
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.chain_ancestry_waivers import (
    chain_ancestry_waived_shas,
    chain_waiver_dir,
    record_chain_ancestry_waiver,
)
from coordinator_core.ipc import register_op
from coordinator_core.coverage import run_coverage_gate

# ---------------------------------------------------------------------------
# Artifact constants
# ---------------------------------------------------------------------------

#: Repo-relative path of the persisted gate-result artifact.
#: Cockpit reads this path as disk-truth per its frozen cockpit-emission read contract.
ARTIFACT_RELPATH: str = "state/coverage/gate-result.json"

_ARTIFACT_SCHEMA_VERSION: str = "1"
_VERDICT_RE = re.compile(r"VERDICT=(\S+)")

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_verdict(verdict_line: str) -> str:
    """Extract the VERDICT token from a verdict_line string.

    Returns the matched token (e.g. "COVERED", "UNCOVERED", "INDETERMINATE")
    or "UNKNOWN" if the pattern is absent (malformed verdict_line).
    """
    m = _VERDICT_RE.search(verdict_line)
    return m.group(1) if m else "UNKNOWN"


def _git_head_timestamp(repo_root: Path) -> Optional[str]:
    """Return the ISO 8601 committer timestamp of HEAD, or None on any failure.

    Uses git log -1 --format=%cI HEAD — deterministic (repo-derived, not wall clock).
    Timeout: 5 s.  Any subprocess or git failure yields None (non-fatal).
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            # Review: code-reviewer (F1) — match every sibling subprocess call site's
            # console-flash/stdin-hang guard convention.
            **no_console_creationflags(),
        )
        ts = proc.stdout.strip()
        return ts if proc.returncode == 0 and ts else None
    except Exception:  # noqa: BLE001
        return None


def _write_artifact(
    repo_root: Path,
    artifact: dict,
) -> None:
    """Atomically write *artifact* to ``<repo_root>/state/coverage/gate-result.json``.

    Uses a tempfile + os.replace for atomicity.  On any I/O failure logs a
    warning and returns — the caller (op handler) treats write errors as
    non-fatal.
    """
    artifact_dir = repo_root / "state" / "coverage"
    artifact_path = artifact_dir / "gate-result.json"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            dir=str(artifact_dir),
            prefix=".gate-result.tmp.",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(artifact, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_str, str(artifact_path))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_str)
            raise
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "coordinator_core.ops.coverage_gate: artifact write failed: %s", exc
        )


def _stage_chain_ancestry_waivers(repo_root: Path, chain_id: str) -> None:
    """Best-effort ``git add -- <chain's waiver dir>`` for a just-minted chain-ancestry
    waiver set (C7, AC12 — see this module's own C7 negative-spec note for what this
    does and does NOT discharge). Never raises: an OSError/subprocess failure here must
    not fail the coverage-gate call it backs, the same posture
    ``record_chain_ancestry_waiver`` itself takes for its own writes.

    Cross-session non-resumption scenario (review finding, 2026-07-31
    chain-ancestry-discriminator slice): this stage — and the later ``wsc_tail``
    stage-path lookup that carries the directory into the ceremony's own bookkeeping
    commit — both key on ``chain_id`` == the closing session's own id. If that session
    never resumes the ceremony itself (context-limit kill, handoff to a fresh EM
    session, crash) and a DIFFERENT session eventually drives the same chain to a
    passing gate, the old directory is never staged/committed by that later session and
    sits as untracked disk litter under
    ``state/review-trail/chain-ancestry-waivers/<old-sid>/``.

    This is no longer an accepted limitation left unfixed — the PM ruling recorded in
    docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md's
    § Problem reversed that acceptance: the corpus is in scope, "you either include
    the corpus now, or it never gets done." A reap path now exists:
    ``coordinator_core.chain_ancestry_waivers.chain_reached_terminal_close`` (the
    terminal-close predicate, fail-closed on "continued", a missing archived record,
    CC-7 errors, or any non-zero exit) backs
    ``coordinator_core.ops.reap_chain_ancestry_waivers``, registered as JSON-RPC op
    ``chain_ancestry_waivers.reap`` — fail-closed, remove-only (only ever unlinks a
    waiver file or rmdirs an emptied chain directory; never writes, mints, merges or
    edits a record), idempotent, and single-chain-scoped. Invoking it against the old
    directory above clears the litter.

    Be precise about what is and is not true today: the reaper exists and is
    invokable, but nothing invokes it automatically yet — no scheduled sweep, no
    ceremony-close hook. A standing bound assertion (companion work, same plan) is
    intended to turn this from "an operator has to remember to invoke the reaper" into
    something that goes red on drift, but until that lands (or something else calls
    the reap op), an untracked directory left by this scenario still requires a manual
    ``chain_ancestry_waivers.reap`` invocation. See
    ``wsc_tail._pending_chain_ancestry_waiver_stage_path``'s docstring for the fuller
    keying reasoning.
    """
    chain_dir = chain_waiver_dir(str(repo_root), chain_id)
    if chain_dir is None or not chain_dir.is_dir():
        return
    try:
        proc = subprocess.run(
            ["git", "add", "--", str(chain_dir)],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        if proc.returncode != 0:
            # Review: code-reviewer (Finding 2) — a non-zero exit (e.g. a peer holding
            # .git/index.lock) previously logged nothing, looking identical to success.
            # Keep best-effort posture — no raise, no check=True — just make it visible.
            _logger.warning(
                "coordinator_core.ops.coverage_gate: staging chain-ancestry waivers for "
                "chain %r exited %d: %s",
                chain_id, proc.returncode, (proc.stderr or "").strip(),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, never fails the gate call
        _logger.warning(
            "coordinator_core.ops.coverage_gate: staging chain-ancestry waivers for "
            "chain %r failed: %s", chain_id, exc,
        )


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------

@register_op("coverage.gate")
async def _coverage_gate(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'coverage.gate' handler.

    Delegates to coordinator_core.coverage.run_coverage_gate for the full gate
    algorithm (SAFE_RANGE validation, JSON/JSONL trail parsing, DAG fixpoint or
    flat rev-list, verdict computation with the three fidelity guards).

    After the gate runs, persists the result as a versioned JSON artifact to
    ``<repo_root>/state/coverage/gate-result.json`` so cockpit can read disk-truth
    without a live engine call.  Artifact writes are non-fatal — a write failure
    logs a warning but does not alter the JSON-RPC reply.

    Params (from JSON-RPC request params dict):
        range              (str, optional)       — git rev-range for flat mode.
        from_handoff       (str, optional)       — closing handoff path for DAG mode.
        scope_paths        (list[str], optional) — path-scope for flat-mode chain_set.
        closing_session_id (str, optional)       — active session ID of the closing handoff
                                                   for D3 case 3 attribution (DAG mode).
        verbose             (bool, optional)      — forwarded verbatim to
                                                   run_coverage_gate's own `verbose` kwarg
                                                   (full raw bookkeeping_shas list in notes
                                                   instead of a count). Default False —
                                                   byte-identical to pre-existing behaviour
                                                   when omitted. Reachable end-to-end from
                                                   coordinator/bin/review-coverage-gate.py's
                                                   ``--verbose`` flag.
        mint_chain_waivers  (bool, optional)      — default False. See module docstring's
                                                   ``mint_chain_waivers`` entry above —
                                                   ceremony-close only; every diagnostic
                                                   invocation must keep the default.

    Returns (as the JSON-RPC result dict):
        {
            "verdict_line":     str,   # frozen CLI contract line (AC11)
            "notes":            list,  # diagnostic notes + uncovered SHA lines
            "exit_code":        int,   # 0 / 1 / 2 per exit-code contract above
            "bookkeeping_shas": list,  # see disk-artifact schema doc above
            "planning_shas":    list,  # see disk-artifact schema doc above — SUBSET of
                                       #   uncovered_shas, not excluded from the verdict
            "unrecordable_shas": list, # see disk-artifact schema doc above — SUBSET of
                                       #   uncovered_shas, not excluded from the verdict
        }

    (The persisted disk artifact additionally carries "coverage_ratio" — see the
    module docstring's JSON schema block above. This op's own JSON-RPC return dict
    does not restate the full artifact schema; callers wanting the ratio without a
    disk read can parse it from verdict_line.)
    """
    # asyncio deferred to first use here (not module scope) — this module is imported
    # by ops/extract_scope_paths.py's eager-load chain; a module-scope `import asyncio`
    # dragged asyncio.base_events (~8ms) into every eager op import even for callers
    # that never invoke coverage.gate/coverage.halt_on_uncovered. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    range_arg = str(params.get("range") or "")
    from_handoff = str(params.get("from_handoff") or "")
    closing_session_id = str(params.get("closing_session_id") or "")
    verbose = bool(params.get("verbose", False))
    mint_chain_waivers = bool(params.get("mint_chain_waivers", False))
    em_disposition_raw = params.get("em_disposition")
    em_disposition = str(em_disposition_raw) if em_disposition_raw else None

    scope_paths_raw = params.get("scope_paths") or []
    if isinstance(scope_paths_raw, str):
        # Accept a single string path as well as a list (defensive)
        scope_paths = [scope_paths_raw] if scope_paths_raw else []
    elif isinstance(scope_paths_raw, list):
        scope_paths = [str(p) for p in scope_paths_raw if p]
    else:
        scope_paths = []

    # AC-5: fail loud when repo_root is absent — daemon cwd is not a valid repo root.
    # repo_root is the third handler arg (C1b-ii); ctx.repo_root backward-compat fallback
    # and os.getcwd() last-resort fallback both removed (AC-5 / global-multiplex migration).
    if repo_root is None:
        return {
            "verdict_line": (
                "ERROR: coverage.gate requires a resolved repo root "
                "(_origin_worktree absent or unresolvable)"
            ),
            "exit_code": 1,
            "notes": [],
        }
    repo_root_path = Path(repo_root)
    repo_root_str = str(repo_root_path)

    # AC-3 Gap-3: run_coverage_gate calls subprocess.run (blocking I/O) synchronously.
    # Offload to asyncio.to_thread so the event loop is not stalled for other partitions
    # while the git + coverage subprocess executes.  This makes the per-request
    # DISPATCH_TIMEOUT_SECS timeout actually interruptible for coverage.gate requests.
    # Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C3
    result = await asyncio.to_thread(
        run_coverage_gate,
        range_arg=range_arg,
        scope_paths=scope_paths,
        from_handoff=from_handoff,
        repo_root=repo_root_str,
        closing_session_id=closing_session_id,
        verbose=verbose,
    )

    # AC5 (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md § C2b):
    # `CoverageResult` carries no `indeterminate` field of its own — that lives on the
    # private `_DagChainResult`, and `run_coverage_gate`'s DAG branch returns early on
    # `dag_result.indeterminate` BEFORE `dag_node_attribution` is ever assigned
    # (defaulting to {}), so an indeterminate derivation can never reach this line with
    # `result.verdict` in the below-threshold set. Keying on `result.verdict` (the
    # structural field, not a re-parse of verdict_line) is what lets this stay true
    # without this op layer reaching into `_DagChainResult` internals. WHY this matters:
    # if minting ever moves INSIDE run_coverage_gate's DAG branch itself, ahead of that
    # early return, this chokepoint stops being free and must be re-guarded explicitly
    # there.
    #
    # C10 token-flip history (2026-08-06, DR-245 deadlock reopened): `run_coverage_gate`
    # retired the binary "UNCOVERED" token in favour of "WARN" for the below-threshold
    # case (see module docstring's C10 note) — "UNCOVERED" can no longer be produced by
    # `run_coverage_gate` (verified: coordinator_core/coverage.py's verdict assembly
    # only ever assigns "COVERED", "WARN", or "INDETERMINATE" — grep confirms no other
    # call site sets `verdict = "UNCOVERED"`). At the time, this guard tested the
    # retired token, so it silently stopped matching every real uncovered
    # chain-terminal close, and `record_chain_ancestry_waiver` was never reached — the
    # exact deadlock DR-245 was written to close. That fix widened the check to
    # `result.verdict in ("WARN", "UNCOVERED")`, which is itself now retired below (see
    # the following paragraph) — kept here as history, not as the current gate.
    #
    # 2026-08-07 fix (three PM-vouch escalations in one evening — state/audits/
    # 2026-08-07-review-gate-scoping-predecessor-and-planning-artifacts.md): the
    # `result.verdict` leg above answered the wrong question and is now DROPPED. The
    # mint and the verdict are about two different things — `result.verdict` /
    # `coverage_ratio` (coordinator_core.coverage.run_coverage_gate) is CODE coverage,
    # deliberately netting `planning_shas` out of both ratio terms; the chain-ancestry
    # waiver is about FOREIGN-SESSION NARROWING, whether a chain-terminal session may
    # credit commits a predecessor session authored. Coupling them produced a dead end:
    # a chain whose only uncovered commits were planning artifacts nets
    # `coverage_ratio == 1.0` -> `verdict == "COVERED"`, so the mint never fired even
    # though the narrowing obligation was real (a planning commit still owes a plan
    # review and stays in `uncovered_shas` until one credits it — see this module's own
    # `planning_shas` doc above). Gating on `result.uncovered_shas` alone below is what
    # correctly fires the mint whenever there is a real uncovered commit to narrow,
    # independent of whether that commit's presence also moves the CODE-coverage
    # verdict.
    #
    # The fabrication hazard this guard exists to prevent is UNCHANGED and is preserved
    # by the `result.uncovered_shas` leg, not by the (now-removed) verdict leg: minting
    # when there is genuinely nothing uncovered would fabricate coverage evidence for
    # commits nobody reviewed (see the em_disposition note below and the sibling-repo
    # incident it cites). With `result.uncovered_shas` required truthy, the mint can
    # only fire when uncovered commits actually exist — do not restore the verdict leg
    # believing this hazard was overlooked; it was deliberately relocated onto the
    # `uncovered_shas` check, which is strictly necessary for the mint to fire at all.
    if (
        mint_chain_waivers
        and from_handoff
        and result.uncovered_shas
    ):
        # Chain identity: mint under the SAME closing_session_id this call threaded to
        # run_coverage_gate above — coverage.py's _chain_ancestry_waived_shas /
        # _narrow_foreign_session_scope match a gate-minted waiver to a later trail
        # record by exact chain_id equality, so minting under any other value silently
        # credits nothing at read time.
        # `em_disposition` threads through verbatim and is written into every minted
        # record even when None — "no EM disposition was recorded at this close" must be
        # a STATED fact on disk, not an absent field a later reader mistakes for a
        # coverage certification (example-retrieval-repo-em, 2026-08-04: 18 minted waivers carrying
        # no reviewer, verdict, justification or disposition, which read as coverage
        # nobody performed). This op cannot derive the disposition itself — the
        # brightline verdict lives in ops/review_brightline_gate.py, which does not call
        # this op — so the ceremony-close caller is the only layer that can supply one.
        # Operator-facing mint note (this incident): the old gate emitted
        # NOTHING on either branch of this condition, so an operator seeing
        # exit 0 could not tell "minted 16 waivers" from "minted nothing" —
        # a chain-terminal EM read silence as success, escalated a false
        # write-guard failure to the PM, and burned an evening on it (see
        # this file's own history for the fix to the *condition* that
        # preceded this note). `chain_ancestry_waived_shas` is read-only and
        # never raises (see its own docstring), so this before/after diff
        # can safely distinguish a NEWLY-written waiver from a re-mint that
        # only rediscovers an already-idempotent one, without touching
        # `record_chain_ancestry_waiver`'s signature or its best-effort
        # contract.
        _waiver_dir = chain_waiver_dir(repo_root_str, closing_session_id)
        try:
            _before_waived = await asyncio.to_thread(
                chain_ancestry_waived_shas, repo_root_str, closing_session_id
            )
            _refused_shas = await asyncio.to_thread(
                record_chain_ancestry_waiver,
                repo_root_str,
                frozenset(result.uncovered_shas),
                closing_session_id,
                from_handoff,
                em_disposition,
            )
            # C7 (AC12): the mint happens at HALT, which aborts the ceremony before its
            # bookkeeping commit runs, so these files are untracked at the moment they are
            # created. Staging them here (best-effort, non-fatal) keeps a later ceremony's
            # dirty-tree gate (coordinator_core.ops.ceremony.commit_gates.dirty_tree_gate)
            # from ever seeing them as an "unattributable" dirty path — a staged path is
            # excluded from that classification unconditionally. This does NOT by itself
            # get the files into a commit (a scoped `git commit -- <pathspec>` only commits
            # paths named in ITS OWN pathspec, staged or not) — that remains a real gap,
            # see this op's module docstring's own C7 negative-spec note below.
            await asyncio.to_thread(_stage_chain_ancestry_waivers, repo_root_path, closing_session_id)
            _after_waived = await asyncio.to_thread(
                chain_ancestry_waived_shas, repo_root_str, closing_session_id
            )
            _newly_written = _after_waived - _before_waived
            if _newly_written:
                _mint_note = (
                    f"chain_ancestry_waivers: minted {len(_newly_written)} waiver(s) "
                    f"for chain {closing_session_id!r} under {_waiver_dir}/ "
                    f"({len(_after_waived)} total waived for this chain)"
                )
            else:
                # Idempotent re-mint: every uncovered sha already had a waiver
                # file on disk for this chain before this call. Reporting the
                # resulting total covered set here (rather than guessing at a
                # "0 newly written" framing that could misread as "nothing
                # happened") is deliberate — see this block's own comment above
                # on why a before/after diff, not a change to
                # `record_chain_ancestry_waiver`'s signature, was used to learn
                # this.
                _mint_note = (
                    f"chain_ancestry_waivers: no NEW waivers minted for chain "
                    f"{closing_session_id!r} — all {len(_after_waived)} uncovered "
                    f"commit(s) already carried a waiver under {_waiver_dir}/"
                )
            if _refused_shas:
                # 2026-08-10: `record_chain_ancestry_waiver` refused these shas
                # outright — a positively-established LIVE foreign owner (see
                # that function's own `_refuse_if_live_foreign_chain_sha`
                # docstring). Surfaced loudly here too, not just via that
                # function's own `logger.warning` calls, so a caller that only
                # reads `result.notes` (the operator-facing surface every
                # ceremony-close subcommand relays) is never left reading
                # silence as "minted everything".
                _mint_note += (
                    f" — {len(_refused_shas)} sha(s) REFUSED (LIVE foreign "
                    f"chain owner): {', '.join(sorted(_refused_shas))}"
                )
        except Exception as exc:  # noqa: BLE001 — best-effort mint, never fatal
            # `record_chain_ancestry_waiver` itself already swallows OSError
            # internally (its own docstring's best-effort contract); this
            # outer guard exists only for the note-emission plumbing around
            # it (the before/after diff, staging), so that adding operator
            # visibility here can never turn a degrade-to-uncredited mint
            # into a blocked ceremony (this task's hard constraint).
            _logger.warning(
                "chain_ancestry_waivers: mint note emission failed for chain "
                "%r (%s) — the read side may not credit uncovered commit(s) "
                "for this chain without a waiver file under %s/",
                closing_session_id, exc, _waiver_dir,
            )
            _mint_note = (
                f"chain_ancestry_waivers: mint attempted for chain "
                f"{closing_session_id!r} but could not be confirmed ({exc}) — "
                f"check {_waiver_dir}/ directly"
            )
        result.notes.append(_mint_note)
    elif mint_chain_waivers:
        # mint_chain_waivers=True but the mint precondition (see the block
        # above) was false — state which leg, in the operator's terms, so
        # silence is never mistaken for "minted." Mirrors this op's own
        # notes idiom (appended here, rendered to stderr by the veneer)
        # rather than a bare log line nobody watching stdout/stderr sees.
        if not from_handoff:
            result.notes.append(
                "chain_ancestry_waivers: mint requested but skipped — no "
                "from_handoff (not a DAG-mode/ceremony-close call)"
            )
        elif not result.uncovered_shas:
            result.notes.append(
                "chain_ancestry_waivers: mint requested but skipped — "
                "uncovered_shas is empty (nothing owed for this chain)"
            )

    # Surface diagnostic notes.
    # Uncovered SHAs are included as notes (mirrors "uncovered: <sha>" to stderr in
    # review-coverage-gate.sh (example-doctrine-repo d9ac8232, 2026-07-19) — the op layer surfaces them
    # as notes since the veneer is responsible for rendering stderr output). Commit
    # subjects are NOT appended here (would require a `git log` spawn this module
    # does not otherwise make on the uncovered-SHA path — see coverage.py's
    # _render_dag_ancestry_notes docstring, which documents the identical
    # spawn-budget refusal for its own render).
    #
    # DAG-mode UNCOVERED already carries a grouped, baton-attributed rendering of
    # every uncovered SHA in result.notes (coverage.py's _render_dag_ancestry_notes,
    # appended when from_handoff and verdict == UNCOVERED). Appending the flat
    # "uncovered: <sha>" form on top of that would print each SHA twice, in two
    # different formats, under one HALT — the exact "wall of undifferentiated SHAs"
    # illegibility that render exists to fix. Emit the flat form only when no
    # grouped attribution is available (flat mode, INDETERMINATE, or an empty
    # dag_node_attribution) — the pre-existing fallback every non-DAG caller relies on.
    notes = list(result.notes)
    if not result.dag_node_attribution:
        for sha in result.uncovered_shas:
            notes.append(f"uncovered: {sha}")

    # ------------------------------------------------------------------
    # C6: persist disk artifact — cockpit reads this as disk-truth.
    # Spec backlink:
    #   docs/plans/2026-07-05-coordinator-core-execution-model-retirem.md § C6
    # ------------------------------------------------------------------
    artifact: dict = {
        "schema_version": _ARTIFACT_SCHEMA_VERSION,
        "verdict": _extract_verdict(result.verdict_line),
        "verdict_line": result.verdict_line,
        "coverage_ratio": result.coverage_ratio,
        "exit_code": result.exit_code,
        "chain_commits": result.chain_commits,
        "covered": result.covered,
        "uncovered": result.uncovered,
        "uncovered_shas": list(result.uncovered_shas),
        "bookkeeping_shas": list(result.bookkeeping_shas),
        "planning_shas": list(result.planning_shas),
        "unrecordable_shas": list(result.unrecordable_shas),
        "notes": notes,
    }
    generated_at = await asyncio.to_thread(_git_head_timestamp, repo_root_path)
    if generated_at is not None:
        artifact["generated_at"] = generated_at

    # Offload to thread: _write_artifact calls blocking I/O (mkdir + file write).
    await asyncio.to_thread(_write_artifact, repo_root_path, artifact)

    return {
        "verdict_line": result.verdict_line,
        "notes": notes,
        "exit_code": result.exit_code,
        "bookkeeping_shas": list(result.bookkeeping_shas),
        "planning_shas": list(result.planning_shas),
        "unrecordable_shas": list(result.unrecordable_shas),
    }


# ---------------------------------------------------------------------------
# coverage.halt_on_uncovered — caller-side halt/override decision
# ---------------------------------------------------------------------------

#: Environment variable that mirrors the fence's warn-and-continue escape hatch.
#: Truthy iff its value is exactly "1" (matches the shell fence's `-eq 1`-style check;
#: no "true"/"yes" fuzzy-truthy parsing — an explicit, single sanctioned value).
OVERRIDE_ENV_VAR: str = "COORDINATOR_OVERRIDE_COVERAGE_GATE"

# C10: the "VERDICT=UNCOVERED" token this message describes can no longer be
# produced by run_coverage_gate (see the module docstring's C10 note above) —
# this string is only ever reached if a caller feeds this op a
# hand-constructed "UNCOVERED" verdict; kept verbatim (not deleted) since this
# op has no production caller INSIDE THIS REPO today and pruning it is out of
# this change's scope. Caveat: this op is registered under `coverage.halt_on_
# uncovered` (op_scopes.py / _registry_map.py) and is externally invocable over
# JSON-RPC by any sibling repo or skill regardless of in-repo callers — "no
# production caller" is an in-repo-only claim, not a guarantee against
# cross-repo callers. Any such caller now silently gets halted=False for every
# verdict, including what used to be a genuine below-threshold case, since
# `verdict` can never equal "UNCOVERED" again. See EM note in review-trail for
# this commit before assuming this branch is truly dead.
_HALT_MESSAGE = (
    "coverage.halt_on_uncovered: VERDICT=UNCOVERED — one or more commits in the "
    "evaluated chain have no covering review-trail record. Halting. Add the missing "
    "review-trail record(s), or re-invoke with override=true / "
    f"{OVERRIDE_ENV_VAR}=1 to warn and continue."
)


def _override_requested(params: dict) -> bool:
    """Return whether an override was requested, param taking precedence over env.

    An explicit ``override`` param (any truthy/falsy bool) always wins over the
    environment variable — the param is the caller's explicit, in-band signal;
    the env var is the out-of-band ambient escape hatch it mirrors.
    """
    if "override" in params:
        return bool(params.get("override"))
    return os.environ.get(OVERRIDE_ENV_VAR) == "1"


@register_op("coverage.halt_on_uncovered")
async def _coverage_halt_on_uncovered(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC 'coverage.halt_on_uncovered' handler.

    Re-runs the coverage-gate algorithm (coordinator_core.coverage.run_coverage_gate)
    to obtain a verdict, then applies the caller-side halt/override decision that
    previously lived in review-coverage-gate.sh's shell veneer. Does not write the
    coverage.gate disk artifact. This op is a pure decision read — no state mutation
    of any kind (op-classification.tsv: idempotency-hazard "none").

    Negative-spec (C2b, docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md
    § C2b): a first draft of C2 attached a ``mint_chain_waivers`` side effect here. This
    op has NO production caller anywhere in the tree — the ceremony-close path actually
    routes through ``coverage.gate`` (via ``review-coverage-gate.py``'s
    ``cc_invoke.route("coverage.gate", ...)``), never this op — so that mint could never
    fire in production. Minting now lives exclusively on ``coverage.gate`` above. Do not
    re-add a mint side effect here without first re-verifying this op has gained a real
    production caller.

    Params (from JSON-RPC request params dict):
        range               (str, optional)  — git rev-range for flat mode.
        from_handoff        (str, optional)  — closing handoff path for DAG mode.
        closing_session_id  (str, optional)  — active Claude Code session ID of the
                                               closing handoff (DAG mode D3 case 3);
                                               threaded through to run_coverage_gate
                                               identically to the coverage.gate op.
                                               Previously hardcoded to "" here, which
                                               silently forced every DAG-mode call
                                               through this op onto the fragile
                                               git-log --follow -M100% add-commit
                                               fallback for session attribution even
                                               when the caller had a trustworthy
                                               session id in hand — see
                                               improvement-queue
                                               2026-07-03-coverage-gate-dag-mode-misfires-on-prede.yaml.
        override            (bool, optional) — explicit warn-and-continue override; see
                                               module docstring for env-var precedence.

    Returns (as the JSON-RPC result dict):
        {
            "halted":           bool,  # True iff the retired UNCOVERED verdict was hit and
                                        #   no override in effect (C10: unreachable today —
                                        #   see module docstring's C10 note above)
            "verdict":          str,   # COVERED | WARN | INDETERMINATE | UNKNOWN
            "override_applied": bool,  # True iff UNCOVERED and override changed the outcome
                                        #   (unreachable today, see above)
            "message":          str,   # halt / warn-and-continue / pass-through text
        }
    """
    import asyncio

    if repo_root is None:
        return {
            "halted": True,
            "verdict": "UNKNOWN",
            "override_applied": False,
            "message": (
                "coverage.halt_on_uncovered requires a resolved repo root "
                "(_origin_worktree absent or unresolvable)"
            ),
        }
    repo_root_path = Path(repo_root)

    range_arg = str(params.get("range") or "")
    from_handoff = str(params.get("from_handoff") or "")
    closing_session_id = str(params.get("closing_session_id") or "")

    # Offload to thread: run_coverage_gate calls subprocess.run (blocking I/O).
    # Same rationale as coverage.gate above (AC-3 Gap-3) — keeps the per-request
    # dispatch timeout interruptible.
    result = await asyncio.to_thread(
        run_coverage_gate,
        range_arg=range_arg,
        scope_paths=[],
        from_handoff=from_handoff,
        repo_root=str(repo_root_path),
        closing_session_id=closing_session_id,
    )
    verdict = _extract_verdict(result.verdict_line)

    if verdict != "UNCOVERED":
        return {
            "halted": False,
            "verdict": verdict,
            "override_applied": False,
            "message": f"coverage.halt_on_uncovered: VERDICT={verdict} — no halt.",
        }

    override_in_effect = _override_requested(params)
    if override_in_effect:
        return {
            "halted": False,
            "verdict": verdict,
            "override_applied": True,
            "message": (
                "coverage.halt_on_uncovered: VERDICT=UNCOVERED but "
                f"override is in effect ({OVERRIDE_ENV_VAR}=1 or override=true) — "
                "warning and continuing."
            ),
        }
    return {
        "halted": True,
        "verdict": verdict,
        "override_applied": False,
        "message": _HALT_MESSAGE,
    }
