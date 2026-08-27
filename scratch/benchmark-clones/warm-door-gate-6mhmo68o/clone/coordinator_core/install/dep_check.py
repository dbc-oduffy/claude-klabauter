"""coordinator_core.install.dep_check — Python-native reimplementation of
coordinator/scripts/lib/dep_check.sh's Phase 0 dependency-chain gate: the
agent-mode consent prompt, functional dep probing, and the crash-safe
visited-set cycle-detection state machine consumed by coordinator's
install-chain setup.sh.

Port source: coordinator/scripts/lib/dep_check.sh [DoE-claude repo] — the
`.sh` file is RETIRED and no longer exists in DoE-claude (all of
`coordinator/scripts/lib/` is gone repo-wide). Historical context: it was
last a thin bash veneer subprocess-calling into this module's function
bodies, and by 2026-07-17 DoE-claude's own coordinator/scripts/setup.sh no
longer sourced it at all (it had become a polyglot trampoline into
coordinator_core.ops.setup_chain_walker.main, which ports
manifest-reading/dep-probing natively) — it survived only as a byte-parity
template source for sibling-repo setup.sh copies. That template rationale
no longer applies now that the file itself is gone: this module is the sole
implementation for every consumer, in-repo and sibling-repo vendoring
alike.

Spec backlink: docs/plans/2026-06-15-coordinator-install-chain-application-phase-b.md §7 C3
Spec backlink: coordinator/docs/wiki/agent-install-contract.md §Dual-mode script UX
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

CLI contract (invoked by the DoE bash veneer as
`python -m coordinator_core.install.dep_check <subcommand> [args...]`):

  phase-zero-should-run                    -> exit 0 (run) / 1 (skip)
  run-mode-prompt                          -> exit 0 (proceed) / 92 (agent-mode)
  dep-probe <dep-id> [manifest-path]       -> stdout: present|missing|present-but-broken; exit 0
  dep-probe-all [manifest-path]            -> stdout: NDJSON, one line per dep; exit 0
  visited-set-crash-cleanup                -> exit 0
  visited-set-init <session-id>            -> exit 0 / 1
  visited-set-check <session-id> <dep-id>  -> exit 0 (visited) / 1 (not visited)
  visited-set-append <session-id> <dep-id> -> exit 0 / 1
  consent-gate [manifest-path] [banner-path] -> exit 0/90/91/93 per §3.3

Reserved CRASH_RC (3): any subcommand that raises an exception NOT already
caught by its own business logic returns this reserved code instead of
letting a bare Python traceback produce an ambiguous exit status that could
collide with a business code (e.g. phase-zero-should-run's own legitimate
"skip" return is 1 — an uncaught exception must NOT also produce 1). The
bash veneer treats any rc it does not recognise as a subcommand's own
documented business code as a transport failure. See dep_check.sh's own
header for the full transport-failure degrade posture per subcommand.

Negative-spec — deliberate scope narrowing vs. the bash original:
  `_co_file_mtime_s`'s BSD-vs-GNU `stat`/`date -r` portability dance is NOT
  ported here — it exists purely to work around bash's lack of a portable
  mtime primitive. Python's `Path.stat().st_mtime` is portable across every
  platform this module runs on, so the whole probe ladder collapses to a
  single stdlib call; no behavioural loss (see `visited_set_crash_cleanup`/
  `visited_set_init`, which use it in-line rather than as a separate
  exported helper). The bash file RETAINS `_co_file_mtime_s` as a
  plain-bash helper for any OTHER caller that sources it directly for that
  primitive alone — it is not routed through this module (see dep_check.sh
  header comment).
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.install import manifest_reader, resolution_journal
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.ops.setup_chain_walker import command_succeeds_native
from coordinator_core.win_portability import no_console_creationflags

# Generator-provenance declaration (generator_provenance.py). Writes only
# chain-walk-<session>.json visited-set state and lockfiles under
# _default_co_dir() = $HOME/.claude/coordinator-claude -- outside the claude-klabauter
# tracked tree, session-runtime dependency-chain state only.
GENERATES = []

_VISITED_SET_WRITE_CLAUSE_INDEX = 0
_VISITED_SET_DELETE_CLAUSE_INDEX = 1
"""Clause indices into `WRITE_SURFACE.clauses` below -- named rather than
restated as bare `0`/`1` at each `record_resolution` call site
(`visited_set_init` writes clause 0's session file and, via its own
stale-file sweep, also contributes to clause 1's deletions; clause 1 is
otherwise `visited_set_crash_cleanup`'s)."""

_PROBE_TIMEOUT_SECS = 10.0

CRASH_RC = 3  # reserved — see module docstring.

_VISITED_SET_DIR_TEMPLATE = "<settings_home>/coordinator-claude"
"""Mirrors `_default_co_dir()`'s `$HOME/.claude/coordinator-claude` shape,
spelled as the settings-home template placeholder rather than this
machine's resolved `$HOME` — the WRITE_SURFACE declaration below derives
from this constant so the writer and the declaration read one spelling."""

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="dep-check",
    source_module="coordinator_core.install.dep_check",
    clauses=(
        # visited_set_init / visited_set_append: creates and rewrites a
        # per-session chain-walk visited-set scratch file. SHAPED — the
        # filename is derived from a caller-supplied session_id, not
        # enumerable in source.
        ShapedClause(
            discovered_by="visited_set_init",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"{_VISITED_SET_DIR_TEMPLATE}/chain-walk-<session-id>.json",
                reason="visited_set_init/visited_set_append: writes the cycle-detection visited-set state file for one dep-chain-walk session",
            ),
        ),
        # visited_set_crash_cleanup / visited_set_init's own stale-file
        # reaping: unlinks aged-out visited-set files from a prior crashed
        # or completed session. A delete, not a write.
        ShapedClause(
            discovered_by="visited_set_crash_cleanup",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"{_VISITED_SET_DIR_TEMPLATE}/chain-walk-<session-id>.json",
                effect="delete",
                reason="visited_set_crash_cleanup: TTL-reaps stale chain-walk visited-set scratch files (>5min crash-recovery / >1h stale-cleanup)",
            ),
            effect="delete",
        ),
    ),
)

_MANIFEST_REL_PATH = "docs/install/AGENT.md"

# READ_ONLY_FLAG_ALLOWLIST — mirrors _co_phase_zero_should_run's canonical
# allowlist (dep_check.sh lines 59-66). Kept as a plain tuple, not a comment
# block, since Python callers grep this module's source directly.
_READ_ONLY_FLAGS = (
    "HELP_FLAG",
    "VERSION_FLAG",
    "PHASE_LIST",
    "LAST_STATUS",
    "I_AM_AGENT",
    "CHECK_FLAG",
)


def _as_bool(value) -> bool:
    """Mirrors bash's `[[ "$_flag" == true ]]` string-compare semantics —
    accepts a real bool or a case-sensitive-to-bash "true" string; anything
    else (including "True"/"1"/absent) is false, matching the oracle's
    string-literal comparison exactly (bash never treated "1" as truthy
    here either)."""
    if isinstance(value, bool):
        return value
    return value == "true"


# ---------------------------------------------------------------------------
# _co_phase_zero_should_run
# ---------------------------------------------------------------------------

def phase_zero_should_run(flags: Dict[str, object]) -> bool:
    """Returns True iff Phase 0 should run (no read-only flag set). Mirrors
    _co_phase_zero_should_run's READ_ONLY_FLAG_ALLOWLIST check."""
    return not any(_as_bool(flags.get(key, False)) for key in _READ_ONLY_FLAGS)


# ---------------------------------------------------------------------------
# _co_run_mode_prompt
# ---------------------------------------------------------------------------

def run_mode_prompt(
    flags: Dict[str, object],
    *,
    stderr=None,
    read_reply: Optional[Callable[[], str]] = None,
    stdin_is_tty: Optional[bool] = None,
) -> int:
    """Implements step (a) of the dual-mode UX PM directive. Returns 0
    (proceed) or 92 (agent-mode detected — caller must exit 92, mirroring
    the bash original's inline `exit 92`). Mirrors _co_run_mode_prompt.
    """
    stream = stderr if stderr is not None else sys.stderr
    run_mode = flags.get("COORDINATOR_RUN_MODE", "")
    i_am_agent = _as_bool(flags.get("I_AM_AGENT", False))
    i_am_human = _as_bool(flags.get("I_AM_HUMAN", False))
    non_interactive = _as_bool(flags.get("NON_INTERACTIVE", "true"))

    if i_am_agent or run_mode == "agent":
        print(f"AGENT_MANIFEST_PATH={_MANIFEST_REL_PATH}", file=stream)
        print("[setup] Agent-direct invocation detected. Use /coordinator:setup instead.", file=stream)
        print(f"[setup] Agent install guide: {_MANIFEST_REL_PATH}", file=stream)
        return 92

    if i_am_human or run_mode == "human":
        return 0

    tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if non_interactive or not tty:
        return 0

    print(
        "[setup] Are you running this script as an autonomous agent rather than via /coordinator:setup? [y/N] ",
        end="",
        file=stream,
    )
    stream.flush()
    reply = read_reply() if read_reply is not None else _safe_input()
    if reply in ("y", "Y"):
        print("", file=stream)
        print(f"AGENT_MANIFEST_PATH={_MANIFEST_REL_PATH}", file=stream)
        print("[setup] Agent-direct invocation confirmed. Use /coordinator:setup instead.", file=stream)
        print(f"[setup] Agent install guide: {_MANIFEST_REL_PATH}", file=stream)
        return 92
    return 0


def _safe_input() -> str:
    try:
        return input()
    except EOFError:
        return ""


# ---------------------------------------------------------------------------
# _co_dep_probe / _co_dep_probe_all
# ---------------------------------------------------------------------------

def _resolve_sibling_root(repo_root: Optional[str]) -> str:
    root = repo_root or os.environ.get("REPO_ROOT")
    if not root:
        raise ValueError("dep_check: repo_root not supplied and $REPO_ROOT is unset")
    return str(Path(root).resolve().parent)


def _nested_layout_fallback(sibling_root: str, sibling_dir: str) -> Optional[str]:
    """Mirrors the bash "-claude"-suffix-strip nested-layout fallback (F1
    fix comment in the bash oracle) — returns the fallback path if it
    exists on disk, else None."""
    if not sibling_dir.endswith("-claude"):
        return None
    bare_name = sibling_dir[: -len("-claude")]
    if not bare_name:
        return None
    candidate = os.path.join(sibling_root, bare_name)
    return candidate if os.path.isdir(candidate) else None


def _probe_python_import(python: str, expr: str) -> bool:
    """Deliberate isolation boundary, not a candidate for an in-process
    import — a failed or heavy dependency import must not land in this
    process's own ``sys.modules``/import-state; the probe's whole value is
    that a broken candidate module cannot corrupt the parent interpreter.
    See ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    if not expr:
        return False
    try:
        result = subprocess.run(
            [python, "-c", expr],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _probe_shell_command(cmd: str) -> bool:
    """Evaluates a manifest `command_succeeds` probe's `cmd` string WITHOUT
    spawning `bash`/`sh` — delegates to the shell-subset evaluator
    `coordinator_core.ops.setup_chain_walker.command_succeeds_native`
    (C11 de-bash cutover; same manifest probe kind, same narrowed-grammar
    call: a `||`-separated fallback chain of shlex-tokenized argv commands,
    no shell metacharacter interpretation). Formerly `bash -c "$_cmd"`;
    reimplemented native (2026-07-21 pure-Python-shop cutover) rather than
    hand-duplicating a second shell-subset evaluator here."""
    if not cmd:
        return False
    return command_succeeds_native(cmd, int(_PROBE_TIMEOUT_SECS))


def dep_probe(
    dep_id: str,
    manifest_path: Optional[str] = None,
    repo_root: Optional[str] = None,
    *,
    stderr=None,
) -> str:
    """Returns 'present' | 'missing' | 'present-but-broken'. Mirrors
    _co_dep_probe, including its fail-safe posture: any internal error
    (unreadable manifest, no python, unresolvable repo_root) is reported to
    stderr AND degrades the probe result to the conservative 'missing' —
    never raises, matching the bash original's `echo "missing"; return 0`
    pattern on every internal-failure branch."""
    stream = stderr if stderr is not None else sys.stderr
    try:
        lines = manifest_reader.manifest_read_ndjson(manifest_path=manifest_path, repo_root=repo_root)
    except (manifest_reader.ManifestCorruptError, ValueError) as exc:
        print(f"ERROR: dep_probe: manifest unreadable or corrupt (dep '{dep_id}'): {exc}", file=stream)
        return "missing"

    dep_json = None
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"WARNING: dep_probe: skipping malformed manifest line: {exc}", file=stream)
            continue
        if obj.get("id") == dep_id:
            dep_json = obj
            break

    if dep_json is None:
        return "missing"

    try:
        sibling_root = _resolve_sibling_root(repo_root)
    except ValueError as exc:
        print(f"ERROR: dep_probe: {exc}", file=stream)
        return "missing"

    sibling_dir = dep_json.get("sibling_dir_name", "")
    probe_kind = dep_json.get("functional_probe_kind", "")
    sibling_path = os.path.join(sibling_root, sibling_dir)

    if not os.path.isdir(sibling_path):
        fallback = _nested_layout_fallback(sibling_root, sibling_dir)
        if fallback is None:
            return "missing"
        sibling_path = fallback

    probe_args = dep_json.get("functional_probe_args", {}) or {}

    if probe_kind == "sibling_dir_exists":
        return "present"

    if probe_kind == "file_exists":
        rel = probe_args.get("path", "")
        return "present" if os.path.isfile(os.path.join(sibling_path, rel)) else "present-but-broken"

    if probe_kind == "file_exists_any":
        paths = probe_args.get("paths", [])
        present = any(os.path.isfile(os.path.join(sibling_path, rel)) for rel in paths)
        return "present" if present else "present-but-broken"

    if probe_kind == "python_import":
        expr = probe_args.get("expr", "")
        try:
            python = manifest_reader.find_python()
        except manifest_reader.NoPythonInterpreterError:
            return "present-but-broken"
        return "present" if _probe_python_import(python, expr) else "present-but-broken"

    if probe_kind == "command_succeeds":
        cmd = probe_args.get("cmd", "")
        return "present" if _probe_shell_command(cmd) else "present-but-broken"

    print(
        f"WARNING: unknown probe kind '{probe_kind}' for dep '{dep_id}'; treating as present-but-broken",
        file=stream,
    )
    return "present-but-broken"


def dep_probe_all(
    manifest_path: Optional[str] = None,
    repo_root: Optional[str] = None,
    *,
    stderr=None,
) -> List[dict]:
    """Probes every direct_dep and returns one record per dep: {id,
    severity, status, sibling_path, hint}. Mirrors _co_dep_probe_all
    (including its re-parse-per-dep redundancy — faithful to the bash
    oracle's own shape, not a new inefficiency this port introduces)."""
    stream = stderr if stderr is not None else sys.stderr
    try:
        lines = manifest_reader.manifest_read_ndjson(manifest_path=manifest_path, repo_root=repo_root)
    except (manifest_reader.ManifestCorruptError, ValueError) as exc:
        print(f"ERROR: dep_probe_all: manifest unreadable or corrupt: {exc}", file=stream)
        return []

    try:
        sibling_root = _resolve_sibling_root(repo_root)
    except ValueError as exc:
        print(f"ERROR: dep_probe_all: {exc}", file=stream)
        return []

    records: List[dict] = []
    for line in lines:
        try:
            dep = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"WARNING: dep_probe_all: skipping malformed manifest line: {exc}", file=stream)
            continue

        dep_id = dep.get("id", "")
        severity = dep.get("severity", "")
        sibling_dir = dep.get("sibling_dir_name", "")
        upstream_url = dep.get("upstream_url", "")

        sibling_path = os.path.join(sibling_root, sibling_dir)
        if not os.path.isdir(sibling_path):
            fallback = _nested_layout_fallback(sibling_root, sibling_dir)
            if fallback is not None:
                sibling_path = fallback

        status = dep_probe(dep_id, manifest_path=manifest_path, repo_root=repo_root, stderr=stream)

        if status == "missing":
            hint = f"Clone from: {upstream_url}"
        elif status == "present-but-broken":
            hint = f"Sibling present but functional probe failed. Re-install or check: {sibling_path}"
        else:
            hint = ""

        records.append(
            {
                "id": dep_id,
                "severity": severity,
                "status": status,
                "sibling_path": sibling_path,
                "hint": hint,
            }
        )
    return records


# ---------------------------------------------------------------------------
# Visited-set state machine
# ---------------------------------------------------------------------------

def _default_co_dir() -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".claude" / "coordinator-claude"


def visited_set_crash_cleanup(co_dir: Optional[Path] = None, *, now: Optional[float] = None) -> None:
    """Phase 0 entry crash-recovery reaper — 5-minute TTL. Mirrors
    _co_visited_set_crash_cleanup."""
    directory = Path(co_dir) if co_dir is not None else _default_co_dir()
    if not directory.is_dir():
        # No visited-set dir means this reaper never examined anything --
        # "we never got there" (never installed, or nothing has run yet),
        # distinct from "we looked and there was nothing stale". No
        # journal row for either clause.
        return
    now_ts = now if now is not None else time.time()
    stale_threshold = 300
    deleted: List[Path] = []
    for candidate in directory.glob("chain-walk-*.json"):
        if not candidate.is_file():
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            # Vanished between the is_file() check and stat() (another
            # session's own reaper/cleanup won the race) -- nothing left
            # to age out.
            continue
        if (now_ts - mtime) > stale_threshold:
            try:
                candidate.unlink()
                deleted.append(candidate)
            except OSError:
                # Already removed by a concurrent reaper, or a permission
                # blip -- this is best-effort TTL GC, not correctness-load
                # bearing (a missed reap just leaves one stale scratch file).
                pass

    # The dir existed and was examined, so this is a real resolution even
    # when `deleted` is empty ("we looked, nothing was stale") -- not an
    # absent row.
    resolution_journal.record_resolution(
        "dep-check",
        _VISITED_SET_DELETE_CLAUSE_INDEX,
        [WriteSurfaceEntry(kind="file-path", path=str(p), effect="delete") for p in deleted],
    )


def visited_set_init(session_id: str, co_dir: Optional[Path] = None, *, now: Optional[float] = None) -> Path:
    """Creates the visited-set file; stale-cleans files >1h old. Mirrors
    _co_visited_set_init. Returns the created file's path."""
    directory = Path(co_dir) if co_dir is not None else _default_co_dir()
    directory.mkdir(parents=True, exist_ok=True)

    now_ts = now if now is not None else time.time()
    deleted: List[Path] = []
    for stale_file in directory.glob("chain-walk-*.json"):
        if not stale_file.is_file():
            continue
        try:
            mtime = stale_file.stat().st_mtime
        except OSError:
            # Same race as visited_set_crash_cleanup: gone before stat()
            # could run, so there's nothing to age out.
            continue
        if (now_ts - mtime) > 3600:
            try:
                stale_file.unlink()
                deleted.append(stale_file)
            except OSError:
                # Best-effort TTL GC -- a concurrent reaper already won,
                # or a permission blip; not correctness-bearing.
                pass

    # This function's own stale-sweep shares clause 1 (delete) with
    # visited_set_crash_cleanup -- see WRITE_SURFACE's discovered_by note.
    # The directory was always examined (mkdir above ensures it exists),
    # so an empty `deleted` is still a real "looked, nothing stale" fact.
    resolution_journal.record_resolution(
        "dep-check",
        _VISITED_SET_DELETE_CLAUSE_INDEX,
        [WriteSurfaceEntry(kind="file-path", path=str(p), effect="delete") for p in deleted],
    )

    visited_file = directory / f"chain-walk-{session_id}.json"
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    data = {"session_id": session_id, "started_at": started_at, "visited": []}
    with open(visited_file, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh)
    resolution_journal.record_resolution(
        "dep-check",
        _VISITED_SET_WRITE_CLAUSE_INDEX,
        [WriteSurfaceEntry(kind="file-path", path=str(visited_file))],
    )
    return visited_file


def visited_set_check(session_id: str, dep_id: str, co_dir: Optional[Path] = None) -> bool:
    """Returns True if dep_id is already in the visited set. Mirrors
    _co_visited_set_check's 0=visited/1=not-visited bash contract — this
    Python function returns bool; the CLI maps True->exit 0, False->exit 1."""
    directory = Path(co_dir) if co_dir is not None else _default_co_dir()
    visited_file = directory / f"chain-walk-{session_id}.json"
    if not visited_file.is_file():
        return False
    try:
        with open(visited_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    return dep_id in data.get("visited", [])


def visited_set_append(session_id: str, dep_id: str, co_dir: Optional[Path] = None) -> None:
    """Atomic read-modify-write append (temp file + os.replace — mirrors
    the bash original's tempfile.NamedTemporaryFile + os.replace pattern,
    itself already atomic; nothing to harden further since the visited-set
    file is plain JSON state, never executable — A5 permission-preservation
    does not apply here). Raises FileNotFoundError if the visited-set file
    does not exist, mirroring the bash original's ERROR-to-stderr + exit 1
    on missing file (the CLI wrapper maps this to exit 1)."""
    directory = Path(co_dir) if co_dir is not None else _default_co_dir()
    visited_file = directory / f"chain-walk-{session_id}.json"
    if not visited_file.is_file():
        raise FileNotFoundError(f"visited-set file not found: {visited_file}")

    with open(visited_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    visited = data.get("visited", [])
    if dep_id not in visited:
        visited.append(dep_id)
        data["visited"] = visited

    fd, tmp_path = tempfile.mkstemp(dir=str(visited_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            json.dump(data, tmp)
        os.replace(tmp_path, visited_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            # Best-effort tmp-file cleanup during error unwind -- the
            # original exception is re-raised below regardless, so a
            # failure to remove the orphaned tempfile must not mask it.
            pass
        raise


# ---------------------------------------------------------------------------
# _co_consent_gate
# ---------------------------------------------------------------------------

_FALLBACK_BANNER = (
    "================================================================\n"
    "WARNING — UNSAFE INSTALL REQUESTED\n"
    "Missing hard deps; see scripts/lib/dep_consent_banner.txt\n"
    "================================================================"
)


def _load_banner(banner_path: Optional[str]) -> str:
    """Loads the consent banner body. Unlike the bash original (which
    resolves the banner path relative to its OWN file location — it lives
    DoE-side, co-located with dep_consent_banner.txt), this claude-klabauter module
    has no DoE-tree-relative default: banner_path is REQUIRED from the
    caller (the DoE bash veneer resolves + passes its own sibling path).
    Falls back to the same inline minimal banner text the bash original
    uses when the file is absent."""
    if not banner_path:
        return _FALLBACK_BANNER
    path = Path(banner_path)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return _FALLBACK_BANNER


def consent_gate(
    flags: Dict[str, object],
    *,
    manifest_path: Optional[str] = None,
    repo_root: Optional[str] = None,
    banner_path: Optional[str] = None,
    stderr=None,
    read_reply: Optional[Callable[[], str]] = None,
    stdin_is_tty: Optional[bool] = None,
    stderr_is_tty: Optional[bool] = None,
) -> int:
    """Emits the verbatim consent banner with missing-dep substitution, then
    enforces the §3.3 confirmation protocol. Mirrors _co_consent_gate.

    Returns: 0 (proceed) / 90 / 91 / 93 — the canonical business codes. Exit
    code 92 (agent-mode) is NOT reachable from this function — that is
    run_mode_prompt's contract, called separately by the caller."""
    stream = stderr if stderr is not None else sys.stderr
    skip_dep_check = _as_bool(flags.get("SKIP_DEP_CHECK", False))
    accept_risk = _as_bool(flags.get("ACCEPT_MISSING_DEPS_RISK", False))
    non_interactive = _as_bool(flags.get("NON_INTERACTIVE", "true"))

    if skip_dep_check and not accept_risk:
        print(
            "ERROR: --skip-dep-check requires --accept-missing-deps-risk (exit 93: override-flag-pair-incomplete)",
            file=stream,
        )
        print("  Pass both flags together: --skip-dep-check --accept-missing-deps-risk", file=stream)
        return 93
    if accept_risk and not skip_dep_check:
        print(
            "ERROR: --accept-missing-deps-risk requires --skip-dep-check (exit 93: override-flag-pair-incomplete)",
            file=stream,
        )
        print("  Pass both flags together: --skip-dep-check --accept-missing-deps-risk", file=stream)
        return 93

    missing_hard: List[str] = []
    for record in dep_probe_all(manifest_path=manifest_path, repo_root=repo_root, stderr=stream):
        if record["severity"] == "hard" and record["status"] in ("missing", "present-but-broken"):
            missing_hard.append(f"{record['id']} [{record['status']}]")

    if not missing_hard:
        return 0

    banner_body = _load_banner(banner_path)
    dep_list = "\n".join(f"  {entry}" for entry in missing_hard)
    banner_rendered = banner_body.replace("<!-- DEPS_LIST_PLACEHOLDER -->", dep_list)

    stderr_tty = stderr_is_tty if stderr_is_tty is not None else sys.stderr.isatty()

    if skip_dep_check and accept_risk:
        print(banner_rendered, file=stream)
        if not (non_interactive or not stderr_tty):
            print(
                "[setup] Override flags accepted: --skip-dep-check --accept-missing-deps-risk. Proceeding.",
                file=stream,
            )
        return 0

    print(banner_rendered, file=stream)

    stdin_tty = stdin_is_tty if stdin_is_tty is not None else sys.stdin.isatty()
    if non_interactive or not stdin_tty:
        print("", file=stream)
        print("ERROR: Hard dependencies missing in non-interactive context (exit 90).", file=stream)
        print("  Run interactively, or pass --skip-dep-check --accept-missing-deps-risk to override.", file=stream)
        return 90

    print("", file=stream)
    print("Do you accept the risk of proceeding without the required dependencies? [y/N] ", end="", file=stream)
    stream.flush()
    reply1 = read_reply() if read_reply is not None else _safe_input()
    if reply1 not in ("y", "Y"):
        print("[setup] Aborted: first confirmation declined (exit 91).", file=stream)
        return 91

    print(
        "Specifically, do you accept that running without required deps may produce contract-violation outcomes? [y/N] ",
        end="",
        file=stream,
    )
    stream.flush()
    reply2 = read_reply() if read_reply is not None else _safe_input()
    if reply2 not in ("y", "Y"):
        print("[setup] Aborted: second confirmation declined (exit 91).", file=stream)
        return 91

    print("[setup] Both confirmations received. Proceeding with unsafe install.", file=stream)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _flags_from_environ() -> Dict[str, object]:
    return dict(os.environ)


def _dispatch(argv: List[str]) -> int:
    if not argv:
        print("Usage: python -m coordinator_core.install.dep_check <subcommand> [args...]", file=sys.stderr)
        return 2

    subcmd, rest = argv[0], argv[1:]
    flags = _flags_from_environ()

    if subcmd == "phase-zero-should-run":
        return 0 if phase_zero_should_run(flags) else 1

    if subcmd == "run-mode-prompt":
        return run_mode_prompt(flags)

    if subcmd == "dep-probe":
        if not rest:
            print("Usage: dep-probe <dep-id> [manifest-path]", file=sys.stderr)
            return 2
        dep_id = rest[0]
        # Review: code-reviewer — bash's "${2:-}" idiom produces an empty-string
        # positional arg (not an omitted one) for the "no manifest path" case; treat
        # empty the same as omitted so the layout-aware default resolution still fires.
        manifest_path = rest[1] if len(rest) > 1 and rest[1] else None
        print(dep_probe(dep_id, manifest_path=manifest_path))
        return 0

    if subcmd == "dep-probe-all":
        manifest_path = rest[0] if rest and rest[0] else None
        for record in dep_probe_all(manifest_path=manifest_path):
            print(json.dumps(record))
        return 0

    if subcmd == "visited-set-crash-cleanup":
        visited_set_crash_cleanup()
        return 0

    if subcmd == "visited-set-init":
        if not rest:
            print("Usage: visited-set-init <session-id>", file=sys.stderr)
            return 2
        try:
            visited_set_init(rest[0])
        except OSError as exc:
            print(f"ERROR: visited-set-init failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if subcmd == "visited-set-check":
        if len(rest) < 2:
            print("Usage: visited-set-check <session-id> <dep-id>", file=sys.stderr)
            return 2
        return 0 if visited_set_check(rest[0], rest[1]) else 1

    if subcmd == "visited-set-append":
        if len(rest) < 2:
            print("Usage: visited-set-append <session-id> <dep-id>", file=sys.stderr)
            return 2
        try:
            visited_set_append(rest[0], rest[1])
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0

    if subcmd == "consent-gate":
        manifest_path = rest[0] if rest and rest[0] else None
        banner_path = rest[1] if len(rest) > 1 and rest[1] else None
        return consent_gate(flags, manifest_path=manifest_path, banner_path=banner_path)

    print(f"ERROR: unknown subcommand: {subcmd}", file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Wraps dispatch in a broad except so an unexpected
    internal defect never produces an exit code that collides with a
    subcommand's own documented business codes (see CRASH_RC docstring)."""
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        return _dispatch(args)
    except Exception:  # noqa: BLE001 — intentional: reserved crash code, see module docstring.
        traceback.print_exc(file=sys.stderr)
        return CRASH_RC


if __name__ == "__main__":
    sys.exit(main())
