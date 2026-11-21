"""Engine-independent backstop: irreversible-external guards run in-process, with no warm engine.

`match(host_event)` returns a deny verdict when an irreversible-external bash guard refuses the
event's shell command, or when that command force-pushes main or master, else None. Guards are
imported lazily; one that raises is skipped (fails open). `match` imports neither `coordinator_core.warm` nor
`coordinator_core.ops`. `degrade_match` adds the data-destroying git verbs for the host guard's degrade path.
"""
from __future__ import annotations

import importlib
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from coordinator_core.host_hooks.contract import HostEvent

# Pinned against deny_registry.GUARD_DENY_CLASS by tests/test_backstop.py.
GUARD_MODULES = {
    "block-push-pr-non-owned-remote": "coordinator_core.bash_guards.block_push_pr_non_owned_remote",
    "block-empty-source-push": "coordinator_core.bash_guards.block_empty_source_push",
    "block-held-push": "coordinator_core.bash_guards.block_held_push",
    "block-perforce-submit": "coordinator_core.bash_guards.block_perforce_submit",
    "p4-verb-fence": "coordinator_core.bash_guards.p4_verb_fence",
}

_COMMAND_KEYS = ("command", "CommandLine", "cmd")


def _command(host_event: HostEvent) -> str:
    tool_input = host_event.tool_input or {}
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _payload(host_event: HostEvent) -> Optional[Dict[str, Any]]:
    from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

    # Native patches and MCP arguments may also contain a `command` string. They
    # are data for another tool, never evidence that a shell will execute it.
    if host_event.tool_name not in (*COMMAND_TOOL_NAMES, "exec_command", "shell", "run_command"):
        return None
    command = _command(host_event)
    if not command:
        return None
    tool_name = host_event.tool_name if host_event.tool_name in COMMAND_TOOL_NAMES else COMMAND_TOOL_NAMES[0]
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": host_event.cwd,
        "session_id": host_event.session_id,
    }


def _reason(record: Any, guard: str) -> str:
    text = getattr(record, "em_text", None) or getattr(record, "summary", None)
    if text:
        return str(text)
    if isinstance(record, dict):
        return str(record.get("reason") or record.get("message") or guard)
    return "BLOCKED by %s" % guard


_MAIN_BRANCHES = ("main", "master")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n]")
_GIT_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")
_PUSH_VALUE_FLAGS = ("--repo", "-o", "--push-option", "--receive-pack", "--exec", "--signed")
_WRAPPERS = ("env", "command", "sudo", "time", "exec", "nohup", "nice")
_FORCE_LONG_FLAGS = ("--force", "--force-with-lease", "--force-if-includes", "--mirror")


def _current_branch(start: str) -> str:
    """The branch HEAD names for the repo containing `start`, else "". Reads files only; no git spawn."""
    if not start:
        return ""
    try:
        here = Path(start)
        for directory in (here, *here.parents):
            dot_git = directory / ".git"
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return ""
                gitdir = Path(text[len("gitdir:"):].strip())
                dot_git = gitdir if gitdir.is_absolute() else directory / gitdir
            if dot_git.is_dir():
                head = (dot_git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
                prefix = "ref: refs/heads/"
                return head[len(prefix):] if head.startswith(prefix) else ""
    except OSError:
        return ""
    return ""


def _push_segment(tokens: list) -> Optional[tuple]:
    """(git -C dir, push args) when `tokens` is a `git ... push ...` invocation, else None."""
    start = 0
    while start < len(tokens) and (tokens[start] in _WRAPPERS or ("=" in tokens[start] and not tokens[start].startswith("-"))):
        start += 1
    if start >= len(tokens) or os.path.basename(tokens[start]).lower() not in ("git", "git.exe"):
        return None
    chdir = ""
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if token in _GIT_VALUE_FLAGS:
            if token == "-C" and i + 1 < len(tokens):
                chdir = tokens[i + 1]
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return (chdir, tokens[i + 1:]) if token == "push" else None
    return None


def _force_push_to_main(command: str, cwd: str) -> bool:
    """True when a segment of `command` is a forcing `git push` whose target is main or master."""
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        found = _push_segment(tokens)
        if found is None:
            continue
        chdir, args = found
        forcing = False
        positionals = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in _PUSH_VALUE_FLAGS:
                i += 2
                continue
            if arg.startswith("--"):
                forcing = forcing or arg.startswith(_FORCE_LONG_FLAGS)
            elif arg.startswith("-"):
                forcing = forcing or "f" in arg[1:]
            else:
                positionals.append(arg)
                forcing = forcing or arg.startswith("+")
            i += 1
        if not forcing:
            continue
        everything = "--all" in args or "--mirror" in args
        refspecs = positionals[1:]
        current = ""
        targets = []
        for refspec in refspecs:
            dst = refspec.lstrip("+").rpartition(":")[2]
            if dst in ("HEAD", ""):
                current = current or _current_branch(os.path.join(cwd, chdir) if chdir else cwd)
                dst = current if dst == "HEAD" else dst
            targets.append(dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst)
        if not refspecs and not everything:
            targets.append(_current_branch(os.path.join(cwd, chdir) if chdir else cwd))
        if everything or any(t in _MAIN_BRANCHES for t in targets):
            return True
    return False


_FORCE_MAIN_REASON = (
    "BLOCKED: this 'git push' force-pushes main (--force / -f / +refspec / --force-with-lease), "
    "which rewrites shared history and can drop commits that exist only on the remote. "
    "Push main as a fast-forward."
)


_DESTRUCTIVE_GUARD = "block-subagent-destructive-action"
_IRREVERSIBLE_GIT_LABELS = (
    "git reset --hard", "git push --force", "git clean", "git branch -D", "git tag -d",
    "git stash drop", "git reflog", "git filter-branch", "git checkout -f", "git switch -f",
)


def _destructive_git(command: str) -> Optional[str]:
    """The label of a data- or history-destroying git verb in `command`, else None.

    Reuses the subagent destructive-action classifier's verdicts, which parse in-process and spawn
    nothing; only labels on `_IRREVERSIBLE_GIT_LABELS` count, so its other denies stay advisory.
    """
    if "git" not in command.lower():
        return None
    from coordinator_core import deny_registry
    from coordinator_core.bash_guards import block_subagent_destructive_action as classifier

    if deny_registry.guard_deny_class(_DESTRUCTIVE_GUARD, subagent=True) not in deny_registry.IRREVERSIBLE_CLASSES:
        return None
    # The same argv0/heredoc normalization the classifier's own entry applies, so a spaced Windows git.exe path is seen.
    command = classifier._normalize_windows_wrapper_argv0(
        classifier._normalize_windows_argv0_head_path_with_spaces(
            classifier._strip_heredoc_bodies(command.replace("\r", ""))))
    if "git" not in command.lower():  # the raw-text surface regex misses a quoted spaced argv0
        return None
    verdict = classifier._evaluate_git_surface(command)
    if verdict and verdict.startswith(_IRREVERSIBLE_GIT_LABELS):
        return verdict
    # The classifier labels `reset --hard <ref>` by its ref branch, ahead of the hard-flag branch.
    if verdict and verdict.startswith("git reset") and "--hard" in command:
        return "git reset --hard"
    return None


_RM_NAMES = ("rm", "rm.exe")
_PS_REMOVE_NAMES = ("remove-item", "ri")
_DISK_FREE_FLOOR = 10 * 1024 ** 3  # pinned to disk_budget.Policy.volume_free_floor by tests
_SIZE_SUFFIX = {"": 1, "c": 1, "w": 2, "b": 512, "k": 1024, "kb": 1000, "kib": 1024, "m": 1024 ** 2,
                "mb": 1000 ** 2, "mib": 1024 ** 2, "g": 1024 ** 3, "gb": 1000 ** 3, "gib": 1024 ** 3,
                "t": 1024 ** 4, "tb": 1000 ** 4, "tib": 1024 ** 4}
_SIZE_TOKEN = re.compile(r"^([0-9]+)([a-zA-Z]*)$")


def _segment_tokens(command: str) -> Iterator[list]:
    """Yield the wrapper-stripped token list of each shell segment of `command`."""
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        tokens = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
        start = 0
        while start < len(tokens) and (tokens[start] in _WRAPPERS or ("=" in tokens[start] and not tokens[start].startswith("-"))):
            start += 1
        if start < len(tokens):
            yield tokens[start:]


def _under_scratch_or_tmp(target: str, cwd: str) -> bool:
    """True when `target` resolves below a `<repo>/scratch` directory or the system temp dir. Stats only."""
    import tempfile

    path = target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)
    try:
        resolved = os.path.normcase(os.path.realpath(path))
        roots = {os.path.normcase(os.path.realpath(p)) for p in (tempfile.gettempdir(), "/tmp") if os.path.isdir(p)}
    except OSError:
        return False
    if any(resolved.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
        return True
    current = os.path.dirname(resolved)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return False
        if os.path.basename(current) == "scratch" and os.path.exists(os.path.join(parent, ".git")):
            return True
        current = parent


_REGENERABLE_DIRS = frozenset(("node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".pytest_cache"))


def _git_root_of(path: str) -> str:
    """The nearest ancestor of `path` (inclusive) holding a `.git` entry, else "". Stats only; no git spawn."""
    current = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent


def _destroys_tracked_tree(target: str, cwd: str) -> bool:
    """True when `target` sits in a git work tree and is not scratch, tmp or a regenerable build directory."""
    path = os.path.abspath(target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target))
    root = _git_root_of(path)
    if not root or _under_scratch_or_tmp(target, cwd):
        return False
    parts = os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace("\\", "/").split("/")
    return not any(part in _REGENERABLE_DIRS for part in parts)


_INDEX_FIXED = 62  # bytes before the name in a v2/v3 index entry
_INDEX_MODES = frozenset((0o100644, 0o100755, 0o120000, 0o160000))
_WALK_CAP = 5000  # files one rm target may hold before the walk gives up
_HASH_CAP = 1 << 20


def _index_path(root: str) -> Optional[str]:
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return os.path.join(dot, "index")
    try:
        with open(dot, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    return os.path.join(root, line[len("gitdir:"):].strip(), "index") if line.startswith("gitdir:") else None


def _entry_start(data: bytes, name_pos: int, version: int) -> Optional[int]:
    """The start of the index record whose name begins at `name_pos`, or None when no record does (a mid-name hit)."""
    import struct

    nul = data.find(b"\0", name_pos)
    if nul < 0:
        return None
    for extended in ((0,) if version == 2 else (0, 2)):
        start = name_pos - _INDEX_FIXED - extended
        if start < 12 or (start - 12) % 8:
            continue
        (flags,) = struct.unpack_from(">H", data, start + 60)
        (mode,) = struct.unpack_from(">I", data, start + 24)
        if bool(flags & 0x4000) == bool(extended) and (flags & 0xFFF) == min(nul - name_pos, 0xFFF) and mode in _INDEX_MODES:
            return start
    return None


def _index_entries(root: str, rel: bytes, tree: bool) -> Optional[Dict[bytes, tuple]]:
    """{name: (size, mtime_s, mtime_ns, sha_hex | None, racy)} for the index entries at `rel`, or under `rel/` when `tree`.

    `racy` is git's racily-clean case on a whole-second timestamp: the stat match proves nothing, so the content is hashed.

    Reads `.git/index` directly: v2/v3 only, located with a byte search because the entries are sorted by name,
    so a tree's are contiguous. None when the index cannot be read that way (absent, v4, a split index), which
    callers treat as not clean.
    """
    import struct

    path = _index_path(root)
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
            index_s = os.fstat(fh.fileno()).st_mtime_ns // 10 ** 9
    except OSError:
        return None
    if data[:4] != b"DIRC" or struct.unpack_from(">I", data, 4)[0] not in (2, 3):
        return None
    version = struct.unpack_from(">I", data, 4)[0]
    key = rel + (b"/" if tree else b"")
    start, at = None, data.find(key, 12)
    for _ in range(64):
        if at < 0:
            return {}
        start = _entry_start(data, at, version)
        if start is not None:
            break
        at = data.find(key, at + 1)
    else:
        return None
    found: Dict[bytes, tuple] = {}
    end = len(data) - 20
    while start + _INDEX_FIXED <= end:
        (flags,) = struct.unpack_from(">H", data, start + 60)
        extended = 2 if version >= 3 and flags & 0x4000 else 0
        name_pos = start + _INDEX_FIXED + extended
        name = data[name_pos:data.find(b"\0", name_pos)]
        if not (name.startswith(key) if tree else name == rel):
            break
        mtime_s, mtime_ns = struct.unpack_from(">II", data, start + 8)
        (size,) = struct.unpack_from(">I", data, start + 36)
        found[name] = (size, mtime_s, mtime_ns, data[start + 40:start + 60].hex() if not flags & 0x3000 else None,
                       mtime_s >= index_s and mtime_ns == 0)
        start += (_INDEX_FIXED + extended + len(name) + 8) & ~7
    return found


def _matches_index(path: str, entry: tuple) -> bool:
    """True when the work-tree file at `path` is the content the index records: stat match, else a blob hash."""
    import stat

    size, mtime_s, mtime_ns, sha, racy = entry
    if sha is None:
        return False
    try:
        info = os.lstat(path)
        if not racy and info.st_size & 0xFFFFFFFF == size and divmod(info.st_mtime_ns, 10 ** 9) == (mtime_s, mtime_ns):
            return True
        if info.st_size > _HASH_CAP:
            return False
        if stat.S_ISLNK(info.st_mode):
            content = os.fsencode(os.readlink(path))
        else:
            with open(path, "rb") as fh:
                content = fh.read()
    except OSError:
        return False
    import hashlib

    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest() == sha


def _rm_loses_work(target: str, cwd: str) -> bool:
    """True when removing `target` would lose content git cannot restore: a repo root or `.git` path, a nested
    repo, an untracked file, or a file whose content differs from the index entry. An absent target, scratch/tmp
    and regenerable dirs lose nothing. Spawn-free: it reads `.git/index`; an unreadable index counts as lost.

    Staged-but-uncommitted content counts as restorable (its blob is already in the object store), where the warm
    guard's `git status` sees it as dirty; the cold route trades that one case for needing no git process.
    """
    path = os.path.abspath(target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target))
    if not os.path.lexists(path):
        return False
    root = _git_root_of(path)
    if not root or _under_scratch_or_tmp(target, cwd):
        return False
    real, real_root = os.path.realpath(path), os.path.realpath(root)
    rel = os.path.relpath(real, real_root).replace("\\", "/")
    parts = rel.split("/")
    if parts == ["."] or ".git" in parts:
        return True
    if any(part in _REGENERABLE_DIRS for part in parts):
        return False
    tree = os.path.isdir(real)
    entries = _index_entries(real_root, rel.encode("utf-8", "surrogateescape"), tree)
    if entries is None:
        return True
    if not tree:
        entry = entries.get(rel.encode("utf-8", "surrogateescape"))
        return entry is None or not _matches_index(real, entry)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(real):
        if ".git" in dirnames or ".git" in filenames:
            return True
        linked = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d not in _REGENERABLE_DIRS and d not in linked]
        for name in filenames + linked:
            seen += 1
            full = os.path.join(dirpath, name)
            key = os.path.relpath(full, real_root).replace("\\", "/").encode("utf-8", "surrogateescape")
            entry = entries.get(key)
            if seen > _WALK_CAP or entry is None or not _matches_index(full, entry):
                return True
    return False


def _clean_destroys(target: str, cwd: str) -> bool:
    """`_destroys_tracked_tree`, except a target under no resolvable work tree counts: the clean's cwd may be unknown here."""
    path = os.path.abspath(target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target))
    return not _git_root_of(path) or _destroys_tracked_tree(target, cwd)


def _git_clean_forced(command: str, cwd: str) -> Optional[bool]:
    """None when no segment is `git clean`; else True when a forcing, non-dry-run clean would hit a tracked tree."""
    found = None
    for tokens in _segment_tokens(command):
        if os.path.basename(tokens[0]).lower() not in ("git", "git.exe"):
            continue
        chdir, i = "", 1
        while i < len(tokens):
            if tokens[i] in _GIT_VALUE_FLAGS:
                if tokens[i] == "-C" and i + 1 < len(tokens):
                    chdir = tokens[i + 1]
                i += 2
            elif tokens[i].startswith("-"):
                i += 1
            else:
                break
        if i >= len(tokens) or tokens[i] != "clean":
            continue
        args = tokens[i + 1:]
        short = "".join(a[1:] for a in args if re.match(r"^-[A-Za-z]+$", a))
        forcing = "f" in short or "--force" in args
        dry = "n" in short or "--dry-run" in args
        cut = args.index("--") if "--" in args else len(args)
        paths = [a for a in args[:cut] if not a.startswith("-")] + args[cut + 1:]
        base = os.path.join(cwd or os.getcwd(), chdir) if chdir else cwd
        found = bool(found)
        if forcing and not dry and any(_clean_destroys(p, base) for p in (paths or ["."])):
            return True
    return found


RM_DENY_REASON = "BLOCKED: recursive rm would delete untracked or uncommitted work git cannot restore. Delete under <repo>/scratch, or retry once the warm engine is up."
CLEAN_DENY_REASON = "BLOCKED: git clean -f in a work tree destroys unrecoverable untracked data. Clean under <repo>/scratch, or retry once the warm engine is up."


def _recursive_rm(command: str, cwd: str) -> bool:
    """True when a segment is a recursive `rm` / `Remove-Item -Recurse` whose target `_rm_loses_work`: untracked or
    modified content, a repo root, or a store. A clean tracked target is allowed, as the warm `destructive-rm` does."""
    for tokens in _segment_tokens(command):
        name = os.path.basename(tokens[0]).lower()
        args = tokens[1:]
        if name in _RM_NAMES:
            recursive = any(a in ("--recursive",) or (re.match(r"^-[A-Za-z]+$", a) and re.search(r"[rR]", a)) for a in args)
            targets = [a for a in args if a == "-" or not a.startswith("-")]
        elif name in _PS_REMOVE_NAMES:
            recursive = any(a.lower().startswith("-r") and "-recurse".startswith(a.lower()) for a in args)
            targets = [a for a in args if not a.startswith("-")]
        else:
            continue
        if recursive and any(_rm_loses_work(t, cwd) for t in targets):
            return True
    return False


def _stash_destruction(command: str) -> bool:
    """True when a segment is `git ... stash drop|clear`."""
    for tokens in _segment_tokens(command):
        if os.path.basename(tokens[0]).lower() not in ("git", "git.exe"):
            continue
        i = 1
        while i < len(tokens):
            if tokens[i] in _GIT_VALUE_FLAGS:
                i += 2
            elif tokens[i].startswith("-"):
                i += 1
            else:
                break
        if i + 1 < len(tokens) and tokens[i] == "stash" and tokens[i + 1] in ("drop", "clear"):
            return True
    return False


def _size(raw: str) -> Optional[int]:
    m = _SIZE_TOKEN.match(raw.lstrip("+"))
    if m is None:
        return None
    mult = _SIZE_SUFFIX.get(m.group(2).lower())
    return None if mult is None else int(m.group(1)) * mult


def _declared_fill(tokens: list) -> Optional[tuple]:
    """(target, bytes) for a `dd of=`, `truncate -s` or `fsutil file createnew` with a declared size, else None."""
    name = os.path.basename(tokens[0]).lower()
    args = tokens[1:]
    if name == "dd":
        kv = dict(a.split("=", 1) for a in args if "=" in a)
        if "of" not in kv or kv["of"].startswith("/dev/"):
            return None
        bs, count = _size(kv.get("bs", "")), _size(kv.get("count", ""))
        return (kv["of"], bs * count) if bs and count else None
    if name == "truncate":
        raw = None
        for i, a in enumerate(args):
            if a in ("-s", "--size") and i + 1 < len(args):
                raw = args[i + 1]
            elif a.startswith("--size="):
                raw = a.split("=", 1)[1]
        positionals = [a for a in args if not a.startswith("-") and a != raw]
        size = _size(raw) if raw and raw[:1] not in "-<>/%" else None
        return (positionals[-1], size) if size and positionals else None
    if name in ("fsutil", "fsutil.exe") and len(args) >= 4 and [a.lower() for a in args[:2]] == ["file", "createnew"]:
        size = _size(args[3])
        return (args[2], size) if size else None
    return None


def _disk_fill(command: str, cwd: str) -> Optional[str]:
    """The deny text when a declared-size write would leave its volume under the free-space floor, else None."""
    import shutil

    for tokens in _segment_tokens(command):
        found = _declared_fill(tokens)
        if found is None:
            continue
        target, size = found
        path = os.path.abspath(target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target))
        probe = os.path.dirname(path)
        while probe and not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            continue
        if free - size < _DISK_FREE_FLOOR:
            return ("BLOCKED: ~%.1f GiB write to %s leaves %.1f GiB free (floor %.1f GiB)."
                    % (size / 1024 ** 3, probe, max(free - size, 0) / 1024 ** 3, _DISK_FREE_FLOOR / 1024 ** 3))
    return None


def degrade_match(host_event: HostEvent) -> Optional[Dict[str, str]]:
    """The deny verdict for an irreversible-class command when the host guard could not evaluate, else None.

    `match` plus spawn-free, import-free classifiers (recursive `rm` outside scratch/tmp, `git stash
    drop|clear`, declared-size disk-fill) and the destroy-data git family (`git clean`, `git reset --hard`).
    """
    denied = match(host_event)
    if denied is not None:
        return denied
    try:
        payload = _payload(host_event)
        if payload is None:
            return None
        command, cwd = payload["tool_input"]["command"], payload.get("cwd") or ""
        if _recursive_rm(command, cwd):
            return {"decision": "deny", "reason": RM_DENY_REASON}
        if _stash_destruction(command):
            return {"decision": "deny", "reason": "BLOCKED: git stash drop/clear destroys data that cannot be recovered."}
        fill = _disk_fill(command, cwd)
        if fill:
            return {"decision": "deny", "reason": fill}
        clean = _git_clean_forced(command, cwd)
        if clean:
            return {"decision": "deny", "reason": CLEAN_DENY_REASON}
        label = _destructive_git(command)
        if clean is False and label and label.startswith("git clean"):
            label = None
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        return None
    if label:
        return {"decision": "deny", "reason": "BLOCKED: %s destroys data or history that cannot be recovered." % label}
    return None


#: Guard names `match` ran for the last event, in run order, as (guard, "allow"|"deny"|"raised"); read once through `take_trace`.
_trace: list = []


def take_trace() -> list:
    """The (guard, verdict) pairs the last `match` ran, cleared."""
    taken = list(_trace)
    del _trace[:]
    return taken


def match(host_event: HostEvent) -> Optional[Dict[str, str]]:
    """The deny verdict `{"decision": "deny", "reason": ...}` for an irreversible command, else None."""
    del _trace[:]
    if host_event.tool_name.startswith("mcp__"):
        try:
            module = importlib.import_module(GUARD_MODULES["block-perforce-submit"])
            record = module.check_mcp({"tool_name": host_event.tool_name,
                                       "tool_input": dict(host_event.tool_input or {})})
            _trace.append(("block-perforce-submit", "deny" if record else "allow"))
            if record:
                return {"decision": "deny", "reason": _reason(record, "block-perforce-submit")}
        except Exception:  # noqa: BLE001 -- same fail-open hook boundary as shell checks
            pass
        return None
    try:
        payload = _payload(host_event)
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        return None
    if payload is None:
        return None
    for guard, module_name in GUARD_MODULES.items():
        try:
            record = importlib.import_module(module_name).check(dict(payload))
        except Exception:  # noqa: BLE001 -- a raising guard is skipped
            _trace.append((guard, "raised"))
            continue
        _trace.append((guard, "deny" if record else "allow"))
        if record:
            return {"decision": "deny", "reason": _reason(record, guard)}
    try:
        forced = _force_push_to_main(payload["tool_input"]["command"], payload.get("cwd") or "")
        _trace.append(("force-push-main", "deny" if forced else "allow"))
        if forced:
            return {"decision": "deny", "reason": _FORCE_MAIN_REASON}
    except Exception:  # noqa: BLE001 -- the backstop never breaks the hook
        pass
    return None
