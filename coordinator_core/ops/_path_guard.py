"""
coordinator_core.ops._path_guard — shared caller-supplied-path containment helpers.

Purpose: Single implementation of the path-containment pattern first proven in
``handoff_lineage_ancestry.py`` (``_SAFE_HANDOFF_ID`` / ``_is_contained``), lifted
and generalized so every op that resolves a caller-supplied string into a
filesystem path it reads or writes can adopt the same guard rather than
reinventing (or omitting) it.

Threat model (see docs/problems/2026-07-08-op-family-path-containment-investigation.md):
this is bounded defense-in-depth, NOT a privilege-boundary control. DR-215 retired
the UDS/HTTP transport; ``coordinator_core`` runs as a same-user, in-process
command-type CLI. The caller already has full filesystem read/write as that user,
so an unguarded op grants no capability the caller lacks. The guard's value is
blast-radius containment against bugs/fat-fingers (a malformed path silently
mutating the wrong file) and consistency across the op family — not defense
against an adversarial caller.

Two primitives:
  - ``safe_id``: allowlist check for a single path segment (filenames, ids).
  - ``contained_path``: post-``.resolve()`` containment check against a set of
    allowed root directories (symlink-safe; fails closed on OSError).

Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
Reference: coordinator_core/ops/handoff_lineage_ancestry.py (``_SAFE_HANDOFF_ID`` / ``_is_contained``)

Negative-spec:
  - Does NOT decide per-op allowed roots — callers pass their own ``allowed_roots``
    (per-op allowed-root sets are a decided table in the spec backlink § 4, not a
    universal constant owned by this module).
  - Does NOT normalize/sanitize a path for reuse — ``contained_path`` returns the
    resolved path or ``None``; it does not mutate or repair the input.
  - Does NOT cover ``memo.transition``'s git-toplevel-relative containment shape —
    that op's root is "any git repo's toplevel", not a worktree-rooted path, and is
    intentionally left on its own ``_containment_check`` (see investigation § 4).
  - Does NOT casefold the comparison — ``relative_to`` stays case-sensitive on
    purpose (that gap is a SEPARATE bug, closed upstream by callers that
    pre-process with ``write_guards._case_fold_path.casefold_path`` before
    ever reaching this module). This module only prefix-normalizes.
  - Does NOT return a prefix-stripped path — the strip in ``contained_path``
    feeds the ``relative_to`` COMPARISON only; the returned ``Path`` is the
    real, untouched ``candidate.resolve()``, extended-length prefix intact
    when genuinely present (a >MAX_PATH Windows path where the prefix is
    load-bearing, not cosmetic — stripping it from the return value would
    hand a caller a path that no longer names the file).

2026-08-03 residual close (PM-authorized, narrow scope — prefix desync only,
no change to the allowed-roots contract/``safe_id``/symlink semantics/
fail-closed posture): callers that pre-process a candidate/root pair with
``write_guards._case_fold_path.casefold_path`` and then hand the STRINGS to
this module were still exposed, because ``contained_path`` does its OWN
``Path.resolve()`` internally, downstream of that string-level fix. If that
internal resolve introduces the extended-length prefix on one operand and not
the other (the length-triggered asymmetry a real Windows host produces — a
short guarded root vs. a long candidate), ``relative_to()`` compares ``Path``
objects, not the caller's casefolded strings, and reports not-contained —
the guard silently does not fire. Closed by reusing
``write_guards._case_fold_path.strip_extended_length_prefix`` on both
resolved operands, comparison-only, immediately before ``relative_to``. See
``state/audits/2026-08-03-extended-length-prefix-call-site-audit.md`` for the
call-site sweep this closes (previously the last open row).

Import-direction note: importing ``write_guards._case_fold_path`` from
``ops._path_guard`` is a one-way, cycle-free edge — ``_case_fold_path.py`` has
no imports of its own (pure ``str`` operations, no filesystem I/O), and
``coordinator_core.write_guards``'s own package ``__init__.py`` does not
import ``coordinator_core.ops`` (it is a PreToolUse guard-discovery package,
independent of the op-registry). Reusing the primitive here (rather than a
third copy) keeps "one implementation of the prefix strip" true regardless of
which side of the ``ops``/``write_guards`` split a caller sits on.
"""

from __future__ import annotations
import sys

import re
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.write_guards._case_fold_path import strip_extended_length_prefix

#: Allowlist for a single path segment — no separators, no bare traversal.
#: Note: the regex alone admits the literal string ".." (it contains only
#: dots), which is why the explicit ``value not in (".", "..")`` check below
#: is required — the reference implementation's regex-only guard was correct
#: only because of how it was subsequently used (single-segment join with a
#: fixed suffix); this shared helper does not rely on that incidental safety.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_id(value: str) -> bool:
    """Return True if ``value`` is a safe single path segment.

    A safe id contains only alphanumerics, ``.``, ``_``, ``-`` (no path
    separators of any kind) AND is not the bare traversal token ``"."`` or
    ``".."``. Use this to validate a caller-supplied id/filename-segment
    BEFORE interpolating it into a path, so a traversal value is rejected
    outright rather than resolved.
    """
    return bool(_SAFE_ID.match(value)) and value not in (".", "..")


def contained_path(candidate: Path, allowed_roots: Iterable[Path]) -> Optional[Path]:
    """Return ``candidate.resolve()`` iff it is under one of ``allowed_roots``.

    Post-``.resolve()`` containment check (symlink-safe — ``Path.resolve()``
    follows symlinks, so a symlink that resolves outside every allowed root is
    caught). Fails closed: any ``OSError`` while resolving ``candidate`` or an
    allowed root results in rejection (``None``), never a permissive fallback.
    A ``ValueError`` from ``relative_to`` (candidate resolves fine but isn't
    under this particular root) is treated as "try the next root," not a
    failure — only exhausting every root without a match rejects.
    (Review: code-reviewer — Finding 5, docstring completeness nit.)

    Windows extended-length-prefix normalization (2026-08-03, PM-authorized
    residual close): the ``relative_to`` COMPARISON is done against a
    prefix-stripped copy of both the resolved candidate and the resolved root
    (``write_guards._case_fold_path.strip_extended_length_prefix``), so an
    asymmetric prefix — ``resolve()`` adding ``\\\\?\\`` to one operand and
    not the other, the length-triggered shape a real Windows host produces —
    cannot manufacture a false not-contained. This is comparison-only: the
    value this function RETURNS is always the untouched
    ``candidate.resolve()``, prefix intact when genuinely present, so a
    caller doing real filesystem work with the returned path still gets a
    name that resolves on a >MAX_PATH Windows path where the prefix is
    load-bearing. A no-op on POSIX (the strip never matches non-Windows
    input).

    Logging: a ``ValueError`` from ``relative_to`` is the ORDINARY "not under
    this root" answer — it fires for every non-matching root on every call,
    including the fully-successful case where a later root matches. It is not
    logged. An ``OSError`` (candidate or root fails to resolve at all) is
    genuinely exceptional and is logged to stderr as a diagnostic.

    Args:
        candidate: The caller-supplied path to check (absolute or relative).
        allowed_roots: Iterable of root directories; ``candidate`` is accepted
            if its resolved form is under (``relative_to``) any one of them.

    Returns:
        The resolved ``Path`` if contained under an allowed root, else ``None``.
    """
    try:
        resolved = candidate.resolve()
    except OSError:
        print(f"skip: contained_path: candidate.resolve() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    candidate_for_compare = strip_extended_length_prefix(str(resolved))
    # Review: code-reviewer cb2c4bcd, Finding 2 -- hoisted out of the loop;
    # was rebuilt from the same fixed string on every iteration.
    candidate_compare_path = Path(candidate_for_compare)
    for root in allowed_roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            print(f"skip: contained_path: root.resolve() failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        root_for_compare = strip_extended_length_prefix(str(root_resolved))
        try:
            candidate_compare_path.relative_to(Path(root_for_compare))
            return resolved
        except ValueError:
            continue
    return None
