"""coordinator_core.roadmap.census_store — on-disk store of each census command's
last completed run and each walked file's identity.

The store is derived machine state under `.coordinator-local/cache/`,
rebuildable by running the census, and never a source of truth. Every record is
checked against its key (a run) or its stat (an identity) before use, so a lost,
stale or torn store costs a spawn or a hash on the next run and never a wrong
verdict.

Racy-clean rule (git's): a stored identity is trusted only when the file's
(size, mtime_ns, ino) equal the stored stat AND the stored mtime_ns is strictly
older than the moment the identity was computed. A file modified within the same
timestamp tick as its hash could have changed after the read without moving its
mtime, so that identity is never trusted.

A file's match records (schema 2) live under one per-file stat identity with a per-pattern map,
so a file costs one stat tuple however many greps ask about it. The same racy-clean rule gates
them. `MATCH_ENTRY_LIMIT` bounds the (file x pattern) entries the store admits: past it a new
entry is refused (decline-to-cache), so load + save never grow into a slower leg.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session.machinery_paths import cache_dir

GENERATES = []

STORE_SCHEMA = 2

#: Most (file x pattern) match entries the store holds. A synthetic store of 32k files x 12
#: patterns (about 416k entries, 15.6MB) cost 157ms to dump and load once on the reference box;
#: this cap keeps the same round trip near 40ms.
MATCH_ENTRY_LIMIT = 100_000


def store_path(repo_root: Path) -> Path:
    """The store file: the main worktree's cache dir, shared by every linked worktree."""
    return Path(cache_dir(str(main_worktree_root(Path(repo_root))))) / "census-store.json"


class CensusStore:
    """In-memory view of the store file; `save` persists it when something changed."""

    def __init__(self, path: Optional[Path] = None,
                 runs: Optional[dict] = None, idents: Optional[dict] = None,
                 matches: Optional[dict] = None) -> None:
        self._path = path
        self._runs: dict = runs if runs is not None else {}
        self._idents: dict = idents if idents is not None else {}
        self._matches: dict = matches if matches is not None else {}
        self._match_entries = sum(
            len(r[4]) for r in self._matches.values() if isinstance(r, list) and len(r) == 5
            and isinstance(r[4], dict)
        )
        self._dirty = False

    @classmethod
    def load(cls, repo_root: Path) -> "CensusStore":
        """The stored view, or an empty one when the file is missing, unreadable,
        corrupt or of another schema. Never raises."""
        try:
            path = store_path(repo_root)
        except Exception:  # noqa: BLE001 — an unresolvable root has no store
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
                return cls(path)
            runs, idents, matches = data.get("runs"), data.get("idents"), data.get("matches")
            if not isinstance(runs, dict) or not isinstance(idents, dict) \
                    or not isinstance(matches, dict):
                return cls(path)
            return cls(path, runs, idents, matches)
        except Exception:  # noqa: BLE001
            return cls(path)

    def run(self, command: str) -> Optional[tuple]:
        """(key, (returncode, stdout, stderr), run_ms, key_ms) of the command's last
        completed run, or None."""
        rec = self._runs.get(command)
        try:
            key, ran, run_ms, key_ms = rec
            rc, out, err = ran
            return (key, (rc, out, err), run_ms, key_ms)
        except (TypeError, ValueError):
            return None

    def put_run(self, command: str, key: Optional[str], ran: tuple, run_ms: int,
                key_ms: Optional[int]) -> None:
        """Record a COMPLETED run, replacing the command's older record. Never put a
        timeout, an OSError or a deadline row."""
        rc, out, err = ran
        rec = [key, [rc, out, err], run_ms, key_ms]
        if self._runs.get(command) != rec:
            self._runs[command] = rec
            self._dirty = True

    def median_run_ms(self) -> Optional[int]:
        """Median run_ms over every stored run, or None when the store holds none."""
        vals = sorted(
            r[2] for r in self._runs.values()
            if isinstance(r, list) and len(r) == 4 and isinstance(r[2], (int, float))
        )
        if not vals:
            return None
        return int(vals[len(vals) // 2])

    def ident(self, rel: str, st: os.stat_result) -> Optional[str]:
        """The stored identity of `rel`, or None when the stat differs or the record is racy."""
        rec = self._idents.get(rel)
        try:
            size, mtime_ns, ino, ident, computed_at_ns = rec
        except (TypeError, ValueError):
            return None
        if (size, mtime_ns, ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            return None
        if not isinstance(computed_at_ns, int) or not mtime_ns < computed_at_ns:
            return None
        return ident

    def put_ident(self, rel: str, st: os.stat_result, ident: str, computed_at_ns: int) -> None:
        """Record `rel`'s identity; `computed_at_ns` is taken before the file was read."""
        rec = [st.st_size, st.st_mtime_ns, st.st_ino, ident, computed_at_ns]
        if self._idents.get(rel) != rec:
            self._idents[rel] = rec
            self._dirty = True

    def match(self, rel: str, st: os.stat_result, pattern_key: str) -> Optional[tuple]:
        """The stored per-file result for (`rel`, `pattern_key`), or None when the stat differs,
        the record is racy or no result was stored. The result is opaque to the store."""
        rec = self._matches.get(rel)
        try:
            size, mtime_ns, ino, computed_at_ns, by_pattern = rec
            result = by_pattern.get(pattern_key)
        except (TypeError, ValueError, AttributeError):
            return None
        if (size, mtime_ns, ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            return None
        if not isinstance(computed_at_ns, int) or not mtime_ns < computed_at_ns:
            return None
        return tuple(result) if isinstance(result, list) else None

    def put_match(self, rel: str, st: os.stat_result, pattern_key: str, result: tuple,
                  computed_at_ns: int) -> None:
        """Record `result` for (`rel`, `pattern_key`); `computed_at_ns` is taken before the file
        was read. A changed stat replaces the file's whole record. A new entry past
        `MATCH_ENTRY_LIMIT` is refused."""
        stat_key = [st.st_size, st.st_mtime_ns, st.st_ino]
        rec = self._matches.get(rel)
        if isinstance(rec, list) and len(rec) == 5 and isinstance(rec[4], dict) \
                and rec[:3] == stat_key and isinstance(rec[3], int) and st.st_mtime_ns < rec[3]:
            by_pattern = rec[4]
        else:
            if isinstance(rec, list) and len(rec) == 5 and isinstance(rec[4], dict):
                self._match_entries -= len(rec[4])
            by_pattern = {}
            rec = [*stat_key, computed_at_ns, by_pattern]
            self._matches[rel] = rec
            self._dirty = True
        value = list(result)
        if by_pattern.get(pattern_key) == value:
            return
        if pattern_key not in by_pattern:
            if self._match_entries >= MATCH_ENTRY_LIMIT:
                if not by_pattern:
                    del self._matches[rel]
                return
            self._match_entries += 1
        by_pattern[pattern_key] = value
        self._dirty = True

    def save(self) -> None:
        """Persist when something changed: temp file in the same directory, then
        os.replace. OSError is swallowed; the last concurrent writer wins."""
        if not self._dirty or self._path is None:
            return
        self._drop_absent_idents()
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"schema": STORE_SCHEMA, "runs": self._runs, "idents": self._idents,
                 "matches": self._matches},
                sort_keys=True,
            )
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self._path)
            self._dirty = False
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _drop_absent_idents(self) -> None:
        """Forget identities and match records of files that no longer exist under the store's
        repo."""
        if self._path is None:
            return
        root = self._path.parent.parent.parent
        for rel in [r for r in self._idents if not os.path.lexists(root / r)]:
            del self._idents[rel]
        for rel in [r for r in self._matches if not os.path.lexists(root / r)]:
            rec = self._matches.pop(rel)
            if isinstance(rec, list) and len(rec) == 5 and isinstance(rec[4], dict):
                self._match_entries -= len(rec[4])
