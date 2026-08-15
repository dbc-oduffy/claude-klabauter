"""
coordinator_core.ops.coordinator_setup_state — setup/orientation milestone
receipt reader/writer at ~/.claude/coordinator-setup-state.yaml.

Purpose: durable, per-machine evidence that coordinator setup concluded and
(optionally) that the operator started/completed the guided orientation. This
is a RECEIPT in the sense of docs/wiki/plugin-identity-and-health-sentinels.md
— written by the actor whose action it witnesses, stale = signal not lie. It
is the cross-repo chaining contract: sibling (branch/leaf) repos read it to
confirm coordinator is bootstrapped before chaining their own
setup/orientation after it.

Idempotent and enduring: each milestone timestamp is set ONCE (first
occurrence wins) and never overwritten on re-run, so re-running /setup or
re-taking the tour does not rewrite history.

Spec backlink: docs/wiki/coordinator-setup-state-receipt.md (DoE-claude)
Port of: coordinator-setup-state.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Commands:
    record <milestone>   set <milestone>_at if unset (atomic, first-write-wins)
    check  <milestone>   exit 0 if recorded, 1 if not
    status                print the receipt (or note absence)
    auto-record-if-source-is-live
                          silent self-heal: on machines where coordinator-claude
                          is registered as propagation_mode="source_is_live"
                          (the install IS the source — the operator is the
                          author, never ran /coordinator:install), record
                          setup_concluded implicitly. No-op otherwise and on
                          every subsequent call (record is first-write-wins).
                          Always exits 0; emits nothing.

    milestone in { setup_concluded, orientation_started, orientation_completed }

Environment: CLAUDE_HOME (defaults to $HOME/.claude) selects the install root.

Negative-spec (faithfully reproduced bash-oracle quirks, NOT bugs to fix here):
    - `record` does NOT create missing intermediate directories under
      CLAUDE_HOME — if `<CLAUDE_HOME>/.claude` (the resolved state-file parent)
      does not exist, the bash oracle's `mktemp` call fails and the script
      exits 1 with a "mktemp failed (seed)" diagnostic. This port reproduces
      that failure mode exactly (raises via os.makedirs=False semantics: no
      makedirs call at all) rather than silently creating the directory —
      changing this would change first-run behavior on a machine whose
      ~/.claude doesn't exist yet, which is exactly the population this
      milestone-receipt is meant to observe.
    - Locking (bash used a portable `mkdir`-based advisory lock, best-effort,
      ~5s timeout) is NOT reproduced here — this port trades the bash
      lock-file dance for a single-process atomic temp-file + os.replace,
      which is safe for this port's own writes but does not exclude a
      concurrent SEPARATE process (bash-era or otherwise) also racing this
      same state file. Acceptable: this receipt is idempotent/first-write-wins
      by design (worst case under a race is a benign slightly-later
      timestamp), matching the bash oracle's own "best-effort" framing of its
      lock.
    - `status` treats a seeded-but-empty (header-only) file as NOT concluded
      (exit 1) — a crash between seed and first record must not be mistaken
      for a completed setup by a gating reader.
"""

from __future__ import annotations

# Generator-provenance declaration: cmd_record()/_seed_file_if_absent()
# write only to <CLAUDE_HOME>/.claude/coordinator-setup-state.yaml -- the
# operator's home directory, outside claude-klabauter's own tracked tree entirely.
GENERATES = []

import contextlib
import io
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import List, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - only on <3.11
    tomllib = None  # type: ignore[assignment]

from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.session.declared_writes import declare_write

_MILESTONES = ("setup_concluded", "orientation_started", "orientation_completed")

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="coordinator-setup-state",
    source_module="coordinator_core.ops.coordinator_setup_state",
    clauses=(
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="structured-file-key",
                    key=f"{milestone}_at",
                    path="<CLAUDE_HOME>/.claude/coordinator-setup-state.yaml",
                    reason=(
                        "cmd_record() merges this one `<milestone>_at` key into "
                        "the receipt YAML via _atomic_write(), preserving every "
                        "other key already present (first-write-wins, never a "
                        "whole-file overwrite of unrelated content) -- the "
                        "structured-file-key shape, not file-path."
                    ),
                )
                for milestone in _MILESTONES
            ),
        ),
    ),
)
"""This writer's declared write surface — one `structured-file-key` entry
per milestone, derived FROM `_MILESTONES` (never a restated literal list),
so a future edit to `_MILESTONES` alone cannot make this declaration
under- or over-report without its test going red. `kind="structured-file-key"`
rather than `"file-path"`: `_seed_file_if_absent`/`cmd_record` merge one key
into an existing YAML receipt other milestones' keys already live in,
rather than owning the whole file's content as an opaque blob. See spec
backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
chunk C3g."""

_USAGE = """Usage:
  coordinator-setup-state.sh record <milestone>
  coordinator-setup-state.sh check  <milestone>
  coordinator-setup-state.sh status

  milestone ∈ { setup_concluded | orientation_started | orientation_completed }
"""

_SEED_HEADER = """# ~/.claude/coordinator-setup-state.yaml — coordinator setup/orientation receipt.
# Operator-local, per-machine, gitignored, NEVER a publish target.
#
# Cross-repo contract: sibling (branch/leaf) repos read this file to confirm coordinator
# setup concluded before chaining their own setup/orientation after it. The presence of
# a non-empty `setup_concluded_at` is the chaining gate.
#
# Schema + reader idiom: docs/wiki/coordinator-setup-state-receipt.md
# Each *_at timestamp is set once (first occurrence wins) and never overwritten.
version: 1
"""

_KEY_RECORDED_RE_TMPL = r"^{key}:[ \t]+[^ \t#][^\n]*$"


def _is_milestone(value: str) -> bool:
    return value in _MILESTONES


def _home_dir(env: Optional[dict] = None) -> str:
    """The operator's home directory: HOME, then USERPROFILE, then
    `os.path.expanduser("~")`.

    HOME alone is a POSIX assumption. Native Windows (PowerShell, cmd) sets
    USERPROFILE and not HOME, so `env.get("HOME", "")` returned "" and every
    caller below built a RELATIVE path — `.claude/coordinator-setup-state.yaml`
    resolved against the process CWD. `record` then wrote a real receipt into
    whatever repo the operator happened to be standing in, reported that path
    truthfully, and left the actual `~/.claude` receipt untouched. Sibling repos
    gate their own setup chaining on that file, so the miss is silent and
    cross-repo. Never reintroduce a bare HOME lookup here.
    """
    env = env if env is not None else os.environ
    return env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")


def _claude_home(env: Optional[dict] = None) -> str:
    env = env if env is not None else os.environ
    return os.path.join(env.get("CLAUDE_HOME") or _home_dir(env), ".claude")


def _state_file(env: Optional[dict] = None) -> str:
    return os.path.join(_claude_home(env), "coordinator-setup-state.yaml")


def _machine_local_dir(env: Optional[dict] = None) -> str:
    """Resolve ``<settings-home>/machine-local`` from an injected ``env`` dict.

    Mirrors ``coordinator_core._settings_home.machine_local_dir()``'s
    precedence — COORDINATOR_SETTINGS_HOME when set, else
    ``.coordinator-claude-settings`` under CLAUDE_HOME or the home directory —
    rather than calling that helper directly; the helper reads
    ``os.environ`` internally, which would ignore this module's env-injection
    contract (every other resolver in this file takes ``env`` for test
    isolation) and silently read the real machine's settings-home instead of
    the caller-supplied one. Distinct on purpose from ``_claude_home`` above:
    the setup-state receipt lives at ``~/.claude`` (correct, unrelated to this
    function), but the machine-local registry TOML lives under settings-home
    -- conflating the two was the bug (previously this module resolved the
    registry at ``<claude_home>/machine-local``, the pre-migration legacy
    location, silently missing the real settings-home registry.local.toml).
    """
    env = env if env is not None else os.environ
    override = env.get("COORDINATOR_SETTINGS_HOME")
    if override:
        settings_home = override
    else:
        settings_home = os.path.join(
            env.get("CLAUDE_HOME") or _home_dir(env), ".coordinator-claude-settings"
        )
    return os.path.join(settings_home, "machine-local")


def _key_recorded(key: str, state_file: str) -> bool:
    """A value is "recorded" when the key exists with a non-empty, non-comment value."""
    if not re.match(r"^[a-z_]+$", key):
        return False
    if not os.path.isfile(state_file):
        return False
    pattern = re.compile(_KEY_RECORDED_RE_TMPL.format(key=re.escape(key)), re.MULTILINE)
    try:
        with open(state_file, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        print(f"skip: _key_recorded: with open(state_file, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return bool(pattern.search(content))


def _atomic_write(target: str, content: str) -> None:
    """mktemp-in-same-dir + rename, mirroring the bash oracle's `mktemp`/`mv` pair.

    Raises OSError if the parent directory does not exist (bash-oracle-faithful
    "mktemp failed" behavior — see module negative-spec).
    """
    parent = os.path.dirname(target)
    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(target) + ".", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
        # DR-276: declared AFTER the write lands, never before — the contract
        # is a report of what was ACTUALLY written, not of an intended
        # surface. This is the one real write site both `_seed_file_if_absent`
        # and `cmd_record` funnel through.
        declare_write(target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            print(f"skip: _atomic_write: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise


def _seed_file_if_absent(state_file: str) -> None:
    if os.path.isfile(state_file):
        return
    _atomic_write(state_file, _SEED_HEADER)


def cmd_record(milestone: str, env: Optional[dict] = None) -> int:
    if not _is_milestone(milestone):
        sys.stderr.write(_USAGE)
        return 2
    state_file = _state_file(env)
    key = f"{milestone}_at"

    try:
        _seed_file_if_absent(state_file)
    except OSError as exc:
        sys.stderr.write(f"coordinator-setup-state: mktemp failed (seed): {exc}\n")
        return 1

    if _key_recorded(key, state_file):
        print(f"{key} already recorded; leaving unchanged.")
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(state_file, encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        sys.stderr.write(f"coordinator-setup-state: mktemp failed (record): {exc}\n")
        return 1

    key_present_re = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if key_present_re.search(content):
        new_content = key_present_re.sub(f"{key}: {now}", content, count=1)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        new_content = content + f"{key}: {now}\n"

    try:
        _atomic_write(state_file, new_content)
    except OSError as exc:
        sys.stderr.write(f"coordinator-setup-state: mktemp failed (record): {exc}\n")
        return 1

    print(f"Recorded {key}: {now} in {state_file}")
    return 0


def cmd_check(milestone: str, env: Optional[dict] = None) -> int:
    if not _is_milestone(milestone):
        sys.stderr.write(_USAGE)
        return 2
    state_file = _state_file(env)
    return 0 if _key_recorded(f"{milestone}_at", state_file) else 1


def cmd_status(env: Optional[dict] = None) -> int:
    state_file = _state_file(env)
    if os.path.isfile(state_file):
        with open(state_file, encoding="utf-8") as fh:
            content = fh.read()
        sys.stdout.write(content)
        if re.search(r"^[a-z_]+_at:[ \t]+[^ \t#]", content, re.MULTILINE):
            return 0
        return 1
    print(f"No coordinator setup-state receipt at {state_file} (setup not concluded on this machine).")
    return 1


def _propagation_mode(registry_path: str) -> Optional[str]:
    """Parse one registry TOML for [plugin.mirrors.coordinator-claude]
    propagation_mode, mirroring the bash oracle's awk windowed block-match
    (does not cross-match a different plugin's table). Returns the declared
    mode string, or None when the file/table/key is absent, unreadable, or the
    value is empty — the tri-state lets the caller fall through to the tracked
    ``registry.toml`` per-key (``machine_resolver.registry_get`` precedence)
    instead of collapsing key-absent to "not source_is_live"."""
    if tomllib is not None:
        try:
            with open(registry_path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError):
            print(f"skip: _propagation_mode: with open(registry_path, \"rb\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
            return None
        mirrors = data.get("plugin", {}).get("mirrors", {})
        entry = mirrors.get("coordinator-claude", {})
        mode = entry.get("propagation_mode")
        return str(mode) if mode else None

    # Fallback: replicate the bash awk state-machine directly (in-block scan,
    # reset on any new `[...]` table header) if tomllib is unavailable.
    try:
        with open(registry_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        print(f"skip: _propagation_mode: with open(registry_path, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    in_block = False
    for line in lines:
        if line.startswith("[plugin.mirrors.coordinator-claude]"):
            in_block = True
            continue
        if line.startswith("["):
            in_block = False
            continue
        if in_block:
            m = re.match(r'^propagation_mode\s*=\s*"([^"]*)"', line)
            if m:
                return m.group(1) or None
    return None


def cmd_auto_record_if_source_is_live(env: Optional[dict] = None) -> int:
    env = env if env is not None else os.environ
    ml_dir = _machine_local_dir(env)
    # Per-key precedence: registry.local.toml's declared mode wins; the tracked
    # registry.toml fills the gap when the local file is absent or silent on
    # the key (previously `.local`-only — a mode declared only in the tracked
    # file was invisible).
    mode: Optional[str] = None
    for fname in ("registry.local.toml", "registry.toml"):
        registry = os.path.join(ml_dir, fname)
        if not os.path.isfile(registry):
            continue
        mode = _propagation_mode(registry)
        if mode is not None:
            break
    if mode != "source_is_live":
        return 0
    # Fire the same record path used by /coordinator:install. Suppress
    # stdout/stderr — this is a silent self-heal (mirrors the bash oracle's
    # `"$0" record setup_concluded >/dev/null 2>&1 || true`).
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cmd_record("setup_concluded", env)
    except Exception:
        print(f"skip: cmd_auto_record_if_source_is_live: with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_st failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    return 0


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else ""
    if not cmd:
        sys.stderr.write(_USAGE)
        return 2

    if cmd == "record":
        milestone = argv[1] if len(argv) > 1 else ""
        return cmd_record(milestone)
    if cmd == "check":
        milestone = argv[1] if len(argv) > 1 else ""
        return cmd_check(milestone)
    if cmd == "status":
        return cmd_status()
    if cmd == "auto-record-if-source-is-live":
        return cmd_auto_record_if_source_is_live()
    if cmd in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 2

    sys.stderr.write(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
