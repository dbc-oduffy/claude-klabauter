"""coordinator_core.bash_guards._disk_fill_contract -- classifier, bounded size
estimator, floor verdict and deny text for the disk-fill guard.

A bulk write or copy (cp -r, rsync, dd of=, truncate -s, tar/zip create,
Copy-Item -Recurse, robocopy, xcopy, fsutil file createnew) is denied only when
its declared or estimated output would leave the target volume under
``disk_budget.Policy.volume_free_floor``. Every other command classifies to
None. Anything unestimable (walk cap or deadline hit, dd without bs= and
count=, missing source, disk_usage OSError) is allow, never deny.

Tokenization is reused from ``block_venv_creation`` / ``_dialect``, never
re-implemented. The clock, disk_usage and policy are injectable so tests never
depend on wall time or the real volume.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards.block_worktree_creation import (
    _skip_leading_env_assignments,
)

GUARD_NAME = "guard-disk-fill"

# The 500ms brightline leaves no room for an unbounded recursive walk: past
# either bound the size is "unknown" and the guard allows.
WALK_ENTRY_CAP = 5000
WALK_DEADLINE_S = 0.05


@dataclass(frozen=True)
class BulkWrite:
    """One bulk write: where it lands, plus its declared size or its sources."""

    target: str
    declared_bytes: Optional[int] = None
    sources: Tuple[str, ...] = ()
    shape: str = ""


@dataclass(frozen=True)
class Verdict:
    """`deny` is True only when free space after the write is under the floor."""

    deny: bool
    text: str = ""


_SIZE_RE = re.compile(r"^([0-9]+)([a-zA-Z]*)$")
_SUFFIX = {
    "": 1, "c": 1, "w": 2, "b": 512,
    "k": 1024, "kb": 1000, "kib": 1024,
    "m": 1024**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1024**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1024**4, "tb": 1000**4, "tib": 1024**4,
}


def _parse_size(raw: str) -> Optional[int]:
    """Bytes in a `dd`/`truncate`/`fsutil` size token such as `10G`, `4096`, `+1M`."""
    m = _SIZE_RE.match(raw.lstrip("+-"))
    if not m:
        return None
    mult = _SUFFIX.get(m.group(2).lower())
    return None if mult is None else int(m.group(1)) * mult


def _positionals(args: Sequence[str], value_flags: Sequence[str] = ()) -> List[str]:
    out: List[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in value_flags:
            skip = True
        elif a.startswith("-") and len(a) > 1:
            continue
        else:
            out.append(a)
    return out


def _short_flags(args: Sequence[str]) -> str:
    return "".join(a[1:] for a in args if re.match(r"^-[A-Za-z]+$", a))


def _classify_cp(rest: List[str]) -> Optional[BulkWrite]:
    flags = _short_flags(rest)
    long_rec = any(a in ("--recursive", "--archive") for a in rest)
    if not (long_rec or re.search(r"[rRa]", flags)):
        return None
    target_dir = None
    for i, a in enumerate(rest):
        if a == "-t" and i + 1 < len(rest):
            target_dir = rest[i + 1]
        elif a.startswith("--target-directory="):
            target_dir = a.split("=", 1)[1]
    pos = _positionals(rest, ("-t", "--target-directory", "-S", "--suffix"))
    if target_dir is not None:
        return BulkWrite(target_dir, None, tuple(pos), "cp -r") if pos else None
    if len(pos) < 2:
        return None
    return BulkWrite(pos[-1], None, tuple(pos[:-1]), "cp -r")


def _classify_rsync(rest: List[str]) -> Optional[BulkWrite]:
    pos = _positionals(rest, ("-e", "--rsh", "--exclude", "--include", "--filter", "--port"))
    if len(pos) < 2:
        return None
    return BulkWrite(pos[-1], None, tuple(pos[:-1]), "rsync")


def _classify_dd(rest: List[str]) -> Optional[BulkWrite]:
    kv = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in rest if "=" in a}
    if "of" not in kv or kv["of"].startswith("/dev/"):
        return None
    bs, count = _parse_size(kv.get("bs", "")), _parse_size(kv.get("count", ""))
    declared = bs * count if bs is not None and count is not None else None
    return BulkWrite(kv["of"], declared, (), "dd")


def _classify_truncate(rest: List[str]) -> Optional[BulkWrite]:
    raw = None
    for i, a in enumerate(rest):
        if a in ("-s", "--size") and i + 1 < len(rest):
            raw = rest[i + 1]
        elif a.startswith("--size="):
            raw = a.split("=", 1)[1]
        elif re.match(r"^-s.+", a):
            raw = a[2:]
    # `-1M`, `<`, `/`, `%` shrink or round an existing file: they never fill a disk.
    if raw is None or raw[:1] in ("-", "<", "/", "%"):
        return None
    size = _parse_size(raw)
    if size is None:
        return None
    pos = _positionals(rest, ("-s", "--size", "-r", "--reference"))
    return BulkWrite(pos[-1], size, (), "truncate -s") if pos else None


def _classify_tar(rest: List[str]) -> Optional[BulkWrite]:
    first = rest[0] if rest else ""
    old_style = bool(first) and not first.startswith("-") and "c" in first
    new_style = "--create" in rest or re.search(r"c", _short_flags(rest) or "") is not None
    if not (old_style or new_style):
        return None
    archive = None
    for i, a in enumerate(rest):
        if a == "-f" or a == "--file":
            archive = rest[i + 1] if i + 1 < len(rest) else None
        elif a.startswith("--file="):
            archive = a.split("=", 1)[1]
        elif re.match(r"^-[A-Za-z]*f$", a) and i + 1 < len(rest):
            archive = rest[i + 1]
    if archive is None and old_style and "f" in first and len(rest) > 1:
        archive = rest[1]
    if archive is None or archive == "-":
        return None
    skip = {archive}
    srcs = [p for p in _positionals(rest, ("-C", "--directory", "--exclude", "-T")) if p not in skip]
    if old_style and srcs and srcs[0] == first:
        srcs = srcs[1:]
    return BulkWrite(archive, None, tuple(srcs), "tar create")


def _classify_zip(rest: List[str]) -> Optional[BulkWrite]:
    pos = _positionals(rest, ("-x", "-i", "-P", "-b"))
    if len(pos) < 2:
        return None
    return BulkWrite(pos[0], None, tuple(pos[1:]), "zip")


def _ps_params(rest: List[str]) -> Tuple[dict, List[str]]:
    named: dict = {}
    pos: List[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("-") and len(a) > 1 and not a[1].isdigit():
            key = a[1:].lower().split(":", 1)[0]
            if ":" in a:
                named[key] = a.split(":", 1)[1]
            elif key in ("path", "destination", "literalpath", "filter") and i + 1 < len(rest):
                named[key] = rest[i + 1]
                i += 1
            else:
                named[key] = True
        else:
            pos.append(a)
        i += 1
    return named, pos


def _classify_copy_item(rest: List[str]) -> Optional[BulkWrite]:
    named, pos = _ps_params(rest)
    if not any(k.startswith("rec") for k in named):
        return None
    src = named.get("path") or named.get("literalpath")
    dst = named.get("destination")
    pos = list(pos)
    if src is None and pos:
        src = pos.pop(0)
    if dst is None and pos:
        dst = pos.pop(0)
    if not src or not dst or dst is True or src is True:
        return None
    return BulkWrite(str(dst), None, (str(src),), "Copy-Item -Recurse")


def _classify_robocopy(rest: List[str]) -> Optional[BulkWrite]:
    pos = [a for a in rest if not a.startswith("/")]
    if len(pos) < 2:
        return None
    return BulkWrite(pos[1], None, (pos[0],), "robocopy")


def _classify_xcopy(rest: List[str]) -> Optional[BulkWrite]:
    flags = {a.lower() for a in rest if a.startswith("/")}
    if not flags & {"/s", "/e"}:
        return None
    pos = [a for a in rest if not a.startswith("/")]
    if len(pos) < 2:
        return None
    return BulkWrite(pos[1], None, (pos[0],), "xcopy")


def _classify_fsutil(rest: List[str]) -> Optional[BulkWrite]:
    low = [a.lower() for a in rest]
    if low[:2] != ["file", "createnew"] or len(rest) < 4:
        return None
    size = _parse_size(rest[3])
    return None if size is None else BulkWrite(rest[2], size, (), "fsutil file createnew")


_PS_COPY = {"copy-item", "copy", "cpi", "cp"}


def classify(segment: Sequence[str], dialect: Optional[Dialect]) -> Optional[BulkWrite]:
    """The bulk write one command segment performs, else None.

    `segment` is one segment's tokens with env assignments and wrappers already
    stripped. A `cp -r` without a recursive flag, `tar x`, or `git commit`
    classifies to None.
    """
    if not segment:
        return None
    head = _normalize_executable_basename(segment[0]).lower()
    if head.endswith(".exe"):
        head = head[:-4]
    rest = list(segment[1:])
    if dialect is Dialect.POWERSHELL and head in _PS_COPY:
        return _classify_copy_item(rest)
    table = {
        "cp": _classify_cp, "rsync": _classify_rsync, "dd": _classify_dd,
        "truncate": _classify_truncate, "tar": _classify_tar, "zip": _classify_zip,
        "copy-item": _classify_copy_item, "robocopy": _classify_robocopy,
        "xcopy": _classify_xcopy, "fsutil": _classify_fsutil,
    }
    fn = table.get(head)
    return fn(rest) if fn else None


def iter_bulk_writes(cmd: str, dialect: Optional[Dialect]) -> Iterator[BulkWrite]:
    """Every bulk write in a full command line; an unparseable command yields none."""
    if dialect is Dialect.POWERSHELL:
        segments = resolve_segments_for_dialect(cmd, dialect, guard_name=GUARD_NAME)
        if segments is None:
            tokens = _tokenize_full_command(strip_powershell_prose_noise(cmd))
            segments = _segments_from_tokens(tokens) if tokens is not None else []
        normalize = lambda t: _strip_ps_quotes(t).replace("`", "")  # noqa: E731
    else:
        tokens = _tokenize_full_command(_strip_heredoc_bodies(cmd))
        segments = _segments_from_tokens(tokens) if tokens is not None else []
        normalize = lambda t: t  # noqa: E731
    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue
        working = _skip_leading_env_assignments(
            _strip_leading_subshell_and_env([normalize(t) for t in seg_tokens])
        )
        hit = classify(working, dialect)
        if hit is not None:
            yield hit


def _abs(path: str, cwd: Optional[str]) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else Path(cwd or os.getcwd()) / p


def estimate_output_bytes(
    bulk: BulkWrite,
    cwd: Optional[str] = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    max_entries: int = WALK_ENTRY_CAP,
    deadline_s: float = WALK_DEADLINE_S,
) -> Optional[int]:
    """Declared size, else the bounded sum of the sources' file sizes; None = unknown.

    Symlinks are not followed. Exceeding `max_entries` or `deadline_s` (on the
    injected `clock`) returns None.
    """
    if bulk.declared_bytes is not None:
        return bulk.declared_bytes
    if not bulk.sources:
        return None
    start = clock()
    total = 0
    seen = 0
    stack: List[Path] = []
    for src in bulk.sources:
        p = _abs(src, cwd)
        if p.exists():
            stack.append(p)
    while stack:
        p = stack.pop()
        seen += 1
        if seen > max_entries or clock() - start > deadline_s:
            return None
        try:
            if p.is_symlink():
                continue
            if p.is_dir():
                with os.scandir(p) as it:
                    stack.extend(Path(e.path) for e in it)
            else:
                total += p.stat().st_size
        except OSError:
            continue
    return total


def target_volume(target: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Nearest existing ancestor of `target` (relative paths resolve against `cwd`)."""
    p = _abs(target, cwd)
    for cand in (p, *p.parents):
        if cand.exists():
            return cand
    return None


def load_policy() -> Any:
    """`disk_budget.Policy.from_env()`, loaded by file path from this checkout."""
    name = "_disk_fill_disk_budget"
    mod = sys.modules.get(name)
    if mod is None:
        path = Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "disk_budget.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod.Policy.from_env()


def _gib(n: float) -> str:
    return "%.1f GiB" % (n / 1024**3)


def verdict(
    target: str,
    est: Optional[int],
    policy: Any = None,
    disk_usage: Callable[[Any], Any] = shutil.disk_usage,
    cwd: Optional[str] = None,
) -> Verdict:
    """Deny iff `est` bytes would leave the target's volume under the floor."""
    if not est:
        return Verdict(False)
    vol = target_volume(target, cwd)
    if vol is None:
        return Verdict(False)
    try:
        free = disk_usage(vol).free
    except OSError:
        return Verdict(False)
    floor = (policy if policy is not None else load_policy()).volume_free_floor
    if free - est >= floor:
        return Verdict(False)
    text = (
        "BLOCKED [%s]: ~%s to %s leaves %s free (floor %s). "
        "Alternative: write to a volume with room, or disk_budget sweep."
        % (GUARD_NAME, _gib(est), vol, _gib(max(free - est, 0)), _gib(floor))
    )
    return Verdict(True, text)
