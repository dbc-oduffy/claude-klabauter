"""The halt-resume record: halt-time work stored beside `halted.json`, plus the bounded-read seam.

`resume.json` carries what a halted run already determined (composed script, hoisted fragments,
sizing gate verdict, landed-state sha) with a fingerprint of its inputs. A reader checks the
fingerprint and never recomputes. No git spawn and no listing here (the caller resolves the halted run first); every read is deadline-bound
through `bounded_read`, which a stalled disk cannot hold past its deadline.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Optional, Union

from coordinator_core.atomic_replace import atomic_write_bytes

RESUME_FILE = "resume.json"
BUILDING_FILE = "resume.building"
RECOMPOSE_TTL_S = 300.0
SCHEMA = "halt-resume-record/1"
RECORD_FIELDS = ("composed_script_rel", "hoisted_fragments", "gate_verdict", "landed_state_sha")


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return False


MISSING = _Sentinel("MISSING")
STALLED = _Sentinel("STALLED")

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _read_file(path: Path) -> bytes:
    return path.read_bytes()


def bounded_read(path: Union[str, Path], deadline_ms: float) -> Union[bytes, _Sentinel]:
    """Bytes of `path`, `MISSING` when absent or unreadable, `STALLED` when the read outlives
    `deadline_ms`. The read runs on a daemon thread left to finish after a stall; at most one read
    per path is in flight, so a second call while one is outstanding returns `STALLED` at once."""
    key = str(path)
    with _inflight_lock:
        if key in _inflight:
            return STALLED
        _inflight.add(key)
    box: list = []
    done = threading.Event()

    def work() -> None:
        try:
            box.append(_read_file(Path(key)))
        except OSError:
            box.append(MISSING)
        except BaseException:
            box.append(MISSING)
        finally:
            with _inflight_lock:
                _inflight.discard(key)
            done.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except BaseException:
        with _inflight_lock:
            _inflight.discard(key)
        return STALLED
    if not done.wait(max(deadline_ms, 0) / 1000.0):
        return STALLED
    return box[0]


HINT_DIR = ".coordinator-local/halt-resume-hint"
HINT_READ_DEADLINE_MS = 50.0


def _hint_path(root: Union[str, Path], sizing_rel: str) -> Path:
    key = hashlib.sha256(sizing_rel.replace("\\", "/").encode("utf-8")).hexdigest()[:20]
    return Path(root) / HINT_DIR / f"{key}.txt"


def write_hint(root: Union[str, Path], sizing_rel: str, run_id: str) -> None:
    """Remember the halted run a refire of `sizing_rel` resumes, so a refire whose halted-run lookup stalls can still name it."""
    path = _hint_path(root, sizing_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, run_id.encode("utf-8"))


def read_hint(root: Union[str, Path], sizing_rel: str) -> Optional[str]:
    """The run id `write_hint` stored for `sizing_rel`; None when absent, unreadable, unsafe or stalled past `HINT_READ_DEADLINE_MS`."""
    raw = bounded_read(_hint_path(root, sizing_rel), HINT_READ_DEADLINE_MS)
    if not isinstance(raw, bytes):
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    return text if text and all(c.isalnum() or c in "-_" for c in text) else None


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def fingerprint_of(root: Union[str, Path], plan_rel: Optional[str], sizing_rel: Optional[str], halt_head: Optional[str]) -> dict:
    """The record's validity key: sha256 of the plan and sizing file bytes (None for an absent or
    unnamed file) and the HEAD at halt. Hashes bytes; spawns nothing."""
    root = Path(root)
    return {
        "plan_sha256": _sha256_file(root / plan_rel) if plan_rel else None,
        "sizing_sha256": _sha256_file(root / sizing_rel) if sizing_rel else None,
        "halt_head": halt_head,
    }


def fingerprint_for_run(
    root: Union[str, Path],
    record_dir: Union[str, Path],
    sizing_rel: Optional[str],
    plan_rel: Optional[str],
    landed_state_sha: Union[str, None, _Sentinel] = MISSING,
) -> dict:
    """The one fingerprint both sides compute: `plan_rel` is the resolved resume plan (the producer
    derives it, the record stores it as `resume_plan_rel`, the reader passes the resolution it
    wants served) and the halt HEAD is `halted_run_base(record_dir)`, stable across peer commits.
    `landed_state_sha` joins the key only when given (the reader never computes it: that costs a
    git spawn). Spawns nothing."""
    from coordinator_core.ops.dispatch_emit.halted_run import halted_run_base

    fp = fingerprint_of(root, plan_rel, sizing_rel, halted_run_base(Path(record_dir)))
    fp["resume_plan_rel"] = plan_rel
    if landed_state_sha is not MISSING:
        fp["landed_state_sha"] = landed_state_sha
    return fp


def resolve_fingerprint_plan(root: Union[str, Path], record_dir: Union[str, Path], sizing_rel: str) -> Optional[str]:
    """The plan both sides hash, resolved from the sizing and the halt marker alone (never from the
    record): the sizing's `plan:` edge, else `s_plan_path` at arm S, else the marker's halt plan.
    Spawns nothing; a sizing that will not load falls through to the marker's plan."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD
    from coordinator_core.ops.dispatch_emit import sizing_fire

    try:
        sizing = sizing_fire.load_sizing(Path(root), sizing_rel)
    except (OSError, ValueError, sizing_fire.SizingFireRefused):
        sizing = {}
    edge = sizing.get("plan")
    if isinstance(edge, str) and edge.strip():
        return edge
    try:
        if sizing_fire.resolve_arm(sizing) == sizing_fire.ARM_S:
            return sizing_fire.s_plan_path(sizing_rel)
    except sizing_fire.SizingFireRefused:
        pass
    marker = halted_run._read(Path(record_dir))
    plan = marker.get(HALT_PLAN_FIELD) if marker else None
    return plan if isinstance(plan, str) and plan else None


def claim_recompose(record_dir: Union[str, Path], now: float, ttl_s: float) -> bool:
    """True when the caller may spawn the recompose: it writes `resume.building`, which a spawn
    younger than `ttl_s` leaves standing so a refire does not stack a second one. The builder
    removes it when it ends."""
    stamp = Path(record_dir) / BUILDING_FILE
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        with stamp.open("x", encoding="utf-8") as fh:
            fh.write(repr(now))
        return True
    except FileExistsError:
        pass
    except OSError:
        return True
    try:
        if now - float(stamp.read_text(encoding="utf-8")) < ttl_s:
            return False
    except (OSError, ValueError):
        pass
    try:
        stamp.write_text(repr(now), encoding="utf-8")
    except OSError:
        pass
    return True


def release_recompose(record_dir: Union[str, Path]) -> None:
    try:
        (Path(record_dir) / BUILDING_FILE).unlink()
    except OSError:
        pass


def build_record(
    *,
    composed_script_rel: Optional[str],
    hoisted_fragments,
    gate_verdict,
    landed_state_sha: Optional[str],
    fingerprint: dict,
    resume_plan_rel: Optional[str] = None,
    retry_not_before: Optional[float] = None,
) -> dict:
    """A record dict in the on-disk shape; `retry_not_before` is set only on a refused build."""
    return {
        "retry_not_before": retry_not_before,
        "schema": SCHEMA,
        "resume_plan_rel": resume_plan_rel,
        "composed_script_rel": composed_script_rel,
        "hoisted_fragments": hoisted_fragments,
        "gate_verdict": gate_verdict,
        "landed_state_sha": landed_state_sha,
        "fingerprint": dict(fingerprint),
    }


def write(record_dir: Union[str, Path], record: dict) -> Path:
    """Atomically write `resume.json` into `record_dir` (created if absent); return its path."""
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    target = record_dir / RESUME_FILE
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return target


def read(record_dir: Union[str, Path], deadline_ms: float) -> Union[dict, _Sentinel]:
    """The record, `MISSING` (absent, corrupt or wrong schema), or `STALLED` (read past deadline)."""
    raw = bounded_read(Path(record_dir) / RESUME_FILE, deadline_ms)
    if raw is MISSING or raw is STALLED:
        return raw
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return MISSING
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or not isinstance(record.get("fingerprint"), dict):
        return MISSING
    return record


def matches(record: dict, fingerprint: dict) -> bool:
    """True when the record's fingerprint equals `fingerprint` on plan sha, sizing sha and halt HEAD,
    and also on `resume_plan_rel` and `landed_state_sha` when `fingerprint` carries them. A None
    halt HEAD never matches (nothing pins the record to a halt), and the record's own plan and
    landed-state fields must agree with its stored fingerprint."""
    stored = record.get("fingerprint") or {}
    if stored.get("halt_head") is None or fingerprint.get("halt_head") is None:
        return False
    keys = ["plan_sha256", "sizing_sha256", "halt_head"]
    keys += [k for k in ("resume_plan_rel", "landed_state_sha") if k in fingerprint]
    if not all(stored.get(k) == fingerprint.get(k) for k in keys):
        return False
    if "resume_plan_rel" in stored and stored["resume_plan_rel"] != record.get("resume_plan_rel"):
        return False
    if "landed_state_sha" in stored and stored["landed_state_sha"] != record.get("landed_state_sha"):
        return False
    return True


def in_backoff(record: dict, now: float) -> bool:
    """True while a refused build's `retry_not_before` (epoch seconds) is still ahead of `now`: the
    caller serves the stored refusal and spawns no recompose."""
    until = record.get("retry_not_before")
    return isinstance(until, (int, float)) and not isinstance(until, bool) and now < until
