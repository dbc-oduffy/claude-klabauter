"""coordinator_core.roadmap.census_store — on-disk store of the census's derived answers.

The store is derived machine state under `.coordinator-local/cache/`, rebuildable by running
the census, and never a source of truth. Every record is keyed by content (a run by its input
key, a match count by blob oid, the index fingerprints by the index checksum), so a lost, stale
or torn store costs a spawn or a read on the next run and never a wrong verdict.

Layout (schema 3): the directory `store_path(root).with_suffix("")` holds `root.marshal` (runs,
index fingerprints, screen memo, cursor, over-bar markers), `plans.marshal` (each plan's parsed
`census:` list under its stat) and one `p-<pattern_key>.marshal`
shard of `{oid: match_count}` per pattern. Each shard is a marshal of
`((STORE_SCHEMA, cache_tag), payload)`; a header mismatch or a corrupt shard loads empty.
Pattern shards load lazily and `save` rewrites dirty shards only.
"""

from __future__ import annotations

import marshal
import os
import re
import sys
import time
from pathlib import Path
from typing import Collection, Optional

from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session.machinery_paths import cache_dir

GENERATES = []

STORE_SCHEMA = 3

_ROOT_SHARD = "root.marshal"
_PLANS_SHARD = "plans.marshal"

#: `run_ms` of a run answered in-process: no spawn was timed, and a negative figure keeps it out
#: of the cost gate, the median and the over-bar test.
INPROC_RUN_MS = -1
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# path -> (size, mtime_ns, ino, payload): what this process last loaded or saved.
_SEEN: dict = {}
# pattern_key -> shard file name
_NAMES: dict = {}


def store_path(repo_root: Path) -> Path:
    """The legacy store file path; the shard directory is this path without its suffix."""
    return Path(cache_dir(str(main_worktree_root(Path(repo_root))))) / "census-store.json"


def _header() -> tuple:
    return (STORE_SCHEMA, sys.implementation.cache_tag)


def _stat_key(path: Path) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _read_shard(path: Path) -> Optional[dict]:
    """The shard's payload dict, or None when missing, corrupt or of another header."""
    try:
        data = path.read_bytes()
        header, payload = marshal.loads(data)
    except Exception:  # noqa: BLE001 — a bad shard is a cold shard
        return None
    if header != _header() or not isinstance(payload, dict):
        return None
    return payload


class CensusStore:
    """In-memory view of the shard directory; `save` persists the shards that changed."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._dir: Optional[Path] = path.with_suffix("") if path is not None else None
        self._root: dict = {}
        self._shards: dict = {}
        self._owned: set = set()
        self._dirty_shards: set = set()
        self._root_dirty = False
        self._root_owned = False
        self._plans: Optional[dict] = None
        self._plans_owned = False
        self._plans_dirty = False
        #: Count of `put_matched` calls that changed a shard; a pass that moved it made progress.
        self.matched_writes = 0
        self._load_ms = 0.0
        if self._dir is not None:
            self._root = self._fetch(self._dir / _ROOT_SHARD) or {}

    @classmethod
    def load(cls, repo_root: Path) -> "CensusStore":
        """The stored view, or an empty one when nothing readable of this schema is stored.
        Never raises. A schema-1 or schema-2 `census-store.json` is ignored."""
        try:
            return cls(store_path(repo_root))
        except Exception:  # noqa: BLE001 — an unresolvable root has no store
            return cls()

    def _fetch(self, path: Path) -> Optional[dict]:
        """The shard payload, reusing this process's copy when the file is unchanged."""
        started = time.process_time()
        try:
            key = _stat_key(path)
            if key is None:
                return None
            seen = _SEEN.get(path)
            if seen is not None and seen[:3] == key:
                return seen[3]
            payload = _read_shard(path)
            if payload is not None:
                _SEEN[path] = (*key, payload)
            return payload
        finally:
            self._load_ms += (time.process_time() - started) * 1000.0

    def load_cost_ms(self) -> int:
        """Process milliseconds spent reading shards."""
        return int(self._load_ms)

    # -- root shard ---------------------------------------------------------------------

    def _root_mut(self) -> dict:
        if not self._root_owned:
            self._root = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self._root.items()}
            self._root_owned = True
        self._root_dirty = True
        return self._root

    def _root_map(self, name: str) -> dict:
        v = self._root.get(name)
        return v if isinstance(v, dict) else {}

    def run(self, command: str) -> Optional[tuple]:
        """(key, (returncode, stdout, stderr), run_ms, key_ms) of the command's last
        completed run, or None."""
        rec = self._root_map("runs").get(command)
        try:
            key, ran, run_ms, key_ms = rec
            rc, out, err = ran
            return (key, (rc, out, err), run_ms, key_ms)
        except (TypeError, ValueError):
            return None

    def put_run(self, command: str, key: Optional[str], ran: tuple, run_ms: int,
                key_ms: Optional[int]) -> None:
        """Record a COMPLETED run, replacing the command's older record and clearing its
        over-bar marker. Never put a timeout, an OSError or a deadline row."""
        rc, out, err = ran
        rec = [key, [rc, out, err], run_ms, key_ms]
        if self._root_map("runs").get(command) != rec:
            self._root_mut().setdefault("runs", {})[command] = rec
        if command in self._root_map("over_bar"):
            del self._root_mut()["over_bar"][command]

    def median_run_ms(self) -> Optional[int]:
        """Median run_ms over every stored run, or None when the store holds none."""
        vals = sorted(
            r[2] for r in self._root_map("runs").values()
            if isinstance(r, list) and len(r) == 4 and isinstance(r[2], (int, float))
            and r[2] >= 0
        )
        if not vals:
            return None
        return int(vals[len(vals) // 2])

    def index_fps(self, checksum: str) -> Optional[dict]:
        """The operand fingerprints stored for this index checksum, or None."""
        rec = self._root.get("fps")
        if isinstance(rec, tuple) and len(rec) == 2 and rec[0] == checksum \
                and isinstance(rec[1], dict):
            return rec[1]
        return None

    def put_index_fps(self, checksum: str, fps: dict) -> None:
        """Keep the fingerprints for `checksum`, dropping any other checksum's."""
        if self.index_fps(checksum) != fps:
            self._root_mut()["fps"] = (checksum, dict(fps))

    def screened(self, command: str) -> Optional[tuple]:
        """(refusal,) when the command's screen verdict is memoised, else None."""
        screens = self._root_map("screens")
        if command in screens:
            return (screens[command],)
        return None

    def put_screened(self, command: str, refusal: Optional[str]) -> None:
        screens = self._root_map("screens")
        if command not in screens or screens[command] != refusal:
            self._root_mut().setdefault("screens", {})[command] = refusal

    def declined(self, checksum: str) -> frozenset:
        """Commands the in-process evaluator declined at the index with this checksum."""
        rec = self._root.get("declined")
        if isinstance(rec, tuple) and len(rec) == 2 and rec[0] == checksum \
                and isinstance(rec[1], frozenset):
            return rec[1]
        return frozenset()

    def put_declined(self, checksum: str, commands: Collection[str]) -> None:
        """Keep the declined set for `checksum`, dropping any other checksum's."""
        fresh = frozenset(commands)
        if self.declined(checksum) != fresh:
            self._root_mut()["declined"] = (checksum, fresh)

    def cursor(self) -> Optional[str]:
        """The command the last pass stopped before, or None."""
        v = self._root.get("cursor")
        return v if isinstance(v, str) else None

    def put_cursor(self, command: Optional[str]) -> None:
        if self.cursor() != command:
            self._root_mut()["cursor"] = command

    def over_bar(self, command: str) -> Optional[int]:
        """The ms a spawn of this command exceeded the bar by, or None."""
        v = self._root_map("over_bar").get(command)
        return v if isinstance(v, int) else None

    def put_over_bar(self, command: str, ms: int) -> None:
        if self._root_map("over_bar").get(command) != ms:
            self._root_mut().setdefault("over_bar", {})[command] = ms

    # -- pattern shards -----------------------------------------------------------------

    def _shard(self, pattern_key: str) -> dict:
        name = self._name(pattern_key)
        shard = self._shards.get(name)
        if shard is None:
            shard = {}
            if self._dir is not None:
                got = self._fetch(self._dir / name)
                if isinstance(got, dict):
                    shard = got
            self._shards[name] = shard
        return shard

    @staticmethod
    def _name(pattern_key: str) -> str:
        name = _NAMES.get(pattern_key)
        if name is None:
            name = _NAMES[pattern_key] = f"p-{_UNSAFE.sub('_', pattern_key)}.marshal"
        return name

    def _shard_mut(self, pattern_key: str) -> dict:
        shard = self._shard(pattern_key)
        name = self._name(pattern_key)
        if name not in self._owned:
            shard = dict(shard)
            self._shards[name] = shard
            self._owned.add(name)
        return shard

    def matched(self, pattern_key: str, oid: str) -> Optional[int]:
        """The stored match count of blob `oid` for the pattern, or None when unchecked."""
        v = self._shard(pattern_key).get(oid)
        return v if isinstance(v, int) else None

    def put_matched(self, pattern_key: str, counts: dict) -> None:
        """Record `{oid: match_count}` for the pattern; only this pattern's shard is dirtied."""
        shard = self._shard(pattern_key)
        if all(shard.get(o) == c for o, c in counts.items()):
            return
        self._shard_mut(pattern_key).update(counts)
        self._dirty_shards.add(self._name(pattern_key))
        self.matched_writes += 1

    def gc_oids(self, live: Collection[str]) -> None:
        """Drop match records of oids not in `live`, from the shards loaded in this process."""
        keep = live if isinstance(live, (set, frozenset)) else set(live)
        for name in list(self._shards):
            shard = self._shards[name]
            dead = [o for o in shard if o not in keep]
            if not dead:
                continue
            if name not in self._owned:
                shard = dict(shard)
                self._shards[name] = shard
                self._owned.add(name)
            for o in dead:
                del shard[o]
            self._dirty_shards.add(name)

    # -- parsed plan census -------------------------------------------------------------

    def _plans_map(self) -> dict:
        if self._plans is None:
            got = self._fetch(self._dir / _PLANS_SHARD) if self._dir is not None else None
            self._plans = got if isinstance(got, dict) else {}
        return self._plans

    def parsed(self, rel: str, st: os.stat_result) -> Optional[list]:
        """The `census:` list parsed from plan `rel` when its (size, mtime_ns, inode) still equal
        `st`, else None. The caller must not mutate the list."""
        rec = self._plans_map().get(rel)
        if (isinstance(rec, tuple) and len(rec) == 4
                and rec[:3] == (st.st_size, st.st_mtime_ns, st.st_ino)
                and isinstance(rec[3], list)):
            return rec[3]
        return None

    def put_parsed(self, rel: str, st: os.stat_result, census: list, computed_at_ns: int) -> None:
        """Keep a plan's parsed census. A whole-second mtime read within two seconds of its
        stamp could still be rewritten inside that tick, so it is not kept; neither is a list
        marshal cannot hold."""
        if st.st_mtime_ns % 1_000_000_000 == 0 and computed_at_ns - st.st_mtime_ns < 2_000_000_000:
            return
        try:
            marshal.dumps(census)
        except ValueError:
            return
        plans = self._plans_map()
        if not self._plans_owned:
            plans = self._plans = dict(plans)
            self._plans_owned = True
        plans[rel] = (st.st_size, st.st_mtime_ns, st.st_ino, census)
        self._plans_dirty = True

    # -- persistence --------------------------------------------------------------------

    def _write(self, path: Path, payload: dict) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(marshal.dumps((_header(), payload)))
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        key = _stat_key(path)
        if key is not None:
            _SEEN[path] = (*key, payload)

    def save(self) -> None:
        """Rewrite the dirty shards (temp file, then os.replace). OSError is swallowed; the
        last concurrent writer wins. The first save removes a schema-1 or schema-2
        `census-store.json`."""
        if self._dir is None or not (self._root_dirty or self._dirty_shards or self._plans_dirty):
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            for name in sorted(self._dirty_shards):
                self._write(self._dir / name, self._shards[name])
            self._dirty_shards.clear()
            self._owned.clear()
            if self._plans_dirty:
                self._write(self._dir / _PLANS_SHARD, self._plans)
                self._plans_dirty = False
                self._plans_owned = False
            if self._root_dirty or not (self._dir / _ROOT_SHARD).exists():
                self._write(self._dir / _ROOT_SHARD, self._root)
                self._root_dirty = False
                self._root_owned = False
            if self._path is not None:
                self._path.unlink(missing_ok=True)
        except OSError:
            pass
