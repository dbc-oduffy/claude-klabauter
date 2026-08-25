"""Shared helper: case-fold a caller-supplied path string before it reaches
`coordinator_core.ops._path_guard.contained_path`.

Ported/lifted from `block_derived_global_doctrine_write._normalize` and
`block_oss_mirror_memo_delivery._casefold_path` — this is the THIRD guard to
need the identical body, which is the threshold this package's own review
convention (see `_sentinel_write_guard.py`'s own docstring) treats as
"factor, don't copy a third time." The underscore prefix is load-bearing:
`engine.py::_discover_guards()` explicitly skips modules whose name starts
with `_`, which is exactly why this is the correct home for shared plumbing
consumed by multiple guards rather than a registered guard of its own.

This module fixes TWO DISTINCT bugs, not one. Do not conflate them:

1. **Case-folding bug** (the original reason this module exists — see "Why
   this exists" below): `os.path.normcase` is a no-op on POSIX, so a
   case-varied write on a case-insensitive-but-case-preserving filesystem
   (macOS APFS) slips past a case-sensitive `relative_to()` comparison.
   Closed by `.casefold()`.
2. **Windows extended-length-prefix desync bug** (added 2026-08-03,
   `state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`):
   `Path.resolve()`/`os.path.realpath` can return the extended-length form
   (a leading two-separator-then-question-mark-then-separator marker, or
   its UNC variant which inserts a ``UNC`` segment right after that marker)
   on Windows,
   while a plain string never carries that prefix. Two strings naming the
   same real directory then compare unequal, and a containment check
   silently reports not-contained — the same fail-open shape as bug 1, one
   layer further out, but a SEPARATE defect with a separate fix
   (prefix-stripping, not casefolding). Closed by `casefold_path`'s
   normalize -> strip -> casefold pipeline below. A reader who fixes only
   the case-folding bug has NOT closed this one, and vice versa.

Why this exists
----------------
`os.path.normcase` is a no-op on POSIX (macOS included) — it only folds case
on Windows. Several guards in this tree pre-process a raw path string with
`os.path.normcase` and then hand the result to
`coordinator_core.ops._path_guard.contained_path`, whose containment check
is `Path.resolve()` + `relative_to()` — a comparison that is itself
case-SENSITIVE. On a case-insensitive-but-case-preserving filesystem (macOS
APFS, the default on every current Mac), a case-varied write
(``Cross-Repo/Inbox`` instead of ``cross-repo/inbox``) lands inside the same
real guarded directory on disk while the string comparison misses it — the
guard silently ALLOWS exactly what it exists to block. `os.path.normcase`
does not close this; only casefolding the strings explicitly, before they
ever reach `Path`/`resolve()`, does.

Found first in `block_oss_mirror_memo_delivery.py` (code-reviewer, Finding 3,
2026-07-31; fixed in commit 50230f4d), then confirmed present in
`block_home_dir_memo_delivery.py` and `guard_memory_store_cap.py` by the same
review sweep. All three now route through this one helper rather than each
carrying its own copy.

Spec backlink: `docs/problems/2026-07-08-op-family-path-containment-
investigation.md` (the `contained_path` containment pattern this helper
pre-processes input for); `block_derived_global_doctrine_write.py`'s own
module docstring (the original string-first-normalization idiom this helper
is lifted from); `state/handoffs/2026-08-03-windows-extended-length-prefix-
desync.md` (bug 2, the extended-length-prefix desync this module now also
closes).

Negative-spec:
  - Does NOT replace `contained_path`'s `Path.resolve()`/`relative_to()`
    containment logic — this only pre-processes the raw strings BEFORE they
    are wrapped in `Path()` and handed to that helper.
  - Does NOT touch `os.path.normcase` calls that guards keep for symmetry
    with a ported reference implementation — this helper is additive, not a
    replacement for that call, on guards that still carry it.
  - Does NOT make any guard fail-closed. An unresolvable/unstrippable path
    still means "no bump" / "no containment match" at the call site; this
    module only makes the STRING comparison agree once both sides reach it.
  - Never raises: any unexpected input is handled by plain `str` operations
    (`replace`, `casefold`, slicing) which do not raise on any `str` input;
    this module does no I/O and no `Path` construction of its own.
"""

from __future__ import annotations

#: Extended-length prefixes in both separator styles a caller might hand in
#: (raw backslash form straight off Windows, or already forward-slash
#: normalized). Checked case-insensitively -- `resolve()`/`realpath` casing
#: of the literal marker/``UNC`` token is not guaranteed. The UNC variant
#: MUST be checked before the bare variant in each style, since the UNC form
#: is itself a superset prefix of the bare one.
_EXTENDED_LENGTH_UNC_PREFIX_BACKSLASH = "\\\\?\\UNC\\"
_EXTENDED_LENGTH_PREFIX_BACKSLASH = "\\\\?\\"
_EXTENDED_LENGTH_UNC_PREFIX_SLASH = "//?/unc/"
_EXTENDED_LENGTH_PREFIX_SLASH = "//?/"


def strip_extended_length_prefix(raw: str) -> str:
    """Strip Windows' extended-length path prefix (plain and UNC form),
    preserving whichever separator style ``raw`` used.

    Plain form drops the leading marker, leaving the drive-letter path that
    follows it untouched (backslash or forward-slash, matching the input).
    UNC form collapses its ``UNC`` segment down to the bare UNC path's own
    leading double-separator (``\\\\<server>\\<share>`` or
    ``//<server>/<share>``), not to nothing.

    A no-op on POSIX-shaped input (no recognized prefix) and idempotent
    (stripping an already-stripped string returns it unchanged). Never
    raises: pure `str` slicing on any `str` input.
    """
    lowered = raw.lower()
    if lowered.startswith(_EXTENDED_LENGTH_UNC_PREFIX_BACKSLASH.lower()):
        return "\\\\" + raw[len(_EXTENDED_LENGTH_UNC_PREFIX_BACKSLASH):]
    if lowered.startswith(_EXTENDED_LENGTH_PREFIX_BACKSLASH.lower()):
        return raw[len(_EXTENDED_LENGTH_PREFIX_BACKSLASH):]
    if lowered.startswith(_EXTENDED_LENGTH_UNC_PREFIX_SLASH.lower()):
        return "//" + raw[len(_EXTENDED_LENGTH_UNC_PREFIX_SLASH):]
    if lowered.startswith(_EXTENDED_LENGTH_PREFIX_SLASH.lower()):
        return raw[len(_EXTENDED_LENGTH_PREFIX_SLASH):]
    return raw


def casefold_path(raw: str) -> str:
    """Backslash -> forward-slash, strip a Windows extended-length prefix
    (plain or UNC form), then Unicode casefold. Order is load-bearing:
    separators normalize first so the prefix check has one shape to match,
    the prefix strips second so it never survives into the casefolded
    result, and casefold runs last over whatever remains.

    Apply to BOTH the candidate path and every allowed-root string before
    either is wrapped in `Path(...)` and passed to
    `coordinator_core.ops._path_guard.contained_path` — casefolding only one
    side reopens the gap this helper exists to close, and the same is true
    of the extended-length-prefix strip: a candidate whose `resolve()` added
    the prefix compared against a root string that never carries one is
    exactly the desync `state/handoffs/2026-08-03-windows-extended-length-
    prefix-desync.md` describes.
    """
    normalized = raw.replace("\\", "/")
    stripped = strip_extended_length_prefix(normalized)
    return stripped.casefold()
