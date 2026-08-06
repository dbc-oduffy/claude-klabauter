"""
coordinator_core.ops.review_trail_readjudication_report — read-only corpus
delta report for C7's read-side foreign-session-stripping extension.

Purpose: C7 (archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md) extended
``coverage.py``'s read-side foreign-commit stripping from ``scope="session"``
alone to ``scope in ("session", "chain", "workstream-close-auto")`` — see
``coordinator_core.coverage._narrow_foreign_session_scope``. That change is
retroactive: every ``chain``/``workstream-close-auto`` review-trail record
already on disk is now CREDITED DIFFERENTLY on the next coverage-gate read,
even though not one byte of the record itself changes. This module answers
"which of my surfaces just re-opened, and why" for an operator about to run
``/workstream-complete`` or the coverage gate, without them having to
cross-reference ``coverage.py``, the record corpus, and the ceremony trail
by hand.

For every ``state/review-trail/*.json`` record whose ``scope`` is ``chain``
or ``workstream-close-auto``, this module recomputes the record's credited
SHA set under BOTH the pre-C7 semantics (the record's ``sha_range`` credited
in full — the old chain/workstream-close-auto entitlement) and the post-C7
semantics (that same set, minus any commit provably attributed via its own
``Session-Id`` trailer to a DIFFERENT session — ``session_attribution.
trailer_foreign_shas``). A record whose credited set SHRINKS under the new
semantics is reported, naming the dropped SHAs and, where a
``state/ceremony/wsc/*.json`` ceremony record's ``review_trail.write``
node evidence names this record's path, that ceremony run as the surface
that last relied on it.

This is diagnostic output, not a gate: it reports the C7-driven credit
delta so a re-opened surface is an inspectable consequence, not a surprise.

Review: code-reviewer — UPPER-BOUND CAVEAT (not intersected with the gate's
DAG chain_set): ``_full_range_shas`` is a raw ``git rev-list <sha_range>``,
never intersected with the ``chain_set`` the live gate actually credits
against (``coverage.py``'s ``_reviewed_via_graph_walk`` always computes
``reviewed & chain_set``, never the raw rev-list of a record's range). A
record whose range includes commits outside the currently-walked DAG (e.g.
superseded ancestry, or commits the gate's own ``graph_base`` never
reaches) is reported here as "dropped" even though the live gate never
credited those commits in the first place. **``dropped_shas``/``flip_count``
in this report's output are therefore a PER-RECORD UPPER BOUND, not the
realized gate-time delta** — do not read them as "how many surfaces
actually flip" without independently confirming against a live gate run.
This caveat is also repeated in both ``_format_human``'s rendered output and
the ``--json``/``to_dict()`` shape (as the ``dropped_shas_is_upper_bound``
field) so a machine consumer sees it as data, not only as prose here.

Negative-spec:
    - Reads only. Does NOT rewrite, re-stamp, or delete any review-trail
      record — not one byte (plan's Out-of-scope section, C8).
    - Does NOT auto-fail or auto-block anything. No exit code encodes a
      pass/fail verdict; a non-empty flip list is not itself a failure.
    - Scans ``state/review-trail/*.json`` only (flat, non-recursive) — NOT
      ``archive/review-trail/``. The plan's original 33-record count (21
      chain, 8 workstream-close-auto, 4 session) was itself a live-only
      count; this module re-counts against the same live-only corpus rather
      than silently widening scope to the archive.
    - Does NOT attempt to correlate a flip against a historical
      ``coverage.gate`` run: ``state/coverage/gate-result.json`` is
      overwritten on every gate invocation, so no per-run history survives
      to trace a flip back to a specific past COVERED verdict. Only the
      ceremony evidence trail (``state/ceremony/**/*.json``) is used for
      back-reference, and only when it names this record's path explicitly.

TWO ROOTS, NOT ONE (do not re-collapse these). CANONICAL STATEMENT — this block
is the single authoritative version of this argument; ``op_scopes.py``'s
``review_trail.readjudication_report`` row (~:739-770) carries a deliberately
near-verbatim restatement, because a future editor is at least as likely to
reach for the scope table as for this file when tempted to move the op into
``common_dir``. TWO COPIES EXIST: amend BOTH or neither — an edit to one alone
produces exactly the stale-citation drift ``2fa1c4cf`` was itself about.
    - CORPUS root — the MAIN worktree. ``state/review-trail/`` and
      ``state/ceremony/`` are main-worktree-rooted, exactly as
      ``review_trail_write.py`` and ``scan_unresolved_ubt_records.py`` treat
      them; both derive it via ``main_worktree_root``. This module derives the
      same root from the caller's worktree
      (``main_worktree_root(git_common_dir(caller_worktree))``) rather than
      joining ``state/`` onto whatever root it was handed. Rooting the corpus
      glob at a LINKED worktree finds no records and reports
      ``records_scanned=0, flips=0`` — which is SILENT, because "no surface
      re-opens" is a legitimate result of this report, so an operator reads a
      clean bill of health that was never computed.
    - GIT root — the CALLER's own worktree. ``_full_range_shas``'s
      ``git rev-list`` and the ``Session-Id`` trailer lookup run with
      ``cwd=caller_worktree`` because a record's ``sha_range`` may name
      ``HEAD``, which is per-worktree state (4 of the 29 chain/
      workstream-close-auto records on disk at authoring do exactly that).
      Resolving those ranges from the main worktree would answer about the
      wrong checkout.
    This is why the op stays ``show_top`` in ``op_scopes.py`` and does NOT join
    its two ``review_trail.*`` siblings in ``common_dir``: ``common_dir`` hands
    a handler ``<main-worktree>/.git`` and the caller's worktree is not
    recoverable from it, so the git half would be silently misrooted. That
    table's entry restates this argument at the point of temptation; this
    block remains its canonical home.

Spec backlink: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C8, AC11
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from coordinator_core.coverage import _TrailParseError, _parse_trail_file
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session_attribution import GitLogFailed, trailer_foreign_shas

#: The two scope values C7 newly subjects to foreign-session stripping.
#: ``scope="session"`` is deliberately excluded here — its stripping
#: behavior is unchanged by C7, so it never flips and is out of this
#: report's business.
_C7_NEWLY_STRIPPED_SCOPES: FrozenSet[str] = frozenset({"chain", "workstream-close-auto"})

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run cmd; return (returncode, stdout.strip(), stderr). Never raises.

    Mirrors ``coverage.py``'s own ``_run`` (same subprocess conventions —
    ``stdin=DEVNULL``, ``CREATE_NO_WINDOW`` on Windows) so the git calls this
    module makes behave identically to the ones the coverage gate itself
    makes over the same records.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
        return result.returncode, result.stdout.strip(), result.stderr
    except Exception as exc:
        return 1, "", str(exc)


class GitRevListFailed(RuntimeError):
    """Raised when ``git rev-list <sha_range>`` fails for a record's range."""


def _full_range_shas(sha_range: str, cwd: str) -> FrozenSet[str]:
    """Return every commit `git rev-list` resolves within `sha_range` — the
    record's PRE-C7 credited set for `chain`/`workstream-close-auto` scope
    (the full window, no foreign-session narrowing)."""
    rc, out, err = _run(["git", "rev-list", sha_range], cwd)
    if rc != 0:
        raise GitRevListFailed(f"git rev-list {sha_range!r} failed: {err.strip()}")
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


class CorpusRootUnresolved(RuntimeError):
    """Raised when the corpus's main-worktree root cannot be derived because git
    itself FAILED — as distinct from there being no repository to ask.

    See ``_corpus_root``'s negative-spec for why these two cases must not share
    a code path.
    """


#: git's own signal for "there is no repository here", the ONE case in which
#: falling back to the caller's own root is genuinely harmless (a single-root
#: tree, where the two roots coincide by construction). Matched against the
#: message ``lifecycle.git_common_dir`` embeds from git's stderr. Anything else
#: git says — absent binary, dubious ownership, unreadable config, a broken
#: gitdir pointer, a permissions error — is a FAILURE, not an absence.
_NOT_A_REPO_RE = re.compile(r"not a git repository|not a working tree", re.IGNORECASE)


def _corpus_root(caller_worktree: str) -> Tuple[Path, Optional[str]]:
    """Return ``(main-worktree root, degrade_reason)`` for the corpus that
    ``state/review-trail`` and ``state/ceremony`` are rooted at, derived from
    the caller's worktree.

    Uses the same single reviewed derivation the two sibling review-trail ops
    use (``main_worktree_root``, over ``lifecycle.git_common_dir``) rather than
    a second local resolver — for a regular repo this returns the caller's own
    root, and for a linked worktree it returns the main worktree that actually
    holds the corpus.

    ``degrade_reason`` is None on a clean derivation, and a human-readable
    string when the tolerated no-repository fallback was taken (surfaced to the
    caller as ``corpus_root_degraded``/``corpus_root_degraded_reason``).

    Negative-spec (2026-07-28) — DO NOT re-broaden this to a bare
    ``except (RuntimeError, OSError): return Path(caller_worktree)``. That
    handler could not tell "no git repository here" from "git FAILED on a real
    linked worktree" (git absent from PATH, unreadable config, dubious
    ownership, a broken gitdir pointer, a permissions error). In the second
    case it re-rooted the corpus glob at the caller's LINKED worktree, which
    holds no records, and the op reported ``records_scanned=0, flips=0`` — the
    exact silent "clean bill of health that was never computed" defect this
    module's TWO ROOTS split exists to eliminate, reached by a different route,
    and the direct opposite of the handler's own AC-5 fail-loud contract.

    Only the no-repository case degrades, because there the fallback is an
    identity rather than a guess: with no repo there are no linked worktrees, so
    the caller's root IS the only root, and a legitimate single-root caller (the
    CLI run outside a repo, a tmp-dir unit test) is never broken by failing
    loud. Every other git failure raises ``CorpusRootUnresolved``.
    """
    try:
        return main_worktree_root(git_common_dir(Path(caller_worktree))), None
    except RuntimeError as exc:
        if _NOT_A_REPO_RE.search(str(exc)):
            return Path(caller_worktree), (
                "no git repository at the caller's root; corpus root fell back to "
                f"the caller's own root ({caller_worktree}). git said: {exc}"
            )
        raise CorpusRootUnresolved(
            "cannot derive the review-trail corpus's main-worktree root from "
            f"{caller_worktree!r}: {exc}. Refusing to scan a possibly-wrong tree — "
            "a corpus glob rooted at a linked worktree finds no records and reports "
            "a clean result that was never computed."
        ) from exc
    except OSError as exc:
        raise CorpusRootUnresolved(
            "cannot derive the review-trail corpus's main-worktree root from "
            f"{caller_worktree!r}: git could not be executed ({exc}). Refusing to "
            "scan a possibly-wrong tree."
        ) from exc


def _build_ceremony_index(corpus_root: Path) -> Dict[str, List[str]]:
    """Map review-trail record basename -> ["<ceremony file> @ <emitted_at>", ...]
    for every ceremony record under ``state/ceremony/**/*.json`` whose
    ``review_trail.write`` node evidence names that record's path.

    Best-effort: a malformed or non-ceremony-shaped JSON file is silently
    skipped (this index is a convenience back-reference, not a required
    input — an empty result for a given record just means "no ceremony
    back-reference found on disk", not an error).

    ``corpus_root`` is the MAIN worktree (see the module docstring's TWO ROOTS
    note), never the caller's linked worktree — ``state/ceremony`` lives with
    the corpus, not with the checkout that asked the question.
    """
    index: Dict[str, List[str]] = {}
    ceremony_dir = corpus_root / "state" / "ceremony"
    if not ceremony_dir.is_dir():
        return index
    for path in sorted(ceremony_dir.rglob("*.json")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        nodes = doc.get("nodes")
        if not isinstance(nodes, list):
            continue
        emitted_at = doc.get("emitted_at", "")
        for node in nodes:
            if not isinstance(node, dict) or node.get("resolving_op") != "review_trail.write":
                continue
            evidence = node.get("evidence") or {}
            acted = evidence.get("acted") or []
            if not isinstance(acted, list):
                continue
            for entry in acted:
                if not isinstance(entry, str) or ":" not in entry:
                    continue
                acted_path = entry.split(":", 1)[1]
                basename = Path(acted_path).name
                index.setdefault(basename, []).append(f"{path} @ {emitted_at}")
    return index


@dataclass
class RecordFlip:
    """One review-trail record whose credited SHA set shrinks under C7."""

    record_path: str
    scope: str
    session_id: Optional[str]
    sha_range: str
    dropped_shas: List[str]
    ceremony_references: List[str] = field(default_factory=list)


@dataclass
class ReadjudicationReport:
    """Full corpus re-adjudication delta for a single ``state/review-trail/``
    directory scan.

    ``records_scanned`` — total ``*.json`` files scanned (before scope
    filtering). ``stripped_scope_records`` — the subset with scope in
    ``_C7_NEWLY_STRIPPED_SCOPES`` (the plan's re-counted 21+8=29-at-authoring
    figure, recomputed live). ``flips`` — the subset of those whose credited
    set actually shrinks. ``skipped`` — records that could not be recomputed
    (parse error or git failure), named so a silent gap is never mistaken
    for "no flip".

    Review: code-reviewer — ``flips[].dropped_shas``/``flip_count`` are a
    PER-RECORD UPPER BOUND, not the realized gate-time delta: see the
    module docstring's "UPPER-BOUND CAVEAT" for why (``_full_range_shas``
    is never intersected with the gate's DAG ``chain_set``). Surfaced as
    the ``dropped_shas_is_upper_bound``/``dropped_shas_caveat`` fields in
    ``to_dict()`` and as a banner line in ``_format_human`` so this isn't
    only prose here.
    """

    generated_at: str
    repo_root: str
    records_scanned: int
    stripped_scope_records: int
    flips: List[RecordFlip]
    skipped: List[str] = field(default_factory=list)

    #: The MAIN worktree the scanned corpus (``state/review-trail``,
    #: ``state/ceremony``) was actually read from. Distinct from ``repo_root``,
    #: which names the CALLER's worktree — the checkout whose git history the
    #: ``git rev-list``/``git log`` half of this report reflects. On a regular
    #: repo the two are equal; invoked from a linked worktree they differ, and
    #: reporting only one of them would name the wrong root for half the
    #: report's own content. Defaulted to "" only so the field can be appended
    #: without reordering the existing positional fields; every construction
    #: site sets it.
    corpus_root: str = ""

    #: Machine-readable form of the module docstring's upper-bound caveat —
    #: always True today (see UPPER-BOUND CAVEAT). Kept as a field rather
    #: than only prose so a --json consumer can assert on it without
    #: parsing free text.
    dropped_shas_is_upper_bound: bool = True
    #: True when ``_corpus_root`` took its ONE tolerated fallback — no git
    #: repository at the caller's root, so the corpus root is the caller's own
    #: root. Reported rather than left implicit so a degraded run is never
    #: indistinguishable from a fully-resolved one in the output (a genuine git
    #: FAILURE does not land here at all: it raises ``CorpusRootUnresolved``).
    corpus_root_degraded: bool = False
    corpus_root_degraded_reason: Optional[str] = None

    dropped_shas_caveat: str = (
        "dropped_shas/flip_count are a per-record upper bound: _full_range_shas "
        "is a raw `git rev-list` of the record's sha_range, never intersected "
        "with the gate's DAG chain_set. Some dropped SHAs may already be "
        "outside the walked chain and were never credited by the live gate — "
        "do not read this number as the realized gate-time delta."
    )

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "corpus_root": self.corpus_root,
            "corpus_root_degraded": self.corpus_root_degraded,
            "corpus_root_degraded_reason": self.corpus_root_degraded_reason,
            "records_scanned": self.records_scanned,
            "stripped_scope_records": self.stripped_scope_records,
            "dropped_shas_is_upper_bound": self.dropped_shas_is_upper_bound,
            "dropped_shas_caveat": self.dropped_shas_caveat,
            "flip_count": len(self.flips),
            "flips": [
                {
                    "record_path": f.record_path,
                    "scope": f.scope,
                    "session_id": f.session_id,
                    "sha_range": f.sha_range,
                    "dropped_shas": f.dropped_shas,
                    "ceremony_references": f.ceremony_references,
                }
                for f in self.flips
            ],
            "skipped": self.skipped,
        }


def compute_readjudication_report(repo_root: str) -> ReadjudicationReport:
    """Recompute the C7 corpus re-adjudication delta against the on-disk
    ``state/review-trail/`` corpus. Read-only — makes `git rev-list` /
    `git log` calls but writes nothing.

    ``repo_root`` is the CALLER's worktree. The corpus is read from the main
    worktree derived from it, while every git call keeps ``cwd=repo_root`` —
    see the module docstring's TWO ROOTS note for why those must not be the
    same value on a linked worktree.

    Raises ``CorpusRootUnresolved`` when git FAILS while deriving the corpus
    root (as opposed to there being no repository at all, which degrades
    visibly — see ``_corpus_root``). Failing loud here is the point: a report
    computed against the wrong root is silently empty, and empty is a
    legitimate-looking result of this report.
    """
    corpus_root, corpus_degrade_reason = _corpus_root(repo_root)
    live_dir = corpus_root / "state" / "review-trail"
    paths = sorted(live_dir.glob("*.json")) if live_dir.is_dir() else []

    ceremony_index = _build_ceremony_index(corpus_root)
    session_cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]] = {}

    stripped_scope_records = 0
    flips: List[RecordFlip] = []
    skipped: List[str] = []

    for path in paths:
        str_path = str(path)
        try:
            records = _parse_trail_file(str_path)
        except _TrailParseError as exc:
            skipped.append(f"{str_path}: {exc}")
            continue

        for rec in records:
            # Review: code-reviewer — a syntactically-valid-JSON, wrong-shape
            # record (e.g. a bare JSON array/string/int as the top-level
            # value) is not caught by _TrailParseError and previously raised
            # an uncaught AttributeError here, aborting the ENTIRE corpus
            # scan rather than just this one record. Route it into skipped[]
            # instead, matching this module's own documented degrade contract.
            if not isinstance(rec, dict):
                skipped.append(f"{str_path}: record is not a JSON object ({type(rec).__name__})")
                continue
            scope = rec.get("scope")
            if scope not in _C7_NEWLY_STRIPPED_SCOPES:
                continue
            stripped_scope_records += 1

            sha_range = rec.get("sha_range")
            session_id = rec.get("session_id")
            if not sha_range:
                skipped.append(f"{str_path}: no sha_range on a {scope!r}-scope record")
                continue

            try:
                full_shas = _full_range_shas(sha_range, repo_root)
            except GitRevListFailed as exc:
                skipped.append(f"{str_path}: {exc}")
                continue

            try:
                foreign = trailer_foreign_shas(
                    sha_range, session_id, repo_root, session_cache, run=_run
                )
            except GitLogFailed as exc:
                skipped.append(f"{str_path}: foreign-session lookup failed: {exc}")
                continue

            dropped = sorted(full_shas & foreign)
            if not dropped:
                continue

            flips.append(
                RecordFlip(
                    record_path=str_path,
                    scope=scope,
                    session_id=session_id,
                    sha_range=sha_range,
                    dropped_shas=dropped,
                    ceremony_references=ceremony_index.get(path.name, []),
                )
            )

    return ReadjudicationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        repo_root=repo_root,
        corpus_root=str(corpus_root),
        corpus_root_degraded=corpus_degrade_reason is not None,
        corpus_root_degraded_reason=corpus_degrade_reason,
        records_scanned=len(paths),
        stripped_scope_records=stripped_scope_records,
        flips=flips,
        skipped=skipped,
    )


def _format_human(report: ReadjudicationReport) -> str:
    lines = [
        f"review-trail C7 re-adjudication report — {report.repo_root}",
        f"generated_at={report.generated_at}",
        # Both roots are printed unconditionally: naming only one of them would
        # mislabel half this report whenever it is run from a linked worktree.
        f"corpus_root={report.corpus_root} (state/review-trail, state/ceremony)",
        f"git_root={report.repo_root} (rev-list / Session-Id trailer lookups)",
        # A degraded corpus root is printed as its own line, never folded into the
        # corpus_root line above: an operator must be able to tell a derived root
        # from a fallen-back one at a glance.
        f"WARNING: corpus_root DEGRADED — {report.corpus_root_degraded_reason}"
        if report.corpus_root_degraded
        else "",
        f"records_scanned={report.records_scanned} "
        f"stripped_scope_records={report.stripped_scope_records} "
        f"flips={len(report.flips)}",
        "",
        # Review: code-reviewer — make the upper-bound caveat impossible to
        # miss in the rendered human output, not just the module docstring.
        f"NOTE: {report.dropped_shas_caveat}"
        if report.dropped_shas_is_upper_bound
        else "",
        "",
    ]
    if not report.flips:
        lines.append("No chain/workstream-close-auto record's credited set shrinks under C7.")
    for flip in report.flips:
        lines.append(f"- {flip.record_path}")
        lines.append(f"    scope={flip.scope} session_id={flip.session_id}")
        lines.append(f"    sha_range={flip.sha_range}")
        lines.append(f"    dropped_shas ({len(flip.dropped_shas)}): {', '.join(flip.dropped_shas)}")
        if flip.ceremony_references:
            for ref in flip.ceremony_references:
                lines.append(f"    last relied on by ceremony: {ref}")
        else:
            lines.append(
                "    last relied on by: not derivable from state/ceremony/**/*.json on disk"
            )
    if report.skipped:
        lines.append("")
        lines.append(f"skipped ({len(report.skipped)} records could not be recomputed):")
        for s in report.skipped:
            lines.append(f"  - {s}")
    return "\n".join(lines)


def _resolve_repo_root() -> Optional[str]:
    rc, out, _err = _run(["git", "rev-parse", "--show-toplevel"], None)
    if rc != 0:
        return None
    return out.strip() or None


def main(argv: List[str]) -> int:
    as_json = "--json" in argv
    repo_root = _resolve_repo_root()
    if not repo_root:
        print(
            "review-trail-readjudication-report: cwd is not a git repo",
            file=sys.stderr,
        )
        return 1
    try:
        report = compute_readjudication_report(repo_root)
    except CorpusRootUnresolved as exc:
        # Fail loud rather than print an empty, uncomputed report.
        print(f"review-trail-readjudication-report: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_human(report))
    return 0


@register_op("review_trail.readjudication_report")
async def _review_trail_readjudication_report(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'review_trail.readjudication_report' handler.

    Read-only diagnostic op — delegates to `compute_readjudication_report`.
    No params are required; `repo_root` is the third handler arg supplied by
    the dispatch layer (mirrors coverage_gate.py's own AC-5 contract: no
    os.getcwd() fallback — an unresolved repo_root fails loud rather than
    silently scanning the wrong tree).

    Scope is ``show_top``, so `repo_root` here is the CALLER's own worktree —
    which is what this op needs, because it derives the corpus's main worktree
    from it internally (module docstring, TWO ROOTS) while keeping the caller's
    worktree for its git history reads. ``common_dir`` would supply
    ``<main-worktree>/.git`` instead, from which the caller's worktree is not
    recoverable at all.

    Returns (as the JSON-RPC result dict): the report's `to_dict()` shape,
    plus "error" (str) instead when repo_root is unresolved, or when the corpus
    root cannot be derived because git FAILED (``CorpusRootUnresolved``) — the
    same fail-loud posture, applied to the second of the two roots. A merely
    absent repository is not an error: it degrades visibly via the result's
    ``corpus_root_degraded`` field.
    """
    import asyncio

    if repo_root is None:
        return {
            "error": (
                "review_trail.readjudication_report requires a resolved repo root "
                "(_origin_worktree absent or unresolvable)"
            ),
        }
    try:
        report = await asyncio.to_thread(compute_readjudication_report, str(repo_root))
    except CorpusRootUnresolved as exc:
        return {"error": str(exc)}
    return report.to_dict()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
