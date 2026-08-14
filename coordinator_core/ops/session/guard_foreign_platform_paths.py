"""
coordinator_core.ops.session.guard_foreign_platform_paths — foreign-platform
path detector for a live `settings.json` (and sibling machine-local JSON
configs), plus a second detector for the agent-readable prose surface
(`CLAUDE.md` / `CLAUDE.local.md`) — see `detect_foreign_platform_paths_in_prose`
below for why prose needs a distinct false-positive posture from the JSON-tree
scan (prose legitimately DISCUSSES the very path shapes this guard exists to
catch, the JSON tree never does).

Purpose: catch the 2026-07-28 incident shape — a POSIX host's `settings.json`
silently rewritten with Windows drive-letter hook-command paths (`X:/DoE-claude/...`,
`C:\\Users\\...`), or the reverse on a Windows host — BEFORE it bricks every
hook, guard, and nudge in the session with zero signal. That incident produced
no error anywhere: the only symptom was tool calls failing against a hook
command naming a drive that does not exist on this machine.

Root cause it defends against (established by investigation, not guessed):
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json` on this fleet's dev
topology carries MACHINE-ABSOLUTE hook paths baked in at install time
(`coordinator_core.install.gen_settings_hooks` rewrites every
`${CLAUDE_PLUGIN_ROOT}` in `coordinator/hooks/hooks.json` to a registry-resolved
absolute path — see `docs/wiki/external-plugin-live-resolution.md § Hook-delivery`,
required because upstream `--plugin-dir` hook-delivery is dead, bug #38699).
`~/.claude` is ALSO a git repo synced across machines (manual push/pull +
periodic "safety-commit" snapshots of the live working tree). Machine-absolute
paths and a synced repo do not mix: whichever machine committed/pushed last
overwrites the other's hook chain on the next pull or safety-commit. Direct
evidence: DoE-claude repo, `~/.claude` commit `ed0b972` ("safety",
2026-07-28T19:55:14+01:00) snapshotted a `settings.json` whose entire `hooks`
block and `extraKnownMarketplaces` had Windows paths, on what is otherwise a
macOS machine.

This module is DETECT-ONLY (no auto-repair) — see the module docstring of the
DoE-side callers for why: `settings.json` here is shared with another machine
via bidirectional git sync, so a "repair" that localizes to this host's own
form is exactly the kind of write that clobbers the OTHER machine on its next
pull. A human/PM call is required to pick a canonical per-machine value; this
module's job is to make the corruption LOUD, never to silently pick a side.

Callers (two independent delivery legs, deliberately not one):
  1. `coordinator/hooks/scripts/guard-foreign-platform-paths.py` (DoE-claude)
     — SessionStart hook, thin stub, imports `detect_foreign_platform_paths`
     + `format_session_banner` in-process. Fires on every boot WHEN the hook
     layer is alive. See that script's own docstring for the residual gap
     this leg cannot close (a corrupted `hooks` block disables every hook,
     this one included — the same circularity the settings-integrity guard's
     wiki claims is solved by living outside settings.json, a claim that is
     STALE for this repo's actual settings.json-baked hook-delivery mechanism).
  2. `coordinator/bin/coordinator-precommit-foreign-platform-check`
     (claude-klabauter) — a native git `pre-commit` hook, installed by
     `install_meta_repo_precommit_hook.py`'s Gate 3. This leg is what would
     have stopped `ed0b972` from ever landing: it fires via git itself, not
     via any Claude Code hook, so it survives a session where every Claude
     Code hook is already dead. It cannot catch corruption that never passes
     through `git commit` on THIS machine (a `git pull`/`--ff-only` merge with
     no new local commit, or a corruption that is never committed at all) —
     named here as a residual gap, not silently assumed away.

Detection is SHAPE-based, never a hardcoded drive letter or username:
  - Windows-drive-letter form on a POSIX host: `X:/...`, `C:\\...`,
    `/cygdrive/x/...` — matched by regex, not by an enumerated drive list.
  - POSIX-rooted form on a Windows host: a leading `/segment/...` — matched by
    regex, deliberately excluding `//host/share`-shaped UNC and `scheme://`
    URLs (a bare `/` immediately followed by another `/` never matches).
  - Cross-platform ENV-VAR REFERENCE SYNTAX (added 2026-07-29, second
    dispatch): `hook_root_env_expr()` (`coordinator_core/install/_shared.py`)
    resolves PER-GENERATING-MACHINE and its own docstring states plainly
    "there is no spelling valid in both" — PowerShell reads an OS env var
    ONLY via `$env:NAME`; POSIX shells read the same var via bare `$NAME` /
    `${NAME}`. A settings.json generated on one machine and synced to the
    other therefore carries an env-var reference that is dead on the
    receiving host, but carries NO drive letter and NO POSIX root segment —
    invisible to the two path-shape regexes above. This is a DISTINCT shape
    from a foreign PATH: no drive letter, no leading slash, just a `$`-form
    that only one platform's shell resolves. Matched by regex on the `$`
    sigil shape itself (`$env:NAME` vs bare `$NAME`/`${NAME}`), never a
    hardcoded variable name — this check would catch a foreign-shaped
    reference to ANY env var, not just `COORDINATOR_CONTENT_ROOT`. Restricted
    to JSON pointers whose final segment is `command` (the one place this
    module's own domain — `hooks.json`'s hook-command strings — actually
    executes shell/PowerShell syntax); an env-var-shaped string sitting in a
    description/comment/prose field is not executable and must not fire.

Detect-only applies doubly here: a guard whose own delivery mechanism is
baked into the very file it inspects (see the `hooks`-block circularity
noted above) can observe a break in that file but can never safely close the
loop by rewriting it — the tool it would use to verify the repair (another
hook fire) is the tool the break may have just disabled. Detection and
repair cannot share that closed loop; a human/PM call breaks it instead.

Spec backlink: DoE-claude `state/subagent-share/32637f2b-204d-4937-89a7-c3518928e38d/
coordinatorexecutor-7bb4cb37.md` (this guard's originating dispatch, 2026-07-28)
and `state/subagent-share/78b683cd-1b62-4a25-904d-954cb3c69412/
coordinatorexecutor-351b3ca2.md` (env-var-reference-shape follow-up, 2026-07-29).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.ops.session._path_shape_regexes import WIN_DRIVE_RE as _WIN_DRIVE_RE

# --- Shape regexes -----------------------------------------------------------
# Windows drive-letter form -- shared with guard_concrete_path_citations.py,
# see _path_shape_regexes.py for why this is a single source rather than a
# second copy of the lookbehind.
# cygdrive/MSYS form: /cygdrive/x/...
_CYGDRIVE_RE = re.compile(r"/cygdrive/[A-Za-z]/")
# POSIX absolute path: a leading '/' followed directly by a path-segment
# character (never another '/', which excludes "//host/share" UNC forms and
# "scheme://" URLs), preceded by a non-path-char boundary so this does not
# fire mid-string on an embedded fragment. The boundary also excludes a
# preceding ':' so a native Windows drive path's own "C:/Users/..." tail is
# never misread as a POSIX root -- that slash is drive-relative, not absolute.
_POSIX_ROOT_RE = re.compile(r'(?<![A-Za-z0-9_./\\:-])/[A-Za-z0-9_][^\s"\']*')

# --- Env-var reference shape regexes -----------------------------------------
# PowerShell env-var read: "$env:NAME" (case-insensitive "env" -- PowerShell
# itself is case-insensitive on this prefix). Requires a valid identifier
# after the ':' so this never fires on bare "$env" prose or an unrelated
# "http://x/env:thing" fragment (no such fragment is even shaped like this --
# the ':' must directly follow the literal "env").
_ENV_VAR_WIN_RE = re.compile(r"\$env:[A-Za-z_][A-Za-z0-9_]*", re.IGNORECASE)
# POSIX env-var read: bare "$NAME" or braced "${NAME}". A leading digit
# never matches (identifier grammar), which is what keeps "$5.00"/"$100" --
# $ used arithmetically or as a currency literal -- out of this shape
# entirely. Matched via `_iter_dollar_identifiers` below rather than a
# single compiled regex with a trailing `(?!:)` lookahead -- that simpler
# form has a real backtracking trap: on "$env:NAME" the maximal match "env"
# fails the lookahead (next char IS ':'), so the engine backtracks to
# shorter candidates ("en", then next char 'v' -- lookahead now PASSES) and
# reports a false bare-var match anyway. Scanning the identifier's full
# greedy extent first, then checking the boundary character in plain Python
# (no backtracking involved), avoids that trap.
_DOLLAR_IDENTIFIER_RE = re.compile(r"\$(\{)?([A-Za-z_][A-Za-z0-9_]*)")

# PowerShell's OWN automatic-variable vocabulary -- a bare "$LastExitCode",
# "$_", "$true" etc. inside a PowerShell command is NATIVE syntax on a
# Windows host, not a foreign POSIX env-var read. This is shell-syntax
# awareness (same category as `_POSIX_ROOT_RE` excluding "//host/share" UNC
# and "scheme://" forms), never a hardcoded PROJECT variable name -- this
# set is PowerShell's own reserved vocabulary, stable across any script that
# uses it, and matches exactly the automatic variable `wrap_hook_command_
# guarded()`'s own Windows branch emits ("exit $LASTEXITCODE"). Case-
# insensitive, matching PowerShell's own case-insensitivity on variable
# names. Source: `about_Automatic_Variables` (PowerShell reference).
_POWERSHELL_AUTOMATIC_VARS = frozenset(
    v.lower()
    for v in (
        "_",
        "args",
        "error",
        "event",
        "eventargs",
        "eventsubscriber",
        "executioncontext",
        "false",
        "foreach",
        "home",
        "host",
        "input",
        "lastexitcode",
        "matches",
        "myinvocation",
        "nestedpromptlevel",
        "null",
        "pid",
        "profile",
        "psboundparameters",
        "pscmdlet",
        "pscommandpath",
        "psculture",
        "psdebugcontext",
        "pshome",
        "psitem",
        "psscriptroot",
        "psuiculture",
        "psversiontable",
        "pwd",
        "sender",
        "shellid",
        "stacktrace",
        "switch",
        "this",
        "true",
    )
)

_DOEROOT_NAME = ".doe-root"

# --- Prose-scan shapes (CLAUDE.md / CLAUDE.local.md) -------------------------
#
# The two path-shape regexes above answer "is this a path shaped for the
# other platform" -- sufficient for settings.json, where every string in the
# tree is either machine-executable or a title/description that never
# mentions a path at all. Free-form doctrine prose is different: it
# LEGITIMATELY discusses paths, including the exact shape this guard exists
# to catch, as illustration of why paths must not be hardcoded (see this
# guard's own originating incident writeup). A bare `_WIN_DRIVE_RE`/
# `_POSIX_ROOT_RE` scan over prose fires on its own remedy text -- e.g.
# `~/.claude/CLAUDE.local.md`'s sibling-repo-map section reads "(`X:\` on
# Windows-native, `/x/` under WSL/Git-Bash, `~/X/` on macOS)". That bare-root
# mention has no trailing segment and correctly stays quiet under the
# structural rule below; a NEIGHBORING sentence in the same file naming a
# per-machine directory DOES carry a trailing segment and correctly fires --
# it is a genuine location claim, not illustration, and needs the
# `foreign-path-ok` marker rather than a heuristic exemption.
#
# The discriminator used here is STRUCTURAL, not a naming-convention guess: a
# matched root is escalated to a finding whenever it is followed by AT LEAST
# ONE path segment, hyphenated or not -- naming a segment is what makes the
# text an assertion about WHERE something lives. A bare root mention with no
# trailing segment is platform illustration, not a location claim, and stays
# quiet.
#
# An earlier revision of this heuristic escalated only when the trailing
# segment contained a hyphen or underscore, on the theory that this fleet's
# repo names are always hyphenated/snake_case. That theory was false on the
# corpus it was built to protect: two real fleet repos with single-word,
# hyphen-free names were among the leaked lines this guard exists to catch,
# and the segment-shape heuristic missed them -- roughly a third of its own
# class -- while its own docstring called that gap unlikely. A structural
# "any segment fires" rule has no such blind spot; it can only over-fire on
# a genuine illustrative mention, which the explicit escape hatch below
# exists to handle deliberately rather than the guard silently guessing
# forever.
#
# The explicit escape hatch is a per-line allow-marker
# (`<!-- foreign-path-ok: <reason> -->`): an author who KNOWS a line is
# illustrative, not asserting a location, can mark it once, deliberately,
# reviewably.
# The TOKEN is the marker, not the comment wrapper around it. Markdown carries
# `<!-- foreign-path-ok: ... -->`, but the same judgement has to be expressible
# in a .py docstring, a .toml sample, a Windows .cmd `rem`, or a PowerShell `#`
# line -- and a marker syntax that only one file type can spell is a marker that
# silently fails everywhere else. Matching the bare token keeps one vocabulary
# across every surface an author might have to mark.
# Both spellings are honored, in both directions. The sibling
# `guard_concrete_path_citations` introduced `abs-path-ok:` for the same
# judgement -- "a human looked at this line and decided the literal path is
# the point." Recognizing only the older token here would make every marker
# written for the newer guard invisible to this one, which is the same
# silent-void failure in mirror image. One decision, one annotation, both
# guards.
_ALLOW_MARKER_RE = re.compile(r"(?:foreign-path-ok|abs-path-ok)\s*:", re.IGNORECASE)

_PROSE_STOP_CHARS = "`'\"),;"
# Reuses the shared `WIN_DRIVE_RE` pattern text (not a second copy of the
# URL-safe lookbehind) with a trailing capture group for the segment.
_SEGMENT_AFTER_WIN_DRIVE_RE = re.compile(
    _WIN_DRIVE_RE.pattern + r"([^\s" + re.escape(_PROSE_STOP_CHARS) + r"]*)"
)
_SEGMENT_AFTER_CYGDRIVE_RE = re.compile(
    r"/cygdrive/[A-Za-z]/([^\s" + re.escape(_PROSE_STOP_CHARS) + r"]*)"
)
_SEGMENT_AFTER_POSIX_ROOT_RE = re.compile(
    r'(?<![A-Za-z0-9_./\\:-])/([A-Za-z0-9_][^\s"\')]*)'
)


def _segment_is_assertion_shaped(segment: str) -> bool:
    """True iff `segment` (the path text following a drive-letter/POSIX
    root) names at least one path component -- the structural discriminator
    between a bare root/mount-point mention (no trailing segment, stays
    quiet) and an asserted repo/project location (fires). See the
    module-level comment above this regex block for the full rationale."""
    core = segment.rstrip("\\/")
    return bool(core)


@dataclass(frozen=True)
class ProseFinding:
    """One foreign-platform-shaped path ASSERTION found in free-form prose
    (CLAUDE.md / CLAUDE.local.md), distinct from `Finding` (settings.json's
    parsed-JSON-tree shape) because the pointer here is a 1-based line
    number, not a JSON pointer."""

    line: int
    value: str
    shape: str  # "windows-drive-path-on-posix-host" | "posix-path-on-windows-host"


def detect_foreign_platform_paths_in_prose(
    text: str,
    host_is_windows: Optional[bool] = None,
) -> List[ProseFinding]:
    """Scan free-form markdown text (CLAUDE.md / CLAUDE.local.md content, not
    settings.json) for a foreign-platform-shaped path ASSERTION -- a
    foreign-platform root followed by at least one path segment, not a bare
    root/mount-point mention with nothing after it. See the module-level
    comment above this function's regexes for the full rationale and worked
    examples.

    A line carrying an inline `<!-- foreign-path-ok: <reason> --> ` marker is
    skipped entirely, regardless of shape -- the deliberate, reviewable
    per-line escape hatch for a genuine illustrative mention that must
    survive as written.
    """
    if host_is_windows is None:
        host_is_windows = os.name == "nt"

    findings: List[ProseFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _ALLOW_MARKER_RE.search(line):
            continue
        if not host_is_windows:
            for regex, shape in (
                (_SEGMENT_AFTER_WIN_DRIVE_RE, "windows-drive-path-on-posix-host"),
                (_SEGMENT_AFTER_CYGDRIVE_RE, "windows-drive-path-on-posix-host"),
            ):
                for m in regex.finditer(line):
                    if _segment_is_assertion_shaped(m.group(1)):
                        findings.append(
                            ProseFinding(line=lineno, value=m.group(0), shape=shape)
                        )
        else:
            for m in _SEGMENT_AFTER_POSIX_ROOT_RE.finditer(line):
                if _segment_is_assertion_shaped(m.group(1)):
                    findings.append(
                        ProseFinding(
                            line=lineno,
                            value=m.group(0),
                            shape="posix-path-on-windows-host",
                        )
                    )
    return findings


def format_prose_banner(findings: List[ProseFinding], file_label: str) -> str:
    """Render a box-drawn, byte-loud banner for prose-scan findings -- same
    posture as `format_banner`, but naming a FILE + LINE (prose has no JSON
    pointer) and the doctrine-specific remedy (resolve via `machine-local
    get repos.<key>`, per `~/.claude/CLAUDE.local.md`'s own sibling-repo-map
    convention, rather than settings.json's "re-run the installer" remedy)."""
    if not findings:
        return ""
    top = "\u2554" + ("\u2550" * 66) + "\u2557"
    bot = "\u255a" + ("\u2550" * 66) + "\u255d"
    lines = [
        "",
        top,
        f"\u2551  \u26a0  FOREIGN-PLATFORM PATH(S) DETECTED in {file_label}",
        "\u2551",
        "\u2551  This file carries at least one path ASSERTING a specific repo/project",
        "\u2551  location shaped for a DIFFERENT operating system than this machine. An",
        "\u2551  agent that reads this file trusts the path and concludes the repo is",
        "\u2551  unreachable, even when it is checked out and readable on this host.",
        "\u2551",
    ]
    for f in findings:
        lines.append(f"\u2551  LINE:     {f.line}")
        lines.append(f"\u2551  FOUND:    {f.value}")
        lines.append(f"\u2551  SHAPE:    {f.shape}")
        lines.append("\u2551")
    lines.append(
        "\u2551  REMEDIATE: name the repo, not the path -- resolve at read time via"
    )
    lines.append(
        "\u2551  `machine-local get repos.<key>`, never a literal path written here."
    )
    lines.append(
        "\u2551  A genuine illustrative mention (not an asserted location) can be"
    )
    lines.append(
        "\u2551  marked with a trailing `<!-- foreign-path-ok: <reason> -->`."
    )
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


def _is_windows_shaped(value: str) -> bool:
    return bool(_WIN_DRIVE_RE.search(value) or _CYGDRIVE_RE.search(value))


def _is_posix_shaped(value: str) -> bool:
    return bool(_POSIX_ROOT_RE.search(value))


def _is_command_pointer(pointer: str) -> bool:
    """True iff the JSON pointer's final segment is `command` -- the only
    place in settings.json's schema where a string is actually parsed and
    executed by a shell/PowerShell (a `hooks[].hooks[].command` entry).
    Restricting the env-var-shape check to this segment is what keeps a
    `$env:FOO`-shaped mention inside a description/comment/prose field from
    false-positiving -- that text is never executed, so a foreign shell
    syntax sitting in it is not a functional break."""
    return pointer.rsplit("/", 1)[-1] == "command"


def _is_windows_env_var_shaped(value: str) -> bool:
    return bool(_ENV_VAR_WIN_RE.search(value))


def _is_posix_env_var_shaped(value: str) -> bool:
    """True iff `value` contains a bare `$NAME` (not immediately followed
    by ':' -- that spelling is the Windows `$env:NAME` form, handled
    separately, and not a PowerShell automatic variable -- that's native
    Windows syntax, see `_POWERSHELL_AUTOMATIC_VARS`) or a braced `${NAME}`
    reference. See `_DOLLAR_IDENTIFIER_RE` for why this is a manual scan
    rather than a single lookahead regex."""
    for match in _DOLLAR_IDENTIFIER_RE.finditer(value):
        braced = match.group(1)
        identifier = match.group(2)
        if identifier.lower() in _POWERSHELL_AUTOMATIC_VARS:
            continue
        if braced:
            return True
        end = match.end()
        if end < len(value) and value[end] == ":":
            continue  # this is "$env:NAME"-shaped, not a bare POSIX reference
        return True
    return False


@dataclass(frozen=True)
class Finding:
    """One foreign-platform-shaped string value found in the config."""

    pointer: str  # JSON-pointer-ish path, e.g. "/hooks/PreToolUse/0/hooks/0/command"
    value: str
    shape: str  # "windows-drive-path-on-posix-host" | "posix-path-on-windows-host"
    suggested: Optional[str]  # best-effort corrected form, or None if underivable


def _read_doe_root(config_dir: Path) -> Optional[str]:
    """Best-effort read of `{config_dir}/.doe-root` -- the local coordinator
    clone root, used to derive a corrected suggestion. Strips only a
    trailing CR/LF (embedded spaces preserved -- Windows "OneDrive - Name"
    paths), mirroring `guard_settings_integrity.is_inline_install`."""
    doeroot_file = config_dir / _DOEROOT_NAME
    if not doeroot_file.is_file():
        return None
    try:
        with doeroot_file.open("r", encoding="utf-8", newline="") as fh:
            first_line = fh.readline()
    except OSError:
        return None
    doe = first_line.rstrip("\r\n")
    return doe or None


def _suggest_corrected(value: str, local_coordinator_root: Optional[str]) -> Optional[str]:
    """Best-effort corrected form: locate the `coordinator/...` tail of the
    foreign path and re-root it under this machine's own coordinator clone.
    Returns None (no opinion) when the tail can't be located or no local
    root is known -- never guesses a full path from nothing."""
    if not local_coordinator_root:
        return None
    normalized = value.replace("\\", "/")
    marker = "/coordinator/"
    idx = normalized.find(marker)
    if idx == -1:
        # Also handle a bare leading "coordinator/..." (no path before it).
        if normalized.startswith("coordinator/"):
            tail = normalized[len("coordinator/"):]
        else:
            return None
    else:
        tail = normalized[idx + len(marker):]
    root = local_coordinator_root.replace("\\", "/").rstrip("/")
    return f"{root}/coordinator/{tail}"


def detect_foreign_platform_paths(
    data: Any,
    host_is_windows: Optional[bool] = None,
    local_coordinator_root: Optional[str] = None,
) -> List[Finding]:
    """Walk a parsed JSON structure (typically `settings.json`'s content) and
    return every string value shaped like a path from the OTHER platform.

    Parameters
    ----------
    data:
        Parsed JSON (dict/list/scalar tree) -- caller's responsibility to
        `json.load` first (this function never touches disk itself, so it is
        directly unit-testable with literal dicts).
    host_is_windows:
        Defaults to `os.name == "nt"` when None -- override in tests.
    local_coordinator_root:
        Best-effort local coordinator clone root for the `suggested` field.
        Callers typically resolve this via `_read_doe_root` against their own
        config dir before calling in.
    """
    if host_is_windows is None:
        host_is_windows = os.name == "nt"

    findings: List[Finding] = []

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                walk(val, f"{pointer}/{key}")
        elif isinstance(node, list):
            for i, val in enumerate(node):
                walk(val, f"{pointer}/{i}")
        elif isinstance(node, str):
            if not host_is_windows and _is_windows_shaped(node):
                findings.append(
                    Finding(
                        pointer=pointer,
                        value=node,
                        shape="windows-drive-path-on-posix-host",
                        suggested=_suggest_corrected(node, local_coordinator_root),
                    )
                )
            elif host_is_windows and _is_posix_shaped(node):
                findings.append(
                    Finding(
                        pointer=pointer,
                        value=node,
                        shape="posix-path-on-windows-host",
                        suggested=_suggest_corrected(node, local_coordinator_root),
                    )
                )
            if _is_command_pointer(pointer):
                if not host_is_windows and _is_windows_env_var_shaped(node):
                    findings.append(
                        Finding(
                            pointer=pointer,
                            value=node,
                            shape="windows-env-var-syntax-on-posix-host",
                            suggested=None,
                        )
                    )
                elif host_is_windows and _is_posix_env_var_shaped(node):
                    findings.append(
                        Finding(
                            pointer=pointer,
                            value=node,
                            shape="posix-env-var-syntax-on-windows-host",
                            suggested=None,
                        )
                    )

    walk(data, "")
    return findings


# ---------------------------------------------------------------------------
# Banner text -- loud by construction (this incident's defining property was
# ~40 dead hooks producing NO signal at all; a whisper here is worthless).
# ---------------------------------------------------------------------------


_PATH_SHAPES = {"windows-drive-path-on-posix-host", "posix-path-on-windows-host"}
_ENV_VAR_SHAPES = {
    "windows-env-var-syntax-on-posix-host",
    "posix-env-var-syntax-on-windows-host",
}


def format_banner(findings: List[Finding], settings_path: str) -> str:
    """Render a box-drawn, byte-loud banner naming every offending key.

    The two finding families -- a foreign PATH literal, and a foreign
    ENV-VAR-REFERENCE SYNTAX -- are reported under distinct sub-headings
    (not merged into one undifferentiated list) so a reader can tell which
    class fired from the banner text alone: a path break needs a path
    hand-fixed, an env-var-syntax break needs the command's `$env:`/`$`
    spelling flipped for this platform -- different remediation, different
    class, must not read as the same finding twice."""
    if not findings:
        return ""
    path_findings = [f for f in findings if f.shape in _PATH_SHAPES]
    env_findings = [f for f in findings if f.shape in _ENV_VAR_SHAPES]
    top = "\u2554" + ("\u2550" * 66) + "\u2557"
    bot = "\u255a" + ("\u2550" * 66) + "\u255d"
    lines = [
        "",
        top,
        f"\u2551  \u26a0  FOREIGN-PLATFORM PATH(S) DETECTED in {settings_path}",
        "\u2551",
    ]

    if path_findings:
        lines.append(
            "\u2551  -- FOREIGN PATH(S) -----------------------------------------------"
        )
        lines.append(
            "\u2551  This file carries at least one path shaped for a DIFFERENT operating"
        )
        lines.append(
            "\u2551  system than this machine. Every hook/guard whose `command` uses one"
        )
        lines.append(
            "\u2551  of these paths will silently fail to fire -- no error, just dead"
        )
        lines.append("\u2551  tooling.")
        lines.append("\u2551")
        for f in path_findings:
            lines.append(f"\u2551  KEY:      {f.pointer}")
            lines.append(f"\u2551  FOUND:    {f.value}")
            lines.append(f"\u2551  SHAPE:    {f.shape}")
            if f.suggested:
                lines.append(f"\u2551  EXPECTED: {f.suggested}")
            else:
                lines.append(
                    "\u2551  EXPECTED: (no local coordinator root resolvable -- hand-fix)"
                )
            lines.append("\u2551")
        lines.append(
            "\u2551  REMEDIATE: hand-fix the path(s) above to this machine's own"
        )
        lines.append(
            "\u2551  coordinator clone root, or re-run the installer to regenerate this"
        )
        lines.append("\u2551  machine's hook block, then reload the session.")
        lines.append("\u2551")

    if env_findings:
        lines.append(
            "\u2551  -- FOREIGN ENV-VAR REFERENCE SYNTAX --------------------------------"
        )
        lines.append(
            "\u2551  This file carries a hook `command` that reads an env var using the"
        )
        lines.append(
            "\u2551  OTHER platform's shell syntax (PowerShell `$env:NAME` vs POSIX"
        )
        lines.append(
            "\u2551  `$NAME`/`${NAME}` -- there is no spelling valid on both). No drive"
        )
        lines.append(
            "\u2551  letter or POSIX root is present, so this is invisible to a plain"
        )
        lines.append(
            "\u2551  path check; the command silently resolves the var to empty/unset"
        )
        lines.append("\u2551  and the hook exits 127 (absent) with no error.")
        lines.append("\u2551")
        for f in env_findings:
            lines.append(f"\u2551  KEY:      {f.pointer}")
            lines.append(f"\u2551  FOUND:    {f.value}")
            lines.append(f"\u2551  SHAPE:    {f.shape}")
            lines.append("\u2551")
        lines.append(
            "\u2551  REMEDIATE: re-run the installer to regenerate this machine's hook"
        )
        lines.append(
            "\u2551  block with THIS platform's env-var syntax (`$env:NAME` on Windows,"
        )
        lines.append(
            "\u2551  `$NAME`/`${NAME}` on POSIX) -- do not hand-edit the `$`-form in"
        )
        lines.append("\u2551  place, it will drift again on the next cross-machine sync.")
        lines.append("\u2551")

    lines.append(
        "\u2551  Action: this is DETECT-ONLY -- settings.json is shared with another"
    )
    lines.append(
        "\u2551  machine via git sync; auto-repair here could clobber that machine on"
    )
    lines.append("\u2551  its next pull. A human/PM call is required to pick a value.")
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


def evaluate_foreign_platform_paths(
    settings_path: Path,
    config_dir: Optional[Path] = None,
    host_is_windows: Optional[bool] = None,
) -> str:
    """Read + parse `settings_path`, return the banner text (empty string ==
    silent/healthy/absent/unparsable). Never raises -- mirrors the fail-open
    posture of the sibling settings-integrity guard.
    """
    if not settings_path.is_file():
        return ""
    try:
        with settings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""

    resolved_config_dir = config_dir if config_dir is not None else settings_path.parent
    local_root = _read_doe_root(resolved_config_dir)

    findings = detect_foreign_platform_paths(
        data, host_is_windows=host_is_windows, local_coordinator_root=local_root
    )
    return format_banner(findings, str(settings_path))
