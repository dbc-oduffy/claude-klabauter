"""
coordinator_core.plan_assemble.predicates.substrate_scans — Layer 0 Branch B
(b) leaf readers: the `gates.substrate.*` collapse/fix-locus/symbol-liveness/
scan-shaped rows of the `plan-assemble` contract.

Purpose: this module owns the eleven contract rows the wave-2 plan's C4 chunk
assigns to it — `:89`, `:106`, `:107`, `:108`, `:111`, `:112`, `:114`,
`:116`, `:118`, `:120`, `:123`, `:125`, `:159`, `:166` — each a scan over the
plan body's prose, the plan's frontmatter, the sizing object's frontmatter,
or a fixed registry file, with no cross-row dependency. `compute(ctx)` is the
one public entry point: it returns the `gates.substrate.*` subtree this
module contributes, keyed by row group, ready for C13's `residue.py` to
merge alongside every sibling Layer 0/1/2 module's own subtree.

Citation conventions this module reads (documented here because the
contract's rows name a "grep the plan body" shape without a fixed format;
these are this module's own, chosen and applied consistently):
  - A `## Fix locus` (any heading level, case-insensitive) section holds an
    optional `path:line`-shaped citation (`:111`) and an optional
    `Gate type: `<name>`` line (`:112`).
  - `Symbol to replace: `<symbol>`` anywhere in the body names the `:116`
    liveness-check target.
  - `Reintroduces: `<path>`` anywhere in the body names the `:118`
    candidate-evidence target.
  - `Dispatch pattern: `<name>`` anywhere in the body names the `:123`
    registry-grep target.
  - `Port seam: `<name>`` anywhere in the body names the `:125`
    candidate-evidence target.
  - `Mutates symbol: `<symbol>`` anywhere in the body names the `:159`
    consumer-count target.
  - A `## Scaffold checklist` section holds a six-item checklist; items 1-5
    are read for their `[x]`/`[ ]` check state, item 6's mere presence is
    `:166`'s `.item_6_grep_present`.
A citation this module expects and does not find is `undetermined` with a
`reason` naming the missing anchor — never a guess, never `False`.

Negative-spec:
  - `:121` (API-rekey) is WITHDRAWN. DoE's counter was accepted in full and
    they confirmed they had no narrower row in mind. This module emits
    NOTHING for it — no field, no `undetermined` entry, no narrower variant.
    Do not "fix" this omission; it is deliberate.
  - `:118` (`reverses_teardown`) and `:125` (`port_seam`) are CANDIDATE
    EVIDENCE ONLY. Neither row emits a `.verdict` or `.recommended` field —
    "reverses a prior teardown" and "wrong shape for the port" are both
    `G`-classified judgment arms the plan skill's EM decides, never this
    engine. Adding either field back is the AC4 failure mode this whole
    package exists to avoid.
  - `:106` (`collapse.invoked`) CONSUMES `sizing_assemble`'s existing
    `probe.signal` persisted field (`probe_signal="collapse"` at route time)
    off `ctx.sizing_frontmatter`. It is never re-derived from the t-shirt
    ladder or any other input this module could compute independently.
  - `:108` (`collapse.fired_this_pass`) is genuinely new pass-state with no
    existing producer. An absent `ctx.caller_flags["collapse_fired_this_pass"]`
    resolves to `undetermined`, never `False` — a pass in which collapse
    truly did not fire is indistinguishable, from this module's vantage,
    from a pass this context was never told about, so guessing `False` would
    silently misreport the unknown case as a known negative.
  - Does not re-implement `prior-art-checker`, `docs-checker`, or any of the
    other genuine consumes this plan's other chunks own — this module reads
    only the rows listed above.
  - Does not walk `git log` for `:116`/`:120`/`:123`/`:159` — those are pure
    grep/text scans over the current tree. `:118` is the one row in this
    module that does call `git log --follow`, because a negative-search
    candidate needs history, not just current absence.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C4
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

#: File extensions that mark a `scope:` entry as native (non-scripting) code
#: for `:120`'s `native_code_plan` row.
_NATIVE_EXTENSIONS = frozenset(
    {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".cs", ".m", ".mm", ".rs", ".go"}
)

#: The one fixed registry file `:112` and `:123` grep against — the
#: op-name -> owning-module seam every other row-group in this plan's other
#: chunks that names "the registry" also resolves to.
_REGISTRY_RELATIVE_PATH = "coordinator_core/ops/_registry_map.py"

_FIX_LOCUS_HEADING_RE = re.compile(r"^#{1,6}\s*Fix\s+Locus\s*$", re.IGNORECASE | re.MULTILINE)
_SCAFFOLD_CHECKLIST_HEADING_RE = re.compile(
    r"^#{1,6}\s*Scaffold\s+Checklist\s*$", re.IGNORECASE | re.MULTILINE
)
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

_FIX_LOCUS_CITATION_RE = re.compile(r"([\w./-]+\.\w+:\d+)")
_GATE_TYPE_RE = re.compile(r"Gate\s+type:\s*`([^`]+)`", re.IGNORECASE)
_SYMBOL_TO_REPLACE_RE = re.compile(r"Symbol\s+to\s+replace:\s*`([^`]+)`", re.IGNORECASE)
_REINTRODUCES_RE = re.compile(r"Reintroduces:\s*`([^`]+)`", re.IGNORECASE)
_DISPATCH_PATTERN_RE = re.compile(r"Dispatch\s+pattern:\s*`([^`]+)`", re.IGNORECASE)
_PORT_SEAM_RE = re.compile(r"Port\s+seam:\s*`([^`]+)`", re.IGNORECASE)
_MUTATES_SYMBOL_RE = re.compile(r"Mutates\s+symbol:\s*`([^`]+)`", re.IGNORECASE)
_CHECKLIST_ITEM_RE = re.compile(r"^\s*\d+\.\s*\[( |x|X)\]", re.MULTILINE)

#: `<repo>@<sha>` — the well-formed peer-SHA citation shape `:89` requires.
#: A hex-looking token of this length NOT preceded by an `@` is the bare-SHA
#: violation the row lints for.
_SHA_TOKEN_RE = re.compile(r"(?<![\w@])([0-9a-f]{7,40})\b")
_HEX_WITH_LETTER_RE = re.compile(r"[a-f]")

#: Leading-only context window (chars immediately BEFORE a candidate
#: token) scanned by the session-id discriminator below. No trailing
#: window: a session id follows its field marker, never precedes it.
_SESSION_ID_CONTEXT_WINDOW = 15

#: A candidate token sitting immediately after one of these field-shaped
#: markers, within `_SESSION_ID_CONTEXT_WINDOW` chars, is read as a session
#: id, not a SHA — matches how session ids actually appear in plan bodies:
#: frontmatter keys and commit trailers (`sent_by:`, `Session-Id:`,
#: `picked_up_by:`), never bare prose. The bare `\bsession\b` alternative
#: was dropped: matched symmetrically it suppressed a genuine bare peer SHA
#: in ordinary prose like "session 958cf906 landed a59f8d79c". Cheap, no
#: I/O; see `_peer_sha_lint`'s docstring for why this is the only
#: session-id discriminator this row implements.
_SESSION_ID_CONTEXT_RE = re.compile(
    r"sent[_-]?by|session[-_ ]?id|picked[_-]?up[_-]?by",
    re.IGNORECASE,
)

#: `:89`'s known-limit caveat, carried in the emitted payload alongside
#: `.fires`/`.bad_citations` so a consumer reading the gate object (not
#: this docstring) still sees it — agent-facing register per
#: `docs/wiki/guard-messaging.md` § Register: one fact, stated once, no
#: apology, no self-legitimacy.
_PEER_SHA_LINT_KNOWN_LIMIT = (
    "bad_citations is candidate evidence, not a verdict — a hex token that "
    "does not resolve as a commit in this repo is still treated as a "
    "candidate peer citation, so an unresolvable own-repo SHA (e.g. from a "
    "shallow clone) can still land here. A session id written in bare "
    "prose with no field-shaped marker (sent_by/session id/picked_up_by) "
    "immediately before it is also indistinguishable from a short SHA and "
    "can still land here. Do not gate on fires."
)


def _section_body(body: str, heading_re: re.Pattern[str]) -> Optional[str]:
    """Return the text between a matched heading and the next heading (or
    end of document), or `None` if the heading is absent."""
    match = heading_re.search(body)
    if match is None:
        return None
    rest = body[match.end():]
    next_heading = _HEADING_RE.search(rest)
    return rest[: next_heading.start()] if next_heading else rest


def _peer_sha_lint(ctx: PredicateContext) -> dict[str, Any]:
    """:89 — `gates.substrate.peer_sha_lint.fires`/`.bad_citations`.

    Flags a bare hex token not preceded by a `<repo>@` prefix, EXCLUDING
    two classes that are not the genuine violation:
      1. a token that resolves as a commit in THIS repo (correctly cited
         bare, house style — resolved via one batched
         `git cat-file --batch-check` call over every candidate token, not
         a per-token shell-out; see `_resolve_local_shas`), and
      2. a token immediately following a field-shaped session-id marker
         (`sent_by:`, `Session-Id:`, `picked_up_by:`) within
         `_SESSION_ID_CONTEXT_WINDOW` chars, leading-only — cheap, no I/O
         (see `_SESSION_ID_CONTEXT_RE`).

    Backticked tokens ARE matched: the token pattern's lookbehind excludes
    `\\w` and `@`, and a backtick is neither.

    KNOWN LIMIT, deliberately not fixed here — a session id written in
    bare prose with no field-shaped marker immediately before it is
    shape-ambiguous with a short SHA and will still be flagged (a
    retained false positive, the deliberate trade for closing the
    symmetric-window false negative this row used to have), and (rarer) a
    genuinely own-repo SHA the local clone cannot resolve (e.g. a shallow
    clone missing that commit) reads as an unresolvable candidate. Both
    are `.known_limit` in the emitted payload (see
    `_PEER_SHA_LINT_KNOWN_LIMIT`), not silently absorbed as if fixed.

    That is survivable only because `.bad_citations` is CANDIDATE EVIDENCE
    an EM reads, never a verdict — do not promote it to one, and do not
    gate anything on `.fires` alone."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    candidates: list[tuple[str, int]] = []
    for match in _SHA_TOKEN_RE.finditer(ctx.plan_body):
        token = match.group(1)
        if not _HEX_WITH_LETTER_RE.search(token):
            continue  # an all-digit token is not distinguishably a SHA
        preceding = ctx.plan_body[max(0, match.start() - 80): match.start()]
        if re.search(r"[\w./-]+@$", preceding):
            continue  # already well-formed `<repo>@<sha>`
        window_start = max(0, match.start() - _SESSION_ID_CONTEXT_WINDOW)
        window = ctx.plan_body[window_start: match.start()]
        if _SESSION_ID_CONTEXT_RE.search(window):
            continue  # immediately follows a session-id marker — not a SHA candidate
        candidates.append((token, match.start()))

    if not candidates:
        return {"fires": False, "bad_citations": [], "known_limit": _PEER_SHA_LINT_KNOWN_LIMIT}

    tokens = [token for token, _ in candidates]
    resolved = _resolve_local_shas(ctx.repo_root, tokens)
    if resolved is None:
        return undetermined(
            "git cat-file --batch-check failed or timed out; cannot resolve "
            "repo-of-origin for candidate SHA tokens"
        )

    bad_citations = [token for token in tokens if not resolved.get(token, False)]

    return {
        "fires": bool(bad_citations),
        "bad_citations": bad_citations,
        "known_limit": _PEER_SHA_LINT_KNOWN_LIMIT,
    }


def _resolve_local_shas(repo_root: Path, tokens: list[str]) -> Optional[dict[str, bool]]:
    """`{token: True}` for every token in `tokens` that resolves as a commit
    in `repo_root`'s own history, `False` for one that does not — via ONE
    batched `git cat-file --batch-check` call over stdin, never a
    per-token shell-out (this module's fast-path posture, matching
    `_git_grep_count`'s sibling budget constraint). Returns `None` (never a
    partial/best-effort dict) on any git failure — not a repo, git
    missing, or a timeout — so the caller degrades the whole row to
    `undetermined` rather than resurrecting the false-positive storm by
    treating every candidate as unresolvable."""
    if not tokens:
        return {}
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=repo_root,
            input="\n".join(f"{token}^{{commit}}" for token in tokens) + "\n",
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    lines = result.stdout.splitlines()
    if len(lines) != len(tokens):
        return None  # unexpected shape — do not guess a partial mapping

    resolved: dict[str, bool] = {}
    for token, line in zip(tokens, lines):
        resolved[token] = not line.endswith("missing") and " commit" in line
    return resolved


def _collapse(ctx: PredicateContext) -> dict[str, Any]:
    """:106/:107/:108 — `gates.substrate.collapse.*`."""
    if ctx.sizing_frontmatter is None:
        invoked: Any = undetermined("no sizing object available (--sizing-object not supplied)")
    else:
        probe = ctx.sizing_frontmatter.get("probe")
        signal = probe.get("signal") if isinstance(probe, dict) else None
        invoked = signal == "collapse"

    if isinstance(invoked, bool):
        route_violation: Any = invoked and ctx.resolved_route in ("roadmap", "pm-decision")
    else:
        route_violation = undetermined(
            "collapse.invoked is undetermined; route_reachability_violation cannot be derived"
        )

    fired_this_pass_flag = ctx.caller_flags.get("collapse_fired_this_pass")
    if fired_this_pass_flag is None:
        fired_this_pass: Any = undetermined(
            "caller_flags['collapse_fired_this_pass'] not supplied"
        )
    else:
        fired_this_pass = bool(fired_this_pass_flag)

    return {
        "invoked": invoked,
        "route_reachability_violation": route_violation,
        "fired_this_pass": fired_this_pass,
    }


def _fix_locus(ctx: PredicateContext) -> dict[str, Any]:
    """:111/:112 — `gates.substrate.fix_locus.*`."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    section = _section_body(ctx.plan_body, _FIX_LOCUS_HEADING_RE)
    if section is None:
        return undetermined("no '## Fix Locus' section found in plan body")

    citation_match = _FIX_LOCUS_CITATION_RE.search(section)
    citation_present = citation_match is not None
    citation = citation_match.group(1) if citation_match else None

    gate_type_match = _GATE_TYPE_RE.search(section)
    if gate_type_match is None:
        registry_has_gate_type: Any = undetermined(
            "no 'Gate type: `<name>`' line found in the Fix Locus section"
        )
    else:
        registry_path = ctx.repo_root / _REGISTRY_RELATIVE_PATH
        try:
            registry_text = registry_path.read_text(encoding="utf-8")
        except OSError:
            registry_has_gate_type = undetermined(
                f"registry file unreadable: {registry_path}"
            )
        else:
            registry_has_gate_type = gate_type_match.group(1) in registry_text

    return {
        "citation_present": citation_present,
        "citation": citation,
        "registry_has_gate_type": registry_has_gate_type,
    }


def _citations_verified(ctx: PredicateContext) -> Any:
    """:114 — `gates.substrate.citations_verified`. CONSUMES the existing
    `ops.doc_content_verify` inline citation checker over the plan body.
    Passes `doc_relative_checker` (not just `repo_exists`) so a plan citing
    a path relative to its OWN directory — rather than repo-root-relative —
    resolves correctly instead of reading as a false `absent`/fabrication.
    Review: code-reviewer — Finding (P2). Does not pass `sibling_checkers`
    or `read_target_text`: this predicate checks fabrication over a plan
    BODY, not a doc with markdown-link anchors, and cross-repo citations are
    intentionally out of scope here (matches `verify_doc`'s own negative-spec
    that `resolves-cross-repo` is never a Finding either way)."""
    if ctx.plan_body is None or ctx.plan_path is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    from coordinator_core.ops.doc_content_verify import (
        _default_checker,
        _doc_relative_checker,
        verify_doc,
    )

    try:
        doc_rel_path = ctx.plan_path.relative_to(ctx.repo_root).as_posix()
    except ValueError:
        doc_rel_path = ctx.plan_path.name

    findings = verify_doc(
        doc_rel_path,
        ctx.plan_body,
        repo_exists=_default_checker(ctx.repo_root),
        doc_relative_checker=_doc_relative_checker(ctx.repo_root, doc_rel_path),
    )
    return not findings


def _symbol_liveness(ctx: PredicateContext) -> dict[str, Any]:
    """:116 — `gates.substrate.symbol_liveness.live`. CANDIDATE grep, not a
    verdict on whether the symbol should still be alive."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    match = _SYMBOL_TO_REPLACE_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("no 'Symbol to replace: `<symbol>`' line found in plan body")

    symbol = match.group(1)
    live = _grep_repo(ctx.repo_root, symbol)
    return {"live": live}


def _reverses_teardown(ctx: PredicateContext) -> dict[str, Any]:
    """:118 — `gates.substrate.reverses_teardown.candidate`. CANDIDATE
    EVIDENCE ONLY — never a `.verdict`/`.recommended` field. Whether this
    genuinely reverses a prior teardown is the `G`-classified judgment arm
    the EM decides, not this module."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    match = _REINTRODUCES_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("no 'Reintroduces: `<path>`' line found in plan body")

    artifact = match.group(1)
    target = (ctx.repo_root / artifact) if not artifact.startswith("/") else Path(artifact)
    if target.exists():
        return {"candidate": False}

    history = _git_log_follow(ctx.repo_root, artifact)
    return {"candidate": bool(history)}


def _native_code_plan(ctx: PredicateContext) -> Any:
    """:120 — `gates.substrate.native_code_plan`. Static extension check
    over the plan's own `scope:` list — no read of file contents."""
    if ctx.plan_frontmatter is None:
        return undetermined("no plan frontmatter available (--plan not supplied or unparseable)")

    scope = ctx.plan_frontmatter.get("scope")
    if not isinstance(scope, list):
        return undetermined("plan frontmatter has no 'scope:' list")

    return any(
        isinstance(entry, str) and Path(entry).suffix.lower() in _NATIVE_EXTENSIONS
        for entry in scope
    )


def _registered_dispatch_added(ctx: PredicateContext) -> Any:
    """:123 — `gates.substrate.registered_dispatch_added`."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    match = _DISPATCH_PATTERN_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("no 'Dispatch pattern: `<name>`' line found in plan body")

    registry_path = ctx.repo_root / _REGISTRY_RELATIVE_PATH
    try:
        registry_text = registry_path.read_text(encoding="utf-8")
    except OSError:
        return undetermined(f"registry file unreadable: {registry_path}")

    return match.group(1) in registry_text


def _port_seam(ctx: PredicateContext) -> dict[str, Any]:
    """:125 — `gates.substrate.port_seam.exists`. CANDIDATE EVIDENCE ONLY —
    "wrong shape for the port" stays the `G`-classified judgment arm."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    match = _PORT_SEAM_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("no 'Port seam: `<name>`' line found in plan body")

    seam = match.group(1)
    exists = (ctx.repo_root / seam).exists() or _grep_repo(ctx.repo_root, seam)
    return {"exists": exists}


def _shared_symbol_mutation(ctx: PredicateContext) -> dict[str, Any]:
    """:159 — `gates.substrate.mutates_shared_symbol`/`.consumer_count`."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    match = _MUTATES_SYMBOL_RE.search(ctx.plan_body)
    if match is None:
        return undetermined("no 'Mutates symbol: `<symbol>`' line found in plan body")

    symbol = match.group(1)
    consumer_count = _grep_repo_count(ctx.repo_root, symbol)
    return {"mutates_shared_symbol": consumer_count > 1, "consumer_count": consumer_count}


def _scaffold_checklist(ctx: PredicateContext) -> dict[str, Any]:
    """:166 — `gates.substrate.scaffold_checklist.items_1_5`/
    `.item_6_grep_present`."""
    if ctx.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    section = _section_body(ctx.plan_body, _SCAFFOLD_CHECKLIST_HEADING_RE)
    if section is None:
        return undetermined("no '## Scaffold Checklist' section found in plan body")

    items = _CHECKLIST_ITEM_RE.findall(section)
    if len(items) < 5:
        return undetermined(
            f"Scaffold Checklist section has {len(items)} item(s), fewer than the 5 required"
        )

    items_1_5 = [state.lower() == "x" for state in items[:5]]
    item_6_grep_present = len(items) >= 6

    return {"items_1_5": items_1_5, "item_6_grep_present": item_6_grep_present}


def _grep_repo(repo_root: Path, token: str) -> bool:
    """`True` if a literal grep for `token` finds any hit under `repo_root`."""
    return _grep_repo_count(repo_root, token) > 0


#: Directory names never descended into by `_grep_repo_count`'s pure-Python
#: fallback walk — VCS internals and interpreter caches, never a legitimate
#: consumer.
_GREP_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})

#: Per-invocation memo for `_grep_repo_count`, keyed by `(repo_root, token)`.
#: This process is spawn-per-call (`plan-assemble` never runs as a resident
#: daemon), so a plain module-level dict is correctly scoped to one CLI
#: invocation without any explicit reset — the process exits and the dict
#: goes with it. Populated regardless of which path (`git grep` or the pure-
#: Python walk) answered the count.
_GREP_REPO_COUNT_CACHE: dict[tuple[str, str], int] = {}


def _git_grep_count(repo_root: Path, token: str) -> Optional[int]:
    """Number of files under `repo_root` containing a literal-string hit for
    `token`, via `git grep -F -c` — the fast path.

    Review: code-reviewer — Finding (P2), integrator disposition. A prior
    revision on this workstream replaced this native `git grep` call with a
    `git ls-files` enumeration plus a per-file, in-process Python
    `read_text` + substring scan, on the stated rationale that plain
    `git grep` "only searches Git-tracked... content by default". That
    rationale was misleading: this function's `--untracked
    --no-exclude-standard` flags ALREADY widen `git grep`'s own search to
    the tracked + untracked + gitignored universe (see below) — the
    tracked-only gap the rewrite described was already closed on this same
    line before the rewrite touched it. Restored to the single native `git
    grep` spawn: it is one process, not up to one Python-side
    `read_text` per repo file, and this module's own stated cost budget
    (`_grep_repo_count`'s docstring) is up to four whole-repo scans per
    `plan-assemble brief --plan` invocation on a machine whose documented
    norm is 50-70 concurrent LLM sessions sharing one disk — a real cost a
    native `git grep` avoids and a Python content-read loop does not.
    `--untracked --no-exclude-standard` widen `git grep`'s search to the
    SAME universe the fallback walks (tracked + untracked + gitignored,
    modulo `.git/` itself, which `git grep` never descends into either
    way) — but the two paths do NOT agree on results in general, only on
    this module's actual call-site input class.

    Review: code-reviewer — Findings (P2/P3), integrator disposition. Two
    concrete divergences, both real and both currently unexercised:
    (1) binary files — `git grep` searches binary byte content by default
    (it only suppresses printing binary *match text*, not matching itself;
    `-a`/`--text` controls the former, not the latter), while the fallback
    (`_grep_repo_count`, below) explicitly `continue`s past
    `UnicodeDecodeError` and never counts an undecodable file as a hit;
    (2) multi-line tokens — `git grep` matches per line, while the
    fallback's `token in text` check is a whole-file substring test that
    would match a token embedding a literal newline across two lines. Ever
    call site (`:441`'s `seam`, `:455`'s `symbol`) passes a single-line
    identifier/symbol-shaped token extracted by regex from plan-body text —
    never raw binary content, never a token containing `\n` — so today's
    call sites are unaffected by either divergence, but that is a property
    of the current callers, not a guarantee this function makes.

    Returns `None` (never `0`) on ANY failure to run `git grep` as a real
    count — not a git repo, `git` not on PATH, a timeout, or an unexpected
    exit code — so the caller falls back to the pure-Python walk rather
    than silently reporting a false negative as "zero hits"."""
    try:
        result = subprocess.run(
            [
                "git",
                "grep",
                "--fixed-strings",
                "--count",
                "--untracked",
                "--no-exclude-standard",
                "--",
                token,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 1:
        return 0  # `git grep` found nothing; a real, legitimate zero.
    if result.returncode != 0:
        return None  # not a git repo, git missing, or another real failure.
    # `-c`/`--count` emits one `path:count` line per matching file; the
    # number of files hit is the number of lines, not the sum of counts.
    return len(result.stdout.splitlines())


def _grep_repo_count(repo_root: Path, token: str) -> int:
    """Number of files under `repo_root` containing a literal-string hit
    for `token`.

    Cost profile: this repo's ops are spawn-per-call and held to an
    end-to-end invocation budget, on a machine whose documented norm is
    50-70 concurrent LLM sessions sharing one disk (`docs/wiki/
    machine-load-norm.md`). This function backs up to four contract rows
    per `plan-assemble brief --plan` call (`:116`/`:123`/`:159`, plus
    `:125`'s existence fallback), so a naive full-tree byte-read walk here
    is up to four whole-repo scans per invocation — unacceptable at that
    load. `git grep` (native, and asked for `--untracked
    --no-exclude-standard` so it covers the SAME universe — tracked,
    untracked, and gitignored files alike — the fallback walk does, not
    tracked-only; see `_git_grep_count`'s docstring) is the fast path; the
    pure-Python `rglob` + `read_text` walk below is kept ONLY as the
    fallback for a target that is not a git repository at all — a shape
    this module's own test fixtures rely on, since they build plain
    `tmp_path` directories, never `git init`. Both paths prune `.git/`,
    `__pycache__/`, and other VCS/cache directories. They do NOT both skip
    binary content: `git grep` matches binary byte content by default (it
    only suppresses printing binary match *text*, not matching itself),
    while this Python walk `continue`s past `UnicodeDecodeError` and never
    counts a binary file as a hit — see `_git_grep_count`'s docstring for
    why this and the multi-line-token divergence don't affect this
    module's actual call sites.
    Results are memoized per `(repo_root, token)` for the lifetime of this
    process, so the same token is never walked twice in one invocation.
    """
    cache_key = (str(repo_root), token)
    cached = _GREP_REPO_COUNT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    fast_count = _git_grep_count(repo_root, token)
    if fast_count is not None:
        _GREP_REPO_COUNT_CACHE[cache_key] = fast_count
        return fast_count

    count = 0
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _GREP_SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if token in text:
            count += 1

    _GREP_REPO_COUNT_CACHE[cache_key] = count
    return count


def _git_log_follow(repo_root: Path, path: str) -> list[str]:
    """`git log --follow --oneline -- <path>` output lines, or `[]` on any
    git failure (not a git repo, path never existed, etc.)."""
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--oneline", "--", path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def compute(ctx: PredicateContext) -> dict[str, Any]:
    """Compute this module's `gates.substrate.*` subtree.

    Returns a flat dict keyed by row group (`peer_sha_lint`, `collapse`,
    `fix_locus`, `citations_verified`, `symbol_liveness`,
    `reverses_teardown`, `native_code_plan`, `registered_dispatch_added`,
    `port_seam`, `mutates_shared_symbol`, `scaffold_checklist`) — every key
    resolves to either a populated value/dict or the `undetermined`
    sentinel, per AC2. `:121` is withdrawn and contributes no key at all;
    `:43` is not this module's row.
    """
    return {
        "peer_sha_lint": _peer_sha_lint(ctx),
        "collapse": _collapse(ctx),
        "fix_locus": _fix_locus(ctx),
        "citations_verified": _citations_verified(ctx),
        "symbol_liveness": _symbol_liveness(ctx),
        "reverses_teardown": _reverses_teardown(ctx),
        "native_code_plan": _native_code_plan(ctx),
        "registered_dispatch_added": _registered_dispatch_added(ctx),
        "port_seam": _port_seam(ctx),
        "mutates_shared_symbol": _shared_symbol_mutation(ctx),
        "scaffold_checklist": _scaffold_checklist(ctx),
    }


__all__ = ["compute"]
