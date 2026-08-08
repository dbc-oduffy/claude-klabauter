"""
coordinator_core.ops.promote_shipped_in_flight_stubs — interim /workday-start
closer: promotes clean-fork consumed spinoff-roadmap stubs whose deliverable
has SHIPPED on origin/main from deployment_state:in_flight to terminal shipped.

Purpose: /workstream-complete ships only the terminal SUCCESSOR handoff of a
forked spinoff-roadmap lineage — it never touches the origin fork-point stub,
which is left frozen at deployment_state:in_flight forever even after the
deliverable it forked from ships. This closer finds those stranded origin
stubs and closes them out.

Join key: the DELIVERABLE SPINE (deliverable_id -> coordinator_core.ops.
rollup_derive), NOT a predecessor-walk. rollup_derive re-derives ship-state on
demand from `Resolves: <deliverable_id>` commit trailers — it is the sole
shipped/not-shipped oracle here.

Negative-spec: does NOT implement a predecessor-walk or multi-signal lineage
matcher. The only join is deliverable_id -> rollup_derive's four-token
contract.

Direct-import design (deviation from the bash oracle's transport, not its
business logic — see docs/wiki, DOE-PORT template variant #1): the bash
oracle routed its stamp/ship mutations through `strangle_route_mutation`,
a three-state facade that dispatches to either a spawned `coordinator_core.
invoke` subprocess (native, when the seam is present on disk) or a legacy
Node.js CLI (`stamp-shipped-in.js` / `handoff-transition.js`, when it is
not) — that indirection existed ONLY because the example-doctrine-repo-side caller was bash
and needed an IPC/subprocess bridge to reach Python. This module runs AS
Python (the example-doctrine-repo-side trampoline direct-imports it), so it calls the already-
registered "handoff.stamp" / "handoff.transition" op handlers in-process via
`coordinator_core.ipc.get_op_handler` — the exact native code path the
facade's State 2 would have spawned a subprocess to reach, minus the spawn.
There is no legacy Node.js fallback in this module: once the caller is
Python, direct import is strictly cheaper and the transport-fallback
machinery that only existed to bridge bash-to-Python is not a distinct
behavior to preserve (see coordinator/docs/wiki, R1 DOE-PORT template
§ 1, "template-variant #1 — direct-import trampoline").

Exit codes: split by AC14's zero-match discriminator (state/audits/
2026-08-04-terminal-state-closer-exit-code-caller-audit.md), scored against
this closer's four aggregate counters (promoted/norc_count/unknown_error_count/
stamp_abort_count) rather than one global boolean:
  - `promoted == 0 and norc_count == 0 and unknown_error_count == 0 and
    stamp_abort_count == 0` (nothing in_flight to scan at all) -> exit 0,
    quiet. This is the "zero candidates at all" case: example-doctrine-repo's `/workday-start`
    runs this closer unconditionally every morning, and most mornings have
    no in-flight spinoff-roadmap stubs — going loud here is a false alarm on
    a day nothing is wrong.
  - `norc_count > 0` with `stamp_abort_count == 0` (candidates found,
    rollup_derive reports no-resolving-commits — unmerged/pre-convention
    branch work, a real and expected state) -> exit 0, quiet. Mirrors
    rollup_derive's own "not an error" framing one hop downstream.
  - `unknown_error_count > 0` (candidates found, rollup_derive reports
    unknown-error — could not determine ship-state at all: not a git repo,
    or origin/main unreachable) -> exit 0, ADVISORY-VISIBLE, never
    conflated with `norc_count`'s quiet "nothing to do" framing.
    Deliberately NOT loud: a stale/unfetched origin/main on the box running
    `/workday-start` would otherwise re-create the every-morning false-alarm
    already fixed once (commit 40bf1064a124) for the zero-candidates case —
    but "could not determine" ("could not determine" != "confirmed not
    shipped", per rollup_derive's own docstring) must still be distinct,
    in both the printed advisory and this module's machine-readable result,
    from "nothing to promote". Review: code-reviewer Finding 2 — this token
    used to fall into the same bare `continue` as `not-shipped`, with no
    counter and no signal at all.
  - `stamp_abort_count > 0` (a candidate resolved shipped and joined cleanly,
    but the shipped_in stamp write itself failed) -> exit non-zero, loud.
    This is "candidates present but unjoinable/unwritable" in the AC14
    sense — a real mutation failure, not an absence of work.
This was previously "0 always" (matching the retired bash oracle's
`set -euo pipefail` + `|| true` fallbacks); AC3/AC14 require the
stamp-write-failure case to survive as a caller-visible signal, so the
unconditional-0 contract no longer holds. A claude-klabauter-link/import failure at
the example-doctrine-repo-side trampoline layer still degrades to exit 0 with a loud stderr
diagnostic (never-block posture, per the porter-brief addendum's
transport-failure rule for best-effort/advisory scripts) — that failure
class is orthogonal to this module's own business-outcome exit code.
Per-stub errors are still reported as WARNING/ABORT lines on stderr and the
aggregate outcomes as advisory lines on stdout; only the aggregate exit code
is new.

Structural non-goals (recorded, not fixed — Q3's territory, PM ruling R2):
this closer cannot fire for unmerged branch work, for two reasons neither of
which this module (or rollup_derive) repairs:
  (1) the `Resolves: <deliverable_id>` commit trailer that `commit_anchors`
      emits ONLY at a completion event on a staged plan + archive/completed/
      entry, deliberately (precision over recall) — it does not backfill or
      emit on every commit carrying a Deliverable-Id, so branch work with no
      completion event yet has no trailer for rollup_derive to find at all
      (surfaces here as `no-resolving-commits`, scored quiet above).
  (2) the origin/main ancestry requirement rollup_derive's shipped/not-shipped
      check enforces (coordinator_core.ops.emit.envelope) — even a commit
      that DOES carry the trailer reads `not-shipped` until it lands on
      origin/main, which by definition excludes unmerged branch work.
Both are load-bearing for why "in_flight" is the correct steady state for
active branch work, not a defect this closer should paper over by joining on
something looser.

Repo-root vs. cwd (SCRIPT_DIR-relative parity): the bash oracle resolved
`state/handoffs` via `${SCRIPT_DIR}/../../state/handoffs` — i.e. relative to
wherever the script physically sits, NOT the invoking shell's cwd — while its
git operations (via `rollup-derive.sh`, `git show`) inherited the invoking
shell's cwd unchanged. This module mirrors that split exactly: `repo_root`
(passed explicitly by the example-doctrine-repo trampoline, derived the same SCRIPT_DIR-
relative way) drives all *filesystem* path resolution (state/handoffs/, the
git common dir passed to the stamp/ship op handlers); the rollup-derive call
and the per-SHA `git show` calls rely on the current process's inherited cwd,
exactly as the bash oracle's git subprocesses did. Callers (the trampoline,
tests) MUST invoke this module with cwd already inside the target repo.

Spec: docs/plans/2026-07-11-consumed-in-flight-stub-shipped-stamp-propagation.md (C1)
Port of: promote-shipped-in-flight-stubs.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix"):
  - Does NOT git-commit anything itself (the stamp/ship op handlers are pure
    frontmatter file mutations; no commit step exists in either the oracle
    or this port).
  - Does NOT validate SHA format beyond truncating to 8 chars — any non-empty
    resolving SHA is accepted.
  - Does NOT collapse "unknown-error" or "not-shipped" tokens into a
    promotion — only the exact "shipped" token triggers stamp+ship.
  - Does NOT re-implement a predecessor-walk; see module docstring above.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import get_op_handler
from coordinator_core.lifecycle import git_common_dir as _git_common_dir

# Import side-effects: register "handoff.stamp" / "handoff.transition" in the
# ipc op-registry so get_op_handler() below resolves via a direct registry hit
# rather than its lazy-import fallback (get_op_handler() self-resolves a MISS
# since 2026-07-25, so this pre-import is belt-and-braces, not strictly
# required for correctness). Mirrors
# coordinator_core.ops.ceremony.consumed_handoff_stamp's own reuse pattern
# (public op-registry contract, not a private cross-module reach).
import coordinator_core.ops.handoff_stamp  # noqa: F401
from coordinator_core.session.declared_writes import declare_write
import coordinator_core.ops.handoff_transition  # noqa: F401
from coordinator_core.ops.refresh_roadmap_callout import main as _refresh_callout_main
from coordinator_core.ops.rollup_derive import main as _rollup_main

_PROG = "promote-shipped-in-flight-stubs.sh"  # literal program-name prefix — matches oracle stderr
_GIT_TIMEOUT_SECS = 30


# ---------------------------------------------------------------------------
# Frontmatter field extraction — parity with the oracle's `_fm_field` awk
# helper (single-key value, whitespace-stripped, single matched pair of
# surrounding quotes stripped).
# ---------------------------------------------------------------------------


class _StubUnreadable(Exception):
    """Raised by ``_fm_field`` when the stub file itself could not be read
    (permission error, vanished mid-scan, etc.) -- a hard I/O failure, never
    silently indistinguishable from "field simply absent" (empty string)."""


def _fm_field(path: Path, key: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _StubUnreadable(str(exc)) from exc
    split = split_frontmatter(text)
    if split is None:
        return ""
    # Unquoted read: replaces a local matched-pair slice that did not unescape
    # YAML's doubled '' inner-quote form, so a quoted value containing a quote
    # came back mangled.
    val = read_fm_field_unquoted(split.fm_text, key)
    return val if val is not None else ""


# ---------------------------------------------------------------------------
# rollup-derive join (deliverable_id -> shipped/not-shipped/unknown-error/
# no-resolving-commits token + resolving SHA list).
# ---------------------------------------------------------------------------


def _rollup_derive(deliverable_id: str) -> Tuple[str, List[str]]:
    """Call coordinator_core.ops.rollup_derive.main in-process, capturing its
    stdout (token line + resolving-SHA lines) exactly as the oracle captured
    `bash "$ROLLUP" "$deliverable_id" 2>/dev/null || true` — treated as an
    opaque CLI contract, never reaching into rollup_derive's private
    internals, so the two modules can evolve independently of each other.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _rollup_main([deliverable_id])
    except SystemExit:
        # Mirrors the oracle's `|| true` -- rollup-derive's exit code is not part
        # of its CLI contract here, only its captured stdout lines are.
        pass
    lines = [line for line in buf.getvalue().splitlines() if line != ""]
    if not lines:
        return "", []
    return lines[0], lines[1:]


# ---------------------------------------------------------------------------
# SHA selection (AC1/AC3 — select by MAX committer timestamp across ALL
# resolving SHAs, not positional trust in line 2 of rollup-derive's output).
# ---------------------------------------------------------------------------


def _batch_committer_timestamps(shas: List[str]) -> dict:
    """Resolve committer-timestamp (``%ct``) for a batch of commit SHAs in
    ONE ``git log`` call.

    Mirrors ``emit/sections/handoffs._resolve_shipped_in_dates``'s
    ``--no-walk --ignore-missing`` shape exactly (the in-tree reconciliation
    reference this chunk was told to cite, not re-derive; the identical
    shape already landed this wave at C13 (0df3818bc) and C20 (ca3390a40)).
    This is an OBJECT question (commit metadata at caller-supplied SHAs),
    not a RANGE question, so it batches unconditionally — ``git log
    --no-walk`` resolves each argv SHA independently; it never merges them
    into one ancestry/reachability set expression the way
    ``git rev-list A..B C..D`` would (the forbidden shape for a DIFFERENT
    git-spawn class entirely; see ``docs/wiki/coverage-gate-perf.md``).

    ``--ignore-missing`` makes an unresolvable SHA silently ABSENT from
    stdout (exit 0) rather than an error — that absence is never read as a
    resolved timestamp. The prefix-match loop below only ever POPULATES the
    returned dict for a SHA it can positively match against stdout; a
    requested SHA absent from the return value is simply absent from the
    map, and ``_select_best_sha`` below treats a candidate missing from this
    map as unresolved and skips it — the same fail-closed contract the prior
    per-sha ``returncode != 0: continue`` path had (see backlog
    2026-07-13-promote-shipped-in-flight-stubs-p4-sha-fails-open.yaml for
    why fail-open here is the defect class to avoid).
    """
    if not shas:
        return {}
    ordered = sorted({sha for sha in shas if sha})
    if not ordered:
        return {}
    try:
        proc = subprocess.run(
            [
                "git", "log",
                "--no-walk=unsorted", "--ignore-missing",
                "--format=%H\x1f%ct",
                *ordered,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}

    sha_ct: dict = {}
    matched: set = set()
    for line in proc.stdout.replace("\r", "").splitlines():
        if not line:
            continue
        full, sep, ct_str = line.partition("\x1f")
        if not sep:
            continue
        try:
            ct = int(ct_str)
        except ValueError:
            continue
        for raw in ordered:
            if raw not in matched and full[: len(raw)] == raw:
                sha_ct[raw] = ct
                matched.add(raw)
                break
    return sha_ct


def _select_best_sha(shas: List[str]) -> str:
    """Select the SHA with the MAX resolvable committer timestamp, resolved
    via ONE batched ``git log`` call (``_batch_committer_timestamps``)
    across all candidates instead of one ``git show`` per candidate. Falls
    CLOSED (returns "") when every candidate is empty or unresolvable — a
    resolution failure (including one silently dropped by
    ``--ignore-missing``) must never be treated as a winning (or any)
    timestamp value, or a garbage/unresolvable SHA can be selected on an
    all-garbage input.
    """
    sha_ct = _batch_committer_timestamps(shas)
    best_sha = ""
    best_ct: Optional[int] = None
    for sha in shas:
        if not sha:
            continue
        ct = sha_ct.get(sha)
        if ct is None:
            continue
        if best_ct is None or ct > best_ct:
            best_ct = ct
            best_sha = sha
    return best_sha


# ---------------------------------------------------------------------------
# STAMP/SHIP — in-process op-handler calls (see module docstring "Direct-
# import design" for why this replaces the oracle's strangle_route_mutation
# three-state facade).
# ---------------------------------------------------------------------------


async def _stamp(stub_abs: str, sha8: str, git_common_dir: Path) -> dict:
    # kind="successor" (DR-096; Review: code-reviewer (Finding 1) -- corrected
    # from the originally-shipped "ship-commit" tag): sha8 is NOT derived from
    # this stub's own scope paths -- it is the MAX-committer-timestamp SHA
    # selected (_select_best_sha, above) from the set of real commits that
    # resolve this stub's deliverable_id via a `Resolves: <deliverable_id>`
    # commit trailer, discovered on the SUCCESSOR baton through
    # rollup_derive. That is exactly handoff.schema.json's `successor`
    # definition ("the SHA belongs to a successor or the deliverable that
    # resolved this baton ... this only tags that shipped_in's SHA was
    # sourced from that lineage, not a direct ship commit") -- NOT
    # `ship-commit`, which means a SHA that is itself a direct commit
    # shipping this handoff's own scope. This stub is never itself shipped;
    # its SHA is entirely inherited from a different, successor baton's
    # resolving-commit trailer, so `successor` is the precise tag, not a
    # cosmetic pick between two synonyms -- a future axis-dependent holding
    # rule that treats `successor` differently from `ship-commit` would
    # silently mis-classify every record this writer produces if left
    # mistagged. See the module docstring's SHA SELECTION comment in
    # _run_promotions ("Do NOT reuse the stamp path's own SHA derivation
    # here -- it would derive from the STUB's OWN scope, wrong for a forked
    # spinoff-roadmap stub").
    handler = get_op_handler("handoff.stamp")
    if handler is None:
        return {"exit_code": 1, "error": "handoff.stamp not registered"}
    return await handler(
        {"handoff_path": stub_abs, "sha": sha8, "kind": "successor"}, git_common_dir
    )


async def _ship(stub_abs: str, git_common_dir: Path) -> dict:
    handler = get_op_handler("handoff.transition")
    if handler is None:
        return {"exit_code": 1, "error": "handoff.transition not registered"}
    return await handler({"verb": "ship", "handoff_path": stub_abs}, git_common_dir)


class _PromotionResult:
    """Accumulator returned by ``_run_promotions`` — plain data holder, no
    behavior of its own."""

    __slots__ = (
        "promoted",
        "norc_count",
        "unknown_error_count",
        "stamp_abort_count",
        "touched_roadmaps",
    )

    def __init__(self) -> None:
        self.promoted = 0
        self.norc_count = 0
        self.unknown_error_count = 0
        self.stamp_abort_count = 0
        self.touched_roadmaps: List[str] = []


async def _run_promotions(handoffs_dir: Path, repo_root_path: Path) -> _PromotionResult:
    """Scan ``handoffs_dir`` and promote every eligible stub, awaiting
    ``_stamp``/``_ship`` directly instead of re-entering ``asyncio.run`` per
    stub (Review: code-reviewer F6 — one event loop for the whole scan
    instead of up to 2N create/destroy cycles for N matched stubs).
    """
    result = _PromotionResult()
    git_common_dir_cache: Optional[Path] = None

    for f in sorted(handoffs_dir.glob("*.md")):
        if not f.is_file():
            continue

        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # A stub that cannot be READ at all (permission error, vanished
        # mid-scan, etc.) is a hard I/O failure, not "no deliverable_id" --
        # previously _fm_field swallowed the OSError and returned "", so an
        # unreadable stub silently blended into the ordinary "field absent"
        # skip below and its deployment_state stayed in_flight forever with
        # no signal anything went wrong. Every OTHER skip path in this
        # function (no-resolving-SHA, stamp-route-failed, shipped_in-did-
        # not-land, ship-route-failed) prints a WARNING/ABORT naming the
        # stub; this makes the unreadable-file case match that contract
        # instead of being the one silent exception to it.
        try:
            status = _fm_field(f, "status")
            dstate = _fm_field(f, "deployment_state")
            kind = _fm_field(f, "kind")
        except _StubUnreadable as exc:
            print(f"{_PROG}: WARNING could not read stub {f}: {exc}", file=sys.stderr)
            continue
        # --- end Tier 2 ---

        # Target predicate: forked spinoff-roadmap stub, still in_flight.
        if not (canonical_kind(kind) == "roadmap-baton" and dstate == "in_flight"):
            continue
        # Defensive: these stubs should also carry status:claimed (or the
        # pre-DR-084 status:consumed, tolerated on read).
        if status not in ("claimed", "consumed"):
            continue

        stub_abs = f.resolve()

        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        try:
            deliverable_id = _fm_field(stub_abs, "deliverable_id")
        except _StubUnreadable as exc:
            print(f"{_PROG}: WARNING could not read stub {stub_abs}: {exc}", file=sys.stderr)
            continue
        # --- end Tier 2 ---
        if not deliverable_id:
            continue

        roadmap_id = _fm_field(stub_abs, "roadmap_id")

        token, shas = _rollup_derive(deliverable_id)

        if token == "shipped":
            pass
        elif token == "no-resolving-commits":
            # Vacuous — pre-convention strand, never close (AC2/AC10).
            result.norc_count += 1
            continue
        elif token == "unknown-error":
            # Review: code-reviewer Finding 2 — "could not determine" is a
            # distinct epistemic state from norc_count's "nothing to do";
            # never fold it into the same bare continue rollup_derive's own
            # docstring says must never happen (unknown-error != not-shipped).
            result.unknown_error_count += 1
            continue
        else:
            # not-shipped / anything else — skip untouched (AC2).
            continue

        # SHA SELECTION (AC1/AC3): select by MAX committer timestamp
        # across ALL resolving SHAs, not positional trust in output
        # order. Do NOT reuse the stamp path's own SHA derivation here —
        # it would derive from the STUB's OWN scope, wrong for a forked
        # spinoff-roadmap stub (the resolving commits live on the
        # successor baton, not this stub's scope).
        best_sha = _select_best_sha(shas)
        sha8 = best_sha[:8]
        if not sha8:
            # Review: code-reviewer — discriminate "zero candidates from
            # rollup-derive" (upstream join gave nothing) from "candidates
            # present but every one unresolvable" (this module's own P4
            # regression class) so a future recurrence is triageable from
            # the WARNING text alone, without re-deriving which case fired.
            if shas:
                print(
                    f"{_PROG}: WARNING shipped token with no resolving SHA for "
                    f"{stub_abs} (candidates={len(shas)}, none resolvable)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"{_PROG}: WARNING shipped token with no resolving SHA for {stub_abs}",
                    file=sys.stderr,
                )
            continue

        # TOCTOU GUARD (AC5): re-read BOTH deployment_state and status
        # right now — a concurrent writer may have changed status
        # without flipping deployment_state since the scan above.
        now_dstate = _fm_field(stub_abs, "deployment_state")
        now_status = _fm_field(stub_abs, "status")
        if not (now_dstate == "in_flight" and now_status in ("claimed", "consumed")):
            continue

        # Lazily resolve the git common dir (Review: code-reviewer F3 —
        # shared coordinator_core.lifecycle.git_common_dir resolver, memoized
        # per scan, instead of a naive `.git`-suffix join; see main()'s
        # docstring note on this cache for why it's lazy).
        if git_common_dir_cache is None:
            git_common_dir_cache = _git_common_dir(repo_root_path)

        # STAMP (AC9) — in-process "handoff.stamp" call.
        stamp_reply = await _stamp(str(stub_abs), sha8, git_common_dir_cache)
        if stamp_reply.get("exit_code") != 0:
            print(f"promote-shipped: WARNING stamp route failed for {stub_abs}", file=sys.stderr)
            continue

        # DR-276: the stamp handler mutated stub_abs directly (via
        # get_op_handler("handoff.stamp"), bypassing ipc.dispatch_message and
        # its scope-touch recording) — declare it here at the real write
        # site so it is not an unclaimed orphan at the scoped_git_commit
        # sink. Declared unconditionally on success, even though the
        # landed-check below may still abort ship: the stamp write already
        # happened by this point.
        declare_write(str(stub_abs))

        # ASSERT shipped_in LANDED (AC9/AC6 — fail-loud, no partial mutation).
        landed = _fm_field(stub_abs, "shipped_in")
        if not landed or landed == "null":
            print(
                f"promote-shipped: ABORT {stub_abs} — shipped_in did not land after stamp; NOT calling ship",
                file=sys.stderr,
            )
            result.stamp_abort_count += 1
            continue

        # SHIP (idempotent; sets deployment_state:shipped) — in-process
        # "handoff.transition" verb=ship call.
        ship_reply = await _ship(str(stub_abs), git_common_dir_cache)
        if ship_reply.get("exit_code") != 0:
            print(f"promote-shipped: WARNING ship route failed for {stub_abs}", file=sys.stderr)
            continue

        result.promoted += 1

        if roadmap_id:
            result.touched_roadmaps.append(roadmap_id)

    return result


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

_USAGE = """Usage: promote-shipped-in-flight-stubs.sh

Promotes clean-fork consumed spinoff-roadmap stubs whose deliverable has
SHIPPED on origin/main from deployment_state:in_flight to terminal shipped.
Scans <repo>/state/handoffs/*.md; no CLI arguments.

Exit codes: 0 when there is nothing to do or every candidate legitimately
has no resolving commits yet (quiet, per AC14 — see module docstring);
non-zero when >=1 candidate resolved shipped but its shipped_in stamp write
failed (loud). Per-stub errors are reported as WARNING/ABORT lines on
stderr; aggregate outcomes print as advisory lines on stdout."""


def main(argv: List[str], *, repo_root: Optional[str] = None) -> int:
    """CLI entry. ``argv`` is accepted for trampoline-call-shape consistency
    with sibling ports but carries no flags — this tool takes none, matching
    the bash oracle. ``repo_root`` is the repo root to scan (see module
    docstring "Repo-root vs. cwd"); when omitted, resolved via
    ``git rev-parse --show-toplevel`` against the current process cwd.
    """
    if argv and argv[0] in ("--help", "-h"):
        print(_USAGE)
        return 0

    if repo_root is None:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                **no_console_creationflags(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"{_PROG}: cannot resolve repo root: {exc}", file=sys.stderr)
            return 0
        if r.returncode != 0 or not r.stdout.strip():
            print(f"{_PROG}: not inside a git repository", file=sys.stderr)
            return 0
        repo_root_path = Path(r.stdout.strip())
    else:
        repo_root_path = Path(repo_root)

    handoffs_dir = repo_root_path / "state" / "handoffs"
    # Review: code-reviewer F3 — git common dir is resolved lazily inside
    # _run_promotions via the shared coordinator_core.lifecycle.git_common_dir
    # resolver (first-use, memoized per scan) rather than eagerly here, so a
    # non-git repo_root (e.g. a scan that never actually promotes a stub)
    # never trips the resolver's RuntimeError. Matches every other
    # "handoff.stamp"/"handoff.transition" caller's derivation — for a linked
    # worktree, <repo_root>/.git is a FILE pointer, not the common dir, and
    # the naive join this replaces would resolve the wrong root.

    promoted = 0
    norc_count = 0
    unknown_error_count = 0
    stamp_abort_count = 0
    touched_roadmaps: List[str] = []

    if handoffs_dir.is_dir():
        # Review: code-reviewer F6 — single asyncio.run() driving the whole
        # scan (instead of up to 2N event-loop create/destroy cycles for N
        # matched stubs) via _run_promotions' awaited _stamp/_ship calls.
        result = asyncio.run(_run_promotions(handoffs_dir, repo_root_path))
        promoted = result.promoted
        norc_count = result.norc_count
        unknown_error_count = result.unknown_error_count
        stamp_abort_count = result.stamp_abort_count
        touched_roadmaps = result.touched_roadmaps

    # -------------------------------------------------------------------
    # POST-LOOP: refresh each touched roadmap's callout (AC4) — deduped.
    # refresh_roadmap_callout.main exits 0 cleanly if the roadmap has no
    # STUB-INDEX/callout, so this is safe to call unconditionally per
    # unique roadmap_id.
    # -------------------------------------------------------------------
    if touched_roadmaps:
        for rid in sorted(set(touched_roadmaps)):
            if not rid:
                continue
            rc = _refresh_callout_main([rid])
            if rc != 0:
                print(
                    f"{_PROG}: WARNING refresh-roadmap-callout.sh failed for {rid}",
                    file=sys.stderr,
                )

    # AC10 — advisory for vacuous no-resolving-commits strands.
    if norc_count > 0:
        print(
            f"{norc_count} in_flight spinoff-roadmap stubs resolved no-resolving-commits "
            "(possible pre-convention strand — manual check)"
        )

    # ADVISORY-VISIBLE for unknown-error tokens (Review: code-reviewer
    # Finding 2) — kept distinct from norc_count's "nothing to do" line and
    # deliberately NOT loud (see module docstring "Exit codes"): a
    # stale/unfetched origin/main would otherwise false-alarm every morning.
    if unknown_error_count > 0:
        print(
            f"{unknown_error_count} in_flight spinoff-roadmap stubs could not be checked "
            "(rollup-derive reported unknown-error — could not determine ship-state; "
            "distinct from no-resolving-commits — manual check)"
        )

    # Aggregate advisory for stamp-landed-guard aborts.
    if stamp_abort_count > 0:
        print(
            f"{stamp_abort_count} in_flight spinoff-roadmap stubs could not be stamped "
            "(shipped_in failed to land — manual investigation)"
        )

    if promoted == 0:
        print("no in_flight spinoff-roadmap stubs promoted")
    else:
        print(f"{promoted} in_flight spinoff-roadmap stubs promoted to shipped")

    # AC14 discriminator (see module docstring "Exit codes"): only a real
    # mutation failure (a shipped-token candidate whose shipped_in stamp
    # write did not land) goes loud. "Zero candidates at all" and
    # "candidates present but not-yet-shipped" (norc_count) both stay quiet
    # — neither is a caller-visible failure, and example-doctrine-repo's /workday-start runs
    # this closer unconditionally every morning where the former is the
    # common case.
    if stamp_abort_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
