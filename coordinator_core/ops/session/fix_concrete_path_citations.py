r"""
coordinator_core.ops.session.fix_concrete_path_citations — the remedy for
`guard_concrete_path_citations` (this package), a hard-deny write/commit
guard that refuses any concrete absolute-path citation (a real username, a
Windows drive letter, a UNC share, or a path mixing `/` and `\`) anywhere in
a tracked file. That guard names the problem; this module is the tool that
fixes it, so an operator who hits the deny has a next action rather than
having to hand-invent the same sweep every time.

Origin: ported from a one-off scratch tool (`sweep.py` + `path_map.json`,
DoE-claude `scratch/abs-path-sweep/`) that took one repo's corpus from 4,254
findings to 0. That script proved the approach; this module is its
permanent, cross-platform, engine-resident successor — the enforcement
(the guard) is permanent, so the remedy must be too.

Three outcomes per finding, never a bare substitution
--------------------------------------------------------
A tool that only does blind substitution corrupts a corpus that also
contains genuine incident evidence and byte-exact test fixtures. Every hit
resolves to exactly one of:
  - **substitute** — a mapped path with a canonical, runtime-resolvable
    target. Only these are ever rewritten by ``--apply``.
  - **marker** — the literal IS the data and must survive: an existing
    ``abs-path-ok:``/``foreign-path-ok: <reason>`` line (already
    adjudicated), or a live-doctrine incident writeup that looks like it
    SHOULD carry one but doesn't yet. Never rewritten either way — a marker
    the tool invents the reason for is worse than one a human writes.
  - **report-only** — no canonical target exists (an unmapped family), the
    hit sits in a test/fixture/code file where even the correct citation
    form is not itself a runtime-resolvable value (see
    `_CODE_EXTENSIONS`/`_is_test_file`), or the hit sits in RECORDED rather
    than authored content — a captured diff body, a review-trail artifact,
    an agent share sidecar (see `_is_recorded_content`). Surfaced, never
    guessed at.

Recorded content is quoted, not authored
----------------------------------------
A recorded artifact's whole value is that it says what was actually there.
Rewriting a path inside a captured `.diff`/`.patch` body, a
`state/review-trail/**` evidence file, or a `state/subagent-share/**`
sidecar does not *correct* a citation — it falsifies the record, and
(for a diff) can also make a byte-addressed hunk no longer apply. These
are report-only unconditionally, exactly like a fixture's exact-byte
assertion.

KNOWN GAP — fenced code blocks inside markdown are NOT carved out. A
```-fenced block in an ordinary authored `.md` is quoted content by the
same argument, but detecting it correctly needs stateful fence tracking
(tilde fences, indented fences, varying backtick runs, front-matter) fed
through `_raw_hits_in_text`'s currently line-local scan, plus a decision
about inline-code spans. That is its own change, not a clause bolted onto
`classify`, and a half-implemented fence tracker that mis-closes on a
nested fence would silently un-protect the very content it claims to
protect. Left explicitly open.

Family discovery is machine-local-derived, not hardcoded
-----------------------------------------------------------
The path families a fleet cares about (which repos exist, what their
canonical short names are) are per-machine, per-fleet data — not something
this engine module should bake in as a literal Python constant (that would
be exactly the "one operator's machine as if it were universal" defect the
guard itself exists to catch, just moved into the remedy). `discover_families`
builds the family list at call time from the machine-local registry
(`repos.*`, `publish.mirrors.*.path`), read in-process straight off the
registry TOML via `machine_resolver` rather than through the `machine-local`
CLI (see `_default_registry_keys` for why the CLI shell-out is not an option
on Windows) — so this tool is useful in any repo whose sibling set differs,
with zero code change, the same design the guard's own fix-text already
points operators at (`machine-local get repos.<key>`). Two universal config
families (`${CLAUDE_HOME:-$HOME}/.claude`, the coordinator settings-home) are added
unconditionally — they are OS/install conventions, not personal paths.

Idempotency
-----------
No canonical replacement form (a `short_name:rest` citation, the
`${CLAUDE_HOME:-$HOME}/.claude` family, the settings-home form) matches any of
the four detection rules in
`guard_concrete_path_citations` — a rewritten line can never be re-flagged
by a second pass. Proven by the op's own test suite (apply twice, diff the
two passes) across all four detection rule shapes and both family
categories (repo/publish_mirror and config), not merely asserted here.

CRLF safety
-----------
Every read/write goes through `open(..., newline="")`, which disables
Python's universal-newline translation in both directions — a CRLF file's
line endings pass through untouched, and a rewritten line is spliced back in
with whatever terminator it already had. A naive `Path.read_text()` /
`Path.write_text()` round-trip would silently flip every CRLF in a touched
file to LF, turning a targeted content fix into a 100%-noise diff on any
file this fleet's Windows tooling (e.g. `break-glass.cmd`) depends on being
CRLF.

Marker vocabulary
-----------------
Both spellings are honored, matching the shared detector
(`guard_concrete_path_citations._MARKER_TOKENS`): `abs-path-ok:` (the token
this guard's own deny text names) and `foreign-path-ok:` (the older token
from the sibling host-conditioned guard, `guard_foreign_platform_paths.py`,
already carrying real adjudicated reasons across this fleet's trees). A
BARE marker with no reason text does not exempt a line — it is scanned
exactly as if the marker weren't there.

Explicit-path operation
------------------------
With no positional PATHS, `main` sweeps the full tracked tree under `--root`
(default: the cwd's git toplevel) via `git ls-files`, exactly as before.
Given one or more positional PATHS, it sweeps exactly those files instead --
tracked or not, inside a git repo or not -- so the write-time guard's own
advisory remedy (`<fixer> --apply <the file just written>`) has something to
invoke against a freshly written UNTRACKED file, or a file outside any git
repo entirely, neither of which `git ls-files` would ever enumerate. Each
path's owning root (the git toplevel containing it, falling back to its own
parent directory) is resolved independently and paths are grouped by root
before `sweep()` runs, once per root group -- see `_sweep_explicit_paths`'s
docstring for why that grouping is load-bearing rather than cosmetic.
`--root` combined with positional PATHS is a usage error: the roots are
derived per path, not supplied globally.

Reused primitives
-----------------
The four detection-shape regexes and their placeholder/well-known-root
exemptions are NOT re-derived here — they are imported from the sibling
`guard_concrete_path_citations` module so this tool and the write-time/
commit-time guards can never drift apart about what counts as a citation.
This module's own scan additionally needs the *token* around each match
(the guard's `Finding` only carries the matched root, not the trailing
path), so it reimplements the anchor-scan loop rather than consuming
`detect_in_text` directly — see `_raw_hits_in_text`.
"""


from __future__ import annotations

MUTATES = [
    "**/*.py",
    "**/*.sh",
    "**/*.bats",
    "**/*.js",
    "**/*.ts",
    "**/*.json",
    "**/*.yaml",
    "**/*.yml",
    "**/*.toml",
    "**/*.md",
]  # --apply rewrites any tracked file (_CODE_EXTENSIONS plus markdown) carrying a mapped concrete-path finding; data-dependent set

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.session.guard_concrete_path_citations import (
    WIN_DRIVE_RE,
    _ANCHOR_RES,
    _POSIX_HOME_RE,
    _UNC_RE,
    _extract_token,
    _has_ellipsis_segment,
    _is_placeholder_segment,
    _is_win_drive_root_exempt,
    _line_has_marker_with_reason,
)

# --- outcomes -------------------------------------------------------------

SUBSTITUTE = "substitute"
MARKER = "marker"
REPORT_ONLY = "report-only"

# --- carve-outs ported from the originating scratch sweep ------------------
# (sweep.py's _classify docstring: correctness limit, everywhere, archive
# included -- a fixture's exact-byte assertion or an executable/structured
# code context is not a place a repo-qualified citation is itself a
# runtime-resolvable value, so even the CORRECT canonical form would be
# wrong there.)

_CODE_EXTENSIONS = {".py", ".sh", ".bats", ".js", ".ts", ".json", ".yaml", ".yml", ".toml"}
_TEST_MARKERS = ("/tests/", "/fixtures/", ".expected", "golden", "snapshot")
_TEST_NAME_PREFIXES = ("test_",)
_TEST_NAME_SUFFIXES = (".test.js",)
# `baseline` is anchored to the filename's stem-suffix position
# (`*-baseline.ext` / `*_baseline.ext` / bare `baseline.ext`), not a bare
# substring -- a bare check misclassified any live-doctrine path merely
# CONTAINING the word (a wiki page titled "...-baseline-established.md")
# as a fixture file, silently under-fixing doctrine that should have been
# corrected.
_BASELINE_NAME_RE = re.compile(r"(?:^|[-_])baseline\.[^.]+$")

# Recorded-content carve-out: content this tool QUOTES rather than authors.
# A rewrite here does not correct a citation, it falsifies the record -- and
# for a diff body it can also break the hunk. Matched two ways, mirroring
# the test/code carve-out above: by file extension (a whole file that IS a
# capture) and by path prefix (a tree whose entire job is holding recorded
# evidence). Prefixes are tested against `/{rel}` so a nested checkout
# (`some-repo/state/review-trail/...`) matches the same as a root-relative
# one -- the same `/`-anchored idiom `_TEST_MARKERS` uses.
_RECORDED_EXTENSIONS = {".diff", ".patch", ".rej", ".orig"}
_RECORDED_PATH_PREFIXES = (
    "state/review-trail/",
    "state/review-findings/",
    "state/subagent-share/",
)

_LIVE_DOCTRINE_PREFIXES = (
    "coordinator/docs/wiki/",
    "coordinator/skills/",
    "coordinator/agents/",
    "coordinator/commands/",
    "coordinator/snippets/",
    "coordinator/pipelines/",
    "global-doctrine/",
)
_LIVE_DOCTRINE_FILES = {
    "CLAUDE.md",
    "INSTALL.md",
    "README.md",
    "DIRECTORY.md",
    "CONTEXT.md",
    "coordinator.local.md",
}
_INCIDENT_EVIDENCE_WORDS = re.compile(
    r"\b(corrupt(?:ed)?|leaked?|incident|the real path|misidentif|orphan(?:ed)?)\b",
    re.IGNORECASE,
)

_EXCLUDE_DIR_MARKERS = ("/node_modules/", "/.git/", "/__pycache__/", "/dist/", "/.coordinator-venv/")
_SIZE_CAP_BYTES = 2_000_000

# Two universal config families -- OS/install conventions, not one
# operator's path. Matched by testing whether the matched TOKEN contains one
# of these folder-name substrings (case-sensitive: both are fixed, real
# folder names, never a placeholder segment).
_CLAUDE_DIR_SEGMENT = ".claude"
_SETTINGS_HOME_SEGMENT = ".coordinator-claude-settings"


def _is_test_file(rel: str) -> bool:
    if any(m in f"/{rel}" for m in _TEST_MARKERS):
        return True
    name = rel.rsplit("/", 1)[-1]
    if _BASELINE_NAME_RE.search(name):
        return True
    if any(name.startswith(p) for p in _TEST_NAME_PREFIXES):
        return True
    if any(name.endswith(s) for s in _TEST_NAME_SUFFIXES):
        return True
    return False


def _extension(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _is_recorded_content(rel: str) -> bool:
    """True when `rel` holds content that was CAPTURED rather than written --
    a diff/patch body or a recorded-evidence tree. See the module docstring's
    "Recorded content is quoted, not authored"."""
    if _extension(rel) in _RECORDED_EXTENSIONS:
        return True
    return any(f"/{rel}".find(f"/{p}") >= 0 for p in _RECORDED_PATH_PREFIXES)


def _is_live_doctrine(rel: str) -> bool:
    return rel in _LIVE_DOCTRINE_FILES or any(rel.startswith(p) for p in _LIVE_DOCTRINE_PREFIXES)


# ---------------------------------------------------------------------------
# Family discovery -- machine-local-derived, not hardcoded.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    id: str
    category: str  # "repo" | "publish_mirror" | "config"
    match_name: str  # folder-name segment to look for in a token, e.g. "DoE-claude"
    short_name: str  # citation prefix, "" for a config family (canonical is fixed text)
    canonical: Optional[str] = None  # fixed replacement text, config families only


_REGISTRY_FILES = ("registry.local.toml", "registry.toml")


def _default_registry_keys() -> List[str]:
    """Every dotted key declared across the machine-local registry files, in
    `_REGISTRY_FILES` precedence order, de-duplicated.

    Reads the registry TOML directly via `coordinator_core.machine_resolver`
    rather than shelling out to the `machine-local` CLI. That CLI's entry
    point is an extensionless POSIX shim under the settings home; on Windows
    `subprocess.run` cannot exec it at all (`OSError: [WinError 193]`), which
    the caller's degrade-to-None contract then swallowed into "zero repo
    families" -- every citation classified `no mapped family` and nothing
    rewritten, on the platform this fleet actually commits from. The direct
    read is also reset-survivable (the CLI's exec bits live under the
    resettable `~/.claude`, the registry TOML does not) -- the same reasoning
    that promoted `machine_resolver.registry_get` in DR-071.

    Enumeration only reads the on-disk files: unlike `registry_get`, the
    `MACHINE_LOCAL_<KEY>` env-override rung cannot participate, because
    `MACHINE_LOCAL_REPOS_CLAUDE_KLABAUTER_REPO` does not invert to a unique dotted
    key. Family discovery needs key NAMES, never values, so an env-pinned
    value for an already-declared key is unaffected.

    Degrades to `[]` on an unreachable or malformed registry (the underlying
    loader is catch-all), never a crash -- see
    `_warn_if_family_discovery_degraded` for how that surfaces.
    """
    try:
        reg_dir = registry_dir()
    except Exception:  # noqa: BLE001 -- settings-home resolution failure degrades like an absent registry
        return []
    seen: set = set()
    keys: List[str] = []
    for fname in _REGISTRY_FILES:
        for key in load_flat_registry_file(reg_dir / fname):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def discover_families(
    keys: Callable[[], List[str]] = _default_registry_keys,
) -> List[Family]:
    """Build the family list from the machine-local registry, plus the two
    universal config families. `keys` is injectable for tests -- it returns
    the registry's dotted key names, mirroring `_default_registry_keys`."""
    families: List[Family] = []
    all_keys = [k.strip() for k in (keys() or [])]
    repo_keys = [k for k in all_keys if k.startswith("repos.") and k.count(".") == 1]
    mirror_path_keys = [
        k for k in all_keys if k.startswith("publish.mirrors.") and k.endswith(".path")
    ]

    for key in repo_keys:
        name = key[len("repos."):]
        short = name.replace("_", "-")
        families.append(
            Family(id=f"repo_{name}", category="repo", match_name=short, short_name=short)
        )

    for key in mirror_path_keys:
        mid = key[len("publish.mirrors."):-len(".path")]
        short = mid.replace("_", "-")
        families.append(
            Family(
                id=f"publish_mirror_{mid}",
                category="publish_mirror",
                match_name=short,
                short_name=short,
            )
        )

    # Longest match_name first. `_locate_family`'s exact-segment match
    # already keeps "example-retrieval-repo-ue-addon" and "example-retrieval-repo" from ever
    # colliding on the SAME segment (a segment can only equal one of the
    # two, exactly), so this sort is a deterministic tie-break rather than
    # the primary defense -- kept anyway so family discovery order never
    # depends on `machine-local keys`' own (unspecified) ordering.
    families.sort(key=lambda f: -len(f.match_name))

    families.append(
        Family(
            id="claude_config_dir",
            category="config",
            match_name=_CLAUDE_DIR_SEGMENT,
            short_name="",
            canonical="${CLAUDE_HOME:-$HOME}/.claude",
        )
    )
    families.append(
        Family(
            id="settings_home",
            category="config",
            match_name=_SETTINGS_HOME_SEGMENT,
            short_name="",
            canonical="${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}",
        )
    )
    return families


def _normalize_segment(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _iter_segments(token: str) -> List[Tuple[int, int, str]]:
    """(start, end, text) for every `/`- or `\\`-delimited segment of
    `token`, in order -- the unit a family match must fill EXACTLY (see
    `_locate_family`)."""
    segments: List[Tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"[\\/]+", token):
        segments.append((start, m.start(), token[start : m.start()]))
        start = m.end()
    segments.append((start, len(token), token[start:]))
    return segments


def _locate_family(token: str, families: List[Family]) -> Optional[Tuple[Family, int]]:
    """First family (already longest-match_name-first for repo/mirror
    families, a deterministic tie-break -- see the sort below) whose
    match_name equals an ENTIRE path segment of `token`, normalized -- not
    merely a normalized substring. A substring test on the whole token
    cannot tell "true subpath boundary" from "prefix of an unrelated
    folder" (`claude-klabauter` is a normalized substring of
    `claude-klabauter-backup-2026` either way); requiring a full-segment
    match means the family name must be bounded by `/`, `\\`, or the ends
    of the token on BOTH sides, so a longer sibling name can never be
    mistaken for a shorter one's prefix. Returns the family plus the token
    offset immediately AFTER the matched segment -- the start of the
    trailing subpath `_replacement_for` needs."""
    segments = _iter_segments(token)
    for fam in families:
        norm_name = _normalize_segment(fam.match_name)
        if not norm_name:
            continue
        for _start, end, seg in segments:
            if _normalize_segment(seg) == norm_name:
                return fam, end
    return None


def _find_family(token: str, families: List[Family]) -> Optional[Family]:
    located = _locate_family(token, families)
    return located[0] if located else None


def _replacement_for(token: str, fam: Family, cut: Optional[int] = None) -> str:
    """Canonical replacement text for one matched token given its family.
    `cut` is the token offset right after the matched segment, as returned
    by `_locate_family`; re-derived here (against just `fam`) when the
    caller didn't already have it."""
    if cut is None:
        located = _locate_family(token, [fam])
        cut = located[1] if located else len(token)
    if fam.category == "config":
        assert fam.canonical is not None
        # The canonical text replaces the matched DIRECTORY, not the whole
        # token -- `<home>/.claude/settings.json` must keep its
        # `/settings.json`. Dropping the tail would silently retarget the
        # citation at the directory under `--apply`, which is a content
        # change dressed as a path fix.
        tail = token[cut:].lstrip("/\\").replace("\\", "/")
        return f"{fam.canonical}/{tail}" if tail else fam.canonical
    # repo / publish_mirror: <short_name>:<trailing-subpath-after-repo-name>,
    # or bare short_name with no trailing subpath. Per
    # coordinator/docs/wiki/cross-repo-citation-conventions.md.
    rest = token[cut:]
    rest = rest.lstrip("/\\").replace("\\", "/")
    if rest:
        return f"{fam.short_name}:{rest}" if fam.short_name else rest
    return fam.short_name if fam.short_name else token


# ---------------------------------------------------------------------------
# Raw hit scan -- token-aware, marker-aware. Deliberately NOT
# `detect_in_text` (which discards marked lines and only carries the root of
# a match, not the trailing path a substitution needs).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    rule: str
    token: str
    marked: bool


def _raw_hits_in_line(line: str) -> List[Tuple[str, str]]:
    """Return (rule, token) pairs for every non-exempt path-shape match in
    one line -- same four rules, same placeholder/well-known-root exemptions
    as `guard_concrete_path_citations.detect_in_text`, but keeping the whole
    token (not just the matched root) so a caller can compute a trailing
    subpath."""
    out: List[Tuple[str, str]] = []

    for m in _POSIX_HOME_RE.finditer(line):
        if _is_placeholder_segment(m.group(1)):
            continue
        out.append(("posix-home", _extract_token(line, m.start())))

    for m in WIN_DRIVE_RE.finditer(line):
        token = _extract_token(line, m.start())
        root_len = m.end() - m.start()
        if _is_win_drive_root_exempt(token, root_len) or _has_ellipsis_segment(token, root_len):
            continue
        out.append(("drive-letter", token))

    for m in _UNC_RE.finditer(line):
        host = m.group(0).lstrip("\\").split("\\")[0]
        if _is_placeholder_segment(host):
            continue
        out.append(("unc", _extract_token(line, m.start())))

    for rx in _ANCHOR_RES:
        for m in rx.finditer(line):
            token = _extract_token(line, m.start())
            if "/" not in token or "\\" not in token:
                continue
            out.append(("mixed-separators", token))

    return out


def _raw_hits_in_text(text: str, filename: str) -> List[Hit]:
    hits: List[Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        marked = _line_has_marker_with_reason(line)
        for rule, token in _raw_hits_in_line(line):
            hits.append(Hit(filename, lineno, rule, token, marked))
    return hits


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    hit: Hit
    outcome: str  # SUBSTITUTE | MARKER | REPORT_ONLY
    replacement: Optional[str] = None  # only set for SUBSTITUTE
    reason: str = ""


def classify(hit: Hit, line_text: str, families: List[Family]) -> Finding:
    if hit.marked:
        return Finding(hit, MARKER, reason="already adjudicated (marker with reason present)")
    rel = hit.file
    if _is_test_file(rel):
        return Finding(hit, REPORT_ONLY, reason="test/fixture file -- correctness limit, not a fidelity call")
    if _extension(rel) in _CODE_EXTENSIONS:
        return Finding(hit, REPORT_ONLY, reason="executable/structured-config file -- correctness limit")
    if _is_recorded_content(rel):
        return Finding(hit, REPORT_ONLY, reason="recorded content (captured diff / evidence tree) -- rewriting it falsifies the record")
    if _is_live_doctrine(rel) and _INCIDENT_EVIDENCE_WORDS.search(line_text):
        return Finding(hit, MARKER, reason="looks like live-doctrine incident evidence -- needs a human-written abs-path-ok: reason, not a guess")
    located = _locate_family(hit.token, families)
    if located is None:
        return Finding(hit, REPORT_ONLY, reason="no mapped family for this citation")
    fam, cut = located
    return Finding(hit, SUBSTITUTE, replacement=_replacement_for(hit.token, fam, cut), reason=f"family: {fam.id}")


# ---------------------------------------------------------------------------
# CRLF-safe file I/O
# ---------------------------------------------------------------------------


def _read_preserving_newlines(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_preserving_newlines(path: Path, text: str) -> None:
    from coordinator_core.session.declared_writes import declare_write

    declare_write(str(path))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _split_keeping_endings(text: str) -> List[Tuple[str, str]]:
    """Split into (body, ending) pairs -- ending is '', '\\n', '\\r\\n', or
    '\\r', preserved exactly so a rewritten body can be re-joined without
    ever touching a line terminator the diff doesn't concern."""
    out: List[Tuple[str, str]] = []
    for raw in text.splitlines(keepends=True):
        if raw.endswith("\r\n"):
            out.append((raw[:-2], "\r\n"))
        elif raw.endswith("\n"):
            out.append((raw[:-1], "\n"))
        elif raw.endswith("\r"):
            out.append((raw[:-1], "\r"))
        else:
            out.append((raw, ""))
    return out


def _tracked_files(root: Path) -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        **no_console_creationflags(),
    ).stdout
    paths = [p for p in out.split("\0") if p]
    return [p for p in paths if not any(m in f"/{p}" for m in _EXCLUDE_DIR_MARKERS)]


def _read(root: Path, rel: str) -> Optional[str]:
    full = root / rel
    try:
        if full.stat().st_size > _SIZE_CAP_BYTES:
            return None
        return _read_preserving_newlines(full)
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    findings: List[Finding]
    files_rewritten: List[str]  # apply=True only: files ACTUALLY written to disk
    files_matched: List[str]  # apply=True or False: files with >=1 SUBSTITUTE finding


def sweep(
    root: Path,
    families: List[Family],
    only_family: Optional[str] = None,
    apply: bool = False,
    list_files: Callable[[Path], List[str]] = _tracked_files,
) -> SweepResult:
    """Scan every tracked file under `root`. Only `SUBSTITUTE` findings for
    a matched family (filtered to `only_family` when given) are ever
    written; `MARKER`/`REPORT_ONLY` findings are always returned for
    reporting and never touch disk. `apply=False` (the CLI default) never
    writes -- `files_rewritten` stays empty in that mode; `files_matched`
    reports what a subsequent `--apply` run would touch.

    `list_files` defaults to `git ls-files` (production behavior); tests
    inject a plain filesystem walk so the sweep's own logic is verifiable
    against a scratch fixture with no git repo involved (git init is a
    destructive-action-guard-denied verb for a dispatched subagent)."""
    all_findings: List[Finding] = []
    files_rewritten: List[str] = []
    files_matched: List[str] = []

    for rel in list_files(root):
        text = _read(root, rel)
        if text is None:
            continue
        lines = _split_keeping_endings(text)
        hits = _raw_hits_in_text(text, rel)
        if not hits:
            continue

        touched = False
        # A single logical match can surface as two Hits carrying the same
        # (line, token) under two different rule labels (e.g. a mixed-
        # separator drive path is flagged as both "drive-letter" and
        # "mixed-separators" -- see `_raw_hits_in_line`, kept that way on
        # purpose to stay in lockstep with the guard's own duplicate
        # scan). Both classify to the same SUBSTITUTE outcome and the
        # first `.replace(..., 1)` already consumes the token, so the
        # second is a silent no-op edit -- but counting BOTH as a
        # substitution inflates the reported total for one real change.
        # Track which (line, token) pairs already counted as SUBSTITUTE
        # this file and only report the first.
        seen_substitutions: set = set()
        new_lines = list(lines)
        for hit in hits:
            body, ending = lines[hit.line - 1]
            finding = classify(hit, body, families)
            if finding.outcome == SUBSTITUTE and only_family and finding.hit.token:
                fam = _find_family(hit.token, families)
                if fam is None or fam.id != only_family:
                    finding = Finding(hit, REPORT_ONLY, reason=f"skipped (--only {only_family})")
            if finding.outcome == SUBSTITUTE:
                key = (hit.line, hit.token)
                if key in seen_substitutions:
                    continue
                seen_substitutions.add(key)
            all_findings.append(finding)
            if finding.outcome == SUBSTITUTE:
                new_body = new_lines[hit.line - 1][0].replace(hit.token, finding.replacement, 1)
                if new_body != new_lines[hit.line - 1][0]:
                    new_lines[hit.line - 1] = (new_body, new_lines[hit.line - 1][1])
                    touched = True

        if touched:
            files_matched.append(rel)
            if apply:
                new_text = "".join(b + e for b, e in new_lines)
                _write_preserving_newlines(root / rel, new_text)
                files_rewritten.append(rel)

    return SweepResult(all_findings, files_rewritten, files_matched)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _root_label(root: "Path | List[Path]") -> str:
    if isinstance(root, list):
        if len(root) == 1:
            return str(root[0])
        return f"{len(root)} roots ({', '.join(str(r) for r in root)})"
    return str(root)


def _print_report(
    result: SweepResult, root: "Path | List[Path]", apply: bool, only: Optional[str]
) -> None:
    counts = {SUBSTITUTE: 0, MARKER: 0, REPORT_ONLY: 0}
    for f in result.findings:
        counts[f.outcome] += 1
    print(f"=== fix-concrete-path-citations: root={_root_label(root)} apply={apply} only={only or 'ALL'} ===")
    print(f"total hits: {len(result.findings)}")
    for k in (SUBSTITUTE, MARKER, REPORT_ONLY):
        print(f"  {k}: {counts[k]}")
    print(f"files with >=1 substitute match: {len(result.files_matched)}")
    print(f"files actually rewritten on disk: {len(result.files_rewritten)}")
    if result.findings:
        print("--- detail (first 40) ---")
        for f in result.findings[:40]:
            extra = f" -> {f.replacement}" if f.replacement else f" ({f.reason})"
            print(f"  {f.hit.file}:{f.hit.line}  [{f.outcome}/{f.hit.rule}]  {f.hit.token}{extra}")
        if len(result.findings) > 40:
            print(f"  ... and {len(result.findings) - 40} more")
    if not apply and counts[SUBSTITUTE]:
        print("\n(dry-run -- pass --apply to write substitute rewrites; marker/report-only are never auto-written)")


def _warn_if_family_discovery_degraded(families: List[Family]) -> None:
    """`_default_registry_keys` returns `[]` uniformly for an absent
    registry directory, a broken `COORDINATOR_SETTINGS_HOME`, or a
    malformed TOML -- deliberately, so a registry read failure never
    crashes the tool. But that degrades silently to the two unconditional
    config families with zero repo/publish_mirror families, and the
    printed report ("no mapped family for this citation" on every hit) is
    indistinguishable from a corpus that is genuinely clean. Warn loudly
    instead of letting "I could not look" render as "there is nothing".

    This warning is the only thing that surfaced the Windows CLI-exec
    defect it now outlives: the tool shipped reporting zero families on
    every Windows run, and the report read as a clean corpus."""
    if not any(f.category in ("repo", "publish_mirror") for f in families):
        print(
            "warning: fix-concrete-path-citations discovered zero repo/publish_mirror "
            "families -- no repos.* / publish.mirrors.*.path keys were readable "
            "from the machine-local registry (check MACHINE_LOCAL_REGISTRY_DIR / "
            "COORDINATOR_SETTINGS_HOME). Every citation will report "
            "as 'no mapped family' rather than reflecting a genuinely clean corpus.",
            file=sys.stderr,
        )


def _git_toplevel_for(cwd: Path) -> Optional[Path]:
    """Repo toplevel for `cwd`, or None if `cwd` is not inside a git repo.
    Delegates to `coordinator_core.git.repo_root.show_toplevel`, which walks
    the tree for a `.git` entry (no spawn) and only spawns `git
    rev-parse --show-toplevel` as a fallback -- eliminating the fork beats
    suppressing it. The caller falls back to `cwd` itself when this returns
    None."""
    toplevel = show_toplevel(str(cwd))
    if toplevel is None:
        return None
    return Path(toplevel)


def _root_for_explicit_path(resolved: Path) -> Path:
    """Owning root for an explicit positional path: the git toplevel
    containing it, falling back to its own parent directory when no repo
    resolves (untracked file, or a path outside any git repo entirely)."""
    parent = resolved.parent
    toplevel = _git_toplevel_for(parent)
    return toplevel if toplevel is not None else parent


def _merge_sweep_results(results: List[SweepResult]) -> SweepResult:
    findings: List[Finding] = []
    files_rewritten: List[str] = []
    files_matched: List[str] = []
    for r in results:
        findings.extend(r.findings)
        files_rewritten.extend(r.files_rewritten)
        files_matched.extend(r.files_matched)
    return SweepResult(findings, files_rewritten, files_matched)


def _sweep_explicit_paths(
    paths: List[Path], families: List[Family], only_family: Optional[str], apply: bool
) -> Tuple[SweepResult, List[Path]]:
    """Sweep exactly the given files, tracked or not, git repo or not.

    Grouping by owning root (rather than sweeping bare basenames) is
    load-bearing, not tidiness: `classify()` branches on the rel path
    (`_is_live_doctrine`, `_is_recorded_content`, `_extension`), so a path
    stripped of its directory context would, for example, make this tool
    rewrite a frozen diff under `state/review-trail/diffs/` that
    `_is_recorded_content` exists to protect.
    """
    groups: "dict[Path, List[Path]]" = {}
    order: List[Path] = []
    for path in paths:
        resolved = path.resolve()
        root = _root_for_explicit_path(resolved)
        groups.setdefault(root, []).append(resolved)
        if root not in order:
            order.append(root)

    results: List[SweepResult] = []
    for root in order:
        rel_paths = [p.relative_to(root).as_posix() for p in groups[root]]
        results.append(
            sweep(
                root,
                families,
                only_family=only_family,
                apply=apply,
                list_files=lambda _root, _rels=rel_paths: _rels,
            )
        )
    return _merge_sweep_results(results), order


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fix-concrete-path-citations",
        description=(
            "Remediate concrete absolute-path citations flagged by "
            "guard_concrete_path_citations. With no positional PATHS, sweeps "
            "the full tracked tree under --root (or the cwd's git toplevel). "
            "With one or more positional PATHS, sweeps exactly those files "
            "instead -- tracked or not, inside a git repo or not -- so a "
            "freshly written untracked file (the guard's own advisory "
            "remedy target) can be remediated directly."
        ),
    )
    ap.add_argument("--root", type=Path, default=None, help="tree to sweep (default: cwd's git toplevel)")
    ap.add_argument("--only", default=None, help="restrict --apply to one family id (see --list-families)")
    ap.add_argument("--apply", action="store_true", help="write SUBSTITUTE rewrites (default: dry-run)")
    ap.add_argument("--list-families", action="store_true", help="print discovered families and exit")
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "sweep exactly these files (tracked or not, in a git repo or "
            "not) instead of the full tracked tree; incompatible with --root"
        ),
    )
    args = ap.parse_args(argv)

    if args.paths and args.root is not None:
        print("error: --root cannot be combined with explicit PATHS -- roots are derived per path", file=sys.stderr)
        return 2

    families = discover_families()
    _warn_if_family_discovery_degraded(families)
    if args.list_families:
        for fam in families:
            print(f"{fam.id}\t{fam.category}\tmatch_name={fam.match_name!r}\tshort_name={fam.short_name!r}")
        return 0

    if args.only is not None:
        known_ids = {fam.id for fam in families}
        if args.only not in known_ids:
            print(
                f"error: unknown --only family id {args.only!r}. Known ids: "
                f"{', '.join(sorted(known_ids)) or '(none discovered)'}",
                file=sys.stderr,
            )
            return 2

    if args.paths:
        for p in args.paths:
            if not p.exists() or not p.is_file():
                print(f"error: not an existing file: {p}", file=sys.stderr)
                return 2
        sweep_result, roots = _sweep_explicit_paths(args.paths, families, args.only, args.apply)
        _print_report(sweep_result, roots, args.apply, args.only)
        return 0

    root = args.root
    if root is None:
        toplevel = show_toplevel()
        if toplevel is None:
            print("error: not inside a git repo (pass --root explicitly)", file=sys.stderr)
            return 2
        root = Path(toplevel)

    sweep_result = sweep(root, families, only_family=args.only, apply=args.apply)
    _print_report(sweep_result, root, args.apply, args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
