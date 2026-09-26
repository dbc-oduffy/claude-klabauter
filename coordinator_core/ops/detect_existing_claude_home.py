"""
coordinator_core.ops.detect_existing_claude_home — three-state classifier for
Claude home directories (pristine / used-vanilla / configured).

Purpose: tell install logic which of THREE postures a candidate Claude home
is in, so it can choose the right path and the right message:

  pristine      Claude Code has never run here — no artifacts at all.
                Track A. Install from zero, no caveats.
  used-vanilla  Claude Code HAS run, but nothing opinionated was set up
                (no git, no installed plugins, no coordinator infra — just
                session history, CC plugin scaffolding, and/or a hand-edited
                CLAUDE.md). Track A. Install from effectively zero; acknowledge
                prior casual use, but do NOT warn about a setup being overwritten.
  configured    An opinionated, deliberately-customized home (git-tracked,
                installed plugins, or coordinator infrastructure — "a setup
                like ours"). Track B. The only tier where the "merge is yours /
                your setup may collide" caution is warranted.

WHY THREE, NOT TWO: the old binary Track A/B collapsed "casually used" into
"existing setup", so a machine that had merely launched Claude Code once (which
leaves plugins/ scaffolding + session dirs) drew the same "your existing setup
may be overwritten" warning as a fully git-tracked, plugin-laden home. Those are
different install postures and deserve different messages.

FILE-LEVEL SAFETY IS TIER-INDEPENDENT. The tier drives posture and messaging
only. Idempotency guards (never clobber CLAUDE.md, settings.json, registry
files) MUST hold in every tier — including used-vanilla, where a hand-edited
CLAUDE.md exists but does not lift the home to "configured".

Decision tiers (first match wins, most-structural first):
  configured   — C1 git-tracked | C2 installed plugin | C3 coordinator infra
  used-vanilla — U1 CLAUDE.md (any size) | U2 session/runtime artifacts | U3 CC scaffolding
  pristine     — none of the above

Output (stdout, via main()): one line of the form:
  state=<pristine|used-vanilla|configured> track=<A|B> reason: <human explanation>
track= is retained for backward-compat with callers that branch on the binary
fork (configured → B, else → A); new callers should branch on state=.

Exit codes:
  0 — always (classification result is in stdout, not exit code)

Read-only contract: this module MUST NOT create, modify, or delete anything
in the target directory. Idempotent: identical output on repeated runs.

Port of: detect-existing-claude-home.sh (DoE 6fb5fb37, 2026-07-22, 221 lines)
Spec backlink: docs/plans/2026-07-16-bash-to-naked-python-engine-migration.md [DEAD-CITATION: plan file never committed to this repo]

Negative-spec / faithful-oracle notes:
    - `_is_cc_managed_entry` reproduces the bash `case` statement's glob
      semantics exactly (dotfile prefix, `*.bak`/`*.bak-*` suffix/infix,
      literal basenames) — case-sensitive, matching bash's default
      (non-`nocasematch`) behavior on all platforms, including Windows.
    - Filesystem-error swallowing mirrors the bash oracle's `2>/dev/null`
      redirects on `find`/`ls`/file reads: any `OSError` (permission denied,
      race-deleted entry, unreadable file) during a predicate degrades that
      predicate to its negative case rather than raising — read-only best-effort
      classification, never a hard crash on a transient FS hiccup. An ABSENT
      path is the pristine-home answer, not a hiccup, so it degrades silently;
      only other OSErrors print a `skip:` line (a stderr `skip:` on every fresh
      home misdirected claude-klabauter#15's diagnosis onto these probes).
    - HOME resolution deliberately does NOT reproduce the bash oracle's bare
      `${HOME}` (POSIX-only) fallback verbatim: `HOME` is frequently unset on
      Windows, where the bash oracle never had to run. `os.path.expanduser("~")`
      is added as a second-tier fallback (resolves via `USERPROFILE` on
      Windows) so the classifier degrades gracefully cross-platform instead of
      resolving to a bogus `/.claude` at process cwd. This is a deliberate
      target-platform fill, not a behavior regression on POSIX (where `HOME`
      is always set and this fallback never triggers).
"""

from __future__ import annotations

import fnmatch
import os
import sys
from typing import List, Optional, Tuple


_CC_MANAGED_BAK_PATTERNS = ("*.bak", "*.bak-*")
_CC_MANAGED_LITERAL_FILES = {
    "config.json",
    "installed_plugins.json",
    "known_marketplaces.json",
    "blocklist.json",
    "plugin-catalog-cache.json",
}
_CC_MANAGED_LITERAL_DIRS = {"repos", "marketplaces", "cache", "data"}

_C3_MARKERS = ("state", "machine-local", "coordinator.local.md", "plugins/coordinator-claude")

_U2_ARTIFACTS = (
    "projects",
    "sessions",
    "todos",
    "shell-snapshots",
    "ide",
    "file-history",
    "statsig",
    "history.jsonl",
    "settings.json",
)


def resolve_target(arg: Optional[str] = None, env: Optional[dict] = None) -> str:
    """Resolve the target Claude home directory.

    Priority, first hit wins: the explicit `arg`, then the CLAUDE_CONFIG_DIR
    env var, then `.claude` under the home directory
    (mirrors `_resolve_target()` in the bash oracle, lines 58-66).
    """
    if env is None:
        env = os.environ
    if arg:
        return arg
    config_dir = env.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        return config_dir
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude")


def _is_cc_managed_entry(name: str) -> bool:
    if name.startswith("."):
        return True
    if any(fnmatch.fnmatchcase(name, pat) for pat in _CC_MANAGED_BAK_PATTERNS):
        return True
    if name in _CC_MANAGED_LITERAL_FILES:
        return True
    if name in _CC_MANAGED_LITERAL_DIRS:
        return True
    return False


def _installed_plugins_json_nonempty(target: str) -> bool:
    """Claude Code's own source of truth. A fresh install has "plugins": {} — an
    empty map. Whitespace-stripping makes the check robust to both pretty and
    compact JSON without a JSON-parse dependency; reasoning is by PRESENCE
    (explicit empty-map branch) rather than absence — mirrors the bash oracle's
    case-statement (no pipeline trap under set -e).
    """
    f = os.path.join(target, "plugins", "installed_plugins.json")
    try:
        with open(f, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return False
    except OSError:
        print(f"skip: _installed_plugins_json_nonempty: with open(f, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    stripped = "".join(raw.split())
    if '"plugins":{}' in stripped:
        return False
    if '"plugins":{' in stripped:
        return True
    return False


def _has_installed_plugin(target: str) -> bool:
    plugins_dir = os.path.join(target, "plugins")
    try:
        entries = os.listdir(plugins_dir)
    except FileNotFoundError:
        return False
    except OSError:
        print(f"skip: _has_installed_plugin: entries = os.listdir(plugins_dir) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    for base in entries:
        if _is_cc_managed_entry(base):
            continue
        entry = os.path.join(plugins_dir, base)
        try:
            if os.path.isfile(entry):
                return True
            if os.path.isdir(entry):
                if os.listdir(entry):
                    return True
        except OSError:
            print(f"skip: _has_installed_plugin: if os.path.isfile(entry): failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return False


def _plugins_dir_nonempty(target: str) -> bool:
    plugins_dir = os.path.join(target, "plugins")
    try:
        return bool(os.listdir(plugins_dir))
    except FileNotFoundError:
        return False
    except OSError:
        print(f"skip: _plugins_dir_nonempty: return bool(os.listdir(plugins_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False


def classify(target: str) -> Tuple[str, str, str]:
    """Classify `target` into (state, track, reason).

    Tier 1 — CONFIGURED (Track B): an opinionated, deliberately-customized
    home. The ONLY tier where the "merge is yours / your setup may collide"
    caution is warranted. Signals are structural intent, not mere prior use.

    NOTE — file-level safety is tier-independent. The tier drives posture and
    messaging only. Idempotency guards (never clobber CLAUDE.md, settings.json,
    registry files) MUST hold in every tier, including used-vanilla.
    """
    if os.path.exists(os.path.join(target, ".git")):
        return ("configured", "B", f"{target} is git-tracked (deliberate version control)")

    if _installed_plugins_json_nonempty(target):
        return (
            "configured",
            "B",
            f"{target}/plugins/installed_plugins.json records installed plugin(s)",
        )
    if _has_installed_plugin(target):
        return (
            "configured",
            "B",
            f"{target}/plugins/ contains a non-Claude-Code-managed plugin entry",
        )

    for marker in _C3_MARKERS:
        if os.path.exists(os.path.join(target, marker)):
            return (
                "configured",
                "B",
                f"{target}/{marker} present (coordinator/opinionated infrastructure)",
            )

    # Tier 2 — USED-VANILLA (Track A): Claude Code has run here, but nothing

    if os.path.isfile(os.path.join(target, "CLAUDE.md")):
        return (
            "used-vanilla",
            "A",
            f"{target}/CLAUDE.md present (hand-edited config, no structural setup)",
        )

    for artifact in _U2_ARTIFACTS:
        if os.path.exists(os.path.join(target, artifact)):
            return (
                "used-vanilla",
                "A",
                f"{target}/{artifact} present (Claude Code has run here)",
            )

    if _plugins_dir_nonempty(target):
        return (
            "used-vanilla",
            "A",
            f"{target}/plugins/ holds Claude-Code-managed scaffolding only",
        )

    # Tier 3 — PRISTINE (Track A): no Claude Code artifacts at all. Never used.
    return ("pristine", "A", f"{target} has no Claude Code artifacts — never used")


def emit(state: str, track: str, reason: str) -> str:
    return f"state={state} track={track} reason: {reason}"


def main(argv: List[str]) -> int:
    arg = argv[0] if argv else None
    target = resolve_target(arg)
    state, track, reason = classify(target)
    print(emit(state, track, reason))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
