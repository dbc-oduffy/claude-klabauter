"""Cross-process store of materialized committed-tree shadows, keyed on what shaped them.

A shadow is served only from an entry whose ``.complete`` marker exists and records the full
key, so an interrupted adoption is never read. The marker is written last. Entries are read by
other live publish processes, so retention never deletes the current entry nor one inside its
lease, and never deletes at all: it renames aside to ``.deleting-<pid>`` and ``reap_doomed`` removes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterable

from coordinator_core.temp_layout import _coordinator_root, coordinator_temp_root

STORE_DIRNAME = "publish-shadow-store"
COMPLETE_MARKER = ".complete"
# How long one publish pass can still be reading a tree it looked up (observed pass ~70 min).
LEASE_S = 4 * 3600
# How long an entry without a marker may live before it is an abandoned adoption.
INCOMPLETE_MAX_AGE_S = 3600
# How long an unmarked entry is presumed to be a concurrent adopter between its rename and marker write.
_IN_FLIGHT_S = 60
_ENTRY_NAME_LEN = 16
_DELETING_INFIX = ".deleting-"
# Test seam: when set, a ShadowStore built without an explicit `root` uses this directory.
ROOT_OVERRIDE: Path | None = None


class ShadowStoreError(RuntimeError):
    """An extracted tree could not be adopted; the caller still owns (and removes) it."""


class ShadowStore:
    """Completed-adoption store under ``coordinator_temp_root(toplevel)/publish-shadow-store``.

    ``root`` overrides the location, then the module-level ``ROOT_OVERRIDE`` (tests). Every
    method tolerates a missing root.
    """

    def __init__(self, toplevel: Path | str, root: Path | None = None) -> None:
        root = root if root is not None else ROOT_OVERRIDE
        self.root = Path(root) if root is not None else coordinator_temp_root(toplevel) / STORE_DIRNAME

    @staticmethod
    def key(toplevel: Path | str, sha: str, pathspec: Iterable[str], withheld_digest: str = "") -> str:
        """sha256 over (toplevel, sha, sorted pathspec, withheld digest); a differing part never shares a key."""
        parts = [str(toplevel), sha, sorted(pathspec), withheld_digest or ""]
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).hexdigest()

    def entry_path(self, key: str) -> Path:
        return self.root / key[:_ENTRY_NAME_LEN]

    @staticmethod
    def _marker_records(entry: Path, key: str | None = None) -> str | None:
        """The key a complete entry's marker records, or None when absent, unreadable or not `key`."""
        try:
            recorded = (entry / COMPLETE_MARKER).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return recorded if recorded and (key is None or recorded == key) else None

    def lookup(self, key: str) -> Path | None:
        """The entry for `key` when its marker exists and records `key`; touches the marker (extends the lease)."""
        entry = self.entry_path(key)
        if self._marker_records(entry, key) is None:
            return None
        try:
            os.utime(entry / COMPLETE_MARKER)
        except OSError:
            pass
        return entry

    def adopt(self, key: str, extracted: Path) -> Path:
        """Move `extracted` into the store under `key` and return the entry.

        `extracted` must be on the store's filesystem. A concurrent adopter that won the rename
        leaves `extracted` removed here and its entry returned. Raises ShadowStoreError, leaving
        `extracted` untouched, when the tree cannot be adopted.
        """
        entry = self.entry_path(key)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ShadowStoreError(f"cannot create shadow store root {self.root}: {exc}") from exc
        for _attempt in range(2):
            try:
                os.replace(extracted, entry)
            except OSError as exc:
                if not entry.exists():
                    raise ShadowStoreError(f"cannot adopt {extracted} as {entry}: {exc}") from exc
                if self._marker_records(entry, key) is not None:
                    if not self._discard(extracted):
                        from coordinator_core.owner_safe_rmtree import fast_rmtree

                        fast_rmtree(extracted)
                    return entry
                if (entry / COMPLETE_MARKER).exists():
                    raise ShadowStoreError(f"{entry} is complete for a different key") from exc
                if self._age_s(entry) < _IN_FLIGHT_S:
                    raise ShadowStoreError(f"{entry} is being adopted by another process") from exc
                self._discard(entry)
                continue
            try:
                self._write_marker(entry, key)
            except OSError as exc:
                self._discard(entry)
                raise ShadowStoreError(f"cannot mark {entry} complete: {exc}") from exc
            return entry
        raise ShadowStoreError(f"cannot adopt {extracted} as {entry}")

    @staticmethod
    def _write_marker(entry: Path, key: str) -> None:
        tmp = entry / f"{COMPLETE_MARKER}.{os.getpid()}.tmp"
        tmp.write_text(key, encoding="utf-8")
        os.replace(tmp, entry / COMPLETE_MARKER)

    @staticmethod
    def _age_s(path: Path, now: float | None = None) -> float:
        try:
            return (now if now is not None else time.time()) - path.stat().st_mtime
        except OSError:
            return 0.0

    def _doomed_name(self, name: str) -> Path:
        """A free ``<name>.deleting-<pid>[-n]`` path beside the entries."""
        base = f"{name}{_DELETING_INFIX}{os.getpid()}"
        doomed = self.root / base
        n = 0
        while doomed.exists():
            n += 1
            doomed = self.root / f"{base}-{n}"
        return doomed

    def _discard(self, entry: Path) -> bool:
        """Rename out of lookup's reach; never deletes (reap_doomed does). False when the rename failed
        and the entry stays."""
        try:
            os.replace(entry, self._doomed_name(entry.name))
        except OSError:
            return False
        return True

    @staticmethod
    def _doomed_pid(child: Path) -> int | None:
        tail = child.name.rpartition(_DELETING_INFIX)[2]
        digits = tail.split("-", 1)[0]
        return int(digits) if digits.isdigit() else None

    def reap_doomed(self, budget_s: float | None = None) -> int:
        """Delete ``.deleting-<pid>`` trees whose pid is this process or dead; returns the count removed.

        A live peer's tree is kept. Stops starting new deletions once `budget_s` has elapsed.
        """
        from coordinator_core.group_em.session_registry import pid_alive
        from coordinator_core.owner_safe_rmtree import fast_rmtree

        start = time.monotonic()
        try:
            children = [p for p in self.root.iterdir() if _DELETING_INFIX in p.name]
        except OSError:
            return 0
        removed = 0
        for child in children:
            if budget_s is not None and time.monotonic() - start >= budget_s:
                break
            pid = self._doomed_pid(child)
            if pid is None or (pid != os.getpid() and pid_alive(pid)):
                continue
            if fast_rmtree(child):
                removed += 1
        return removed

    @classmethod
    def store_roots(cls) -> list[Path]:
        """Every existing ``<coordinator temp>/<repo>/publish-shadow-store`` dir; ROOT_OVERRIDE wins."""
        if ROOT_OVERRIDE is not None:
            return [Path(ROOT_OVERRIDE)] if Path(ROOT_OVERRIDE).is_dir() else []
        roots: list[Path] = []
        try:
            with os.scandir(_coordinator_root()) as scan:
                for repo in scan:
                    candidate = Path(repo.path) / STORE_DIRNAME
                    if candidate.is_dir():
                        roots.append(candidate)
        except OSError:
            pass
        return roots

    def retain(self, current_key: str, keep: int = 2) -> None:
        """Rename complete entries beyond the newest `keep`, and unmarked entries older than an hour, aside.

        Never deletes (reap_doomed does), never touches the `current_key` entry nor a complete entry
        whose marker is younger than LEASE_S, and skips ``.deleting-`` trees.
        """
        now = time.time()
        current = self.entry_path(current_key).name
        complete: list[tuple[float, Path]] = []
        try:
            children = [p for p in self.root.iterdir() if _DELETING_INFIX not in p.name and p.is_dir()]
        except OSError:
            return
        for child in children:
            if self._marker_records(child) is None:
                if self._age_s(child, now) > INCOMPLETE_MAX_AGE_S:
                    self._discard(child)
                continue
            try:
                complete.append(((child / COMPLETE_MARKER).stat().st_mtime, child))
            except OSError:
                continue
        complete.sort(key=lambda item: item[0], reverse=True)
        for mtime, child in complete[max(keep, 0):]:
            if child.name == current or now - mtime < LEASE_S:
                continue
            self._discard(child)
