"""
resolve_validation_cmd.py — Python port of
coordinator/lib/coordinator-resolve-validation-cmd.sh.

Port of: coordinator-resolve-validation-cmd.sh (coordinator-claude c187f5b9, 2026-07-21)
Spec backlink: archive/specs/2026-05-28-workday-complete-fast-test-resolution.md § 3.4

Purpose: resolve which shell command to use for test validation. Two tiers:
  - cs_resolve_fast_test_cmd — the FAST-tier gate for /workday-complete Step 1,
    /workweek-complete Step 2, and the /validate skill (three-step resolution).
  - cs_resolve_full_test_cmd — the FULL suite for /bug-blitz, which chases every
    failing test (four-step resolution; falls back to the fast tier with a caveat
    rather than skipping, so a repo with no full_test_cmd still gets coverage).

Fast-tier resolution order (cs_resolve_fast_test_cmd — three steps, no
conventional fallback — the Staff Engineer F1):
  1. COORDINATOR_FAST_TEST_CMD env var — if set and non-empty, use verbatim.
  2. coordinator.local.md at repo root — flat top-level fast_test_cmd: key.
  3. Skip-with-notice — stderr names both remediation paths; ResolvedCommand(None, 2).
Full-tier resolution order (cs_resolve_full_test_cmd — four steps, falls back
to fast rather than skipping): COORDINATOR_FULL_TEST_CMD -> full_test_cmd: key ->
fast-tier fallback (exit 3) -> unconfigured (exit 2). See its docstring below.

Return-code contract (mirrors the bash oracle's exit-code contract):
  ResolvedCommand.cmd        — resolved command string (only non-None when exit_code
                                is 0 or 3)
  ResolvedCommand.exit_code
    0   — command resolved; caller should execute it and capture its exit code
    2   — no command configured; caller should treat validation as skipped
    3   — (cs_resolve_full_test_cmd ONLY) resolved by fast-tier FALLBACK; .cmd is
          the fast command, and the caller MUST report coverage as fast-tier-only
    126 — the configured value carries an un-interpretable backslash-escaped quote
          (`\"` / `\'`) that survived scalar unquoting. A HARD config failure, NOT
          a skip: handing such a value to `bash -c` re-splits the quoted
          sub-argument into separate argv tokens and the gate reports a build/test
          failure that is really a config defect. Callers MUST surface it as
          blocking. See _strip_wrapping_quotes / _check_escape_residue.
    127 — command resolved to a bare `python` token but NEITHER python3 nor python
          is on PATH. A HARD environment failure, NOT a skip — callers MUST surface
          this as a blocking validation failure (distinguish from exit_code 2). This
          closes the silent-127 swallow (a python3-only machine previously skipped
          validation entirely). A leading `python ` token is normalized python3-first
          at resolution time, so this fires only when no Python interpreter exists
          at all.

Channel-adaptation note (deliberate, not a scope drop): the bash oracle's "stdout"
was purely the return-value channel back to the invoking shell (`cmd=$(cs_resolve_fast_test_cmd)`).
Python has a real return-value channel, so this port returns ResolvedCommand
instead of also printing the resolved command to stdout — printing it too would be
a doubling, not parity. The stderr DIAGNOSTIC contract (step=env-var / step=local-md
/ step=skipped / step=fast-fallback prose, the metachar WARN tripwire, and the
no-interpreter fail-loud line) is preserved byte-for-byte, including message text,
for parity testing against the bash oracle.

Trust model: COORDINATOR_FAST_TEST_CMD is an escape hatch for trusted contexts
(local shell, CI-managed env). It is not safe to accept from untrusted sources
(memo bodies, webhook payloads, third-party files, untrusted hook input). Callers
MUST NOT set this from any prompt-derived input. No sanitization of shell
metacharacters is performed by design — the configured command is trusted. A LIGHT
advisory tripwire fires on stderr when the resolved value contains $( ` or `; `
(visible signal only, not a block); suppress via
COORDINATOR_FAST_TEST_CMD_SUPPRESS_METACHAR_WARN=1.

No first-token heuristic (the Staff Engineer F5): the resolver does NOT pre-parse, probe, or
validate the command string beyond the python-token normalization below. Runtime
exit-code from the configured command, once actually executed by the caller, is
the safety net.

This module is authored as the claude-klabauter-side Python equivalent for future Python
callers; it does not replace or alter any bash caller's own resolution path.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FAST_TEST_CMD_ENV = "COORDINATOR_FAST_TEST_CMD"
FULL_TEST_CMD_ENV = "COORDINATOR_FULL_TEST_CMD"
SUPPRESS_METACHAR_WARN_ENV = "COORDINATOR_FAST_TEST_CMD_SUPPRESS_METACHAR_WARN"

_FAST_TEST_CMD_KEY_RE = re.compile(r"^fast_test_cmd:\s*")


class NoPythonInterpreterError(RuntimeError):
    """Raised when a bare `python` token needs normalizing but neither python3
    nor python is on PATH. Mirrors the bash oracle's exit 127 (a hard environment
    failure, distinct from exit 2 "unconfigured skip")."""


class MalformedValueError(RuntimeError):
    """Raised when a resolved value carries an un-interpretable escaped quote
    after wrapping-quote unquoting. Mirrors the bin twin's exit 126 (a hard
    config failure, distinct from exit 2 "unconfigured skip")."""


_ESCAPED_QUOTE_RE = re.compile(r"\\[\"']")


@dataclass(frozen=True)
class ResolvedCommand:
    """Return type for both resolvers. See module docstring for the exit_code
    contract this mirrors from the bash oracle."""

    cmd: Optional[str]
    exit_code: int


# --- Unit 1: helpers (_cs_redact_for_diag, _cs_metachar_warn,
# _cs_resolve_python_interp, _cs_normalize_python_token) ---------------------


def redact_for_diag(s: str) -> str:
    """Truncate a command string for stderr diagnostic display.

    Prevents secret leakage when the configured command embeds tokens (e.g.
    `MY_TOKEN=abc python run.py`). Diagnostic stderr is captured into review
    trail JSON and session transcripts; truncating past 60 chars keeps the
    resolver step legible while bounding leakage surface.
    """
    if len(s) > 60:
        return f"{s[:60]}…(truncated; {len(s)} chars total)"
    return s


def metachar_warn(s: str, step: str, caller: str = "cs_resolve_fast_test_cmd") -> None:
    """Light advisory tripwire on shell-metacharacter shapes.

    Visible-signal-only; does NOT block. Suppressible via
    COORDINATOR_FAST_TEST_CMD_SUPPRESS_METACHAR_WARN=1 for known-safe
    configurations that legitimately use these shapes (e.g.
    `python foo.py && python bar.py`).
    """
    if os.environ.get(SUPPRESS_METACHAR_WARN_ENV, "0") == "1":
        return
    if "$(" in s or "`" in s or "; " in s:
        print(
            f"[{caller}] WARN (step={step}): resolved value contains shell "
            "metacharacters ($( or backtick or ;) — confirm this is intended; "
            f"set {SUPPRESS_METACHAR_WARN_ENV}=1 to silence this warning for "
            "this and other callers.",
            file=sys.stderr,
        )


def resolve_python_interp() -> Optional[str]:
    """python3-first interpreter resolution.

    Returns `python3` when present, else `python`; None when neither is on
    PATH. Centralizes the `command -v python3 || command -v python` idiom.
    """
    if shutil.which("python3"):
        return "python3"
    if shutil.which("python"):
        return "python"
    return None


def normalize_python_token(cmd: str) -> str:
    """Interpreter-portability normalization.

    When <cmd>'s first whitespace-delimited token is the BARE interpreter
    `python` (not python3, not an explicit path, not VAR=x env-prefixed),
    rewrite it to a python3-first resolved interpreter. This is the
    load-bearing fix for coordinator.local.md's `fast_test_cmd: python …`
    staying interpreter-agnostic and portable across python3-only machines
    (modern macOS/Linux — `python` absent) and python-only machines
    (Windows/pyenv — `python3` absent), without hardcoding either.

    Raises NoPythonInterpreterError (mirrors bash exit 127) when the token is
    bare `python` and NEITHER interpreter exists — this replaces the silent
    command-not-found 127 the validate gate previously swallowed as a skip.

    python3, explicit paths (/usr/bin/python), env-prefixed (FOO=1 python …),
    and non-python commands (pnpm test, pytest, /validate) pass through
    untouched. Matching contract: matches the bare interpreter `python` with
    OR without arguments (`python` and `python <args>`); does NOT match
    `python2`, `python3`, or path forms.
    """
    if cmd == "python" or cmd.startswith("python "):
        interp = resolve_python_interp()
        if interp is None:
            print(
                f"[cs_resolve] no python3/python on PATH — cannot run '{cmd}' "
                "(refusing to skip; fail loud)",
                file=sys.stderr,
            )
            raise NoPythonInterpreterError(cmd)
        return interp + cmd[len("python"):]
    return cmd


# --- frontmatter parsing shared by both units --------------------------------


def _extract_frontmatter(path: Path) -> str:
    """Lines strictly between the first and second `---` marker lines.

    Mirrors: awk '/^---$/{n++; if (n==2) exit; next} n==1' <path>
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    n = 0
    for line in text.splitlines():
        if line == "---":
            n += 1
            if n == 2:
                break
            continue
        if n == 1:
            out.append(line)
    return "\n".join(out)


def _strip_wrapping_quotes(val: str) -> str:
    """Unquote ONE wrapping YAML scalar pair, preserving interior quotes.

    Interior quotes are load-bearing: a configured command routinely contains
    a quoted sub-argument (`-m 'not cadence and not pending_fix'`,
    `--filter "a b"`). Stripping every quote character — the shape the
    retired bash oracle's `tr -d '"' | tr -d "'"` pipeline used, and which
    this module previously inherited verbatim as a "faithful port, not a bug
    fix" — silently shell-splits such a value into separate argv tokens
    (pytest exits 4 with `file or directory not found: and`, collecting zero
    tests). Only the outermost matched pair, which is YAML scalar syntax
    rather than shell syntax, is ours to remove.

    Removing the pair is not sufficient on its own: inside a YAML
    double-quoted scalar the interior quotes are themselves escaped
    (`"… -m \\"a and b\\" …"`), and leaving those backslashes in place hands
    `bash -c` a value it re-splits exactly as the old transform did — the
    same silent corruption wearing a different costume. So a double-quoted
    scalar additionally resolves its `\\"` and `\\\\` escapes, and a
    single-quoted scalar its doubled-`''` escape, in one left-to-right pass
    (no other YAML escape is interpreted — see _check_escape_residue for what
    happens to the rest). Ported from the bin twin
    (coordinator/bin/coordinator-resolve-validation-cmd.py
    `_strip_wrapping_quotes`), which already carried this fix.
    """
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return _unescape_double_quoted(val[1:-1])
    if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
        return val[1:-1].replace("''", "'")
    return val


def _unescape_double_quoted(body: str) -> str:
    """Resolve the `\\"` and `\\\\` escapes of a YAML double-quoted scalar body.

    Single left-to-right pass so `\\\\"` reads as backslash-then-quote-delimiter
    rather than being double-processed. Every other backslash sequence is
    passed through verbatim — Windows paths (`C:\\tools\\py`) must survive
    untouched, and an escape this function does not claim to understand is
    caught downstream by _check_escape_residue rather than silently mangled
    here.
    """
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in ('"', "\\"):
            out.append(body[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _check_escape_residue(val: str, step: str, caller: str) -> None:
    """Fail loud when an un-interpretable escaped quote survives unquoting.

    This resolver is a flat-key frontmatter reader, not a YAML parser: it
    claims only the escapes _strip_wrapping_quotes resolves. Anything else
    carrying a `\\"` or `\\'` — an unquoted scalar written with shell-style
    escaping, a partially-quoted value, a hand-edited frontmatter line —
    cannot be handed to `bash -c` intact, because the shell drops the
    backslash and promotes the quote to a real delimiter, re-splitting the
    sub-argument. Raises MalformedValueError; callers map it to exit_code 126.
    """
    if _ESCAPED_QUOTE_RE.search(val):
        print(
            f"[{caller}] FAIL (step={step}): resolved value carries an escaped quote "
            f"this resolver cannot interpret: {redact_for_diag(val)}\n"
            f"[{caller}] Passing it to `bash -c` would re-split the quoted argument. "
            "Wrap the whole value in single quotes and use plain double quotes inside "
            "(fast_test_cmd: 'pytest -m \"a and b\"'), or drop the backslashes.",
            file=sys.stderr,
        )
        raise MalformedValueError(val)


def _extract_fast_test_cmd(frontmatter: str) -> str:
    """Extract fast_test_cmd: as a bare string from a frontmatter block.

    Parser shape matches bin/audit-enabled-plugins.sh (flat-top-level awk).
    Strips ONE wrapping quote pair and trims leading/trailing whitespace, but
    PRESERVES internal whitespace and internal quotes — matches the bash
    sed-trim (not xargs, which word-splits + collapses double-spaces). See
    _strip_wrapping_quotes for why a surrounding-pair-only strip (not a
    global tr -d) is required.
    """
    for line in frontmatter.split("\n"):
        if line.startswith("fast_test_cmd:"):
            rest = _FAST_TEST_CMD_KEY_RE.sub("", line, count=1)
            return _strip_wrapping_quotes(rest.strip())
    return ""


# --- Unit 2: cs_resolve_fast_test_cmd, cs_read_local_md_key,
# cs_resolve_full_test_cmd -----------------------------------------------------


def cs_resolve_fast_test_cmd(
    repo_root: Optional[str] = None, *, _quiet: bool = False
) -> ResolvedCommand:
    """Resolve the fast-test command for the given repo root.

    Arguments:
      repo_root — optional; path to the repo root to check for
                  coordinator.local.md. Defaults to os.getcwd(). Callers may
                  pass an explicit path for testability.
      _quiet    — internal; suppresses stderr diagnostics. Used by
                  cs_resolve_full_test_cmd's Step 3 fallback, mirroring the
                  bash oracle's `2>/dev/null` on that call site — fast-tier
                  diags must not bleed into full-tier output.
    """
    repo_root = repo_root if repo_root is not None else os.getcwd()

    def _eprint(msg: str) -> None:
        if not _quiet:
            print(msg, file=sys.stderr)

    # Step 1 — COORDINATOR_FAST_TEST_CMD env var
    env_val = os.environ.get(FAST_TEST_CMD_ENV, "")
    if env_val:
        _eprint(
            f"[cs_resolve_fast_test_cmd] step=env-var resolved: {redact_for_diag(env_val)}"
        )
        if not _quiet:
            metachar_warn(env_val, "env-var")
        try:
            _check_escape_residue(env_val, "env-var", "cs_resolve_fast_test_cmd")
            norm = normalize_python_token(env_val)
        except MalformedValueError:
            return ResolvedCommand(None, 126)
        except NoPythonInterpreterError as exc:
            _eprint(f"[cs_resolve_fast_test_cmd] step=env-var hard-fail: {exc}")
            return ResolvedCommand(None, 127)
        return ResolvedCommand(norm, 0)

    # Step 2 — coordinator.local.md flat top-level fast_test_cmd: key
    local_md = Path(repo_root) / "coordinator.local.md"
    if local_md.is_file():
        fm = _extract_frontmatter(local_md)
        fast_test_cmd = _extract_fast_test_cmd(fm)
        if fast_test_cmd:
            _eprint(
                f"[cs_resolve_fast_test_cmd] step=local-md resolved: {redact_for_diag(fast_test_cmd)}"
            )
            if not _quiet:
                metachar_warn(fast_test_cmd, "local-md")
            try:
                _check_escape_residue(fast_test_cmd, "local-md", "cs_resolve_fast_test_cmd")
                norm = normalize_python_token(fast_test_cmd)
            except MalformedValueError:
                return ResolvedCommand(None, 126)
            except NoPythonInterpreterError as exc:
                _eprint(f"[cs_resolve_fast_test_cmd] step=local-md hard-fail: {exc}")
                return ResolvedCommand(None, 127)
            return ResolvedCommand(norm, 0)

    # Step 3 — skip-with-notice (no conventional fallback — the Staff Engineer F1)
    _eprint("[cs_resolve_fast_test_cmd] step=skipped — no fast-test command configured.")
    _eprint("[cs_resolve_fast_test_cmd] To configure, choose one of:")
    _eprint('  a) Set env var: export COORDINATOR_FAST_TEST_CMD="<your-command>"')
    _eprint('  b) Add to coordinator.local.md frontmatter: fast_test_cmd: "<your-command>"')
    return ResolvedCommand(None, 2)


def cs_read_local_md_key(repo_root: str, key: str) -> str:
    """Extract a flat top-level frontmatter key from coordinator.local.md.

    Applies the same quote-strip / whitespace-trim discipline as
    cs_resolve_fast_test_cmd's inline parser (preserves internal whitespace
    AND internal quotes — see _strip_wrapping_quotes). Always "succeeds"
    (never raises for a missing file/key) — empty string means the file or
    key is absent. `key` is matched as a LITERAL substring (mirrors bash
    `grep -F`), never as a regex.

    Faithful oracle-bug repro (NOT a bug fix): the bash prefix-strip
    (`val="${line#"${key}:"}"`) only strips `"${key}:"` when it is an exact
    PREFIX of the matched line. Because the grep match itself is unanchored
    (matches `key:` anywhere in the line, not just at the start), a line
    where `key:` appears mid-line (not at column 0) passes the grep but the
    subsequent bash prefix-strip is a silent no-op, leaving `val` equal to
    the whole matched line. This port reproduces that quirk exactly rather
    than "fixing" it to an anchored match.

    Quote handling IS a bug fix (not a faithful repro): the previous
    implementation stripped a leading AND a trailing quote of EACH type
    independently (4 sequential, un-paired strips), rather than treating the
    quotes as one wrapping pair. For a value with an outer YAML wrapper AND
    an inner shell-quoted sub-argument (e.g.
    `"python3 -m pytest -m 'not a and not b'"`), that produced: strip leading
    `"` -> strip trailing `"` (the true wrapper) -> then ALSO strip a
    trailing `'` that was in fact the closing delimiter of the inner shell
    quote, leaving an unbalanced quote in the resolved command. See
    _strip_wrapping_quotes for the single-pair-only replacement.
    """
    local_md = Path(repo_root) / "coordinator.local.md"
    if not local_md.is_file():
        return ""
    fm = _extract_frontmatter(local_md)
    needle = f"{key}:"
    line = None
    for candidate in fm.split("\n"):
        if needle in candidate:
            line = candidate
            break
    if line is None:
        return ""
    val = line[len(needle):] if line.startswith(needle) else line
    val = val.strip()
    return _strip_wrapping_quotes(val)


def cs_resolve_full_test_cmd(repo_root: Optional[str] = None) -> ResolvedCommand:
    """Resolve the FULL test suite command — the heavier tier bug-blitz runs
    to chase every failing test (distinct from the fast-tier gate the
    /validate skill runs).

    Resolution order (four steps — falls back to fast, NOT to skip):
      1. COORDINATOR_FULL_TEST_CMD env var — if set and non-empty, use verbatim.
      2. coordinator.local.md flat top-level full_test_cmd: key.
      3. FALL BACK to cs_resolve_fast_test_cmd, with a caveat on stderr — a
         repo that has not declared a full suite still gets coverage (the
         fast tier), and the caller is told the coverage is fast-tier-only so
         it can surface that honestly.
      4. If even the fast resolver skips (exit_code 2), propagate exit_code 2
         (nothing configured).

    Return contract:
      exit_code 0   — a FULL command resolved (env or local.md); coverage is
                       full-suite.
      exit_code 3   — resolved by fast-tier FALLBACK; .cmd is the fast
                       command, and the caller MUST report coverage as
                       fast-tier-only (caveat already emitted on stderr).
      exit_code 2   — nothing configured at either tier; caller treats the
                       suite as unconfigured.
      exit_code 127 — hard environment failure (no python interpreter),
                       propagated from either tier, NOT a skip.
    """
    repo_root = repo_root if repo_root is not None else os.getcwd()

    # Step 1 — COORDINATOR_FULL_TEST_CMD env var
    env_val = os.environ.get(FULL_TEST_CMD_ENV, "")
    if env_val:
        print(
            f"[cs_resolve_full_test_cmd] step=env-var resolved: {redact_for_diag(env_val)}",
            file=sys.stderr,
        )
        metachar_warn(env_val, "env-var", "cs_resolve_full_test_cmd")
        try:
            _check_escape_residue(env_val, "env-var", "cs_resolve_full_test_cmd")
            norm = normalize_python_token(env_val)
        except MalformedValueError:
            return ResolvedCommand(None, 126)
        except NoPythonInterpreterError as exc:
            print(
                f"[cs_resolve_full_test_cmd] step=env-var hard-fail: {exc}",
                file=sys.stderr,
            )
            return ResolvedCommand(None, 127)
        return ResolvedCommand(norm, 0)

    # Step 2 — coordinator.local.md flat top-level full_test_cmd: key
    full_test_cmd = cs_read_local_md_key(repo_root, "full_test_cmd")
    if full_test_cmd:
        print(
            f"[cs_resolve_full_test_cmd] step=local-md resolved: {redact_for_diag(full_test_cmd)}",
            file=sys.stderr,
        )
        metachar_warn(full_test_cmd, "local-md", "cs_resolve_full_test_cmd")
        try:
            _check_escape_residue(full_test_cmd, "local-md", "cs_resolve_full_test_cmd")
            norm = normalize_python_token(full_test_cmd)
        except MalformedValueError:
            return ResolvedCommand(None, 126)
        except NoPythonInterpreterError as exc:
            print(
                f"[cs_resolve_full_test_cmd] step=local-md hard-fail: {exc}",
                file=sys.stderr,
            )
            return ResolvedCommand(None, 127)
        return ResolvedCommand(norm, 0)

    # Step 3 — FALL BACK to the fast tier (not skip). Surface the caveat so
    # the caller reports fast-tier-only coverage rather than claiming
    # full-suite. Fast-tier diags are suppressed (mirrors bash `2>/dev/null`
    # on this call site) — fast-tier diags must not bleed into full-tier output.
    fast_result = cs_resolve_fast_test_cmd(repo_root, _quiet=True)
    if fast_result.exit_code == 0:
        print(
            "[cs_resolve_full_test_cmd] step=fast-fallback — no full_test_cmd configured; "
            "running the FAST tier. Coverage is fast-tier-only. To run the full suite, set "
            "full_test_cmd: in coordinator.local.md or $COORDINATOR_FULL_TEST_CMD.",
            file=sys.stderr,
        )
        return ResolvedCommand(fast_result.cmd, 3)
    if fast_result.exit_code != 2:
        # Exit 2 is the fast tier's "unconfigured" signal (handled at Step 4
        # below). ANY OTHER non-zero — canonically 127 (bare `python` token,
        # no interpreter) — is a HARD environment failure; propagate it so
        # the full tier fails loud rather than masking interp-missing as an
        # unconfigured skip (same swallow class the fast-tier fix kills).
        print(
            f"[cs_resolve_full_test_cmd] step=fast-fallback — fast resolver hard-failed "
            f"(rc={fast_result.exit_code}); propagating (NOT a skip).",
            file=sys.stderr,
        )
        return ResolvedCommand(None, fast_result.exit_code)

    # Step 4 — neither tier configured (fast_exit == 2)
    print(
        "[cs_resolve_full_test_cmd] step=skipped — no full or fast test command configured.",
        file=sys.stderr,
    )
    return ResolvedCommand(None, 2)
