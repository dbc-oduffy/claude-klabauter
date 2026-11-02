"""coordinator_core.percolate.guard_cache -- per-run file cache and persistent
clean-verdict memo shared by the percolate guards.

`run_guards` opens `guard_run()`: every guard in it then shares one walk per
distinct file-surface and one read per file, instead of re-walking and
re-reading the tree once per guard. A guard that mutates the tree calls
`invalidate()` afterwards.

`CleanMemo` records "this exact content passed this exact check" across
processes. Only clean verdicts are stored, keyed on the content bytes plus a
check identity (pattern, interpreter, guard code), so a stale entry can only
ever skip a check whose answer is already known. Outside the published tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
from typing import Callable, Iterator, Optional

#: Bytes cached per run are capped so a large tree cannot balloon the driver.
_RUN_BYTES_CAP = 128 * 1024 * 1024
_MAX_KEYS_PER_MEMO = 400_000
_MEMO_SUBDIR = "percolate-guard-memo"

_active: "Optional[_RunCache]" = None
_memos: "dict[tuple[str, str], CleanMemo]" = {}


class _RunCache:
    def __init__(self) -> None:
        self.walks: "dict[object, list[Path]]" = {}
        self.blobs: "dict[str, bytes]" = {}
        self.binary: "dict[Path, bool]" = {}
        self.blob_bytes = 0

    def clear(self) -> None:
        self.walks.clear()
        self.binary.clear()
        self.blobs.clear()
        self.blob_bytes = 0


@contextlib.contextmanager
def guard_run() -> Iterator[None]:
    """Open the shared per-run cache (re-entrant: an inner call joins the outer)
    and persist every touched memo on exit."""
    global _active
    if _active is not None:
        yield
        return
    _active = _RunCache()
    try:
        yield
    finally:
        _active.clear()
        _active = None
        for memo in _memos.values():
            memo.save()


def invalidate() -> None:
    """Drop cached walks and bytes; call after anything that mutates the tree."""
    if _active is not None:
        _active.clear()


def cached_walk(key: object, produce: "Callable[[], Iterator[Path]]") -> "Iterator[Path]":
    if _active is None:
        return produce()
    paths = _active.walks.get(key)
    if paths is None:
        paths = list(produce())
        _active.walks[key] = paths
    return iter(paths)


def cached_binary_verdict(path: Path, probe: "Callable[[Path], bool]") -> bool:
    """`probe(path)`, remembered for the run so several walks over one tree sniff each file once."""
    if _active is None:
        return probe(path)
    verdict = _active.binary.get(path)
    if verdict is None:
        verdict = _active.binary[path] = probe(path)
    return verdict


def read_bytes(path: Path) -> bytes:
    """`path`'s bytes, read once per run. Raises `OSError` like `Path.read_bytes`."""
    if _active is None:
        return Path(path).read_bytes()
    key = str(path)
    data = _active.blobs.get(key)
    if data is None:
        data = Path(path).read_bytes()
        if _active.blob_bytes + len(data) <= _RUN_BYTES_CAP:
            _active.blobs[key] = data
            _active.blob_bytes += len(data)
    return data


def decode_text(data: bytes, *, errors: str) -> str:
    """`Path.read_text(encoding='utf-8', errors=errors)` over already-read bytes,
    including its universal-newline translation."""
    text = data.decode("utf-8", errors)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _memo_dir() -> "Optional[Path]":
    try:
        from coordinator_core.session.machinery_paths import cache_dir

        return Path(cache_dir(str(Path(__file__).resolve().parents[2]))) / _MEMO_SUBDIR
    except Exception:
        return None


_code_identity_value: "Optional[str]" = None


def code_identity() -> str:
    """Hash of the guard code a verdict was computed by, so editing it drops every entry."""
    global _code_identity_value
    if _code_identity_value is None:
        digest = hashlib.blake2b(digest_size=16)
        here = Path(__file__).resolve().parent
        for name in ("guards.py", "guard_cache.py", "protect.py"):
            try:
                digest.update((here / name).read_bytes())
            except OSError:
                digest.update(b"?")
        _code_identity_value = digest.hexdigest()
    return _code_identity_value


class CleanMemo:
    def __init__(self, directory: "Optional[Path]", name: str, identity: str) -> None:
        suffix = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).hexdigest()
        self._path = None if directory is None else directory / f"{name}-{suffix}.keys"
        self._known: "set[str]" = set()
        self._new: "list[str]" = []
        if self._path is not None:
            try:
                self._known = set(self._path.read_text(encoding="ascii").split())
            except (OSError, ValueError):
                pass

    def _key(self, data: bytes) -> str:
        return hashlib.blake2b(data, digest_size=16).hexdigest()

    def known_clean(self, data: bytes) -> bool:
        return self._key(data) in self._known

    def record_clean(self, data: bytes) -> None:
        key = self._key(data)
        if key not in self._known:
            self._known.add(key)
            self._new.append(key)

    def save(self) -> None:
        if not self._new or self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._known) > _MAX_KEYS_PER_MEMO:
                self._path.write_text("\n".join(self._known) + "\n", encoding="ascii")
            else:
                with open(self._path, "a", encoding="ascii", newline="\n") as handle:
                    handle.write("\n".join(self._new) + "\n")
        except OSError:
            pass
        self._new = []


def clean_memo(name: str, identity: str) -> CleanMemo:
    """Process-wide memo for `(name, identity)`; `identity` must fold in everything
    the verdict depends on besides the content bytes."""
    full = f"{code_identity()}\0{identity}"
    memo = _memos.get((name, full))
    if memo is None:
        memo = _memos[(name, full)] = CleanMemo(_memo_dir(), name, full)
    return memo
