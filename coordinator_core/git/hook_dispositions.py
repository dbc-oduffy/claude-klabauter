"""The hook disposition table: every retired or replaced git-hook body, and the
applier that brings a clone to the table's state.

`.git/hooks/` is untracked per-clone state, so a hook retirement is a table
entry here, never a remover script. Entries live one per module under
`coordinator_core.git.hook_disposition_entries`; `DISPOSITIONS` is assembled
from them on first access.

Contract:
  - `classify_repo(root)` reads, never writes. `apply_repo(root, check_only=...)`
    returns the same shape; with `check_only=False` it also acts.
  - Both return one `(entry_id, hook_name, verdict)` per entry that
    `applies_to` the clone. Verdicts are the closed set in `VERDICTS`.
  - An entry acts only on a body its `identify` recognises exactly. Anything
    else is left byte-identical.
  - The original bytes are copied to `<hook>.retired` (next free
    `<hook>.retired.<n>` if taken) before any mutation; if that fails the hook
    is not touched (`refused-no-backup`).
  - A hook git is running cannot be replaced or unlinked on Windows: a
    `PermissionError` yields `busy`, leaves the original, retries next walk.
    A file already gone is `absent`.

Zero-spawn: resolves the hooks directory through `coordinator_core.git.git_dir`
and does file I/O only.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir

GENERATES: list = []  # writes only untracked per-clone `.git/hooks` files

ACTIONS = ("remove", "excise-block", "replace")

VERDICTS = (
    "absent",
    "current",
    "stale",
    "removed",
    "block-excised",
    "replaced",
    "unidentified-left-alone",
    "refused-no-backup",
    "busy",
)

Verdict = Tuple[str, str, str]


@dataclass(frozen=True)
class Match:
    """Half-open `[start, end)` character span of the retired content in the
    decoded hook text. For `remove` and `replace` it covers the whole body."""

    start: int
    end: int


def _every_repo(_repo_root: Path) -> bool:
    return True


@dataclass(frozen=True)
class HookDisposition:
    """`identify` must recognise an exact body, an append block, or an
    installer-stamped banner -- never a mere mention of a retired name.
    `replacement` is required for `replace` and ignored otherwise."""

    id: str
    hook_name: str
    action: str
    identify: Callable[[str], Optional[Match]]
    replacement: Optional[Callable[[], str]] = None
    applies_to: Callable[[Path], bool] = _every_repo

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r} for {self.id}")
        if self.action == "replace" and self.replacement is None:
            raise ValueError(f"{self.id}: action 'replace' needs a replacement")


def _load_dispositions() -> Tuple[HookDisposition, ...]:
    from coordinator_core.git.hook_disposition_entries import load_entries

    return load_entries()


def __getattr__(name: str):
    # The entry modules import this module for the types above, so the table
    # is assembled on first access to break the import cycle.
    if name == "DISPOSITIONS":
        table = _load_dispositions()
        globals()["DISPOSITIONS"] = table
        return table
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _hooks_dir(repo_root: Path) -> Path:
    return resolve_git_common_dir(repo_root) / "hooks"


def _read(hook_path: Path) -> Optional[Tuple[bytes, Optional[str]]]:
    """`(raw, text)` for an existing hook; `text` is None when not UTF-8.
    None when the hook is absent."""
    try:
        raw = hook_path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, None


def _next_backup_path(hook_path: Path) -> Path:
    first = hook_path.with_name(hook_path.name + ".retired")
    if not first.exists():
        return first
    n = 1
    while True:
        candidate = hook_path.with_name(f"{hook_path.name}.retired.{n}")
        if not candidate.exists():
            return candidate
        n += 1


def _write_backup(hook_path: Path, raw: bytes) -> Optional[Path]:
    """Exclusive-create so a concurrent walk never overwrites a backup."""
    for _ in range(64):
        target = _next_backup_path(hook_path)
        try:
            with open(target, "xb") as fh:
                fh.write(raw)
            return target
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


def _replace_file(hook_path: Path, new_text: str) -> None:
    tmp = hook_path.with_name(f".{hook_path.name}.disposition.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o755)
        os.replace(tmp, hook_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _classify_one(entry: HookDisposition, hook_path: Path) -> Tuple[str, Optional[Match], bytes]:
    got = _read(hook_path)
    if got is None:
        return "absent", None, b""
    raw, text = got
    if text is None:
        return "unidentified-left-alone", None, raw
    match = entry.identify(text)
    if match is not None:
        return "stale", match, raw
    if entry.action == "replace" and text != entry.replacement():
        return "unidentified-left-alone", None, raw
    return "current", None, raw


_ACTED = {"remove": "removed", "excise-block": "block-excised", "replace": "replaced"}


def _act(entry: HookDisposition, hook_path: Path, match: Match, raw: bytes) -> str:
    text = raw.decode("utf-8")
    backup = _write_backup(hook_path, raw)
    if backup is None:
        return "refused-no-backup"
    try:
        if entry.action == "remove":
            hook_path.unlink()
        elif entry.action == "excise-block":
            _replace_file(hook_path, text[: match.start] + text[match.end :])
        else:
            _replace_file(hook_path, entry.replacement())
    except FileNotFoundError:
        with contextlib.suppress(OSError):
            backup.unlink()
        return "absent"
    except PermissionError:
        with contextlib.suppress(OSError):
            backup.unlink()
        return "busy"
    return _ACTED[entry.action]


def _walk(repo_root: Union[str, Path], check_only: bool) -> List[Verdict]:
    root = Path(repo_root)
    hooks_dir = _hooks_dir(root)
    out: List[Verdict] = []
    table = globals().get("DISPOSITIONS")
    if table is None:
        table = __getattr__("DISPOSITIONS")
    for entry in table:
        if not entry.applies_to(root):
            continue
        hook_path = hooks_dir / entry.hook_name
        verdict, match, raw = _classify_one(entry, hook_path)
        if verdict == "stale" and not check_only:
            verdict = _act(entry, hook_path, match, raw)
        out.append((entry.id, entry.hook_name, verdict))
    return out


def classify_repo(repo_root: Union[str, Path]) -> List[Verdict]:
    """Which live dispositions does this clone's hooks directory still match?"""
    return _walk(repo_root, True)


def apply_repo(repo_root: Union[str, Path], *, check_only: bool) -> List[Verdict]:
    """Bring the clone to the table's state, or report what would change."""
    return _walk(repo_root, check_only)
