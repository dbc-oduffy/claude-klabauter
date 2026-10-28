"""
coordinator_core.hooks._em_write_budget — the XS-lane / residue-window budget verdict for the EM's writes.

Purpose: one mode-free home for the verdict `nudge_em_code_dispatch` renders on an Edit/Write and the
Bash guard `guard_xs_lane_shell_write` renders on a shell write, so the guard imports this leaf and
never the hook. The leaf must not import `nudge_em_code_dispatch`, `session.mode_resolution` or
`session.fleet_mode`, directly or transitively: `test_fleet_mode_reader_set` walks the import closure
and a reader that reaches the fleet mode record widens the fleet reader set.

Invariants: signatures of `shell_write_budget` and `_budget_core` are pinned by their callers; the
hook re-binds only the names it or its tests read, so a test patching a name `_budget_core` or
`shell_write_budget` reads (e.g. `_COORDINATOR_PATH_MARKERS`) must patch THIS module, not the hook.
"""

from __future__ import annotations

import os
import re
import sys

from coordinator_core._hook_envelope import deny
from coordinator_core._settings_home import settings_home
from coordinator_core.group_em.gem_home import contains as _path_contains
from coordinator_core.hooks import _em_post_fire
from coordinator_core.hooks.dispatch_escape import _take_escape
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.session import general_work_scope
from coordinator_core.session.engine_route_state import session_sizing_state as _session_sizing_state
from coordinator_core.warp_fire import fire_step

_EXT_EXECUTOR_MAP: dict[str, str] = {
    ".py": "python-executor",
    ".js": "js-executor",
    ".mjs": "js-executor",
    ".cjs": "js-executor",
    ".ts": "ts-executor",
    ".tsx": "ts-executor",
    ".jsx": "js-executor",
    ".sh": "shell-executor",
    ".bash": "shell-executor",
    ".rb": "ruby-executor",
    ".go": "go-executor",
    ".rs": "rust-executor",
    ".java": "jvm-executor",
    ".kt": "jvm-executor",
    ".c": "cpp-executor",
    ".cpp": "cpp-executor",
    ".cc": "cpp-executor",
    ".h": "cpp-executor",
    ".hpp": "cpp-executor",
    ".swift": "swift-executor",
    ".sql": "sql-executor",
}

# Path markers that signal coordinator-domain files -> coordinator-executor override.
_COORDINATOR_PATH_MARKERS: list[str] = [
    "coordinator/",
    "coordinator_core/",
    "plugins/coordinator",
]

_DEVOPS_FILENAME_RE = re.compile(
    r"^(?:[Mm]akefile|[Dd]ockerfile(?:\..+)?|[Rr]akefile|[Gg]runtfile\.js|[Gg]ulpfile\.js)$"
)

_SESSION_SIZING = "<this session's sizing path>"

_SIZED_ADVISORY = (
    "[em-code-dispatch nudge] EM edits route through the warp run. "
    + fire_step(_SESSION_SIZING)
)

_ROUTE_UNFIRED_DENY = (
    "Sized S or larger; its engine run has not fired. " + fire_step(_SESSION_SIZING)
)


def _bootstrap_dirs() -> list[str]:
    """Machine-config/bootstrap dirs the F7 carve-out skips the nudge for.

    Mirrors JS bootstrapDirs() for the `$HOME`-rooted rungs: HOME must be set
    (matches JS's process.env.HOME guard), and those rungs are omitted when it
    is absent.

    DIVERGENCE FROM THE JS MIRROR — `<settings-home>/bin` is resolved
    independently of the HOME guard, via the canonical
    `coordinator_core._settings_home.settings_home()` (COORDINATOR_SETTINGS_HOME,
    else CLAUDE_HOME, else the platform home, with `.coordinator-claude-settings`
    appended to the latter two). That resolver has a Windows rung `Path.home()`
    that a bare `$HOME` read does not, so gating it behind `HOME` would drop the
    live executable surface's carve-out on exactly the platform where HOME is
    unset. Resolution failure (a cwd-relative COORDINATOR_SETTINGS_HOME, or an
    undeterminable home) degrades to omitting the rung, never to raising — this
    is an advisory hook on the write path.

    `~/.claude/bin` is retained as a legacy rung: its executable surface moved to
    `<settings-home>/bin` on 2026-07-28 (the old dir renamed aside), but a machine
    that has not been reinstalled since still has live bits there, and a stale
    carve-out entry costs nothing but a suppressed nudge on a dir nobody writes.

    Matching is prefix-on-abspath (see `_is_under_bootstrap_dir`), not realpath —
    a symlinked settings home is matched by the path as written, not its target.
    """
    dirs: list[str] = []

    try:
        dirs.append(str(settings_home() / "bin"))
    except (ValueError, RuntimeError, OSError):
        pass

    home = os.environ.get("HOME")
    if home:
        dirs.extend([
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".claude", "bin"),
            os.path.join(home, ".claude", "shell"),
        ])

    return dirs


def _is_under_bootstrap_dir(file_path: str) -> bool:
    abs_file = os.path.normcase(os.path.abspath(file_path))
    for d in _bootstrap_dirs():
        abs_dir = os.path.normcase(os.path.abspath(d))
        if abs_file == abs_dir or (abs_file + os.sep).startswith(abs_dir + os.sep):
            return True
    return False


def _is_outside_git_work_tree(file_path: str) -> bool:
    d = os.path.abspath(os.path.dirname(file_path))
    root = os.path.splitdrive(d)[0] + os.sep if os.name == "nt" else os.sep
    while True:
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return False
        except OSError as exc:
            print(f"_em_write_budget: cannot stat {d}: {exc} (treating as not found)", file=sys.stderr)
        if d == root:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return True


def _is_under_general_work_root(file_path: str) -> bool:
    """Target is under the registry-declared `coordinator.general_work_root`; an unset or unreadable key exempts nothing."""
    root = general_work_scope.general_work_root()
    return bool(root) and _path_contains(root, file_path)


def _is_bootstrap_or_out_of_repo(file_path: str) -> bool:
    if _is_under_bootstrap_dir(file_path):
        return True
    if _is_outside_git_work_tree(file_path):
        return True
    return _is_under_general_work_root(file_path)


def _derive_executor_info(file_path: str) -> tuple[str, bool]:
    ext = os.path.splitext(file_path)[1].lower()
    basename = os.path.basename(file_path)
    norm_path = file_path.replace("\\", "/")

    if any(marker in norm_path for marker in _COORDINATOR_PATH_MARKERS):
        return "coordinator-executor", False

    by_ext = _EXT_EXECUTOR_MAP.get(ext)
    if by_ext:
        return by_ext, False

    if _DEVOPS_FILENAME_RE.match(basename):
        return "devops-executor", False

    return "generic-executor", True


def _is_scratch_or_tmp(file_path: str) -> bool:
    import tempfile

    abs_path = os.path.abspath(file_path)
    if "scratch" in abs_path.replace("\\", "/").split("/"):
        return True
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    return (os.path.normcase(abs_path) + os.sep).startswith(tmp + os.sep)


def _budget_text(prefix: str, usage) -> str:
    return f"{prefix} {usage.files}/2 files, {usage.lines}/30 lines."


def _budget_deny(text: str) -> dict | None:
    return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", text))


def _post_fire_root(payload: dict) -> str:
    try:
        from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root

        return str(_repo_root(payload) or "")
    except Exception:
        return ""


def shell_write_budget(
    payload: dict,
    session_id: str,
    file_path: str,
    projected: str | None,
    unprojectable: str | None,
) -> dict | None:
    """Budget verdict for a main-EM shell write to `file_path` whose post-write text is `projected`.
    Runs the Edit/Write hook's exemption chain, then the lane/residue measure; returns the hook
    envelope (deny, residue context) or None. `projected=None` with an `unprojectable` reason is
    denied as unmeasurable only where a lane governs the session. Consumes an honoured escape
    sentinel, as the hook does: the escape is taken ahead of the lane check, so it is spent even
    where no lane would have denied."""
    if "agent_id" in payload or not file_path:
        return None
    if _is_bootstrap_or_out_of_repo(file_path):
        return None
    if os.path.splitext(file_path)[1].lower() in _DOC_DATA_EXTENSIONS:
        return None
    if _is_scratch_or_tmp(file_path):
        return None
    if _take_escape(payload, session_id, file_path, "nudge-em-code-dispatch"):
        return None
    if _em_post_fire.fired(session_id, _post_fire_root(payload)):
        return None

    def _project() -> str:
        if projected is None:
            raise _Unprojectable(unprojectable or "write cannot be projected")
        return projected

    handled, result = _budget_core(payload, session_id, file_path, _project, shell=True)
    return result if handled else None


def _closed_text(rec: dict) -> str:
    return f"XS hand edits closed. {fire_step()} Put `git diff {rec['base_sha'][:12]}` in the ask."


class _Unprojectable(Exception):
    """Carries the caller's own reason; rendered without an exception-type prefix."""


def _strict() -> bool:
    from coordinator_core import machine_profile

    try:
        return machine_profile.guard_level("nudge-em-code-dispatch") == "strict"
    except Exception:
        return False


def _latched_text() -> str:
    return "XS hand edits closed until engine-ask fires. " + fire_step()


def _latch(xlb, root, session_id: str, unmeasured_path: str | None = None) -> None:
    """Latches the lane so a strict guard denies further writes until engine-ask fires; never changes the verdict."""
    try:
        xlb.latch_advised(root, session_id, unmeasured_path)
    except Exception:
        pass


def arm_advised(root, session_id) -> None:
    """Latches the session's lane after an engine-ask advisory was emitted; a lane-less session gets
    an open XS lane at HEAD to carry the flag. Never raises, never changes the verdict."""
    try:
        if not root or not session_id:
            return
        from coordinator_core.git import git_state
        from coordinator_core.session import xs_lane_budget as xlb

        if xlb.read_lane(root, session_id) is None:
            head = git_state.head_sha(root)
            if head:
                xlb.open_xs_lane(root, session_id, head)
        xlb.latch_advised(root, session_id)
    except Exception:
        pass


def advised_deny_envelope(root, session_id, paths: tuple = ()) -> dict | None:
    """The PreToolUse deny when the lane is advised, engine-ask has not fired, and (given `paths`)
    one is a product path under `root`; else None. Passes through `apply_guard_level`."""
    try:
        if not root or not session_id:
            return None
        from coordinator_core.session import xs_lane_budget as xlb

        rec = xlb.read_lane(root, session_id)
        if rec is None or rec.get("corrupt") or not xlb.is_advised(rec):
            return None
        if _em_post_fire.fired(session_id, root):
            return None
        if paths and not any(_em_post_fire.is_product_path(p, root, session_id) for p in paths):
            return None
        text = "Product writes closed until engine-ask fires. " + fire_step()
        return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", text)) or None
    except Exception:
        return None


def _charge_unmeasured(xlb, root, session_id: str, file_path: str) -> None:
    """Charges the lane a full budget for a write that could not be priced; never changes the verdict."""
    try:
        xlb.record_deny(
            root, session_id, xlb.Usage(xlb.BUDGET_FILES, xlb.BUDGET_LINES, True), [file_path]
        )
    except Exception:
        pass


def _discard_fresh(xlb, root, session_id: str) -> None:
    """Drops the XS lane this call opened so a refused write leaves none; never changes the verdict."""
    try:
        xlb.discard_fresh_xs_lane(root, session_id)
    except Exception:
        pass


def _trajectory(xlb, root, rec: dict, file_path: str) -> str:
    """The lane's spent and remaining budget before this write; 'used unknown' when the base is unreadable."""
    try:
        u = xlb.measure(root, rec, file_path, xlb._read_text(xlb._abs(root, xlb._norm_rel(root, file_path))))
    except Exception:
        return f"XS budget used unknown of {xlb.BUDGET_FILES} files, {xlb.BUDGET_LINES} lines."
    return (
        f"XS budget used {u.files}/{xlb.BUDGET_FILES} files, {u.lines}/{xlb.BUDGET_LINES} lines; "
        f"remaining {max(xlb.BUDGET_FILES - u.files, 0)} files, {max(xlb.BUDGET_LINES - u.lines, 0)} lines."
    )


def _applied_text(xlb, root, rec: dict) -> str:
    """' Applied: <file> (<n> lines), ... Revert: git checkout -- <files>.' for earlier accepted edits; '' when none."""
    try:
        changes = xlb.applied_changes(root, rec)
    except Exception:
        return ""
    if not changes:
        return ""
    listed = ", ".join(f"{rel} ({n} lines)" for rel, n in changes)
    return f" Applied: {listed}. Revert: git checkout -- {' '.join(rel for rel, _ in changes)}."


def _budget_core(
    payload: dict, session_id: str, file_path: str, project, shell: bool = False
) -> tuple[bool, dict | None]:
    """Lane open/residue/measure logic shared by the hook's `_budget_decision` and `shell_write_budget`;
    `project()` returns the post-write text or raises. `shell` marks a Bash write: at strict an
    unmeasurable one latches the lane (see `xs_lane_budget.latch_advised`)."""
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.session import xs_lane_budget as xlb

    root = _repo_root(payload)
    if root is None or not session_id:
        return False, None
    opened_here = False
    try:
        s_plus = xlb.touched_s_or_larger(session_id, root)
        rec = xlb.read_lane(root, session_id)
        if rec is not None and rec.get("kind") != xlb.KIND_XS:
            return False, None
        if rec is None and not s_plus and not _session_sizing_state(payload, session_id)[0]:
            from coordinator_core.git import git_state

            head = git_state.head_sha(root)
            if head and xlb.open_xs_lane(root, session_id, head):
                opened_here = True
                rec = xlb.read_lane(root, session_id)
        unfired = False
        if s_plus:
            from coordinator_core.session.engine_route_state import PHASE_UNFIRED, session_route_phase

            unfired = (
                session_route_phase(root, session_id) == PHASE_UNFIRED
                and not _session_sizing_state(payload, session_id)[1]
            )
    except Exception:
        return False, None
    if unfired:
        return True, _budget_deny(_ROUTE_UNFIRED_DENY)
    if rec is None:
        return False, None
    kind = rec.get("kind")
    if s_plus:
        return False, None
    over_text = lambda u: _budget_text("XS budget spent (projected):", u) + " Size S or larger. " + fire_step()
    if rec.get("corrupt"):
        return True, _budget_deny(over_text(xlb.Usage(0, 0, True)))
    if xlb.deny_count(rec) >= xlb.GOLF_DENY_LIMIT:
        return True, _budget_deny(_closed_text(rec))
    try:
        usage = xlb.measure(root, rec, file_path, project())
    except Exception as exc:
        reason = str(exc) if isinstance(exc, _Unprojectable) else f"{type(exc).__name__}: {exc}"
        text = f"Budget unmeasurable ({reason}). {_trajectory(xlb, root, rec, file_path)} Size S or larger. {fire_step()}"
        if shell and _strict():
            _latch(xlb, root, session_id, file_path)
            _charge_unmeasured(xlb, root, session_id, file_path)
        elif opened_here:
            _discard_fresh(xlb, root, session_id)
        return True, _budget_deny(text)
    if usage.over:
        if opened_here:
            _discard_fresh(xlb, root, session_id)
        else:
            try:
                denies = xlb.record_deny(root, session_id, usage, [*rec.get("paths", []), file_path])
            except Exception:
                denies = 0
            if denies >= xlb.GOLF_DENY_LIMIT:
                return True, _budget_deny(_closed_text(rec))
        return True, _budget_deny(over_text(usage) + ("" if opened_here else _applied_text(xlb, root, rec)))
    if _strict() and xlb.is_advised(rec):
        return True, _budget_deny(_latched_text())
    try:
        xlb.add_path(root, session_id, file_path)
    except Exception:
        pass
    return True, None
