"""
coordinator_core.hooks.guard_terminal_review — Stop-hook engine op, the
terminal review gate: a session whose OWN commits carry an unreviewed code
diff may not stop.

Purpose: `/workstream-complete`'s review requirement only fires when that
skill is invoked, which measured 6 of 38 runs (`experiments@6e2c5ba`,
`coordinator-recipe/diagnosis/gate-fidelity.md`) — 16 of those 38 runs had
no code review of the diff at all. This op makes the requirement mechanical
by firing on the EM's own Stop, independent of whether any skill ran.

Trigger conditions (all must hold to refuse a Stop):
  1. The Stop is the EM's own — a payload with `agent_id` set, or
     `stop_hook_active` set, is skipped (same two-leg platform contract as
     every peer blocking guard, e.g. `guard_kira_verdict_routed`).
  2. The session has at least one CODE-diff commit of its own — a commit
     whose `Session-Id` trailer equals the payload's `session_id`, and at
     least one touched path is CODE. A path is non-code (exempt) when it is
     bookkeeping (`coverage._is_bookkeeping_path`, plus the two gate-local
     prefixes `docs/plans/` and `.coordinator-local/`) OR doc-only by the
     Scale ladder's own rule (suffix `.md`/`.yaml`/`.yml`, wherever it
     sits). A mixed commit (any code path present) counts as code and fails
     closed.
  3. At least one such code commit is uncredited by all three arms below.
  4. On refusal, the message names up to five short SHAs (then a count) and
     the two discharges: dispatch a code-reviewer, or make a follow-up
     commit carrying a valid `Inline-Review:` trailer. It never says
     "amend" (a follow-up commit is always the correct discharge — an
     amend on a pushed branch is a history rewrite, and scoped-commit may
     not admit one anyway).
  5. Fail OPEN on any git/read failure: the Stop passes and (via the
     `deny`/`post_advisory`/`no_advisory` handler contract only — see
     below) a one-line advisory breadcrumb names why.

Three credit arms, any one of which covers a code commit:
  - Receipt credit — `review_trail.receipt_credit.receipt_credited_shas`,
    reused unchanged. A counting reviewer receipt in this session's share
    dir, stamped no earlier than the commit.
  - Window credit (new here) — the commit's `committed_at` falls inside
    `[review_receipt.stamped_at, review_completion.stamped_at]` of one
    counting review-shaped sidecar of this session, where "counting
    review-shaped" means the receipt's bare `agent_type` is a member of
    `reviewer_vocabulary.DELEGATE_REVIEWERS` — the same allowlist
    `receipt_credit` treats as a counting reviewer. A plan-review or other
    non-code-review sidecar's window credits nothing.
  - Inline credit, session-wide — a valid `Inline-Review:` trailer on ANY
    of this session's own commits (code or bookkeeping) credits every
    otherwise-uncredited code commit of the session:
      * `em-verified — <statement, >=20 chars>` — self-review. Credits
        only while the gross code LOC (doc-only paths excluded) summed
        over every session code commit it would credit is at most
        `_EM_VERIFIED_LOC_CEILING` (50). This binds only the LOC leg of
        the Scale ladder's `code-reviewer` trigger — the executor-
        dispatched leg and the shared-schema leg are not re-derived here
        (known limit; `/workstream-complete` still owns those).
      * `applies <sidecar-stem> — <statement, >=20 chars>` — the EM
        applied a reviewer's findings itself. Credits only if
        `<sidecar-stem>` resolves to a counting reviewer sidecar of this
        session; not ceiling-bound, because the review ran.

Two entry points:
  - `op(payload) -> {"message": str} | None` — the pointer-shim contract
    (`_engine_root.run_stop_hook_pointer_shim`, DoE's C3). Returns a
    message ONLY on a genuine refusal; every other path (pass, skip, or a
    fail-open advisory) returns None, because the pointer shim treats ANY
    truthy `message` as blocking (exit 2) — an advisory must never travel
    through this channel or a fail-open would wrongly block the turn.
  - `_guard_terminal_review_handler(params) -> deny(...) | post_advisory(...)
    | no_advisory()` — the `guard_kira_verdict_routed`-shaped handler C2
    composes into the engine's own `stop_dispatch` fold set. This is the
    channel that DOES surface the fail-open advisory breadcrumb, since
    `post_advisory` is non-blocking by construction.

Budget: the hit path makes exactly ONE git spawn (`git log`, `--numstat`
plus per-commit trailer fields, bounded to `_COMMIT_WALK_BOUND` commits).
The op imports only stdlib plus already-existing coordinator_core modules;
no new dependency.

Cross-plan contract with `retire-review-integrator` (062436): this module
reads receipts and windows, never an agent shape — no integrator artifact
(`integrated_from`, `## Integrator Dispositions`) is read as proof of
review, so deleting `review-integrator` removes nothing this gate needs.

Spec backlink: docs/plans/2026-09-26-terminal-review-gate-mechanical.md § C1
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.coverage import _is_bookkeeping_path
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, post_advisory
from coordinator_core.ipc import register_op
from coordinator_core.review_trail.receipt_credit import receipt_credited_shas
from coordinator_core.session.machinery_paths import share_dirs as _share_dirs
from coordinator_core.win_portability import no_console_creationflags

#: How far back one `git log` spawn walks looking for this session's own
#: commits. A session whose own commits sit further back than this is a
#: known miss — cheap direction (see module docstring § budget).
_COMMIT_WALK_BOUND = 500

#: The LOC leg of the Scale ladder's own `code-reviewer` trigger
#: ("executor dispatched, or >50 LOC, or shared-schema touched") — the only
#: leg mechanically readable at Stop. See module docstring's known limit.
_EM_VERIFIED_LOC_CEILING = 50

#: Two gate-local non-code prefixes, additional to `_is_bookkeeping_path`'s
#: own set (`state/`, `archive/`, `tasks/`, `cross-repo/`). `docs/plans/` is
#: exempt because plan review is its own gate; `.coordinator-local/` is
#: coordinator machinery, never authored code.
_GATE_LOCAL_NONCODE_PREFIXES = ("docs/plans/", ".coordinator-local/")

#: Scale ladder's own doc-only rule: a path with one of these suffixes is
#: doc-only WHEREVER it sits — the decision that doctrine markdown is not
#: gated (§ Design condition 2).
_DOC_ONLY_SUFFIXES = (".md", ".yaml", ".yml")

_GIT_TIMEOUT_SECS = 30
_CREATIONFLAGS = no_console_creationflags()

_HEADER_SENTINEL = "\x02"
_FIELD_SEP = "\x1f"
_MULTI_SEP = "\x1e"

_EM_VERIFIED_RE = re.compile(r"^em-verified\s*—\s*(.*)$")
_APPLIES_RE = re.compile(r"^applies\s+(\S+)\s*—\s*(.*)$")
_MIN_STATEMENT_CHARS = 20

_GRAMMAR_FORMAT = (
    f"{_HEADER_SENTINEL}%H{_FIELD_SEP}%cI{_FIELD_SEP}"
    f"%(trailers:key=Session-Id,valueonly=true,unfold=true,separator={_MULTI_SEP})"
    f"{_FIELD_SEP}"
    f"%(trailers:key=Inline-Review,valueonly=true,unfold=true,separator={_MULTI_SEP})"
)


class _GitUnavailable(Exception):
    """Raised internally when the one git spawn this op makes fails or the
    git binary is absent — never escapes `_core`; converted to the
    'advisory' tri-state there."""


@dataclass
class _Commit:
    sha: str
    committed_at_iso: str
    committed_at: Optional[datetime]
    touched_paths: List[str] = field(default_factory=list)
    code_loc: int = 0
    is_code: bool = False
    inline_review: List[str] = field(default_factory=list)


def _is_code_path(path: str) -> bool:
    """True when `path` must count toward the gate — i.e. it is neither
    bookkeeping nor doc-only by the Scale ladder's suffix rule."""
    if _is_bookkeeping_path(path):
        return False
    if any(path.startswith(prefix) for prefix in _GATE_LOCAL_NONCODE_PREFIXES):
        return False
    if path.endswith(_DOC_ONLY_SUFFIXES):
        return False
    return True


def _parse_timestamp(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _bare_agent_type(agent_type) -> Optional[str]:
    if not isinstance(agent_type, str):
        return None
    return agent_type.rpartition(":")[2] if ":" in agent_type else agent_type


def _repo_root(payload: dict) -> Optional[str]:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return show_toplevel(cwd)


def _run_git(args: List[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitUnavailable(str(exc)) from exc
    if result.returncode != 0:
        raise _GitUnavailable(result.stderr.strip() or "git log failed")
    return result.stdout


def _session_commits(repo_root: str, session_id: str) -> List[_Commit]:
    """This session's own commits, oldest-first, within `_COMMIT_WALK_BOUND`
    of HEAD, from ONE `git log` spawn. A commit is included only when its
    `Session-Id` trailer equals `session_id` exactly."""
    out = _run_git(
        [
            "log",
            "--no-merges",
            f"-{_COMMIT_WALK_BOUND}",
            "--numstat",
            f"--pretty=format:{_GRAMMAR_FORMAT}",
        ],
        cwd=repo_root,
    )

    commits: List[_Commit] = []
    current: Optional[_Commit] = None
    keep_current = False

    for line in out.splitlines():
        if line.startswith(_HEADER_SENTINEL):
            header = line[len(_HEADER_SENTINEL):]
            sha, _, rest = header.partition(_FIELD_SEP)
            committed_at_iso, _, rest2 = rest.partition(_FIELD_SEP)
            session_field, _, inline_field = rest2.partition(_FIELD_SEP)
            session_ids = [s for s in session_field.split(_MULTI_SEP) if s]
            inline_reviews = [s for s in inline_field.split(_MULTI_SEP) if s]
            keep_current = session_id in session_ids
            if keep_current:
                current = _Commit(
                    sha=sha,
                    committed_at_iso=committed_at_iso,
                    committed_at=_parse_timestamp(committed_at_iso),
                    inline_review=inline_reviews,
                )
                commits.append(current)
            else:
                current = None
            continue

        if not keep_current or current is None:
            continue

        if not line.strip():
            continue

        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        path = path.replace("\\", "/")
        if path not in current.touched_paths:
            current.touched_paths.append(path)
        if _is_code_path(path):
            current.is_code = True
            added = int(added_raw) if added_raw.isdigit() else 0
            deleted = int(deleted_raw) if deleted_raw.isdigit() else 0
            current.code_loc += added + deleted

    return commits


def _window_credited(
    repo_root: str, session_id: str, code_commits: List[_Commit]
) -> Set[str]:
    """SHAs of `code_commits` whose `committed_at` falls inside a counting
    reviewer sidecar's `[review_receipt.stamped_at, review_completion.
    stamped_at]` window (§ Design, Window credit). Sidecars that are not
    review-shaped by `DELEGATE_REVIEWERS`, or that lack either block, or
    whose `session_id` does not match, lend nothing."""
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter
    from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS

    credited: Set[str] = set()
    if not code_commits:
        return credited

    for share_dir in _share_dirs(repo_root, session_id):
        try:
            filenames = [f for f in os.listdir(share_dir) if f.endswith(".md")]
        except OSError:
            continue
        for fname in filenames:
            try:
                with open(
                    os.path.join(share_dir, fname), "r", encoding="utf-8", errors="replace"
                ) as fh:
                    text = fh.read()
            except OSError:
                continue
            try:
                parsed = parse_frontmatter(text)
            except Exception:
                continue
            frontmatter = parsed.get("frontmatter")
            if not isinstance(frontmatter, dict):
                continue
            receipt = frontmatter.get("review_receipt")
            completion = frontmatter.get("review_completion")
            if not isinstance(receipt, dict) or not isinstance(completion, dict):
                continue
            if receipt.get("session_id") != session_id:
                continue
            if completion.get("session_id") != session_id:
                continue
            bare = _bare_agent_type(receipt.get("agent_type"))
            if bare is None or bare not in DELEGATE_REVIEWERS:
                continue
            start = _parse_timestamp(receipt.get("stamped_at"))
            end = _parse_timestamp(completion.get("stamped_at"))
            if start is None or end is None:
                continue
            for commit in code_commits:
                if commit.committed_at is None:
                    continue
                if start <= commit.committed_at <= end:
                    credited.add(commit.sha)

    return credited


def _resolve_applies_sidecar(repo_root: str, session_id: str, stem: str) -> bool:
    """True iff `stem` names a counting reviewer sidecar (`<stem>.md`) of
    this session in either share root."""
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter
    from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS

    if not stem or "/" in stem or "\\" in stem:
        return False

    for share_dir in _share_dirs(repo_root, session_id):
        path = os.path.join(share_dir, stem + ".md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        try:
            parsed = parse_frontmatter(text)
        except Exception:
            continue
        frontmatter = parsed.get("frontmatter")
        if not isinstance(frontmatter, dict):
            continue
        bare = _bare_agent_type(frontmatter.get("agent_type"))
        if bare is not None and bare in DELEGATE_REVIEWERS:
            return True

    return False


def _parse_inline_review(trailer: str) -> Optional[Tuple]:
    """`("em-verified", statement)` / `("applies", stem, statement)` for a
    grammatically valid trailer (statement >= `_MIN_STATEMENT_CHARS`), else
    None. An invalid trailer (wrong grammar, or a too-short statement)
    credits nothing — see the `trailer-too-short` / `inline-applies-
    dangling` test cases."""
    text = trailer.strip()
    m = _EM_VERIFIED_RE.match(text)
    if m:
        statement = m.group(1).strip()
        if len(statement) >= _MIN_STATEMENT_CHARS:
            return ("em-verified", statement)
        return None
    m = _APPLIES_RE.match(text)
    if m:
        stem, statement = m.group(1), m.group(2).strip()
        if len(statement) >= _MIN_STATEMENT_CHARS:
            return ("applies", stem, statement)
        return None
    return None


def _refusal_message(shas: List[str], ceiling_note: Optional[str]) -> str:
    shown = [sha[:12] for sha in shas[:5]]
    extra = len(shas) - len(shown)
    sha_text = ", ".join(shown)
    if extra > 0:
        sha_text += f" (+{extra} more)"
    lines = [
        f"[guard] This session's Stop is refused: {len(shas)} code commit(s) "
        f"this session authored carry no review ({sha_text}).",
        "Dispatch a code-reviewer, or make a follow-up commit carrying a "
        "valid `Inline-Review:` trailer (`em-verified — <what was "
        "checked, >=20 chars>` up to 50 code LOC, or `applies <sidecar-stem> "
        "— <which findings, >=20 chars>`) to cover them.",
    ]
    if ceiling_note:
        lines.append(ceiling_note)
    return "\n".join(lines)


def _core(payload: dict) -> Tuple[str, Optional[str]]:
    """Returns `("pass", None)`, `("block", message)`, or
    `("advisory", text)`. Shared by both entry points below."""
    if not isinstance(payload, dict):
        return ("pass", None)
    if payload.get("agent_id"):
        return ("pass", None)
    if payload.get("stop_hook_active"):
        return ("pass", None)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return (
            "advisory",
            "[guard] guard-terminal-review could not evaluate: no "
            "session_id in the Stop payload",
        )
    session_id = session_id.strip()

    repo_root = _repo_root(payload)
    if repo_root is None:
        return (
            "advisory",
            "[guard] guard-terminal-review could not evaluate: could not "
            "resolve repo root from cwd",
        )

    try:
        commits = _session_commits(repo_root, session_id)
    except _GitUnavailable as exc:
        return (
            "advisory",
            f"[guard] guard-terminal-review could not evaluate: {exc}",
        )

    code_commits = [c for c in commits if c.is_code]
    if not code_commits:
        return ("pass", None)

    try:
        receipt_shas = receipt_credited_shas(
            repo_root,
            [(c.sha, c.committed_at_iso, session_id) for c in code_commits],
        )
    except Exception:
        receipt_shas = set()

    try:
        window_shas = _window_credited(repo_root, session_id, code_commits)
    except Exception:
        window_shas = set()

    credited = receipt_shas | window_shas
    remaining = [c for c in code_commits if c.sha not in credited]
    if not remaining:
        return ("pass", None)

    inline_trailers: List[str] = []
    for commit in commits:
        inline_trailers.extend(commit.inline_review)

    ceiling_note: Optional[str] = None
    for trailer in inline_trailers:
        parsed = _parse_inline_review(trailer)
        if parsed is None:
            continue
        if parsed[0] == "em-verified":
            total_loc = sum(c.code_loc for c in remaining)
            if total_loc <= _EM_VERIFIED_LOC_CEILING:
                return ("pass", None)
            ceiling_note = (
                f"An `em-verified` trailer is present, but this session's "
                f"uncredited code LOC ({total_loc}) exceeds the "
                f"{_EM_VERIFIED_LOC_CEILING}-line self-review ceiling."
            )
        elif parsed[0] == "applies":
            stem = parsed[1]
            try:
                resolved = _resolve_applies_sidecar(repo_root, session_id, stem)
            except Exception:
                resolved = False
            if resolved:
                return ("pass", None)

    message = _refusal_message([c.sha for c in remaining], ceiling_note)
    return ("block", message)


def op(payload: dict) -> Optional[dict]:
    """Pointer-shim contract: `{"message": str}` on a genuine refusal only;
    `None` on pass, skip, AND fail-open (an advisory must never travel
    through this channel — see module docstring)."""
    state, text = _core(payload)
    if state == "block":
        return {"message": text}
    return None


@register_op("hooks.guard_terminal_review")
def _guard_terminal_review_handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    try:
        state, text = _core(dict(payload) if isinstance(payload, dict) else {})
    except Exception:
        return no_advisory()
    if state == "block":
        return deny("Stop", text)
    if state == "advisory":
        return post_advisory(text)
    return no_advisory()
