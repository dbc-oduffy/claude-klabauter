"""coordinator_core.ops.fleet._memo_owning_box -- which box owns a target repo's live EM.

Read-only, zero spawns: one listdir plus one small read per Unimatrix session entry. A session
entry counts under the same terms as `unimatrix-roster.entry_listed`; the roster itself is not
imported. The cwd->repo match is the roster's own rule (last component of the "/"-normalised
cwd), applied to `repo_path.name`.

Entrypoint: resolve_owning_box(repo_path, sessions_dir=None) -> OwningBox.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, NamedTuple, Optional

# Mirrors unimatrix-roster FAR_SEEN_MAX_AGE_SECONDS (5 * POLL_SECONDS, POLL_SECONDS = 30); pinned by test.
FAR_SEEN_MAX_AGE_SECONDS = 150
_FAR_SEEN_FIELD = "unimatrixFarSeen"
_FAR_SOCKET_FIELD = "macSocketPath"
_REMOTE_FIELD = "unimatrixRemote"

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
_MODULES: dict = {}


class OwningBox(NamedTuple):
    """Verdict: `this_box` / `other_box` / `unknown`; `box` and `session` name the matched live EM row."""

    state: str
    box: Optional[str]
    session: Optional[str]


def _load(key: str, filename: str) -> Any:
    """A `coordinator/bin` module loaded by path, cached once per process."""
    mod = _MODULES.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(f"memo_owning_box_{key}", _BIN / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[key] = mod
    return mod


def _contract() -> Any:
    return _load("contract", "unimatrix-peer-contract.py")


def _liveness() -> Any:
    return _load("liveness", "unimatrix-liveness.py")


def _entries(sessions_dir: str) -> list:
    """Every readable dict entry in `sessions_dir`, in filename order; [] when unreadable."""
    try:
        files = sorted(os.listdir(sessions_dir))
    except OSError:
        return []
    found = []
    for fname in files:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, fname), encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def _listed(entry: dict, liveness: Any) -> bool:
    """Every term of unimatrix-roster.entry_listed."""
    if entry.get("unimatrixStale") or not liveness.entry_live(entry):
        return False
    if entry.get(_FAR_SOCKET_FIELD) and not liveness.supervisor_live(
        {"pid": entry.get("unimatrixRosterPid"), "procStart": entry.get("unimatrixRosterProcStart")}
    ):
        return False
    if (entry.get(_FAR_SOCKET_FIELD) or entry.get(_REMOTE_FIELD)) and not liveness.stamp_fresh(
        entry, _FAR_SEEN_FIELD, FAR_SEEN_MAX_AGE_SECONDS
    ):
        return False
    return not (liveness.agy_face_unbacked(entry) or liveness.codex_face_unbacked(entry))


def _repo_component(cwd: Any) -> str:
    """Last "/" component of `cwd` after "\\" -> "/" and trailing "/" strip, normcased."""
    text = str(cwd or "").replace("\\", "/").rstrip("/")
    return os.path.normcase(text.rsplit("/", 1)[-1])


def resolve_owning_box(repo_path: Path, *, sessions_dir: Optional[str] = None) -> OwningBox:
    """The box holding a live session whose cwd basename is `repo_path.name`.

    A live match on this box wins; else a live match on another box (first by name); else
    `unknown`. Never raises: an unreadable dir, a bad entry or a failing contract load reads
    `unknown` / is skipped.
    """
    try:
        wanted = os.path.normcase(Path(repo_path).name)
        if not wanted:
            return OwningBox("unknown", None, None)
        sess = sessions_dir if sessions_dir is not None else os.path.expanduser("~/.claude/sessions")
        contract = _contract()
        liveness = _liveness()
        local = contract.local_box()
    except Exception:
        return OwningBox("unknown", None, None)
    here: list = []
    there: list = []
    for entry in _entries(sess):
        try:
            if _repo_component(entry.get("cwd")) != wanted or not _listed(entry, liveness):
                continue
            row = contract.agent_row(entry, local)
            box = row["box"]
            mine = row.get("this_box", box == local)
            item = (str(row.get("name") or ""), str(box))
        except Exception:
            continue
        (here if mine else there).append(item)
    if here:
        name, box = sorted(here)[0]
        return OwningBox("this_box", box, name)
    if there:
        name, box = sorted(there)[0]
        return OwningBox("other_box", box, name)
    return OwningBox("unknown", None, None)
