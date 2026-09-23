"""coordinator_core.hooks.derive_global_doctrine_live_copy — PostToolUse
(Write|Edit|MultiEdit) AND SessionStart op: re-derive the live global
CLAUDE.md copy, the live `~/.claude/rules/*.md` mirror, AND the in-plugin
published copy under `coordinator/templates/global-doctrine/`, whenever
their TRACKED sources (in the coordinator-claude doctrine-plane repo) may
have changed.

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/derive-global-doctrine-live-copy.py`
(itself coordinator-claude-repo-resident doctrine content, reached from this
engine the same command/native-door way every sibling hook in this row is).
Shape, per the W4-C1 verdict: command/native-door, `hooks.<name>` op,
payload-dict-in/response-out -- stdin JSON read and `_message_envelope.emit()`
are replaced with `register_op`'s contract and this package's own
`allow_advisory`/`no_advisory` builders. The CHANNEL_STOP-per-drifted-target
shape (up to four independent stderr writes, blank-line-separated, in one
invocation) folds into ONE advisory `Message` per call, its prose lines
joined with `"; "` -- there is no multi-emit channel in the `hooks.<name>`
contract, and unlike the source's own terminal-rendered stderr stream this
engine surfaces at most one advisory per payload.

Repo-root resolution -- REPLACED, not preserved verbatim. The source resolved
`_repo_root()` from `Path(__file__).resolve().parents[3]`: correct there
because that module lived four levels under the coordinator-claude repo root
(`coordinator/hooks/scripts/<file>.py`). This module now lives inside the
ENGINE (a different checkout entirely), so that walk has nothing to anchor
against. Replaced with `_resolve_doctrine_repo_root()` below, an adaptation
of `cater_subagent_start._resolve_role_append_snippet_path`'s /
`provision_report.resolve_plugin_root()`'s own multi-rung plugin-root probe
(CLAUDE_PLUGIN_ROOT env, `<claude_config_dir>/plugins/coordinator-claude` in
both known shapes, `.doe-root` pointer) -- ONE LEVEL UP from those probes'
own target (they resolve the coordinator-claude CONTENT root, i.e. the
`coordinator/` subdir; this hook needs the REPO ROOT one level above it,
where `global-doctrine/`, `coordinator/templates/global-doctrine/`, and the
`.coordinator-dev-repo` OSS-clobber sentinel actually live). Every rung
probes for this resolver's OWN artifact (`.coordinator-dev-repo`), never bare
directory existence, per that same precedent's own stated rationale. A
resolution miss at every rung means `_is_dev_repo()` returns False and the
hook no-ops silently -- the identical safe default the source's own gate
already guaranteed on any OSS/non-dev install, so a wrong-but-safe repo-root
guess on the flat/OSS layout (where no distinct "one level up" exists) is
harmless: the sentinel simply is not there either way.

Everything else is unchanged: the mirror direction (TRACKED -> LIVE, never
the reverse), the OSS-clobber gate (`.coordinator-dev-repo`, fail CLOSED on
any uncertainty), the rules-dir copy-in-only/never-prune contract, and the
fail-loud-on-real-failure / silent-when-already-synced contract.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import claude_config_dir, machine_local_dir
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#derive-global-doctrine-mirror-and-fail-loud"
)

_DEV_SENTINEL_NAME = ".coordinator-dev-repo"


def _artifact_at(root: Path) -> Optional[Path]:
    candidate = root / _DEV_SENTINEL_NAME
    return candidate if candidate.is_file() else None


def _resolve_doctrine_repo_root() -> Optional[Path]:
    """Locate the coordinator-claude doctrine-plane repo ROOT -- one level
    up from the CONTENT root every other ported hook's plugin-root probe
    resolves -- by probing for `.coordinator-dev-repo` at each rung. Returns
    `None` on a miss at every rung; the caller's `_is_dev_repo()` then
    returns False and every derivation no-ops. See module docstring's
    "Repo-root resolution" section."""
    import os

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        env_path = Path(env_root)
        for candidate_root in (env_path.parent, env_path):
            found = _artifact_at(candidate_root)
            if found is not None:
                return candidate_root

    plugin_base = claude_config_dir() / "plugins" / "coordinator-claude"
    found = _artifact_at(plugin_base)
    if found is not None:
        return plugin_base

    try:
        pointer = machine_local_dir() / ".doe-root"
        doe_root_text = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        doe_root_text = ""
    if doe_root_text:
        candidate_root = Path(doe_root_text)
        found = _artifact_at(candidate_root)
        if found is not None:
            return candidate_root

    return None


def _tracked_path(repo_root: Path) -> Path:
    return repo_root / "global-doctrine" / "CLAUDE.md"


def _live_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def _display_path(path: Path, repo_root: Optional[Path] = None) -> str:
    """Render `path` for a note/advisory reader without leaking the
    operator's absolute home directory or checkout location -- collapses to
    `~/...` when `path` sits under `Path.home()`, to a repo-relative form
    when `repo_root` is given and `path` sits under it, native absolute
    otherwise. Mirrors `hooks/support/message_envelope.py::_render_resolved`'s
    same home-collapse convention (that helper is private to its own module,
    so this is a small local equivalent, not a cross-module import)."""
    try:
        relative = path.relative_to(Path.home())
        return f"~/{relative.as_posix()}" if relative.parts else "~"
    except (ValueError, RuntimeError, OSError):
        pass
    if repo_root is not None:
        try:
            return path.relative_to(repo_root).as_posix()
        except (ValueError, RuntimeError, OSError):
            pass
    return str(path)


def _published_path(repo_root: Path) -> Path:
    return repo_root / "coordinator" / "templates" / "global-doctrine" / "CLAUDE.md"


def _published_rules_dir(repo_root: Path) -> Path:
    return repo_root / "coordinator" / "templates" / "global-doctrine" / "rules"


def _tracked_rules_dir(repo_root: Path) -> Path:
    return repo_root / "global-doctrine" / "rules"


def _live_rules_dir() -> Path:
    return Path.home() / ".claude" / "rules"


def _tracked_rules_files(repo_root: Path) -> "list[Path]":
    rules_dir = _tracked_rules_dir(repo_root)
    try:
        if not rules_dir.is_dir():
            return []
        return sorted(p for p in rules_dir.iterdir() if p.is_file() and p.suffix == ".md")
    except Exception:
        return []


def _is_dev_repo(repo_root: Optional[Path]) -> bool:
    if repo_root is None:
        return False
    try:
        return (repo_root / _DEV_SENTINEL_NAME).is_file()
    except Exception:
        return False


def _derive_live_copy(tracked: Path, live: Path, notes: "list[str]", repo_root: Optional[Path] = None) -> bool:
    """Read/compare/write for one mirrored pair. Appends a note to `notes`
    only when a real derivation happened or a failure occurred (matches the
    source's silent-when-synced contract); returns True on any failure
    (fail-loud signal for the caller's overall advisory)."""
    display = _display_path(live) if repo_root is None else _display_path(live, repo_root)
    try:
        source_bytes = tracked.read_bytes()
    except Exception as exc:
        notes.append(f"tracked unreadable, live unchanged ({exc})")
        return True

    try:
        live_bytes = live.read_bytes()
    except Exception:
        live_bytes = None

    if live_bytes == source_bytes:
        return False

    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tracked, live)
    except Exception as exc:
        notes.append(f"write failed ({len(source_bytes)}B read OK): {display} ({exc})")
        return True

    notes.append(f"OK {display} ({len(source_bytes)}B)")
    return False


def evaluate(payload: dict):
    """Pure core: given a parsed PostToolUse(Write|Edit|MultiEdit) OR
    SessionStart payload, performs whichever derivations the event/path
    imply and returns the joined advisory `Message`, or `None` for a silent
    no-op (gate failed, already in sync, or unrecognized payload shape)."""
    if not isinstance(payload, dict):
        return None

    repo_root = _resolve_doctrine_repo_root()
    if not _is_dev_repo(repo_root):
        return None
    assert repo_root is not None  # _is_dev_repo(None) is False, above

    hook_event_name = payload.get("hook_event_name")
    if not isinstance(hook_event_name, str):
        hook_event_name = ""

    tool_input = payload.get("tool_input")
    file_path = ""
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path", "") or ""
    if not isinstance(file_path, str):
        file_path = ""

    session_start_mode = hook_event_name == "SessionStart"

    notes: "list[str]" = []

    if session_start_mode:
        tracked = _tracked_path(repo_root)
        if tracked.is_file():
            _derive_live_copy(tracked, _live_path(), notes)
            _derive_live_copy(tracked, _published_path(repo_root), notes, repo_root)
        for rules_tracked in _tracked_rules_files(repo_root):
            rules_live = _live_rules_dir() / rules_tracked.name
            _derive_live_copy(rules_tracked, rules_live, notes)
            _derive_live_copy(
                rules_tracked, _published_rules_dir(repo_root) / rules_tracked.name, notes, repo_root
            )
    else:
        if not file_path:
            return None

        try:
            resolved = Path(file_path).resolve()
        except Exception:
            return None

        tracked = _tracked_path(repo_root)
        try:
            tracked_resolved = tracked.resolve()
        except Exception:
            tracked_resolved = tracked

        if resolved == tracked_resolved:
            _derive_live_copy(tracked, _live_path(), notes)
            _derive_live_copy(tracked, _published_path(repo_root), notes, repo_root)
        else:
            rules_dir = _tracked_rules_dir(repo_root)
            try:
                rules_dir_resolved = rules_dir.resolve()
            except Exception:
                rules_dir_resolved = rules_dir

            try:
                resolved.relative_to(rules_dir_resolved)
                under_rules_dir = True
            except ValueError:
                under_rules_dir = False

            if under_rules_dir and resolved.suffix == ".md" and resolved.is_file():
                _derive_live_copy(resolved, _live_rules_dir() / resolved.name, notes)
                _derive_live_copy(
                    resolved, _published_rules_dir(repo_root) / resolved.name, notes, repo_root
                )
            else:
                return None

    if not notes:
        return None
    return compose("; ".join(notes), anchor=_WIKI_ANCHOR)


@register_op("hooks.derive_global_doctrine_live_copy")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Write|Edit|MultiEdit) AND SessionStart op: re-derive the
    live global CLAUDE.md/rules mirror and the in-plugin published copy from
    their tracked coordinator-claude-repo sources, when drifted and this
    process resolves a dev checkout (OSS-clobber gate, fail-closed)."""
    message = evaluate(params)
    if message is None:
        return no_advisory()
    event_name = params.get("hook_event_name") if isinstance(params, dict) else None
    return allow_advisory(event_name if isinstance(event_name, str) and event_name else "PostToolUse", render(message))
