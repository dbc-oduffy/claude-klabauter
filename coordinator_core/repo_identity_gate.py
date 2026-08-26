"""coordinator_core/repo_identity_gate.py -- the lean home of
`compute_repo_identity_gate`, extracted out of `coordinator_core.pickup_assemble`
(10,072 lines, ~360ms to import per that module's own inline comment) so the
~25 `coordinator/bin` CLIs that reach this gate through
`coordinator.bin.lib.repo_identity.resolve_checked_repo_root` stop paying for
a room they never otherwise enter.

Spec backlink: `pln-a-ceremony-must-not-be-able-to-5e9421` § C1 (spike-verified
anchor: `docs/research/spike-verdicts/2026-08-11-harness-session-registry-as-
repo-identity-anchor.md`).

This is an EXTRACTION, not a rename or a rewrite: behaviour, verdict
vocabulary, and memoization semantics are frozen at parity with the code this
replaces. `coordinator_core.pickup_assemble` re-exports `compute_repo_identity_gate`
from here so its existing importers (the five direct importers repointed
alongside this module, plus anything still reaching it through the
re-export) keep working unchanged.

Only the genuine dependencies travel with the gate: `session.harness_registry`
(`self_record`/`snapshot`/`registry_dir`) for the anchor lookup, and
`session.core.stable_pid_alive` for the AC10 trust check. Nothing else the
10k-line module carries (git object plumbing, frontmatter, baton state, dag,
etc.) is a real dependency of this function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.session import core as _session_core
from coordinator_core.session import harness_registry as _harness_registry

#: `compute_repo_identity_gate` verdict vocabulary -- mirrors
#: `memo_check_addressee`'s existing three-valued ladder rather than
#: inventing a fourth string.
_REPO_IDENTITY_MATCH = "MATCH"
_REPO_IDENTITY_MISMATCH = "MISMATCH"
_REPO_IDENTITY_UNRESOLVED = "UNRESOLVED"


def _repo_identity_plausible_cwd(raw_cwd: Optional[str]) -> Optional[Path]:
    """Plausibility band gating MISMATCH (staff-eng re-review finding 0).

    A registry `cwd` value can be well-formed and type-correct while still
    being useless as positive evidence of a *different* real repo -- the
    measured case is harness issue #27627, a version that wrote `cwd=/`.
    Before a failed containment check may produce MISMATCH, the anchor
    `cwd` must: exist as a directory, not be a filesystem root, and have a
    `.git` in itself or some ancestor. A `cwd` failing this band is not
    positive evidence of a different real repo -- the rule this function
    exists to state: MISMATCH requires positive evidence of a different
    real repo; absence of that evidence is UNRESOLVED, never MISMATCH.

    Returns the resolved `Path` when the band is cleared, else `None`.
    """
    if not raw_cwd:
        return None
    try:
        candidate = Path(raw_cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_dir():
        return None
    if candidate.parent == candidate:
        # Filesystem root (POSIX `/`, or a Windows drive root like `C:\`).
        return None
    if not any((p / ".git").exists() for p in (candidate, *candidate.parents)):
        return None
    return candidate


def compute_repo_identity_gate(repo_root: Path, sid: Optional[str]) -> dict[str, Any]:
    """C1 -- the shared repo-identity gate: MECHANICAL comparison of the
    session's harness-level launch anchor against the ceremony's suspect
    `repo_root`, zero-spawn.

    Spec backlink: `pln-a-ceremony-must-not-be-able-to-5e9421`
    § C1 (spike-verified anchor:
    `docs/research/spike-verdicts/2026-08-11-harness-session-registry-as-
    repo-identity-anchor.md`).
    Fixes the "sender closed a ceremony against the wrong repo after an
    uncaught tool-subprocess `cd`" incident this plan exists for.

    **Definition of "anchor":** the session's CURRENT harness-level `cwd` as
    recorded in the harness session registry -- the value `/cd` moves, not a
    launch-immutable fact. **Coverage boundary, stated explicitly:**
    tool-subprocess `cd` drift IS caught (the reported incident); harness-
    level `/cd` is NOT caught, by design -- the session genuinely moved, so a
    ceremony closed there afterward is a correct MATCH, not a gap.

    Composition:
      1. anchor -- `session.harness_registry.self_record()` (the O(1)
         pid-keyed leg), falling back to a `snapshot()` lookup keyed by
         `sid` when `self_record()` returns `None` or its `sessionId` does
         not equal `sid` (per `docs/reference/harness-session-registry.md`
         § Two measured traps: "do not key on the filename PID alone" --
         this fallback is a directory scan, not zero-cost, but stays
         zero-spawn, which is all AC1 requires).
      2. trust check (AC10) -- whichever record resolved must have
         `sessionId == sid` AND `session.core.stable_pid_alive(pid,
         stored_start_epoch)` must hold. On the `snapshot()`-fallback leg
         the `sessionId` check is tautological (the lookup key IS `sid`) --
         only `stable_pid_alive` is live there; the equality check has
         real bite only on the pid-keyed leg, where a stale or reused pid
         could otherwise carry an unrelated session's record. Either check
         failing on both legs is UNRESOLVED, never MATCH -- the
         wrongful-takeover shape `harness_registry`'s own negative-spec
         defends against (`DoE-claude@642195ba` / `88929bea`).
      3. compare -- CONTAINMENT, not equality: is the anchor `cwd` (at
         arbitrary depth -- a launch from `<repo_root>/coordinator_core`
         measures exactly that) contained within `repo_root`? Resolved via
         `Path.resolve()` + `Path.is_relative_to`, the shape already used in
         `ops/fleet/archive_plans.py` and `ops/fleet/memo_send.py`.
         Deliberately NOT `_memo_resolver.same_repo_path` -- that is exact
         directory equality and would refuse a subdirectory-launched
         session, a false positive worse than the fail-open it replaces.
      4. plausibility band (`_repo_identity_plausible_cwd`) -- gates
         MISMATCH; see that function's docstring. A `cwd` failing the band
         falls to UNRESOLVED, never MISMATCH.
      5. verdict -- MATCH | MISMATCH | UNRESOLVED, the same ladder
         `memo_check_addressee` already established.

    UNRESOLVED is a first-class outcome, not an error, and this function
    NEVER refuses on its own -- refusal is entirely the caller's decision
    (see C2). UNRESOLVED is produced by: no registry record for this pid
    (`self_record()` miss with no `snapshot()` hit either), a record
    failing the AC10 cross-check on both legs, an unreadable/malformed
    record, an unresolvable `<claude-config>`, or an anchor `cwd` failing
    the plausibility band. Note for a future reader: the SUBAGENT case
    resolves through the dispatcher's own inherited `CLAUDE_PID`/`sid`
    rather than a registry record of its own -- subagents never register,
    by harness design -- so a subagent's gate call routes identically to
    its dispatcher's and is not a hole to "fix".

    **Known limitation, stated honestly, not engineered away:** the
    `snapshot()` fallback is lossy, not total. `snapshot()` is
    `sessionId`-keyed and documents last-writer-wins on a duplicate
    `sessionId` (unspecified `Path.glob` iteration order). A crash-then-
    resume reuses the same `sessionId` under a new pid, so a stale dead-pid
    record and the live one can share a key; if the stale one wins,
    `stable_pid_alive` correctly rejects it and the gate concludes
    UNRESOLVED -- silently inert in this reachable, coin-flip-by-directory-
    order case. Accepted as a limitation of the fallback leg, not fixed
    here; the test surface constructs this case so it is observed.

    Never spawns a subprocess: `self_record()`/`snapshot()` are pure-Python
    file reads and `stable_pid_alive` is `psutil`-only, no `git`/`ps`/`wmic`
    shell-out anywhere in this call graph.

    Returns a dict carrying `verdict`, `session_root` (the anchor `cwd`,
    or `None` when unresolved), `resolved_root` (`repo_root`, stringified),
    `sid`, and a rendered `message` naming all three so a MISMATCH refusal
    is auditable (AC3) and an UNRESOLVED entry states plainly that the
    check could not run (AC4).
    """
    resolved_root = str(repo_root)

    def _verdict(verdict: str, session_root: Optional[str], detail: str) -> dict[str, Any]:
        message = (
            f"repo-identity: sid={sid} session_root={session_root} "
            f"resolved_root={resolved_root} verdict={verdict} — {detail}"
        )
        return {
            "verdict": verdict,
            "session_root": session_root,
            "resolved_root": resolved_root,
            "sid": sid,
            "message": message,
        }

    if not sid:
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "no sid supplied")

    # --- 1. anchor: self_record() (O(1) pid-keyed leg), falling back to a
    # snapshot() scan by sid on a miss or sessionId mismatch.
    record_session_id: Optional[str] = None
    record: Optional[_harness_registry.RegistryRecord] = None

    self_hit = _harness_registry.self_record()
    if self_hit is not None and self_hit[0] == sid:
        record_session_id, record = self_hit
    else:
        fallback = _harness_registry.snapshot().get(sid)
        if fallback is not None:
            record_session_id, record = sid, fallback

    if record is None or record_session_id is None:
        # A registry that holds files but parses to nothing is a DIFFERENT
        # condition from one that parses fine and simply has no row for this
        # sid -- the first is a parser/shape defect (see `harness_registry`'s
        # `procStart` note: an integer-only parser read every POSIX record as
        # unparseable and left this gate silently inert fleet-wide), the
        # second is the ordinary miss this arm was written for. Reporting
        # both as "0 parsed" would restate the defect's own camouflage.
        detail = "no registry record for this session"
        try:
            registry_dir = _harness_registry.registry_dir()
            if registry_dir is not None and registry_dir.is_dir():
                file_count = sum(1 for _ in registry_dir.glob("*.json"))
                if file_count > 0:
                    parsed_count = len(_harness_registry.snapshot())
                    detail = (
                        f"no registry record for this session "
                        f"(registry holds {file_count} file(s), {parsed_count} parsed)"
                    )
        except Exception:
            pass
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, detail)

    # --- 2. trust check (AC10) -- sessionId equality (tautological on the
    # snapshot() fallback leg, live on the pid-keyed leg) AND
    # stable_pid_alive. Either failing is UNRESOLVED, never MATCH.
    if record_session_id != sid:
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "registry record sessionId does not match sid")
    if not _session_core.stable_pid_alive(record.pid, stored_start_epoch=str(int(record.start_epoch))):
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "registry record failed the stable_pid_alive trust check")

    # --- 3/4. compare by containment, gated by the plausibility band.
    plausible_cwd = _repo_identity_plausible_cwd(record.cwd)
    session_root_display = record.cwd

    try:
        resolved_repo_root = repo_root.resolve()
    except (OSError, RuntimeError):
        return _verdict(_REPO_IDENTITY_UNRESOLVED, session_root_display, "repo_root did not resolve")

    if plausible_cwd is not None:
        if plausible_cwd.is_relative_to(resolved_repo_root):
            return _verdict(_REPO_IDENTITY_MATCH, session_root_display, "anchor cwd is contained within repo_root")
        return _verdict(
            _REPO_IDENTITY_MISMATCH,
            session_root_display,
            "anchor cwd is a real, plausible directory outside repo_root",
        )

    # cwd absent or failed the plausibility band: absence of positive
    # evidence of a different real repo is UNRESOLVED, never MISMATCH.
    return _verdict(_REPO_IDENTITY_UNRESOLVED, session_root_display, "anchor cwd failed the plausibility band")
