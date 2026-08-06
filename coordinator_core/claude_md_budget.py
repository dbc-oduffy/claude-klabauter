"""
coordinator_core.claude_md_budget — single source of truth for the CLAUDE.md
size budget and the GOVERNED-SURFACE discriminant.

Purpose: unify the two independent, drifting literals this module replaces:
    - example-doctrine-repo `coordinator/hooks/scripts/check-claude-md-size.py`: 39900 hard / 39000 soft
    - claude-klabauter `coordinator_core.bash_guards.dispatch_checks.check_validate_commit`
      ("Check 7"): 40000 hard / 38000 soft
Both gates measured the SAME external constraint independently and had quietly
drifted to different safety margins under it. Consumed by both call sites —
this repo is the claude-klabauter-owned SSOT per the C1 re-siting (engine-subject: a
constant consumed by claude-klabauter's own Check 7 cannot live in a example-doctrine-repo hook script).

Derivation (2026-07-27, plan `docs/plans/2026-07-27-doctrine-envelope-allocation.md`
chunk C1, per the Director of Engineering F10 review-integration finding):

The 40,000-byte figure predates this module and traces back to Claude Code's
own load-time PERFORMANCE WARNING threshold for an auto-loaded CLAUDE.md file —
documented behaviour, not truncation. There is no claim anywhere (official or
otherwise) that crossing 40KB causes a CLAUDE.md to load incompletely or fail
to load; "silent truncation breaking every session" is folklore this module
explicitly does NOT reproduce. The two prior literals never actually disagreed
about what the ceiling protects against — only about how much headroom to
leave under it (example-doctrine-repo's hook: 100-byte margin; claude-klabauter's Check 7: 2000-byte
margin). This SSOT keeps the Check-7 pair (40000 hard / 38000 soft) because it
is the Claude-Code-documented number verbatim rather than an inward-adjusted
variant, and because a wider soft-warning margin gives an editor more room to
react before the hard gate fires.

The REAL constraint the byte ceiling proxies is adherence + context cost, not
truncation: Anthropic's own CLAUDE.md guidance recommends keeping the file
under roughly 200 lines because "longer files consume more context and reduce
adherence." This module retains a byte ceiling (rather than switching the gate
to a line-count or token-count ceiling) because both existing call sites
already measure bytes at commit/edit time and a byte figure is cheap to check
without invoking a tokenizer — but records the REAL, token-shaped cost here so
a future reader does not re-derive the debunked "hard technical ceiling"
framing themselves.

Measured 2026-07-27 via this repo's own oracle
(`coordinator_core.ops.measure_token_envelope`, ~4-chars/token heuristic — see
that module's docstring for why no exact tokenizer dependency is pinned):

    coordinator/CLAUDE.md   (example-doctrine-repo, dev-repo sentinel)   39,896 B  -> ~9,974 tokens
    ~/.claude/CLAUDE.md     (global)                          28,331 B  -> ~7,083 tokens

Both figures are 3.5x-5x `docs/wiki/tiered-context-loading.md`'s stated Tier-0
"<=2K tokens, always loaded" ceiling for CLAUDE.md — see that wiki's own
reconciliation (chunk C1 follow-up) for which side of that contradiction was
corrected; this module does not itself enforce a token ceiling, only reports
the token figure for the byte ceiling above.

Negative-spec — do NOT "fix" while reading this module:
    - This is NOT a tokenizer. `measure_token_envelope.estimate_tokens` is a
      cheap heuristic (~4 chars/token), not exact parity with any specific
      model's tokenizer. Treat any token figure derived from it as directional,
      not authoritative.
    - The byte ceiling below is a PERFORMANCE-WARNING headroom budget, not a
      correctness/truncation ceiling — do not restore the "silent truncation"
      framing this module's derivation note explicitly debunks.

Spec backlink: docs/plans/2026-07-27-doctrine-envelope-allocation.md § C1

C7b addendum (2026-07-31, `docs/plans/2026-07-30-boot-doctrine-cut-and-
refill-gate.md` § C7b) -- AUDIENCE-BASED GOVERNANCE AND THE RATCHET WATERMARK.

(a) GOVERN BY AUDIENCE, NOT FILENAME. `is_governed_claude_md` and
`governed_surface_paths` now additionally accept a caller-supplied
`audience_manifest` (an iterable of repo-root-relative, POSIX-separated
paths) plus the `repo_root` those paths resolve against. A NEW always-on
surface becomes governed the moment it is added to a repo's own manifest --
no further edit to THIS module. `load_audience_manifest` resolves that
manifest from the conventional per-repo file
`<repo_root>/coordinator/audience-manifest.txt` (one path per line, `#`
comments, blank lines ignored) when a caller does not pass one explicitly;
an absent manifest file degrades to `[]` (only the two legacy hardcoded
surfaces govern), never an error -- an unmanifested repo is not a broken
repo. Example-doctrine-repo's own manifest content (its `GOVERNED_AUTHORING_SURFACES`
tuple, `coordinator/hooks/scripts/_claude_md_ledger.py`) is example-doctrine-repo-owned
working data this module does not author -- see that module's own C7a
docstring for the enumeration.

(b) THE RATCHET, NOT A SECOND CEILING. `resolve_ledger_path` /
`parse_watermark` / `ratchet_check` re-implement, byte-for-byte grammar
compatible, the SAME "## Watermark" / "- Bytes: N" / "- Reason: ..."
convention example-doctrine-repo's `_claude_md_ledger.py` (C7a) already defined and reads for
its own PreToolUse admission gate. This module does NOT import that example-doctrine-repo
module -- `check_claude_md_size.py`'s own docstring already rules out this
engine resolving "the example-doctrine-repo repo" and reading its working data, and that
ruling stands: a cross-repo Python import would invert the coordinator-
claude-depends-on-claude-klabauter direction. What this module does instead
is READ THE SAME REPO-LOCAL LEDGER FILE the write/commit already concerns --
exactly as it already reads the governed CLAUDE.md-class file's own bytes
from that same working tree -- via the identical path/grammar convention, so
the two enforcement points (example-doctrine-repo's PreToolUse hook, this engine's commit-time
Check 7 and write-guard) agree without sharing code. A repo with no ledger
file at the resolved path is simply UNARMED for that surface (matches C7a's
own "not an error" framing) -- the flat `HARD_LIMIT_BYTES`/`SOFT_LIMIT_BYTES`
ceiling below still applies regardless.

REJECTED: deriving the watermark from git history (a parent-commit size).
`~/.claude/CLAUDE.md` is not tracked in any repo, so a git-history-derived
watermark would require shelling into a repo from a PreToolUse/commit-time
hot path on every governed write -- a subprocess spawn this repo's own
anti-spawn ruling forbids -- and a parent-commit-derived value is unstable
on a shared branch besides (a rebase or a sibling session's concurrent
commit silently re-baselines it). The ledger's own "## Watermark" section
is the single source instead: explicit, reasoned, and read once per check
the same way the byte budget already is.

RECONCILIATION: an earlier resolution record for this same constant family
(a roadmap-dir file under `claude-klabauter state/roadmap/boot-envelope/`,
absent at this module's 2026-07-31 execution time -- not re-created here)
chose a one-time recorded derivation ("one derivation, recorded, consumed")
for a retiring workstream. This module diverges deliberately: the ratchet
here is a STANDING, per-surface, continuously-enforced watermark living in
the ledger C7a already reads, not a one-time snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, List, Optional, Tuple, Union

#: Unified byte thresholds — see "Derivation" above. Both example-doctrine-repo's hook and
#: claude-klabauter's Check 7 import these rather than carrying independent literals.
SOFT_LIMIT_BYTES: int = 38000
HARD_LIMIT_BYTES: int = 40000

#: Repo-root sentinel that marks a checkout as the coordinator-claude DEV
#: SOURCE repo (example-doctrine-repo) rather than an OSS/percolated install or an
#: unrelated sibling repo. See example-doctrine-repo `.coordinator-dev-repo` and
#: `coordinator/docs/wiki/claude-md-surfaces.md` for the discriminant this
#: sentinel exists to support.
DEV_REPO_SENTINEL: str = ".coordinator-dev-repo"


#: Conventional per-repo file (repo-root-relative) a consuming repo may
#: author to enumerate ADDITIONAL governed CLAUDE.md-class surfaces without
#: any edit to this module -- AC3's "no code change" requirement. See
#: `load_audience_manifest` and the module docstring's C7b addendum.
AUDIENCE_MANIFEST_RELPATH: str = "coordinator/audience-manifest.txt"


def load_audience_manifest(repo_root: Union[str, Path]) -> List[str]:
    """Read `<repo_root>/coordinator/audience-manifest.txt` -- one governed,
    repo-root-relative, POSIX-separated surface path per line; blank lines
    and `#`-prefixed comment lines are ignored. Returns `[]` (never raises)
    when the file does not exist -- an unmanifested repo simply governs the
    two legacy hardcoded surfaces `is_governed_claude_md` always recognizes,
    same as before this addendum; a missing manifest is not an error.
    """
    manifest_path = Path(repo_root) / AUDIENCE_MANIFEST_RELPATH
    if not manifest_path.is_file():
        return []
    entries: List[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def is_governed_claude_md(
    path: Union[str, Path],
    home: Optional[Path] = None,
    repo_root: Optional[Union[str, Path]] = None,
    audience_manifest: Optional[Iterable[str]] = None,
) -> bool:
    """True iff `path` is a FLEET-LOADED (governed) CLAUDE.md-class surface.

    Purpose: replaces a bare `os.path.basename(path) == "CLAUDE.md"` match
    (the shape `check-claude-md-size.py` used before this module existed),
    which fires on ANY file named CLAUDE.md — including a repo-scoped copy
    (example-doctrine-repo's own repo-root CLAUDE.md, claude-klabauter's, any sibling
    repo's) that is not fleet-loaded and must not share this budget. See
    `coordinator/docs/wiki/claude-md-surfaces.md` for the enumerated surface
    table this function encodes.

    Governed (True), unconditionally:
        - `~/.claude/CLAUDE.md` (global — every session on the machine —
          note the `.claude/` subdirectory: `~/CLAUDE.md` bare is NOT this
          surface and is never governed).
        - `<repo-root>/coordinator/CLAUDE.md` where `<repo-root>` carries the
          `.coordinator-dev-repo` sentinel file at its root — i.e. the
          example-doctrine-repo coordinator plugin-doctrine source file specifically,
          never a percolated/installed copy elsewhere.

    Governed (True), ADDITIONALLY (C7b, AC3 -- audience, not filename), only
    when BOTH `repo_root` is supplied AND `path` matches an entry of
    `audience_manifest` (or, if `audience_manifest` is not passed explicitly,
    of `load_audience_manifest(repo_root)`) resolved under `repo_root`. This
    is how a NEW always-on surface — of any name, e.g.
    `coordinator/snippets/agent-role-dispatched.md` — becomes governed
    without a further edit here: the repo's own manifest names it.

    NOT governed (False), even though the basename matches, absent a
    manifest entry naming it:
        - `<repo-root>/CLAUDE.md` (any repo's own project-scoped file,
          example-doctrine-repo's included — that file lives at repo root, not under
          `coordinator/`).
        - `<any-other-repo>/coordinator/CLAUDE.md` lacking the dev-repo
          sentinel (an OSS install, a percolated mirror, or any tree that
          merely happens to have a `coordinator/CLAUDE.md`-shaped path).

    Args:
        path: candidate path (absolute or relative — resolved before compare).
        home: override for the operator home dir (test seam); defaults to
            `Path.home()`.
        repo_root: the repo `path` is presumed to live under, for resolving
            `audience_manifest` entries. `None` disables the audience-manifest
            leg entirely (pure back-compat with the pre-C7b two-surface set).
        audience_manifest: explicit override for the manifest entries (test
            seam / caller-precomputed manifest); when `None` and `repo_root`
            is given, resolved via `load_audience_manifest(repo_root)`.

    Returns:
        True iff `path` resolves to one of the governed surfaces above.
    """
    resolved = Path(path).resolve()
    home_dir = (home or Path.home()).resolve()

    if resolved == (home_dir / ".claude" / "CLAUDE.md").resolve():
        return True

    if resolved.name == "CLAUDE.md" and resolved.parent.name == "coordinator":
        repo_root_for_sentinel = resolved.parent.parent
        if (repo_root_for_sentinel / DEV_REPO_SENTINEL).is_file():
            return True

    if repo_root is not None:
        root = Path(repo_root).resolve()
        manifest = (
            list(audience_manifest)
            if audience_manifest is not None
            else load_audience_manifest(root)
        )
        for entry in manifest:
            if resolved == (root / Path(entry)).resolve():
                return True

    return False


def is_claude_md_class(path: Union[str, Path]) -> bool:
    """True iff `path` belongs to the FULL CLAUDE.md-class surface family —
    every file a example-doctrine-repo session can encounter that carries CLAUDE.md-shaped
    fleet/doctrine content, not just the narrower FLEET-LOADED subset
    `is_governed_claude_md` discriminates for the byte-budget gate.

    Widens `is_governed_claude_md` (which covers 2 of the 5-6 surfaces
    enumerated in example-doctrine-repo `coordinator/docs/wiki/claude-md-surfaces.md` —
    the wiki's surface table is this predicate's DEFINITION, re-derive from
    there before editing this docstring) to the full class:

        - `~/.claude/CLAUDE.md`               (global — harness auto-load)
        - `<repo-root>/global-doctrine/CLAUDE.md` (tracked backup mirror)
        - `<repo-root>/coordinator/CLAUDE.md`  (plugin doctrine, any repo —
          no dev-repo-sentinel gate here, unlike `is_governed_claude_md`:
          this predicate answers "is this CLAUDE.md-shaped", not "is this
          the one fleet-loaded copy")
        - `coordinator/templates/CLAUDE.md.tmpl` (the installer template)
        - `<repo-root>/CLAUDE.md`              (any repo's own project file)

    Matched BY PATTERN (basename + immediate-parent-directory name), never
    a hardcoded path list — a hand-maintained list is the exact drift class
    this predicate exists to end (see module docstring). Every one of the
    five bullets above is either literally named `CLAUDE.md` (bullets 1-3
    and 5 collapse to one basename check) or `CLAUDE.md.tmpl` under a
    `templates/` directory (bullet 4) — there is no repo- or path-specific
    branch to maintain as new repos/installs appear.

    Negative-spec — do NOT narrow this to `is_governed_claude_md`'s
    dev-repo-sentinel gate. That gate exists to answer a DIFFERENT question
    (which single copy is fleet-loaded, for the byte-budget ceiling); this
    predicate answers "is this file CLAUDE.md-class at all" for consumers
    (e.g. engine-side write_guards) that must recognize every surface in
    the class, not only the one governed for byte budget.

    Args:
        path: candidate path (absolute or relative — resolved before compare).

    Returns:
        True iff `path` resolves to a CLAUDE.md-class surface per the
        pattern rules above.

    Spec backlink: example-doctrine-repo docs/plans/2026-07-27-claude-md-altitude-triage.md
    § C2; class definition: example-doctrine-repo coordinator/docs/wiki/claude-md-surfaces.md.
    """
    resolved = Path(path).resolve()

    if resolved.name == "CLAUDE.md":
        return True

    if resolved.name == "CLAUDE.md.tmpl" and resolved.parent.name == "templates":
        return True

    return False


def is_ledger_admission_scoped(path: Union[str, Path]) -> bool:
    """True iff `path` is in scope for the C7 CI-tier admission check —
    widened (2026-07-27, C3) from `is_governed_claude_md`'s narrow
    fleet-loaded-only surface to the FULL `is_claude_md_class` family, so
    `coordinator/CLAUDE.md` (any repo, no dev-repo-sentinel gate) and the
    installer template (`coordinator/templates/CLAUDE.md.tmpl`) are subject
    to the same CI-tier check that previously covered only the two
    fleet-loaded surfaces `is_governed_claude_md` recognizes.

    `doctrine-envelope-allocation` C7 (`ff2255c1e`) introduced two
    independent enforcement points that share one predicate module
    (`_claude_md_ledger.py`, example-doctrine-repo): a PreToolUse hook and a CI-path
    invariant test. Both currently scope themselves to `~/.claude/CLAUDE.md`
    only. `coordinator_core.bash_guards.dispatch_checks.check_validate_commit`
    ("Check 7", this repo's own commit-time CI-tier byte-budget gate) is the
    third, narrower enforcement point this predicate widens directly: it
    currently scopes its `claudemd_files` selection to `is_governed_claude_md`,
    the same narrow surface. Consumers migrating to the widened CI-tier scope
    should import THIS function rather than `is_governed_claude_md`.

    This is a THIN wrapper over `is_claude_md_class` — see that function for
    the pattern-matched surface family — introduced under a separate name
    (not a redefinition of `is_governed_claude_md`) because the two
    predicates answer different questions that must not be conflated:
    `is_governed_claude_md` answers "is this the ONE fleet-loaded
    byte-budget surface" (must stay narrow — several existing tests in this
    module pin its exact NOT-governed cases); this predicate answers "is
    this in scope for the C7 CI-tier admission check", which
    `docs/plans/2026-07-27-claude-md-altitude-triage.md` DEC-3 widens
    deliberately.

    Negative-spec — do NOT read this as retroactively widening the
    `~/.claude/CLAUDE.md`-only C2(narrowed) ledger's own per-heading
    disposition table (`state/audits/2026-07-27-doctrine-envelope-
    classification.md`). That ledger's own "Scope" note explicitly excludes
    `coordinator/CLAUDE.md` and `CLAUDE.md.tmpl` — their disposition is
    tracked by a sibling plan's own classification sidecar. This predicate
    widens WHICH FILES the byte-budget/CI-tier check applies to, not the
    per-heading admission logic that ledger drives.

    Args:
        path: candidate path (absolute or relative — resolved before compare).

    Returns:
        True iff `path` resolves to a CLAUDE.md-class surface per
        `is_claude_md_class`.

    Spec backlink: example-doctrine-repo docs/plans/2026-07-27-claude-md-altitude-triage.md
    § C3.
    """
    return is_claude_md_class(path)


def governed_surface_paths(
    home: Optional[Path] = None,
    repo_root: Optional[Union[str, Path]] = None,
    audience_manifest: Optional[Iterable[str]] = None,
) -> List[Path]:
    """Enumerate the known governed CLAUDE.md-class surfaces for a machine/repo.

    Purpose: convenience list for callers (e.g. the token oracle CLI) that want
    to measure "the governed set" without hand-listing paths. Always includes
    the global `~/.claude/CLAUDE.md`. Includes `<repo_root>/coordinator/CLAUDE.md`
    only when `repo_root` is supplied AND carries the dev-repo sentinel — a
    caller running from a non-example-doctrine-repo repo (or omitting `repo_root`) gets
    just the one global entry, which is correct: there is no second governed
    surface to report in that case.

    C7b (AC3): when `repo_root` is supplied, ALSO appends every entry of
    `audience_manifest` (or, if not passed explicitly,
    `load_audience_manifest(repo_root)`) resolved under `repo_root` — the
    audience-governed set, of any filename, a repo names in its own manifest.
    An unmanifested repo (no file at `AUDIENCE_MANIFEST_RELPATH`, no explicit
    `audience_manifest` passed) appends nothing here, matching pre-C7b
    behaviour exactly.

    Args:
        home: override for the operator home dir (test seam); defaults to
            `Path.home()`.
        repo_root: candidate repo root to probe for the dev-repo sentinel and
            to resolve `audience_manifest` entries against.
        audience_manifest: explicit override for the manifest entries (test
            seam); when `None` and `repo_root` is given, resolved via
            `load_audience_manifest(repo_root)`.

    Returns:
        List of `Path` objects (existence NOT checked here — the token oracle
        degrades a missing path gracefully; see
        `coordinator_core.ops.measure_token_envelope.measure_surface`).
    """
    home_dir = home or Path.home()
    paths: List[Path] = [home_dir / ".claude" / "CLAUDE.md"]

    if repo_root is not None:
        root = Path(repo_root)
        if (root / DEV_REPO_SENTINEL).is_file():
            paths.append(root / "coordinator" / "CLAUDE.md")

        manifest = (
            list(audience_manifest)
            if audience_manifest is not None
            else load_audience_manifest(root)
        )
        for entry in manifest:
            paths.append(root / Path(entry))

    return paths


# ---------------------------------------------------------------------------
# C7b — the AC4 ratchet watermark. Grammar-compatible with (but NOT importing)
# example-doctrine-repo's `coordinator/hooks/scripts/_claude_md_ledger.py` -- see the
# module docstring's "C7b addendum" for why this is a re-implementation, not
# a shared dependency.
# ---------------------------------------------------------------------------

#: Explicit ledger-path override for a governed surface whose classification
#: ledger predates the `state/audits/<surface-slug>-classification.md`
#: convention -- mirrors example-doctrine-repo's `_claude_md_ledger._LEDGER_PATH_OVERRIDES`
#: exactly (same surface, same target path) so a watermark armed in that
#: ledger is found by both enforcement points without further coordination.
_LEDGER_PATH_OVERRIDES = {
    "global-doctrine/CLAUDE.md": "state/audits/2026-07-27-doctrine-envelope-classification.md",
}

_WATERMARK_HEADING = "## Watermark"
_WATERMARK_BYTES_RE = re.compile(r"^-\s*Bytes:\s*(\d+)\s*$", re.IGNORECASE)
_WATERMARK_REASON_RE = re.compile(r"^-\s*Reason:\s*(.+?)\s*$", re.IGNORECASE)


class RatchetWatermarkError(Exception):
    """Raised when a ledger's "## Watermark" section exists but is malformed
    (no `- Bytes: N` row, or no non-empty `- Reason: ...` row) -- an
    armed-but-broken watermark must fail loud, never silently disable the
    ratchet it claims to enforce. Mirrors example-doctrine-repo's `_claude_md_ledger.LedgerError`
    raised from the same condition, without importing that module."""


@dataclass(frozen=True)
class RatchetWatermark:
    bytes: int
    reason: str


def surface_slug(surface: str) -> str:
    """`global-doctrine/CLAUDE.md` -> `global-doctrine-claude-md` -- the
    conventional ledger-filename fragment for a governed surface with no
    `_LEDGER_PATH_OVERRIDES` entry. Mirrors example-doctrine-repo's `_claude_md_ledger.
    surface_slug` byte-for-byte (path separators become hyphens, a trailing
    `.md` is dropped, the whole slug is lower-cased)."""
    slug = surface.replace("/", "-")
    if slug.lower().endswith(".md"):
        slug = slug[: -len(".md")]
    return slug.lower()


def resolve_ledger_path(repo_root: Union[str, Path], surface: str) -> Path:
    """Resolve `surface`'s (repo-root-relative, POSIX-separated) per-surface
    classification ledger path under `repo_root`. An override in
    `_LEDGER_PATH_OVERRIDES` wins; otherwise the path is the
    `state/audits/<surface-slug>-classification.md` convention -- the exact
    convention example-doctrine-repo's `_claude_md_ledger.resolve_ledger_path` already uses, so
    a watermark armed there resolves here with no further coordination."""
    override = _LEDGER_PATH_OVERRIDES.get(surface)
    if override:
        return Path(repo_root) / Path(override)
    return Path(repo_root) / "state" / "audits" / f"{surface_slug(surface)}-classification.md"


def parse_watermark(ledger_path: Union[str, Path]) -> Optional[RatchetWatermark]:
    """Parse the ratchet watermark from `ledger_path`'s own "## Watermark"
    section. Returns `None` when the ledger is missing or carries no
    "## Watermark" section at all -- the ratchet is simply UNARMED for that
    surface, not an error (arming it is a per-surface, ledger-authoring act,
    not this function's job). Raises `RatchetWatermarkError` when the section
    EXISTS but is malformed."""
    path = Path(ledger_path)
    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == _WATERMARK_HEADING:
            start = i
            break
    if start is None:
        return None

    bytes_val: Optional[int] = None
    reason_val: Optional[str] = None
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        m = _WATERMARK_BYTES_RE.match(stripped)
        if m:
            bytes_val = int(m.group(1))
            continue
        m2 = _WATERMARK_REASON_RE.match(stripped)
        if m2:
            reason_val = m2.group(1)

    if bytes_val is None:
        raise RatchetWatermarkError(
            f"Admission ledger at {path} has a '## Watermark' section with "
            f"no '- Bytes: N' row -- the ratchet cannot be enforced against "
            f"a malformed watermark. Fix the row by hand; this parser will "
            f"not guess."
        )
    if not reason_val:
        raise RatchetWatermarkError(
            f"Admission ledger at {path}'s '## Watermark' section has no "
            f"non-empty '- Reason: ...' row -- a watermark bump requires a "
            f"stated reason. Add one before this ledger can arm the ratchet."
        )
    return RatchetWatermark(bytes=bytes_val, reason=reason_val)


def ratchet_check(new_size_bytes: int, watermark: Optional[RatchetWatermark]) -> Tuple[bool, str]:
    """The AC4 ratchet predicate: a governed surface may shrink or hold,
    never grow past its recorded watermark, without an explicit reasoned
    bump. `watermark is None` means the ratchet is unarmed for this surface
    (nothing to check yet) -- always allowed."""
    if watermark is None:
        return True, ""
    if new_size_bytes > watermark.bytes:
        return False, (
            f"Refused: this edit grows the surface to {new_size_bytes} bytes, "
            f"past its recorded ratchet watermark of {watermark.bytes} bytes "
            f"(bumped for: {watermark.reason}). The budget only shrinks or "
            f"holds without an explicit, reasoned watermark bump -- raise "
            f"the ledger's '## Watermark' 'Bytes:' row and state a new "
            f"'Reason:' for the bump, or trim the addition back under the "
            f"watermark."
        )
    return True, ""
