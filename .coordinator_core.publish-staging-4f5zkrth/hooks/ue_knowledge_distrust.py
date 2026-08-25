"""
coordinator_core.hooks.ue_knowledge_distrust -- SessionStart(startup|clear|compact)
UE-project detection + knowledge-distrust banner + per-project plugin-gating
auto-bootstrap (Port of: ue-knowledge-distrust.sh, DoE e91827a7, 2026-07-20,
W4b bash-to-python-migration cohort).

Disposition: naked-Python DIRECT PORT (recipe section 2.5:
X:/DoE-claude/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md).

Ported behavior, in order:
  1. Bounded ".uproject" search: cwd-relative, maxdepth 3 (matches the bash
     oracle's "find . -maxdepth 3 -name *.uproject -print -quit" -- NOT the
     "find /" root-anchor hazard the repo's install-time guard blocks). Silent
     (empty banner, no stderr) when no match -- this hook is conditional-fire
     and near-zero-cost on non-UE repos.
  2. Per-project plugin-gating auto-bootstrap: reads "<cwd>/.claude/settings.json"
     natively (no jq subprocess) and decides one of three outcomes:
       a. explicit enabledPlugins["holodeck-control@claude-unreal-holodeck"] == false
          -> SKIP with a warning (user deliberately opted out; do not overwrite).
       b. settings.json missing, unreadable/malformed, or the override key is
          not literally true -> write/merge the UE plugin override natively
          (see `_run_bootstrap`; C5 retired the bash `claude-ue-bootstrap.sh`
          subprocess spawn on this session-hot-path -- the write/merge logic
          it performed now lives directly in this module, no bash/jq/node
          dependency of any kind).
       c. override already true -> no action, no message (falls through both
          branches, mirroring the bash elif chain where no branch fires).
  3. UE PROJECT DETECTED banner -- byte-identical golden-diff target, emitted
     whenever a ".uproject" was found (unconditional on the bootstrap outcome).

INTENTIONAL DIVERGENCE from the bash oracle (flagged per recipe section 2.5,
"same divergence class as section 2.3"): the bash oracle's jq-ABSENT /
node-ABSENT branches (jq AND node both missing from PATH while settings.json
already exists -> hard failure, no merge possible) have NO equivalent here.
Python's stdlib json module has no external-tool dependency, so that
precondition can never occur in the port -- it is correctly and permanently
unreachable, not silently dropped.

Called IN-PROCESS by the thin DoE SessionStart stub
(coordinator/hooks/scripts/ue-knowledge-distrust.py), mirroring
coordinator_reminder.render_reminder / project_rag_detect.detect_banner's
direct-import shape -- no IPC round trip, no register_op().

Spec backlink: X:/DoE-claude/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md section 2.5
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple, Optional

_OVERRIDE_KEY = "holodeck-control@claude-unreal-holodeck"
_MAX_DEPTH = 3

# The full set of plugin keys claude-ue-bootstrap.sh wrote/merged to true.
# Mirrors that script's EXPECTED_KEYS array verbatim (C5 port target).
_BOOTSTRAP_KEYS = (
    "holodeck-control@claude-unreal-holodeck",
    "holodeck@claude-unreal-holodeck",
    "game-dev@claude-unreal-holodeck",
    "game-dev@coordinator-claude",
    "project-rag@project-rag",
    "project-rag-ue-addon@project-rag-ue-addon",
)


class DistrustResult(NamedTuple):
    """Return shape for run()."""

    banner: str  # UE PROJECT DETECTED heredoc text, or "" if no .uproject found
    stderr_lines: list[str]  # operator-facing warning/status lines (stderr)


def _find_uproject(start_dir: str, max_depth: int = _MAX_DEPTH) -> Optional[str]:
    """Bounded, cwd-relative search for the first *.uproject file.

    Mirrors "find . -maxdepth 3 -name *.uproject -print -quit": depth 0 is
    the starting dir itself (files directly inside it are depth 1), matches
    are allowed up to and including depth max_depth, and the FIRST match
    short-circuits the walk (mirrors -print -quit -- does not keep walking
    the whole tree looking for a second match). Directories are visited in
    sorted order for deterministic output; unreadable directories are
    silently skipped (mirrors the bash oracle's 2>/dev/null).
    """
    start = Path(start_dir)

    def _walk(dir_path: Path, depth: int) -> Optional[str]:
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError:
            return None

        subdirs: list[os.DirEntry] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                # Unreadable/vanished entry (permission, race) -- skip it,
                # mirrors the bash oracle's 2>/dev/null (see docstring).
                continue
            if not is_dir and entry.name.endswith(".uproject"):
                return str(Path(dir_path) / entry.name)
            if is_dir:
                subdirs.append(entry)

        if depth < max_depth:
            for sub in subdirs:
                found = _walk(Path(dir_path) / sub.name, depth + 1)
                if found is not None:
                    return found
        return None

    return _walk(start, 0)


def _read_override_value(settings_path: Path) -> tuple[bool, Optional[bool]]:
    """Read the UE-override key out of settings.json.

    Returns (exists, value):
      exists -- True iff settings_path is a regular file.
      value  -- True/False if the key resolves to a JSON boolean; None if the
                file is missing, unreadable, not valid JSON, not a JSON
                object, or the key/its parent object is absent. A malformed
                or absent key is deliberately treated the same as "not true"
                (mirrors "jq -e '... == true'" returning non-zero on any of
                those conditions, which the bash oracle's elif treats
                identically).
    """
    if not settings_path.is_file():
        return False, None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, None
    if not isinstance(data, dict):
        return True, None
    plugins = data.get("enabledPlugins")
    if not isinstance(plugins, dict):
        return True, None
    value = plugins.get(_OVERRIDE_KEY)
    if isinstance(value, bool):
        return True, value
    return True, None


def _run_bootstrap(plugin_root: str, cwd: str) -> tuple[bool, str]:
    """Port of: claude-ue-bootstrap.sh (DoE 4518ca1a, 2026-07-21)'s
    settings.json write/merge logic (C5: retires the bash
    `["bash", script, cwd]` subprocess spawn this session-hot-path hook
    used to make -- no bash, jq, or node dependency of any kind remains).

    `plugin_root` is kept in the signature for call-site compatibility with
    the pre-port shape (it previously located
    "<plugin_root>/bin/claude-ue-bootstrap.sh"); the write/merge logic now
    lives directly here, so it is unused.

    Mirrors the bash oracle's two paths:
      - No existing "<cwd>/.claude/settings.json": write it fresh with all
        `_BOOTSTRAP_KEYS` set true (oracle's here-doc fast path).
      - Existing settings.json: merge `_BOOTSTRAP_KEYS` into its
        "enabledPlugins" object, preserving every other key untouched
        (oracle's jq/node merge path -- `. * $new` / shallow key-assignment).
        A no-op (message only, no write) when every key is already true
        (oracle's `jq -e '...== true and ...'` short-circuit).

    Returns (ok, message); ok is False on any I/O failure (mkdir/read/write),
    in which case the caller fails open (silent) -- matches the bash oracle,
    which never checked the bootstrap script's own exit code from the
    session hook's perspective (only this function's own success/failure
    gates whether `message` is treated as a real status line).
    """
    del plugin_root  # unused post-port; kept for call-site compatibility
    project_dir = Path(cwd)
    claude_dir = project_dir / ".claude"
    settings_path = claude_dir / "settings.json"

    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"ERROR: could not create {claude_dir}: {exc}"

    if not settings_path.is_file():
        data = {"enabledPlugins": {key: True for key in _BOOTSTRAP_KEYS}}
        tmp_path = settings_path.with_name(f"{settings_path.name}.tmp.{os.getpid()}")
        try:
            tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
            os.replace(tmp_path, settings_path)
        except OSError as exc:
            return False, f"ERROR: could not write {settings_path}: {exc}"
        finally:
            # Review: code-reviewer — os.replace() moves tmp_path onto
            # settings_path on success (nothing left at tmp_path to unlink);
            # unlink(missing_ok=True) is a no-op then. On any exception after
            # write_text() succeeded (e.g. os.replace() failing), this clears
            # the orphaned .tmp.<pid> file instead of leaking it forever.
            tmp_path.unlink(missing_ok=True)
        return True, f"wrote UE override to {settings_path} (native python)"

    try:
        raw = settings_path.read_text(encoding="utf-8")
        existing = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"ERROR: could not read/parse {settings_path}: {exc}"
    if not isinstance(existing, dict):
        return False, f"ERROR: {settings_path} does not contain a JSON object at the top level"

    enabled = existing.get("enabledPlugins")
    if not isinstance(enabled, dict):
        enabled = {}

    if all(enabled.get(key) is True for key in _BOOTSTRAP_KEYS):
        return True, f"{settings_path} already carries UE override — no change"

    merged = dict(enabled)
    for key in _BOOTSTRAP_KEYS:
        merged[key] = True
    existing["enabledPlugins"] = merged

    tmp_path = settings_path.with_name(f"{settings_path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp_path, settings_path)
    except OSError as exc:
        return False, f"ERROR: could not write {settings_path}: {exc}"
    finally:
        # Review: code-reviewer — see fresh-write path above; clears an
        # orphaned .tmp.<pid> file left by a write_text()-succeeds/
        # os.replace()-fails split.
        tmp_path.unlink(missing_ok=True)
    return True, f"merged UE override into {settings_path} (native python)"


def run(cwd: str, plugin_root: str) -> DistrustResult:
    """Full ue-knowledge-distrust.sh port: detect, gate, bootstrap, banner.

    Args:
      cwd: the working directory to search (bash oracle used "$(pwd)").
      plugin_root: the DoE coordinator plugin root (bash oracle derived this
        from "${BASH_SOURCE[0]}/../.."; the DoE stub passes it explicitly --
        it owns bin/claude-ue-bootstrap.py, not makima).
    """
    # Review: code-reviewer -- Finding 4, 2026-07-24-codereview-sliceowns-zero-makima
    # sidecar (docstring above still said "claude-ue-bootstrap.sh"; the
    # DoE-side source script was renamed extensionless-to-.py by the
    # bash-kill campaign -- repointed to match, same as the user-facing
    # message below).
    stderr_lines: list[str] = []

    uproject = _find_uproject(cwd)
    if uproject is None:
        return DistrustResult(banner="", stderr_lines=stderr_lines)

    project_name = Path(uproject).stem  # basename ... .uproject

    settings_path = Path(cwd) / ".claude" / "settings.json"
    exists, value = _read_override_value(settings_path)

    if exists and value is False:
        stderr_lines.append(
            "UE override SKIPPED — %s explicitly disables UE plugins" % settings_path
        )
        stderr_lines.append(
            "To enable UE plugins in this project, run: <settings-home>/bin/claude-ue-bootstrap %s" % cwd
        )
    elif (not exists) or value is not True:
        _ok, bootstrap_output = _run_bootstrap(plugin_root, cwd)
        # The bash oracle ran `claude-ue-bootstrap.sh "$(pwd)" >&2`, whose own
        # stdout+stderr landed on the parent's stderr ahead of the "override
        # written" message below (C5 replaced that subprocess spawn with the
        # native `_run_bootstrap` above, but preserves this same ordering and
        # surfacing -- dropping it here would silently swallow the write/merge
        # status line, e.g. "wrote UE override to ... (native python)").
        if bootstrap_output:
            for line in bootstrap_output.splitlines():
                stderr_lines.append(line)
        stderr_lines.append(
            "UE override written — close and re-open Claude Code to load UE "
            "plugins (current session uses lean defaults)"
        )
    # else: override already true -- no action, no message (mirrors the bash
    # elif chain falling through with no branch firing).

    banner = (
        "UE PROJECT DETECTED (%s): LLM training data about Unreal "
        "Engine is broadly untrustworthy. Function names, parameter signatures, "
        "class hierarchies, default behaviors, deprecation status — any of it "
        "may be wrong, stale, or hallucinated. You have 333K+ indexed doc "
        "chunks and 73K verified API declarations available via MCP. Treat "
        "MCP tools as ground truth and training knowledge as unverified "
        "hypothesis. Use quick_ue_lookup before asserting any UE API usage.\n"
    ) % project_name

    return DistrustResult(banner=banner, stderr_lines=stderr_lines)
