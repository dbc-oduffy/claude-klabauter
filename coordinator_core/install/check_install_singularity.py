"""
coordinator_core.install.check_install_singularity — canonical-install-locus
invariant probe.

Port of example-doctrine-repo coordinator/lib/check-install-singularity.sh: enforces that
exactly one canonical coordinator tree is reachable. Two shapes are
recognized:
  Pre-cutover (~/.claude shape): canonical tree =
    ~/.claude/plugins/coordinator-claude (flat install; coordinator source
    lives inside ~/.claude).
  Maximalist post-W4.2 shape: canonical tree = example-doctrine-repo clone resolved via
    plugin.mirrors.coordinator-claude.live_path in registry.local.toml
    (delivered live via --plugin-dir; ~/.claude/plugins/coordinator-claude is
    ABSENT).
In both cases exactly one distinct canonical tree is expected. Detects
accidental splits where >1 coordinator tree, divergent settings files, a
doubled venv pin, or a .claude-suffixed CLAUDE_HOME breaks the invariant.

Exits:
  0  — install singularity confirmed, or consented dev-loop override active
  1  — accidental split detected (prints remediation to stderr)
  2  — internal/transport error (mirrors the bash oracle's usage/internal-
       error reservation for exit 2; the bash-version guard that also used
       exit 2 has no Python analogue — this module has no minimum-interpreter
       floor below what already runs it)

Design seam discriminator (PM-ruled 2026-06-26, preserved verbatim from the
bash oracle):
  EXEMPT (exit 0, INFO): exactly ONE of COORDINATOR_CLONE / COORDINATOR_ROOT
  is explicitly set AND points at a .git-backed clone. The exemption applies
  only to the tree-count check (CHECK 4) — CLAUDE_HOME/venv/settings checks
  always run. CLAUDE_PLUGIN_ROOT is harness-injected, NOT an operator consent
  signal, and is NOT in the exemption set.

  FAIL: accidental split — >1 distinct canonical coordinator tree (after
  dedupe), OR two PRESENT settings files naming different coordinator-claude
  marketplace paths, OR doubled .claude/.claude venv pin, OR .claude-suffixed
  CLAUDE_HOME.

False-FAIL guards (verbatim from the bash oracle):
  (a) Every enumerated tree is canonicalized (symlink-resolved) and deduped
      before the >1 test. The registry live_path and flat
      ~/.claude/plugins/coordinator-claude commonly resolve to the same
      canonical path on a normal install (both normalize to the plugin-root
      level after stripping a trailing /coordinator component).
  (b) Dedupe leaves <=1 distinct tree -> exit 0 via the count check (renamed
      from the bash oracle's "source_is_live" framing per its own B-F5 review
      note: exit 0 is purely because only one tree exists, not because a
      .git dir was detected).
  (c) Tri-file cross-check: absent settings.local.json is concordant (not a
      disagreement); FAIL only when two PRESENT files name different paths.

Divergence from the bash oracle: `_canonical_dir` used `cd <dir> && pwd -P`
(DR-148: no `realpath` binary on stock macOS bash 3.2). This module needs no
external binary — `Path.resolve()` is a pure-stdlib symlink-following
canonicalizer with the same semantics, so there is no subprocess call here at
all (the bash oracle also shelled out to python3 for TOML/JSON parsing on
every read; this port uses tomllib/json in-process, closing four subprocess
spawn sites per invocation). The bash oracle's `declare -A` iteration order is
non-deterministic (its own CHECK-4 fail-path "Trees found:" listing and its
final-verdict F10 review note both call this out); this port uses an ordered
dict, so the "Trees found:" listing order is now deterministic — an
improvement, not a scope-drop, since the bash oracle never made any ordering
guarantee for callers to depend on.

Invocation: CLI entry (`main(argv) -> int`), no flags/args consumed (the bash
oracle takes none either — it is a pure environment-driven probe). Invoked
via `python3 -m coordinator_core.install.check_install_singularity` or
imported directly. The example-doctrine-repo-side trampoline
(coordinator/lib/check-install-singularity.sh, kept at its existing filename
per the plugin_health/sentinel.py P-18 probe's hardcoded subprocess call —
see that module's own docstring for the not-yet-repointed caller) does a
plain in-process `from coordinator_core.install.check_install_singularity
import main` after resolving CLAUDE_KLABAUTER_ROOT (template-variant #1, direct-import
trampoline).

Env:
  CLAUDE_HOME     — optional; a $HOME substitute (Convention A: CLAUDE_HOME
                    is the PARENT of .claude, not .claude itself).
  HOME / USERPROFILE — fallback chain when CLAUDE_HOME is unset.
  COORDINATOR_CLONE / COORDINATOR_ROOT / CLAUDE_PLUGIN_ROOT — tree-enumeration
                    inputs; see CHECK 3/4 below.

Spec backlink:
  docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R1b
  tasks/install-friction-triage/cluster-B-path-venv-registration.md § ISSUE #4
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
  (BIG_PORT Wave B, item check-install-singularity)
Port of: coordinator/lib/check-install-singularity.sh [example-doctrine-repo repo]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core import machine_resolver

# ---------------------------------------------------------------------------
# Unit 1 — bootstrap + path helpers
# ---------------------------------------------------------------------------


def _emit_fail(msg: str, remedy: str) -> str:
    """Format one FAIL diagnostic (mirrors bash `_emit_fail` stderr shape)."""
    return f"ERROR [singularity]: {msg}\n  Remediation: {remedy}\n"


def _emit_info(msg: str) -> str:
    return f"INFO [singularity]: {msg}\n"


def _norm_sep(p: str) -> str:
    """Backslash-to-forward-slash normalization for TEXTUAL path comparison.

    Every prefix/suffix/substring/basename-split check in this module was
    written against forward-slash paths (mirroring the bash oracle's `case`
    patterns and `${var%/...}` parameter expansions). On Windows, values
    sourced from `CLAUDE_HOME`/env vars or built via `str(Path(...))` arrive
    backslash-separated, so an un-normalized check silently never matches —
    e.g. CHECK 1's `.claude`-suffix guard never fires, and `_to_plugin_root`'s
    trailing-`/coordinator` strip never strips, on Windows. Pure text
    transform — no realpath (DR-148); identity on POSIX inputs since a bare
    backslash is not a path separator there.
    """
    return p.replace("\\", "/") if p else p


def _canonical_dir(d: str) -> str:
    """Canonicalize a directory path (symlink-resolved). Empty string if the
    path does not exist or is not a directory — mirrors the bash oracle's
    `cd "$_d" && pwd -P` failure-to-empty-string behavior."""
    if not d:
        return ""
    p = Path(d)
    try:
        if not p.is_dir():
            return ""
        return str(p.resolve())
    except OSError:  # mirrors bash oracle's silent-degrade-to-empty
        return ""


def _to_plugin_root(raw: str) -> str:
    """Strip a trailing /coordinator component to normalize content-root
    paths to plugin-root level. Only strips when the last path component is
    literally "coordinator" (not "coordinator-claude" or other names).

    Mirrors bash `${1%/}` (strips at most ONE trailing slash, not all of
    them) followed by a `${_p##*/}` basename compare — a double-trailing-
    slash input like "/foo/coordinator//" is left with one slash intact in
    the oracle and does NOT match the "coordinator" basename test.

    Windows separator normalization: backslashes are folded to forward
    slashes (via ``_norm_sep``) BEFORE the suffix/basename tests. Without
    this the tests are forward-slash-only, so a native-Windows content root
    (``X:\\example-doctrine-repo\\coordinator``, the shape ``CLAUDE_PLUGIN_ROOT`` carries)
    fails both ``basename == "coordinator"`` and ``endswith("/coordinator")``
    and is NOT normalized to plugin-root level — while the registry's
    ``live_path`` for the same tree (stored forward-slashed,
    ``X:/example-doctrine-repo/coordinator``) IS. The one tree then enters ``_TreeSet``
    at two different levels, reads as 2 distinct canonical paths, and the gate
    hard-fails a correct install with "accidental split" naming
    ``X:\\example-doctrine-repo`` and ``X:\\example-doctrine-repo\\coordinator`` — the two halves of
    the SAME clone. This is the one-level-offset trap (example-doctrine-repo's plugin root
    is the ``coordinator/`` subdir, not the repo root) reached via a path-syntax
    bug rather than a real split.

    ``_norm_sep`` is gated on ``os.name == "nt"`` here: unlike the *sourced*
    Windows-shaped inputs ``_norm_sep``'s own docstring describes
    (``CLAUDE_HOME``/env vars, ``str(Path(...))``), a raw path handed to this
    function may contain a backslash that is a LITERAL POSIX filename
    character rather than a foreign-OS separator (e.g. a directory named
    ``bar\\coordinator``). Folding it unconditionally would corrupt that
    real path component into two components and silently mis-normalize it
    to plugin-root level. Gating on the live platform avoids the corruption
    on POSIX while preserving the intended Windows normalization.
    """
    if os.name == "nt":
        raw = _norm_sep(raw)
    p = raw[:-1] if raw.endswith("/") else raw
    if not p:
        return p
    basename = p.rsplit("/", 1)[-1]
    # bash `${_p%/coordinator}` only strips when the string ends with the
    # LITERAL suffix "/coordinator" (a leading slash required) — a bare
    # "coordinator" with no path separator matches the basename test but has
    # no "/coordinator" suffix to strip, so it is returned unchanged.
    if basename == "coordinator" and p.endswith("/coordinator"):
        return p[: -len("/coordinator")]
    return p


def _claude_base_dir() -> Optional[str]:
    """Derive the ~/.claude base directory.

    Follows Convention A: CLAUDE_HOME is a $HOME substitute; .claude is
    appended. (The form ${CLAUDE_HOME:-$HOME/.claude} is wrong — see wiki.)
    """
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home:
        return None
    return f"{_norm_sep(home)}/.claude"


def _registry_live_path(_claude_base: Optional[str]) -> str:
    """Read plugin.mirrors.coordinator-claude.live_path from the canonical
    settings-home registry (``machine_resolver.registry_get``). ``_claude_base``
    is accepted (underscore-prefixed: unused by convention) for call-site
    symmetry with the other CHECK-4 inputs but is NOT used to locate the
    registry — the registry lives under settings-home
    (``$COORDINATOR_SETTINGS_HOME``), never under ``~/.claude``. Returns empty
    string on any failure (no registry, parse error, absent field, or an
    empty-string declaration in the tracked ``registry.toml``).

    Negative-spec: this function used to read
    ``{claude_base}/machine-local/registry.local.toml`` directly — wrong
    directory (registry lives under settings-home, not ~/.claude) AND
    `.local`-only (never fell through to the tracked `registry.toml`). Both
    defects were silent (empty-string degrade on every failure path). Do not
    re-introduce a hand-rolled TOML read here; route through
    ``machine_resolver.registry_get``.
    """
    return machine_resolver.registry_get("plugin.mirrors.coordinator-claude.live_path") or ""


def _registry_venv_pin(_claude_base: Optional[str]) -> str:
    """Read coordinator.python venv pin from the canonical settings-home
    registry (``machine_resolver.registry_get``). See ``_registry_live_path``
    negative-spec — same two defects (wrong dir, `.local`-only) applied here
    before this fix."""
    return machine_resolver.registry_get("coordinator.python") or ""


def _read_json(path: str) -> Optional[dict]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — mirrors bash oracle's silent-degrade-to-empty
        return None


def _settings_coordinator_path(file_path: str) -> str:
    """Read coordinator-claude marketplace path from a settings JSON file
    (settings.json or settings.local.json).
    Path is at: extraKnownMarketplaces["coordinator-claude"]["source"]["path"].
    Returns empty string on any failure."""
    data = _read_json(file_path)
    if data is None:
        return ""
    mkts = data.get("extraKnownMarketplaces", {})
    entry = mkts.get("coordinator-claude", {}) if isinstance(mkts, dict) else {}
    if not isinstance(entry, dict):
        return ""
    src = entry.get("source", {})
    if not isinstance(src, dict):
        return ""
    path = src.get("path", "")
    return str(path) if path else ""


def _known_marketplaces_coordinator_path(file_path: str) -> str:
    """Read coordinator-claude marketplace path from known_marketplaces.json.
    Path is at: ["coordinator-claude"]["source"]["path"].
    Returns empty string on any failure."""
    data = _read_json(file_path)
    if data is None:
        return ""
    entry = data.get("coordinator-claude", {})
    if not isinstance(entry, dict):
        return ""
    src = entry.get("source", {})
    if not isinstance(src, dict):
        return ""
    path = src.get("path", "")
    return str(path) if path else ""


# ---------------------------------------------------------------------------
# Unit 2 — tree-set accumulator, CHECK 1-3
# ---------------------------------------------------------------------------


def _looks_like_coordinator_tree(d: str) -> bool:
    """Does ``d`` actually hold a coordinator plugin, or is it just a directory
    that happens to sit at a candidate path?

    Existence alone is NOT evidence of an install. The harness and coordinator's
    own doctor create ``~/.claude/plugins/coordinator-claude/data/`` purely for
    runtime state, with no plugin content in it. Counting that as a second
    "tree" made this gate fail-loud on a machine in the *expected* post-W4.2
    maximalist shape, and the printed remediation told the operator to delete a
    directory the system itself manages.

    Marker is the plugin manifest, checked at both shapes this gate normalizes
    between: plugin-root level (``<d>/coordinator/.claude-plugin/plugin.json``)
    and content level (``<d>/.claude-plugin/plugin.json``).
    """
    if not d:
        return False
    base = Path(d)
    return (
        (base / ".claude-plugin" / "plugin.json").is_file()
        or (base / "coordinator" / ".claude-plugin" / "plugin.json").is_file()
    )


def _has_parent_child_pair(paths: List[str]) -> bool:
    """Do any two of the given canonical tree paths sit one inside the
    other (parent/child), rather than being genuinely distinct trees?

    This is the shape a single repo takes when its plugin root is a
    subdirectory of its own repo root (e.g. Example-doctrine-repo's ``coordinator/``
    under the repo root) and something upstream (a separator/case/
    trailing-slash mismatch — see ``_to_plugin_root``'s docstring for a
    concrete instance) kept the two spellings of the SAME tree from
    deduping to one canonical path. Detecting it lets CHECK 4's FAIL
    remediation say "this is one repo counted twice" instead of advising
    the operator to delete one of the two paths, which would delete half
    of their own clone.
    """
    norm = [p.replace("\\", "/") if os.name == "nt" else p for p in paths]
    for i, a in enumerate(norm):
        for j, b in enumerate(norm):
            if i == j:
                continue
            if b.startswith(a.rstrip("/") + "/"):
                return True
    return False


class _TreeSet:
    """Ordered canonical-path accumulator — mirrors the bash oracle's
    `declare -A _seen_trees` + `_tree_count`, but with deterministic
    insertion-ordered iteration (see module docstring divergence note)."""

    def __init__(self) -> None:
        self._seen: "Dict[str, int]" = {}

    def add(self, raw: str) -> None:
        if not raw:
            return
        norm = _to_plugin_root(raw)
        if not norm:
            return
        if not Path(norm).is_dir():
            return
        if not _looks_like_coordinator_tree(norm):
            return
        canon = _canonical_dir(norm)
        if not canon:
            return
        if canon not in self._seen:
            self._seen[canon] = 1

    @property
    def count(self) -> int:
        return len(self._seen)

    @property
    def paths(self) -> List[str]:
        return list(self._seen.keys())


def _check1_claude_home_suffix_guard(claude_home: Optional[str]) -> Optional[Tuple[str, str]]:
    """CHECK 1 — CLAUDE_HOME suffix guard.

    A .claude-suffixed CLAUDE_HOME means the operator has set CLAUDE_HOME to
    the .claude dir itself (Convention B). Convention A requires CLAUDE_HOME
    to be the *parent* (a $HOME substitute). The doubled-venv bug (issue #3)
    is a direct consequence of this mismatch.

    Returns (msg, remedy) on failure, else None.
    """
    if not claude_home:
        return None
    # Mirrors bash `case "$CLAUDE_HOME" in */.claude|*/.claude/)` — matches a
    # string ending in "/.claude" or "/.claude/" (a literal "/" must precede
    # ".claude"; a bare ".claude" with no path separator does NOT match).
    claude_home_cmp = _norm_sep(claude_home)
    if claude_home_cmp.endswith("/.claude") or claude_home_cmp.endswith("/.claude/"):
        return (
            f"CLAUDE_HOME is set to a .claude-suffixed path: {claude_home}",
            "CLAUDE_HOME must be a $HOME substitute (the parent directory), not the .claude "
            "dir. Unset CLAUDE_HOME, or set it to its parent (e.g. export CLAUDE_HOME=$HOME).",
        )
    return None


def _check2_venv_pin_doubled_path_guard(venv_pin: str) -> Optional[Tuple[str, str]]:
    """CHECK 2 — Venv pin doubled-path guard.

    coordinator.python in the registry should never contain /.claude/.claude/
    which is the symptom of a Convention-B CLAUDE_HOME in effect when the
    venv was built.
    """
    if not venv_pin:
        return None
    if "/.claude/.claude/" in _norm_sep(venv_pin):
        return (
            f"Venv pin contains doubled .claude path: {venv_pin}",
            "Fix CLAUDE_HOME (must be the parent dir, e.g. $HOME), then re-run "
            "ensure-coordinator-venv.sh to reset the coordinator.python registry pin.",
        )
    return None


def _check3_exemption(
    coordinator_clone: Optional[str], coordinator_root: Optional[str]
) -> Tuple[bool, str]:
    """CHECK 3 — Exemption check (consented dev-loop override).

    Exactly ONE of COORDINATOR_CLONE / COORDINATOR_ROOT explicitly set AND
    pointing at a .git-backed clone -> consented contributor override; caller
    emits INFO and skips the tree-count check (only that check). All other
    checks still run. CLAUDE_PLUGIN_ROOT is harness-injected and is NOT in
    the exemption set.

    "Explicitly set" means the variable is non-empty in the current
    environment. Both COORDINATOR_CLONE and COORDINATOR_ROOT being set
    simultaneously (or neither) is NOT exempt.

    Returns (exempt, reason).
    """
    clone_set = 1 if coordinator_clone else 0
    root_set = 1 if coordinator_root else 0

    if clone_set + root_set != 1:
        return False, ""

    if clone_set:
        # COORDINATOR_CLONE must have a .git directory directly (it IS the repo root)
        if Path(f"{coordinator_clone}/.git").is_dir():
            return True, f"COORDINATOR_CLONE dev-loop override active -> {coordinator_clone} (consented)"
        return False, ""

    # COORDINATOR_ROOT is a content root; its plugin root (parent if
    # basename=coordinator) must have a .git directory for the exemption to apply.
    cr_plugin_root = _to_plugin_root(coordinator_root)  # type: ignore[arg-type]
    if Path(f"{cr_plugin_root}/.git").is_dir():
        return True, f"COORDINATOR_ROOT dev-loop override active -> {coordinator_root} (consented)"
    return False, ""


# ---------------------------------------------------------------------------
# Unit 3 — CHECK 4-5 + final verdict
# ---------------------------------------------------------------------------


def _check4_tree_enumeration(
    claude_base: Optional[str],
    coordinator_clone: Optional[str],
    coordinator_root: Optional[str],
    claude_plugin_root: Optional[str],
) -> Tuple[_TreeSet, str]:
    """CHECK 4 — Coordinator tree enumeration + canonical dedupe.

    Enumerate all paths where the resolver could find a coordinator plugin.
    Normalize each to plugin-root level (strip trailing /coordinator
    component), canonicalize, then dedupe.

    Paths enumerated:
      - Flat installed path: ~/.claude/plugins/coordinator-claude
      - Registry live_path (strip /coordinator to get plugin root)
      - COORDINATOR_CLONE (already at plugin-root level — has the .git)
      - COORDINATOR_ROOT (strip /coordinator if content-subdir shape)
      - CLAUDE_PLUGIN_ROOT (harness-injected; strip /coordinator; NOT exempt)

    Returns (tree_set, live_path) — live_path is threaded into CHECK 5's
    branch-remediation decision.
    """
    trees = _TreeSet()

    if claude_base:
        trees.add(f"{claude_base}/plugins/coordinator-claude")

    live_path = _registry_live_path(claude_base)
    if live_path:
        trees.add(live_path)

    if coordinator_clone:
        trees.add(coordinator_clone)

    if coordinator_root:
        trees.add(coordinator_root)

    if claude_plugin_root:
        trees.add(claude_plugin_root)

    return trees, live_path


def _check5_settings_tri_file(claude_base: str, live_path: str) -> Optional[Tuple[str, str, dict]]:
    """CHECK 5 — Settings tri-file cross-check.

    Compare the coordinator-claude marketplace path across three settings
    files. Absent settings.local.json -> concordant (not a disagreement).
    FAIL only when two PRESENT files name different coordinator-claude paths
    (after canonical dedupe when the directories exist on disk).

    Returns (msg, remedy, found_paths) on failure, else None. found_paths
    carries the raw (pre-dedupe) values for the diagnostic printout.
    """
    settings_json = f"{claude_base}/settings.json"
    settings_local = f"{claude_base}/settings.local.json"
    known_mkts = f"{claude_base}/plugins/known_marketplaces.json"

    p_settings = _settings_coordinator_path(settings_json) if Path(settings_json).is_file() else ""
    # Guard: only read settings.local.json if it exists (absent -> concordant
    # per false-FAIL guard c)
    p_local = _settings_coordinator_path(settings_local) if Path(settings_local).is_file() else ""
    p_known = (
        _known_marketplaces_coordinator_path(known_mkts) if Path(known_mkts).is_file() else ""
    )

    mkt_seen: "Dict[str, int]" = {}

    def _add_mkt_path(raw: str) -> None:
        if not raw:
            return
        canon = _canonical_dir(raw) if Path(raw).is_dir() else raw
        if not canon:
            return
        if canon not in mkt_seen:
            mkt_seen[canon] = 1

    _add_mkt_path(p_settings)
    _add_mkt_path(p_local)
    _add_mkt_path(p_known)

    if len(mkt_seen) <= 1:
        return None

    found = {"settings.json": p_settings, "settings.local.json": p_local, "known_marketplaces.json": p_known}

    if live_path:
        return (
            f"Settings files disagree on the coordinator-claude marketplace path "
            f"({len(mkt_seen)} distinct paths found).",
            "Maximalist post-W4.2 shape: Remove the coordinator-claude marketplace entry from "
            "all present settings files — in this shape the coordinator is delivered via "
            "--plugin-dir and must NOT appear as a marketplace path. Re-run "
            "platform-localize.sh to regenerate known_marketplaces.json without the "
            "coordinator-claude entry.",
            found,
        )
    return (
        f"Settings files disagree on the coordinator-claude marketplace path "
        f"({len(mkt_seen)} distinct paths found).",
        "Pre-cutover shape: Re-run platform-localize.sh (fires on SessionStart) so all "
        "present settings files agree on ${CLAUDE_HOME:-$HOME}/.claude/plugins/coordinator-claude.",
        found,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run() -> Tuple[int, str, str]:
    """Run all checks against the current environment. Returns
    (exit_code, stdout, stderr) — the CLI entry writes these to the real
    streams; tests call this directly for output-shape assertions."""
    out_lines: List[str] = []
    err_lines: List[str] = []
    fail_count = 0

    claude_home_env = os.environ.get("CLAUDE_HOME") or None
    claude_base = _claude_base_dir()

    # CHECK 1
    c1 = _check1_claude_home_suffix_guard(claude_home_env)
    if c1 is not None:
        err_lines.append(_emit_fail(*c1))
        fail_count += 1

    # CHECK 2
    venv_pin = _registry_venv_pin(claude_base)
    c2 = _check2_venv_pin_doubled_path_guard(venv_pin)
    if c2 is not None:
        err_lines.append(_emit_fail(*c2))
        fail_count += 1

    # CHECK 3
    coordinator_clone = os.environ.get("COORDINATOR_CLONE") or None
    coordinator_root = os.environ.get("COORDINATOR_ROOT") or None
    claude_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or None
    exempt_tree_check, exempt_reason = _check3_exemption(coordinator_clone, coordinator_root)

    # CHECK 4
    trees, live_path = _check4_tree_enumeration(
        claude_base, coordinator_clone, coordinator_root, claude_plugin_root
    )
    if exempt_tree_check:
        out_lines.append(_emit_info(exempt_reason))
    elif trees.count > 1:
        if _has_parent_child_pair(trees.paths):
            remedy = (
                "The paths below are a PARENT and a CHILD of each other, not two separate "
                "installs -- this is the one-level-offset shape a single repo takes when its "
                "plugin root is a subdirectory of its own repo root (e.g. Example-doctrine-repo's "
                "coordinator/ under the repo root). Do NOT remove either path: they are the "
                "SAME clone counted at two levels, and deleting one deletes part of the "
                "other. Likely cause: a path-separator/case/trailing-slash mismatch between "
                "two sources naming this tree (e.g. a backslashed CLAUDE_PLUGIN_ROOT vs. a "
                "forward-slashed registry live_path) kept them from deduping to one canonical "
                "path -- fix that resolver so both spellings normalize identically, then "
                "re-run /coordinator:install to verify the count drops to one."
            )
        else:
            remedy = (
                "Expected: exactly one canonical tree. Post-W4.2 maximalist shape: the example-doctrine-repo "
                "clone (resolved via plugin.mirrors.coordinator-claude.live_path in "
                "registry.local.toml) is the sole tree — ~/.claude/plugins/coordinator-claude "
                "must be absent. Pre-cutover shape: ~/.claude/plugins/coordinator-claude is "
                "the sole tree — no live_path in the registry. A stray second tree (e.g. "
                "~/coordinator-claude, a worktree, or a COORDINATOR_CLONE/ROOT/"
                "CLAUDE_PLUGIN_ROOT pointing elsewhere) is never the expected shape. Remove "
                "the extra tree(s). Unset COORDINATOR_CLONE/COORDINATOR_ROOT/"
                "CLAUDE_PLUGIN_ROOT if pointing at stray trees. Re-run /coordinator:install "
                "to verify."
            )
        err_lines.append(
            _emit_fail(
                f"Multiple coordinator trees detected ({trees.count} distinct canonical "
                "paths) — accidental split.",
                remedy,
            )
        )
        fail_count += 1
        err_lines.append("  Trees found:\n")
        for tp in trees.paths:
            err_lines.append(f"    {tp}\n")

    # CHECK 5
    if claude_base:
        c5 = _check5_settings_tri_file(claude_base, live_path)
        if c5 is not None:
            msg, remedy, found = c5
            err_lines.append(_emit_fail(msg, remedy))
            fail_count += 1
            if found["settings.json"]:
                err_lines.append(f"  settings.json:            {found['settings.json']}\n")
            if found["settings.local.json"]:
                err_lines.append(f"  settings.local.json:      {found['settings.local.json']}\n")
            if found["known_marketplaces.json"]:
                err_lines.append(f"  known_marketplaces.json:  {found['known_marketplaces.json']}\n")

    # Final verdict
    if fail_count == 0:
        if exempt_tree_check:
            all_trees = " ".join(trees.paths) + (" " if trees.paths else "")
            out_lines.append(
                _emit_info(
                    f"coordinator install singularity OK (exempted) — {exempt_reason} — "
                    f"trees resolved: {all_trees}"
                )
            )
        else:
            if trees.paths:
                out_lines.append(
                    _emit_info(f"coordinator install singularity OK — single canonical tree: {trees.paths[0]}")
                )
            else:
                out_lines.append(
                    _emit_info(
                        "coordinator install singularity OK — no coordinator tree resolved "
                        "(fresh install or all paths absent)."
                    )
                )
        return 0, "".join(out_lines), "".join(err_lines)

    return 1, "".join(out_lines), "".join(err_lines)


_USAGE = """Usage: check-install-singularity [no args]
Canonical-install-locus invariant probe. Pure environment-driven — no flags.
Exit 0=singularity confirmed (or exempted), 1=accidental split detected,
2=internal error.
"""


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    try:
        rc, out, err = run()
    except Exception as exc:  # noqa: BLE001 — internal-error floor, exit 2
        sys.stderr.write(f"ERROR [singularity]: internal error: {exc}\n")
        return 2
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
