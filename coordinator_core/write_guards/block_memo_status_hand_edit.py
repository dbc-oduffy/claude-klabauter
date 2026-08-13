"""coordinator_core.write_guards.block_memo_status_hand_edit — hard-deny guard.

Closes the hand-edit hole named by the 2026-07-26 incident: STEP_2_65C's
ceremony prose asked an operator to flip a memo's `status:` frontmatter field
directly (``open`` -> ``actioned``) because no op existed for that atomic
transition — `_action` in `coordinator_core.ops.memo_transition` requires
`status: in_progress`, so a same-turn open->actioned flip had no legal op
path at all. A hand-`Edit` bypassing an unwritten op is exactly the failure
class this guard closes, independent of whether the `memo.transition resolve`
verb (this plan's C1) has landed yet — this chunk has no dependency on C1/C2
and is deliberately executed first (see the plan's Execution Order note).

Per ``write_guards/INTERFACE.md``'s hard-deny/advisory discriminator:
hard-deny guards can BLOCK the tool call outright; advisory guards only add
context and always let the write through. A hand-edit of `status:` on a
disk-truth memo record is a correctness/integrity defect (it bypasses the
locked_rmw exclusion, the live-claim collision check, and the disposition
validation that the op enforces) — advisory would let the exact incident this
guard exists to close recur with a warning attached, which is the "asks a
model to refuse work it is eager to do" shape `CLAUDE.md` names as
advisory-in-practice. This is CLASS = "hard-deny", the established category
for exactly this failure shape alongside `block_cutover_phase_hand_edit.py` /
`block_tracker_edit.py` / `block_subagent_archive_write.py` — not a novel
escalation.

Modelled on the sibling `block_consumed_handoff_edit.py` — same package, same
interface (`write_guards/INTERFACE.md`), same matcher set (Write|Edit|
MultiEdit — no NotebookEdit; a memo record is never a notebook), same
candidate extraction (top-level `file_path`, or every `edits[].file_path` for
`MultiEdit`), the same backslash/slash-run normalization, `..`-traversal
rejection, and git-root-relative containment check. The `status:`-line touch
detection (does THIS edit's fragment actually change the `status:` field,
not merely land somewhere in a memo file) follows `block_cutover_phase_hand_edit.py`'s
`_edit_touches_phase`/`_touches_phase_line` shape, generalized to the
`status:` field.

Design-as-offers: the deny reason LEADS with the op to call instead
(`memo.transition` verb `resolve` — one atomic `open` -> `actioned` closure
with a REQUIRED disposition, exactly one of `--decision` / `--decision-note`
/ `--realized-by` / `--actioned-note`), not with the violation. A denial that
just says no is what drives the next bypass.

Scope is precise (Anti-scope): only `cross-repo/inbox/*.md` and
`cross-repo/archive/*.md` are in scope — those are the disk-truth memo
records the op exists to protect. `state/memo-outbox/` drafts are
hand-authored BY DESIGN (an operator composing an outbound memo before
`cross-repo-memo` sends it) and MUST stay writable; this guard's path-shape
gate does not match that directory at all, so no outbox edit is ever a
candidate.

Negative-spec:
  - Does NOT deny an edit to any OTHER memo field (`title`, `summary`,
    `kind`, body prose, etc.) — only a fragment that touches the `status:`
    frontmatter line is in scope, mirroring
    `block_cutover_phase_hand_edit.py`'s `phase:`-only scoping. A blanket
    file-level deny would also block the op's own writes if it ever routed
    through this same tool surface.
  - Does NOT deny a `Write`/`Edit`/`MultiEdit` that CREATES a new memo (file
    does not yet exist on disk) — authoring a fresh inbound/outbound memo is
    the normal path, not a hand-edit of an existing record's disposition.
  - Does NOT generalize to any other `cross-repo/` subdirectory or to
    `state/memo-outbox/` — scope is exactly `cross-repo/inbox/` and
    `cross-repo/archive/`, per the Anti-scope bullet forbidding a broader
    guard.
  - Does NOT fail closed on any error — every subprocess/file-read failure
    degrades to ALLOW (fewer candidates matched), matching the sibling
    guards' fail-open discipline; a guard crash never fabricates a deny
    (`write_guards/engine.py`'s own contract).

2026-08-06 RESHAPE (not a flip — CLASS stays "hard-deny", PRIORITY unchanged):
the guard denied every status-field hand-edit unconditionally even though
its own rationale ("bypasses ... the live-claim collision check") only ever
applies when a session actually holds a live claim on the memo. Two fixes,
both required — narrowing only one leaves the other half of the over-fire:
  - Gate on a LIVE CLAIM (`_has_live_claim`): the memo-claim directory
    convention from `ops.fleet.archive_actioned_memos._memo_claim_dir`
    (`<git-common-dir>/coordinator-sessions/memo-claims/<memo-filename>/`),
    liveness read via `coordinator_core.liveness.cs_claim_holder_live`. A
    live claim keeps the hard-deny; no live claim degrades to an advisory
    `additionalContext` envelope (Axis-A: names the same runnable
    `archive-stamp-cli resolve-memo` alternative) rather than blocking.
  - ANCHOR THE MATCHER (`_frontmatter_block` + the Edit/MultiEdit leg of
    `_edit_touches_status`): a fragment matching `_STATUS_LINE_RE` is only
    a genuine touch when the OLD fragment is actually present inside the
    real on-disk frontmatter block, not merely a line that starts with
    "status:" anywhere in the file (e.g. quoted body prose). Falls back to
    the prior unanchored match only when no frontmatter block can be
    established at all (no pre-image, or the file has no frontmatter
    fence) — fails toward deny, this module's existing conservative
    direction for an ambiguous match.

Spec backlink: pln-give-the-memo-disposition-flip-e580c2 (chunk C3);
docs/plans/2026-08-06-apply-guard-class-census.md (chunk C18)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 56

#: Escape hatch — recovery-only, mirrors the sibling guards' override pattern.
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_MEMO_STATUS_HAND_EDIT"

#: '..' as a full path component.
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")

#: Memo record path shape: cross-repo/inbox/<name>.md or
#: cross-repo/archive/<name>.md — flat directories, one path segment.
#: Case-insensitive by design: macOS (APFS default) and Windows are both
#: case-insensitive filesystems, so a `Cross-Repo/Inbox/x.md` candidate is
#: the SAME on-disk file as `cross-repo/inbox/x.md` on either platform, yet
#: Path.resolve(strict=False) does not case-normalise (it resolves symlinks
#: and `..`, it does not consult directory entries for casing) — a
#: case-sensitive match here would silently drop the candidate and ALLOW the
#: write while it lands on the real memo. Folding here is safe on
#: case-sensitive filesystems (Linux) too: this regex only widens the
#: shape-gate, and `check()` still requires `candidate_disk.is_file()` against
#: the literal candidate path before treating it as in-scope, so a
#: differently-cased path that is genuinely a distinct, non-existent file on
#: Linux is filtered out there regardless of this fold.
_MEMO_RECORD_RE = re.compile(r"(^|/)cross-repo/(inbox|archive)/[^/]+\.md$", re.IGNORECASE)

#: Frontmatter `status:` line, first-block only (mirrors the sibling guards'
#: field matchers).
_STATUS_LINE_RE = re.compile(r"^status:[ \t]*(.*)$")


def _collapse_slashes(value: str) -> str:
    """Backslash -> slash, collapse slash runs."""
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --show-toplevel``. Fails open.

    AC4 (docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3b):
    delegates to the shared, process-lifetime-memoized
    ``write_guards._repo_root.resolve_repo_root`` instead of hand-rolling its
    own spawn — same fail-open-to-``None`` contract as the prior inline
    ``subprocess.run``, so no verdict changes. The prior inline spawn only
    logged a forensic diagnostic on an actual spawn ``OSError``; the shared
    resolver has no way to distinguish "genuine resolution failure" from the
    ordinary, non-exceptional case of ``cwd`` simply not being inside a git
    repo (both come back as ``None``), so this print is WIDER than what the
    prior code emitted — it now also fires for that ordinary not-in-a-repo
    case (plausible for a memo-shaped write attempted in a scratch/test
    checkout with no ``.git`` at all). Verdict is unaffected either way
    (``_normalize_and_gate`` already treats ``None`` the same as any other
    unresolved git root); this is a diagnostic-scope widening, not a
    restoration, and is accepted as such rather than scoped back, since
    scoping it to the genuine-failure case alone would require the shared
    resolver to distinguish the two cases, which is out of this module's
    scope (`write_guards/_repo_root.py` is peer-landed).
    """
    result = resolve_repo_root(cwd)
    if result is None:
        print(
            f"block_memo_status_hand_edit: no git root resolved for cwd="
            f"{cwd!r} (unresolvable OR simply not inside a git repo; both "
            f"resolve to None here), treating as no git root (decision "
            f"unaffected)",
            file=sys.stderr,
        )
    return result


def _extract_candidates(payload: Dict[str, Any]) -> List[str]:
    """Top-level ``file_path`` (Write/Edit), or every ``edits[].file_path``
    (MultiEdit) when there is no top-level ``file_path``."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    fp = tool_input.get("file_path")
    if fp:
        return [fp]
    out: List[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                efp = edit.get("file_path")
                if efp:
                    out.append(efp)
    return out


def _normalize_and_gate(cand: str, git_root: Optional[str]) -> Optional[str]:
    """Normalize + traversal-reject + containment-gate + path-shape-gate a
    single candidate. Returns the normalized string, or ``None`` if this
    candidate is out of scope."""
    cn = _collapse_slashes(cand)

    if _TRAVERSAL_RE.search(cn):
        return None

    if git_root:
        if cn.startswith("/") or re.match(r"^[A-Za-z]:", cn):
            abs_cn = cn
        else:
            abs_cn = git_root.rstrip("/") + "/" + cn
        try:
            abs_cn_canon = casefold_path(str(Path(abs_cn).resolve(strict=False)))
        except OSError as exc:
            print(f"block_memo_status_hand_edit: path resolve failed for "
                  f"{abs_cn!r}, gating on unresolved form: {exc}", file=sys.stderr)
            abs_cn_canon = abs_cn
        # Both sides through `casefold_path`, never a bare `.casefold()` --
        # it folds separators AND case, and `abs_cn_canon` above is already
        # built with it. A bare `.casefold()` here leaves the root side's
        # native separators intact, so on Windows the two sides can never
        # match and every candidate is gated out: the guard goes silently
        # inert. Latent from this guard's first commit and live from the
        # moment `_resolve_git_root` moved off `git rev-parse` (forward
        # slashes) onto the shared resolver (native separators). Matches
        # `block_consumed_handoff_edit`/`block_cutover_phase_hand_edit`,
        # which spell it this way for the same reason.
        # `rstrip("/\\")`, never `rstrip("/")` -- a trailing BACKSLASH is not
        # stripped by the latter, so a `git_root` of `X:\repo\` (or `X:\`, a
        # drive-root repo) composes `X:\repo\/cross-repo/`, which casefolds to
        # a DOUBLE-slash prefix and stops matching the single-slash form
        # `Path.resolve()` produces on the candidate side -- the same silent
        # inertness this fix exists to close, reintroduced through the one
        # separator the strip did not cover. Reviewer finding, this close.
        # Scope check: verified the drive-root repo is the ONLY reachable
        # shape of this bug -- `resolve_repo_root`'s walk path returns
        # `str(Path(...))`, which never carries a trailing separator except
        # at a bare drive root (a one-letter Windows drive spec with no
        # further path segment); an ordinary repo one level or more below a
        # drive root never round-trips with a trailing separator, so this
        # strip is not patching a broader class than the sidecar named.
        # Honesty note (reviewer
        # finding, not re-opened here): this `rstrip` closes the one
        # reachable symptom, but the root side STILL never routes through
        # `Path(...).resolve()` the way `abs_cn_canon` above does -- it is a
        # raw string concatenation with a targeted strip, not the same
        # normalization pipeline. That structural mismatch is real and
        # deferred (matches the sibling guards, pre-existing, no other
        # reachable symptom found) -- not claimed closed by this line.
        expected_prefix = casefold_path(git_root.rstrip("/\\") + "/cross-repo/")
        if not abs_cn_canon.startswith(expected_prefix):
            return None

    if not _MEMO_RECORD_RE.search(cn):
        return None

    return cn


def _resolve_git_common_dir(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --git-common-dir``, resolved to an absolute path.
    Fails open (``None``) on any resolution error — mirrors
    ``_resolve_git_root``'s fail-open discipline. Used only to locate the
    memo-claim directory (see ``_has_live_claim``); a failure here degrades
    to "no claim dir found", never to a deny.

    D4 (docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md):
    delegates to the shared, non-spawning
    ``coordinator_core.git.git_dir.resolve_git_common_dir`` seam instead of
    hand-rolling its own ``git rev-parse --git-common-dir`` spawn. That
    resolver takes a REPO ROOT (not an arbitrary cwd) and builds its result
    via join/normpath with no ``.resolve()`` — its output is absolute only
    when the ``repo_root`` argument already is. ``resolve_repo_root`` (the
    peer-landed shared seam, C2-C4 of
    docs/plans/2026-08-06-eliminate-claude-klabauter-s-non-test-subprocess-spawn-population.md)
    always returns an already-``Path.resolve()``d absolute string or
    ``None``, so the semantic trap does not materialize here — the result
    stays absolute exactly as the prior subprocess-plus-join did.
    """
    repo_root = resolve_repo_root(cwd)
    if not repo_root:
        return None
    return str(resolve_git_common_dir(Path(repo_root)))


def _has_live_claim(cwd: Optional[str], memo_filename: str) -> bool:
    """``True`` iff a session currently holds a live claim on this memo.

    Convention mirrors ``ops.fleet.archive_actioned_memos._memo_claim_dir``:
    ``<git-common-dir>/coordinator-sessions/memo-claims/<memo-filename>/``
    (itself mirroring the shell ``cs_sweep_actioned_memos`` claim_dir
    derivation). This is the missing half of the guard's original design:
    the docstring always argued a hand-edit "bypasses ... the live-claim
    collision check" but the guard never actually looked for one — every
    status-field edit was denied unconditionally, live claim or not, which
    is the over-fire this gate closes.

    Fails open (``False``, i.e. "no live claim") on any resolution/read
    error — this guard's established fail-open-on-error discipline (module
    docstring's negative-spec: "Does NOT fail closed on any error"). A
    crash here degrades check() to the advisory leg, never to a
    fabricated deny.
    """
    common_dir = _resolve_git_common_dir(cwd)
    if not common_dir:
        return False
    claim_dir = Path(common_dir) / "coordinator-sessions" / "memo-claims" / memo_filename
    if not claim_dir.is_dir():
        return False
    try:
        from coordinator_core.liveness import cs_claim_holder_live
        return bool(cs_claim_holder_live(str(claim_dir)))
    except Exception as exc:
        print(f"block_memo_status_hand_edit: cs_claim_holder_live raised for "
              f"{claim_dir} — treating as no live claim: {exc}", file=sys.stderr)
        return False


def _frontmatter_block(text: Optional[str]) -> Optional[str]:
    """Return the raw text of the first YAML frontmatter block in ``text``
    (delimiters excluded), or ``None`` if ``text`` has no first-line ``---``
    fence or no closing fence.

    This is the anchor for ``_edit_touches_status``'s Edit/MultiEdit leg: a
    fragment match against ``_STATUS_LINE_RE`` alone (see
    ``_touches_status_line``) cannot tell a genuine frontmatter ``status:``
    field from unrelated body prose that happens to start a line with
    ``status:`` — the matcher was never anchored to the file's actual
    frontmatter bounds. Comparing the matched fragment against THIS
    substring (real on-disk frontmatter, not a per-line regex) is that
    anchor.
    """
    if text is None:
        return None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            return "".join(lines[1:i])
    return None


def _extract_status(text: str) -> str:
    """First-frontmatter-block ``status:`` value, comment/quote-stripped.
    Empty string if no frontmatter block or no ``status:`` line."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ""
    for line in lines[1:]:
        if line.rstrip() == "---":
            break
        m = _STATUS_LINE_RE.match(line)
        if m:
            val = m.group(1)
            val = re.sub(r"[ \t]*#.*$", "", val)
            val = val.strip()
            val = re.sub(r"^['\"]", "", val)
            val = re.sub(r"['\"]$", "", val)
            return val
    return ""


def _touches_status_line(fragment: Optional[str]) -> bool:
    """``True`` if any line in ``fragment`` matches the ``status:``
    frontmatter field shape (used against Edit/MultiEdit ``old_string`` /
    ``new_string`` chunks, which are not full files).

    Deliberately NOT anchored to an actual frontmatter block: a fragment is
    an arbitrary substring of the file and routinely does not carry the
    surrounding ``---`` delimiters at all, so requiring them would blind
    this check to genuine frontmatter edits rather than narrow it. The
    tradeoff is a body line that happens to start with ``status:`` (e.g.
    quoted prose) also counts as a touch — fails toward deny, the safe
    direction for a hard-deny guard, and consistent with this module's
    fail-open-on-error / fail-closed-on-ambiguous-match discipline.
    """
    if not fragment:
        return False
    for line in fragment.splitlines():
        if _STATUS_LINE_RE.match(line):
            return True
    return False


def _edit_touches_status(
    tool_name: str, tool_input: Dict[str, Any], cand: str, pre_image: Optional[str] = None
) -> bool:
    """Whether the specific edit targeting ``cand`` would touch the
    ``status:`` field. ``Write`` supplies a full ``content`` replacement;
    ``Edit``/``MultiEdit`` supply ``old_string``/``new_string`` fragments.

    ``pre_image`` (the on-disk text before this Write) is REQUIRED for the
    ``Write`` path: a full-file replacement always contains a ``status:``
    line (every extant memo has one), so a presence-only check would
    hard-deny every Write to an existing memo — a typo fix, an appended
    paragraph, a reformat — even when the status VALUE is unchanged, which
    contradicts this module's own negative-spec ("does NOT deny an edit to
    any OTHER memo field"). The old/new VALUES are compared instead
    (``_extract_status`` on both sides), mirroring how Edit/MultiEdit diff
    on fragments below. When ``pre_image`` could not be read, the old value
    is treated as unknown/empty so any status-bearing Write is conservatively
    denied — fails toward deny, never toward a silent bypass.
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        content_str = content if isinstance(content, str) else ""
        new_status = _extract_status(content_str)
        old_status = _extract_status(pre_image) if pre_image is not None else ""
        return new_status != old_status

    # Edit/MultiEdit anchor: a fragment matching _STATUS_LINE_RE is only a
    # genuine touch when the OLD fragment is actually present inside the
    # real on-disk frontmatter block — a body-prose line that merely starts
    # with "status:" can never be a substring of the frontmatter block
    # itself (the frontmatter closes before the body starts). When the
    # frontmatter block cannot be established at all (no pre_image, or the
    # file has no frontmatter fence), fall back to the unanchored line
    # match — fails toward deny, this module's existing conservative
    # direction for an ambiguous match.
    frontmatter = _frontmatter_block(pre_image)

    def _fragment_touches(old_s: Optional[str], new_s: Optional[str]) -> bool:
        if not _touches_status_line(old_s) and not _touches_status_line(new_s):
            return False
        if frontmatter is None:
            return True
        return old_s is not None and old_s in frontmatter

    if tool_name == "Edit":
        return _fragment_touches(tool_input.get("old_string"), tool_input.get("new_string"))

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return False
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            if edit.get("file_path") != cand:
                continue
            if _fragment_touches(edit.get("old_string"), edit.get("new_string")):
                return True
        return False

    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        cwd = payload.get("cwd") or None
        git_root = _resolve_git_root(cwd)
        base_dir = Path(cwd) if cwd else Path.cwd()

        file_path = ""
        disk_path: Optional[Path] = None
        for cand in candidates:
            cn = _normalize_and_gate(cand, git_root)
            if cn is None:
                continue
            candidate_disk = Path(cn) if Path(cn).is_absolute() else (base_dir / cn)
            # A memo that does not exist yet is being AUTHORED, not
            # hand-edited — out of scope (negative-spec above).
            if not candidate_disk.is_file():
                continue
            # Read for BOTH Write (needed by the before/after status-value
            # diff) and Edit/MultiEdit (needed to anchor the fragment match
            # against the real frontmatter block — see _frontmatter_block).
            try:
                pre_image = candidate_disk.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pre_image = None
            if not _edit_touches_status(tool_name, tool_input, cand, pre_image):
                continue
            file_path = cand
            disk_path = candidate_disk
            break

        if not file_path or disk_path is None:
            return None

        # Gate on a LIVE CLAIM: the guard's own docstring has always argued
        # a hand-edit "bypasses ... the live-claim collision check", but the
        # guard never checked for one — every status-field edit was denied
        # unconditionally. The genuine case (a hand-edit racing a live
        # session's claim on this memo) stays hard-deny; the over-fire case
        # (no live claim, nothing to collide with) degrades to advisory —
        # the offer, not the block.
        if _has_live_claim(cwd, disk_path.name):
            _note = operator_override_note(_OVERRIDE_ENV, payload=payload)
            reason = (
                "Memo status hand-edit blocked: status: is disk-truth, changed "
                "only via the op — it also skips the collision/validation "
                "checks the op runs (the 2026-07-26 hand-edit incident this "
                "closes), and a live session currently holds this memo's "
                "claim. Use instead:\n"
                "  archive-stamp-cli resolve-memo <memo-path> --actioned-note "
                "\"...\""
                + ("\n\n" + _note if _note else "")
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        advisory = (
            "Memo status hand-edit: status: is disk-truth, normally changed "
            "only via the op — it also runs the collision/validation checks "
            "a hand-edit skips. No live session currently holds this memo's "
            "claim, so this edit is not blocked, but the op is the safer "
            "path:\n"
            "  archive-stamp-cli resolve-memo <memo-path> --actioned-note "
            "\"...\"\n"
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": advisory,
            }
        }
    except Exception:
        return None
