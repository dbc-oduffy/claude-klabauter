"""
coordinator_core.ops.review_reverify — registers ``review.reverify``: lists the product files a
review stage changed since ``review.prep`` froze them, and inverts the applied findings failing tests implicate.

Params: ``base`` (str), ``slice_prefix`` (str), optional ``session_id``, optional ``failing``
(list of pytest node ids), optional ``sidecars`` (list of review sidecar paths), optional
``failing_before`` (list of node ids), optional ``failing_at_base`` (list of node ids), optional
``probe`` (``"open"`` or ``"close"``), optional ``run_id`` and ``plan_path``: a probe open refuses a
path another halted run withholds, and a revert keeps one (``land_run.foreign_halted_withheld``; an
empty ``run_id`` owns nothing, so every halted run is foreign). Keying scope: show_top.

Reads the write-once pre-review snapshot ``review.prep`` left at
``<share>/<slice_prefix>.pre-review/`` (``manifest.json`` plus one blob per sha256). By path
convention only; no ``review_prep`` import.

Baseline: each manifest path is measured against its snapshot blob, unless a commit moved it
since the snapshot (its leaf differs between the anchor commit's tree and HEAD's; the anchor is the
manifest ``head``, else its ``base``). A moved path's baseline is HEAD's blob, so a revert never
restores bytes older than HEAD. With no HEAD the baseline is always the snapshot. When HEAD or the
anchor tree is unreadable, revert mode returns ``error`` and writes nothing (fail closed); list mode
keeps the snapshot baseline and adds ``"head_unreadable": true``.

List mode (no ``failing``): ``{"edited", "created", "foreign", "outside_declared", "scope_files", "owned",
"tripwires", "tripwires_not_selected", "notice", "snapshot", "error"}`` (``scope_files``: the snapshot manifest's
files, the prep's frozen review scope),
plus ``head_unreadable`` when applicable. When review changed nothing (``edited`` and ``created`` both
empty), they are derived from the run diff instead: manifest paths whose bytes differ from the ``base``
commit's blob (``edited``) or that base lacks (``created``), using HEAD instead for paths moved
since the snapshot, with ``"derived_from": "run_diff"``.
``outside_declared`` is the sorted tracked paths (tracked in the snapshot base commit) the session's review stage wrote
that the manifest does not name, no other live run declared, written after the snapshot by an agent this run's own
record names: a review fix outside the declared writes. Spawn-free (the same session write record as ``created``);
revert mode treats them as ``foreign``, so they are never reverted.
``owned`` is the touched tests plus the stem-mapped or importing tests of the edited paths (at most
``reverify_scope._SUITE_BOUND`` beyond the touched ones). ``tripwires`` are the other tracked test modules that
import an edited module, then those whose source holds a string literal naming an edited or created path,
same package first, cut at ``reverify_scope.TRIPWIRE_BOUND`` and never holding an ``owned`` file;
``tripwires_not_selected`` is the rest (a coverage gap to report as not run), ``notice`` says what was cut. All
are empty for an empty edit set. When the tracked-file index cannot
be read, ``owned`` and ``tripwires`` are empty and ``error`` is ``"tripwire scan unavailable: <reason>"``; the other
fields stay filled. ``edited`` is every
manifest path whose bytes now differ from its baseline (a missing file counts); ``created`` is every path the
session's review stage wrote that the manifest and its declared ``paths`` do not name. ``foreign``
is such a path under ``scratch/``, or one another live run declared that is tracked in the snapshot's base commit, or that predates the snapshot: the session's write record
spans concurrent runs, so it is reported, never reverted or removed. In revert mode ``foreign`` lists
the failing node ids whose test file is foreign, plus those implicating none of this run's paths whose test file, imported module or named path another live run declared.

Revert mode (``failing`` given, no ``probe``): ``{"reverted", "unattributed", "pre_existing",
"sibling_owned", "kept", "record", "error"}``. ``sibling_owned`` is the unattributed node ids whose
caller-supplied ``cause_files`` (``{node id: [paths]}``) are all outside the run footprint (manifest
plus created paths) and all withheld by a readable foreign halt record; an unreadable record, an
in-footprint cause file, or no cause files leaves the node ``unattributed``. ``foreign_red``
(``{node id: [culprit paths]}``) carries the remaining nodes whose every cause file is outside the
footprint and dirty versus HEAD, whether or not a halt record holds it (unreadable record excepted): the
failure is shared-tree contamination, evidence rather than a blocker; so is a node with no cause file in
a test file outside the footprint that reaches no footprint path (``_reaches``: imports followed
transitively, string literals, ``conftest.py``) and whose failure text names none, and so is a node with
no cause file whose test file reaches a path another halted run withholds and no footprint path
(``_withheld_reach``; ``foreign_red_runs`` maps it to the holding run ids); a node green at the run base
(``failing_at_base`` supplied) classes there only when every such cause file is untracked. Absent ``cause_files``
they are derived (``_derive_cause_files``); a bare file name resolving nowhere falls back to the sole
dirty tracked path of that basename. A failing node id's
test file implicates itself when edited or created, and every edited ``.py`` module it imports, plus
every edited or created path a string literal in it names. Trap: a tripwire naming several edited paths implicates all of them. An implicated edited
path is rewritten as its current bytes with each applied finding on it inverted (the ledger row's
``after`` replaced by its ``before``; ``action`` ``"reverted-findings"``, ``source`` ``"findings"``);
every other byte, executor hunks included, is untouched. A finding that cannot be inverted, and an
implicated path with no applied finding, go to ``kept`` with a reason and write nothing. A created
path is removed by moving its bytes to ``<snapshot>/removed/<path>`` (``kept_at`` in the entry),
never unlinked; one tracked at HEAD is restored to HEAD's bytes only if an applied finding names it
(no ``sidecars`` means none does). A created path no run finding names is another run's dirt: it
goes to ``kept`` and its tests to ``unattributed``, bytes untouched. Every restore or findings
rewrite first copies the current bytes to ``<snapshot>/reverted/<path>``; the entry's ``kept_at`` names that stash.
A created path whose writing agent also wrote a foreign path, or wrote nothing in this run's
surface (manifest paths, the snapshot share dir, a path naming the slice prefix), is foreign. Before
anything is written, the revert (tripwire-named paths included) refuses (``error``, nothing written,
the whole call) when it would remove a top-level symbol that a committed file in the run's surface
(manifest paths plus paths changed anchor..HEAD) still uses. An id implicating nothing is
``unattributed`` and reverts nothing. A path an open ``kept.json`` holds (``kept_work.held_paths``)
is never restored, unlinked or inverted: it lands in ``kept`` as ``kept-work-held:<run_id>``. A failing id
in an edited or created test file whose ``failing_output`` is pytest's ``not found:`` was renamed or removed
by the run: it is ``renamed_node`` (never implicated, reverted or blocking) and its file is in ``recollect``
(speculative: no engine consumer yet; the caller reruns that file). Every
rewritten or removed path is passed to ``declare_write``. The record ``reverify.json`` merges across calls. Revert refuses while a probe is open.

Probe: ``"probe":"open"`` (with ``failing`` and ``sidecars``) writes the finding-inverted bytes of
each implicated edited path into the tree, stashing the post-review bytes beside the snapshot, and
names the ``rerun`` ids for the test-runner; a path an open ``kept.json`` holds is not probed.
``"probe":"close"`` puts the post-review bytes back.
An id in ``failing_at_base`` (red at the run base with none of the run's edits applied) is
``pre_existing`` whatever the probe ran: it is never reverted and never ``unattributed``. An absent
or empty list classes nothing ``pre_existing``. An id in ``failing_before`` that a closed probe
re-ran, but not in ``failing_at_base``, implicates no review edit: its hunks are kept and it is
``unattributed``. An id this record already lists in a ``reverted`` entry's ``tests`` and that is
passed again in revert mode still fails after its revert: it is ``pre_existing``, never reverted
twice. An id no closed probe re-ran is never honoured as ``failing_before``.
Optional ``base_export_path`` names a saved baseline_export output: its ``FAILED``/``ERROR`` ids join
``failing_at_base`` when its trailing JSON line has ``export`` "ok" and ``runner_exit`` 0, 1 or 4. A missing or invalid file sets ``base_unmeasured`` and moves the
``unattributed`` ids to ``unmeasured``: not pre_existing (they still block), not the run's own. File read only.
Optional ``export_failing_path`` names a saved ``clean_overlay_verify --export-run`` output of the failing ids
(HEAD plus the run's declared paths), judged by the ``base_export_path`` validity rule: a failing id absent
from its reds (green with peer dirt removed) and not otherwise skipped lands in ``peer_dirt``, never reverted
or ``unattributed``. A missing or invalid file sets ``export_unmeasured`` and classes nothing. ``peer_dirt``
is merged into the record.
Mirror-only red (revert mode): a failing id with an empty ``failing_output`` message (absent ``failing_output``
counts as empty), or one in optional ``leg_passed`` (ids the run's own test leg passed), that passes when
``_rerun_alone`` runs it in the real tree is a mirror artefact, not a verdict on the run: it lands in
``mirror_only_red`` and, as ``{node: [its test file]}``, in ``foreign_red``, so it never blocks. A suspect id
that is also red in the tree, or whose rerun could not run, classes nothing and keeps blocking.
A failing id in a write-set test file (manifest or edited, not created) that no base verdict covers
(not in ``failing_at_base``, not ruled or recorded, and no ``base_export_path`` named, even an unreadable one) is measured at the run
base in ONE batched export run (``reverify_base_measure.measure_ids``, needs a 40-hex base): red at base
joins ``failing_at_base`` (so ``pre_existing``), green stays the run's own, and an unmeasurable base keeps
the owned verdict and sets ``base_measure_error``. The reply carries ``base_measured_here``; the record
keeps the verdicts under ``base_measured``.
Optional ``run_key`` names a run whose ``owned_red.jsonl`` ids join ``failing_at_base``: a failing id
recorded there (and not in ``failing_at_base``) is reported in the ``owned_red`` bucket, never reverted or
``unattributed``. The same ``run_key`` also names the EM's pre-existing ruling (``ruled_pre_existing``:
a ``pre_existing_ruled.jsonl`` row or a live revise note naming the id): a ruled failing id joins
``failing_at_base`` and lands in ``pre_existing``, and the reply's ``ruled_pre_existing`` maps each such id
to its reason. A ruling never classes an id outside ``failing``.

Negative-spec: no test execution and no git verb of its own; the only spawn is the ``git cat-file``
inside ``land_run.foreign_halted_withheld`` (up to one per still-held halted run), reached on a probe
open and a revert. A refusal (no manifest, a
path escaping the worktree, an unreadable or corrupt blob) is returned as ``error`` with nothing
written, never raised. Attribution is by ledger finding, never by snapshot diff: a write-once
snapshot predates a refire's executor writes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.commenting import authorship
from coordinator_core.commenting.added import added_comments
from coordinator_core.commenting.allow import strip_marker
from coordinator_core.commenting.authorship import agent_added_comments
from coordinator_core.commenting.windowed import OVERSIZE_CHARS, windowed_added_comments
from coordinator_core.ops.ceremony import governed_by
from coordinator_core.git import git_objects, git_state
from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.index_write import (
    DROP_IF_PHANTOM_INTENT_TO_ADD,
    IndexWriteError,
    splice_index,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops import review_reverify_probe as probe_io
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.red_attribution import names_own_path_in_output
from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused
from coordinator_core.ops.owned_red import read_owned_red
from coordinator_core.ops.pre_existing_ruling import ruled_pre_existing
from coordinator_core.ops.pre_review_dir import pre_review_dir
from coordinator_core.ops.review_reverify_findings import applied_rows, invert
from coordinator_core.ops.review_test_blocker import NOT_FOUND_RE
from coordinator_core.ops.review_tripwires import (
    TripwireScanUnavailable,
    _tracked_test_bytes as tracked_test_bytes,
    names_in,
    scan_tokens,
    tree_scanners,
    walked_created,
)
from coordinator_core.ops.reverify_callers import callers_of, removed_symbols
from coordinator_core.ops.reverify_scope import select_for_paths
from coordinator_core.pyimports import collect_imports, module_name_for_file, resolve_relative_module
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.session.touch_record import compute_content_hash

MUTATES = [
    ".coordinator-local/subagent-share/*/*.pre-review/reverify.json",
    ".coordinator-local/subagent-share/*/*.pre-review/probe.json",
    ".coordinator-local/subagent-share/*/*.pre-review/probe-closed.json",
    ".coordinator-local/subagent-share/*/*.pre-review/probe/*",
    "<review-edited product files named by the snapshot manifest>",
]

_MANIFEST = "manifest.json"
_RECORD = "reverify.json"
_REMOVED = "removed"
_STASHED = "reverted"
_MACHINERY_PREFIXES = (".coordinator-local/", "state/")


def _fail(message: str, revert: bool, probe: Optional[str] = None) -> dict:
    if probe == "open":
        return {"probed": [], "rerun": [], "record": None, "error": message}
    if probe == "close":
        return {"restored": [], "rerun": [], "error": message}
    if revert:
        return {
            "reverted": [], "unattributed": [], "pre_existing": [], "owned_red": [], "peer_dirt": [],
            "kept": [], "flaky": [], "record": None, "error": message,
        }
    return {"edited": [], "created": [], "snapshot": None, "error": message}


#: Wall-clock cap on the one isolated rerun of the implicated reds; past it nothing is vouched flaky.
_FLAKY_RERUN_BUDGET_S = 120.0


def _rerun_alone(repo_root: Path, ids: List[str]) -> List[str]:
    """The ``ids`` that pass when re-run once, together and alone, on the current tree (a red that does
    not reproduce is transient, never a revert's cause). Only ids whose test file exists in the tree
    are run; any runner fault, timeout or unparsable result vouches for nothing, so a revert proceeds
    as before."""
    runnable = [n for n in ids if (repo_root / _node_file(n)).is_file()]
    if not runnable:
        return []
    from coordinator_core.ops import test_leg_run

    try:
        rc, lines = test_leg_run._pytest(runnable, str(repo_root), _FLAKY_RERUN_BUDGET_S)
    except Exception:  # noqa: BLE001 -- unmeasured is not flaky
        return []
    if rc not in (0, 1):
        return []
    again = set(test_leg_run._reds(lines))
    return sorted(n for n in runnable if n not in again)


#: Wall-clock cap on the one batched base export measuring the write-set reds nobody measured.
_BASE_MEASURE_BUDGET_S = 240.0


def _measure_at_base(
    repo_root: Path, manifest: dict, base: str, prior: dict, nodes: List[str]
) -> Tuple[Set[str], Set[str], Optional[str]]:
    """``(red, green, error)`` for ``nodes`` at the run base: ONE batched export run for all of them, ids
    already in the record's ``base_measured`` answered from it. ``error`` names why nothing was measured
    (no 40-hex base, a fault, a timeout); the nodes then keep their owned verdict. Never raises."""
    if not nodes:
        return set(), set(), None
    saved = prior.get("base_measured")
    saved = saved if isinstance(saved, dict) else {}
    red = {n for n in nodes if n in set(saved.get("red") or [])}
    green = {n for n in nodes if n in set(saved.get("green") or [])}
    todo = [n for n in nodes if n not in red and n not in green]
    if not todo:
        return red, green, None
    sha = next((v for v in (manifest.get("base"), base) if _is_sha(v)), None)
    if sha is None:
        return red, green, "no 40-hex run base to measure at"
    from coordinator_core.ops import reverify_base_measure

    new_red, new_green, error = reverify_base_measure.measure_ids(sha, todo, str(repo_root), _BASE_MEASURE_BUDGET_S)
    return red | new_red, green | new_green, error


def _norm(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _rel(path: str, repo_root: Path) -> str:
    """Repo-relative POSIX form; an absolute path under the root is relativised."""
    text = _norm(path)
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        try:
            return Path(text).resolve().relative_to(repo_root.resolve()).as_posix()
        except (ValueError, OSError):
            return text
    return text


def _node_file(node_id: str) -> str:
    return _norm(node_id.split("::", 1)[0])


def _read_manifest(snapshot_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((snapshot_dir / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def _since(recorded_at: object) -> Optional[float]:
    if not isinstance(recorded_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _created(
    repo_root: Path, session: str, manifest: dict, share: Path, slice_prefix: str = "",
    outside_sink: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """(created, foreign). The session's write record spans every run the session fired, so a
    path is this run's only when no other live run declared it, it was written after the
    snapshot, its writing agent wrote no foreign path, and that agent is named by this run's own
    record (``_run_agents``); anything else, an unattributable write included, is foreign and is
    never reverted. An ``outside_declared`` path is one tracked at the snapshot base that the
    manifest does not name, no sibling declared, written after the snapshot by this run's own
    agent: a review fix outside the declared writes, never created or reverted here. It extends
    ``outside_sink`` when given, else counts as ``foreign``."""
    from coordinator_core.ops.dispatch_emit import review_edits

    since = _since(manifest.get("recorded_at"))
    writes = review_edits.review_writes(repo_root, session, since)
    if not writes:
        return [], []
    sibling = {_norm(p) for p in review_edits.live_sibling_declared(repo_root, None, slice_prefix)}
    tracked = _tracked_at_base(repo_root, manifest.get("base"), writes)
    known: Set[str] = {_norm(p) for p in manifest["files"]}
    known.update(_norm(p) for p in (manifest.get("paths") or []) if isinstance(p, str))
    share_rel = _rel(str(share), repo_root)
    out: Set[str] = set()
    foreign: Set[str] = set()
    foreign_agents: Set[str] = set()
    candidates: List[Tuple[str, str]] = []
    tracked_candidates: List[Tuple[str, str]] = []
    for raw in writes:
        rel = _rel(raw, repo_root)
        if rel in known or rel.startswith(_MACHINERY_PREFIXES) or rel.startswith(share_rel + "/"):
            continue
        target = repo_root / rel
        if not target.is_file():
            continue
        if rel.startswith("scratch/"):
            foreign.add(rel)
        elif rel in sibling or _predates(target, since):
            foreign.add(rel)
            foreign_agents.add(writes[raw])
        elif rel in tracked:
            foreign_agents.add(writes[raw])
            tracked_candidates.append((rel, writes[raw]))
        else:
            candidates.append((rel, writes[raw]))
    members = _run_agents(repo_root, session, since, share_rel, slice_prefix)
    for rel, agent in candidates:
        own = agent not in foreign_agents and agent in members
        (out if own else foreign).add(rel)
    outside: Set[str] = set()
    for rel, agent in tracked_candidates:
        (outside if agent in members else foreign).add(rel)
    if outside_sink is None:
        foreign |= outside
    else:
        outside_sink.extend(sorted(outside))
    return sorted(out), sorted(foreign)


_PRE_REVIEW_SUFFIX = ".pre-review"


def own_written_paths(repo_root: Path, run_id: str, base: Optional[str], sessions: List[str]) -> List[str]:
    """Sorted paths the run's own review prep snapshots classify as its own writes (``_created``'s ``created``
    plus ``outside_declared``) over every run-mode snapshot of ``run_id`` in each of ``sessions``' share dirs:
    the canonical prefix for ``base``, plus any ``manifest-<run id>-*-prep`` snapshot. A sibling-declared or
    foreign path is never here. Spawn-free; an unreadable snapshot or record contributes nothing, and a
    path escaping the worktree empties the answer."""
    from coordinator_core.ops.review_slice_id import RUN_PREP_PLAN_STEM, run_prep_slice_prefix

    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
    found: Set[str] = set()
    for session in sorted({s for s in sessions if s}):
        if "/" in session or "\\" in session or ".." in session:
            continue
        share = Path(share_dir(str(repo_root), session))
        snapshots: Dict[Path, str] = {}
        if base:
            prefix = run_prep_slice_prefix(run_id, base)
            snapshots[pre_review_dir(share, prefix)] = prefix
        try:
            for snap in share.glob(f"{RUN_PREP_PLAN_STEM}-{safe}-*-prep{_PRE_REVIEW_SUFFIX}"):
                snapshots.setdefault(snap, snap.name[: -len(_PRE_REVIEW_SUFFIX)])
        except OSError:
            pass
        for snap, prefix in snapshots.items():
            manifest = _read_manifest(snap)
            if manifest is None:
                continue
            outside: List[str] = []
            created, _foreign = _created(repo_root, session, manifest, share, prefix, outside_sink=outside)
            found.update(created)
            found.update(outside)
    rels = sorted(found)
    return rels if _contained(repo_root, rels) else []


def _run_agents(
    repo_root: Path, session: str, since: Optional[float], share_rel: str, slice_prefix: str
) -> Set[str]:
    """Agents of the session that this run's own record names: each wrote a path under the run's
    snapshot share dir or a path named for its slice prefix. A manifest path is not evidence (a
    sibling run's agent can touch it). Empty, so every candidate is foreign, when the per-agent
    record cannot answer."""
    from coordinator_core.ops.dispatch_emit import review_edit_source

    by_agent = review_edit_source.session_agent_writes(repo_root, session, since)
    if not by_agent:
        return set()
    members: Set[str] = set()
    for agent, paths in by_agent.items():
        for raw in paths:
            rel = _rel(raw, repo_root)
            if rel.startswith(share_rel + "/") or (slice_prefix and slice_prefix in rel):
                members.add(agent)
                break
    return members


def _tracked_at_base(repo_root: Path, base: object, writes: dict) -> Set[str]:
    """Write paths present in the tree of commit ``base``: tracked at the snapshot, so review
    cannot have created them. Spawn-free (object reader); an unreadable base answers empty."""
    if not isinstance(base, str) or len(base) != 40:
        return set()
    from coordinator_core.git import git_state

    info = git_state.read_commit(repo_root, base)
    if info is None:
        return set()
    rels = [_rel(raw, repo_root) for raw in writes]
    spine = git_state.read_tree_spine(repo_root, rels, root_tree_sha=info.tree)
    if spine is None:
        return set()
    out: Set[str] = set()
    for rel in rels:
        parent, _, name = rel.rpartition("/")
        entry = spine.get(parent, {}).get(name)
        if entry is not None and entry[0] != 0o40000:
            out.add(rel)
    return out


def _predates(target: Path, since: Optional[float]) -> bool:
    try:
        return since is not None and target.stat().st_mtime < since
    except OSError:
        return False


_MODE_TREE = 0o40000
_Entry = Tuple[int, str]


class _View:
    """HEAD against the snapshot anchor for a set of paths. ``state`` is ``"none"`` (no HEAD:
    nothing is committed, so the snapshot is the baseline), ``"ok"`` or ``"unreadable"``.
    ``anchor_map`` / ``head_map`` map a path to its leaf ``(mode, sha)``; an absent path has no key."""

    def __init__(self, state: str, head: Optional[str] = None, anchor: Optional[str] = None,
                 head_tree: Optional[str] = None, anchor_map: Optional[Dict[str, _Entry]] = None,
                 head_map: Optional[Dict[str, _Entry]] = None) -> None:
        self.state = state
        self.head = head
        self.anchor = anchor
        self.head_tree = head_tree
        self.anchor_map = anchor_map or {}
        self.head_map = head_map or {}

    def moved(self, rel: str) -> bool:
        """True when a commit since the snapshot changed ``rel`` (absent on one side counts)."""
        return self.state == "ok" and self.anchor_map.get(rel) != self.head_map.get(rel)


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value.lower()) <= set("0123456789abcdef")


def _leaves(repo_root: Path, tree: str, rels: List[str]) -> Optional[Dict[str, _Entry]]:
    spine = git_state.read_tree_spine(repo_root, rels, root_tree_sha=tree)
    if spine is None:
        return None
    out: Dict[str, _Entry] = {}
    for rel in rels:
        parent, _, name = rel.rpartition("/")
        entry = spine.get(parent, {}).get(name)
        if entry is not None and entry[0] != _MODE_TREE:
            out[rel] = (entry[0], entry[1])
    return out


def _view(repo_root: Path, manifest: dict, rels: List[str]) -> _View:
    """Spawn-free object reads only. The anchor is the manifest ``head``, else its ``base``."""
    head = git_state.head_sha(repo_root)
    if head is None:
        return _View("none")
    anchor = next((v for v in (manifest.get("head"), manifest.get("base")) if _is_sha(v)), None)
    if anchor is None:
        return _View("unreadable")
    head_info = git_state.read_commit(repo_root, head)
    anchor_info = git_state.read_commit(repo_root, anchor)
    if head_info is None or anchor_info is None:
        return _View("unreadable")
    anchor_map = _leaves(repo_root, anchor_info.tree, rels)
    head_map = _leaves(repo_root, head_info.tree, rels)
    if anchor_map is None or head_map is None:
        return _View("unreadable")
    return _View("ok", head, anchor, head_info.tree, anchor_map, head_map)


def _blob(repo_root: Path, entry: _Entry) -> Optional[bytes]:
    got = git_objects.read_object(resolve_git_common_dir(repo_root), entry[1])
    if got is None or got[0] != "blob":
        return None
    return got[1]


def _edited(repo_root: Path, manifest: dict, view: _View) -> List[str]:
    """Manifest paths whose bytes differ from their baseline: HEAD's blob for a path a commit
    moved since the snapshot, the snapshot blob otherwise."""
    out: List[str] = []
    for rel, digest in manifest["files"].items():
        rel = _norm(rel)
        target = repo_root / rel
        if view.moved(rel):
            entry = view.head_map.get(rel)
            if entry is None:
                if target.exists():
                    out.append(rel)
                continue
            blob = _blob(repo_root, entry)
            digest = eol_folded_sha256(blob) if blob is not None else None
        if compute_content_hash(target) != digest:
            out.append(rel)
    return sorted(out)


def _run_diff(repo_root: Path, manifest: dict, view: _View) -> Tuple[List[str], List[str]]:
    """(edited, created): manifest paths whose current bytes differ from the manifest ``base``
    commit's blob, or that the base tree lacks; moved paths use HEAD's leaf instead.
    Spawn-free; an unreadable base answers empty."""
    base = manifest.get("base")
    if not _is_sha(base):
        return [], []
    info = git_state.read_commit(repo_root, base)
    rels = sorted({_norm(p) for p in manifest["files"]})
    leaves = _leaves(repo_root, info.tree, rels) if info is not None else None
    if leaves is None:
        return [], []
    edited: List[str] = []
    created: List[str] = []
    for rel in rels:
        target = repo_root / rel
        if not target.is_file():
            continue
        entry = view.head_map.get(rel) if view.moved(rel) else leaves.get(rel)
        if entry is None:
            created.append(rel)
            continue
        blob = _blob(repo_root, entry)
        if blob is not None and compute_content_hash(target) != eol_folded_sha256(blob):
            edited.append(rel)
    return edited, created


def _contained(repo_root: Path, rels: List[str]) -> bool:
    return all(contained_path(repo_root / rel, [repo_root]) is not None for rel in rels)


def _module_files(module: str) -> List[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _imported_edited(repo_root: Path, test_file: str, edited_py: Set[str]) -> Set[str]:
    """Edited ``.py`` paths that ``test_file`` imports; empty when it is unreadable or unparseable."""
    try:
        tree = ast.parse((repo_root / test_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return set()
    records, _ = collect_imports(tree)
    is_pkg = test_file.rsplit("/", 1)[-1] == "__init__.py"
    hit: Set[str] = set()
    for rec in records:
        if rec.level == 0:
            base = rec.module or None
        else:
            base = resolve_relative_module(rec.module, rec.level, module_name_for_file(test_file), is_pkg)
        if not base:
            continue
        wanted = list(_module_files(base))
        for name in rec.names:
            if name != "*":
                wanted.extend(_module_files(f"{base}.{name}"))
        hit.update(p for p in wanted if p in edited_py)
    return hit


def _literal_named(repo_root: Path, test_file: str, named: List[str]) -> List[str]:
    """Paths in ``named`` that a string literal in ``test_file`` names; empty when it is unreadable."""
    if not named:
        return []
    try:
        data = (repo_root / test_file).read_bytes()
    except OSError:
        return []
    return names_in(data, named)


_Plan = Dict[str, Tuple[Optional[bytes], str]]


def _candidate_sources(
    repo_root: Path, view: _View, manifest: dict, plan: _Plan
) -> Tuple[Dict[str, bytes], Optional[str]]:
    """Committed ``.py`` callers in the run's surface (manifest paths plus paths changed between
    the anchor and HEAD), each at its HEAD blob; a revert target is scanned at its post-revert
    bytes instead (a removal drops it). Returns ``(sources, error)``."""
    changes = git_state.diff_commit_trees(repo_root, str(view.anchor), str(view.head))
    if changes is None:
        return {}, "could not diff the snapshot anchor against HEAD"
    paths = {_norm(p) for p in manifest["files"]}
    paths.update(_norm(p) for p in (manifest.get("paths") or []) if isinstance(p, str))
    paths.update(c.path for c in changes)
    rels = sorted(p for p in paths if p.endswith(".py"))
    leaves = _leaves(repo_root, str(view.head_tree), rels)
    if leaves is None:
        return {}, "could not read the HEAD tree"
    sources: Dict[str, bytes] = {}
    for rel, entry in leaves.items():
        if rel in plan:
            continue
        blob = _blob(repo_root, entry)
        if blob is None:
            return {}, f"could not read the HEAD blob for {rel}"
        sources[rel] = blob
    for rel, (data, _src) in plan.items():
        if data is not None and rel.endswith(".py"):
            sources[rel] = data
    return sources, None


def _dangling(repo_root: Path, view: _View, manifest: dict, plan: _Plan) -> Optional[str]:
    """An error message when applying ``plan`` would remove a top-level symbol a committed file
    still uses; ``None`` when it is safe (always, with no HEAD)."""
    if view.state != "ok":
        return None
    sources: Optional[Dict[str, bytes]] = None
    for rel in sorted(plan):
        module = module_name_for_file(rel)
        if not module:
            continue
        entry = view.head_map.get(rel)
        if entry is not None:
            before = _blob(repo_root, entry)
            if before is None:
                return f"could not read the HEAD blob for {rel}"
        else:
            try:
                before = (repo_root / rel).read_bytes()
            except OSError:
                before = b""
        removed = removed_symbols(before, plan[rel][0] or b"")
        if not removed:
            continue
        if sources is None:
            sources, error = _candidate_sources(repo_root, view, manifest, plan)
            if error:
                return error
        hits = callers_of(module, removed, sources)
        if hits:
            return f"revert would leave {hits[0]['caller']} referencing removed {module}.{hits[0]['symbol']}"
    return None


def _orphaned_importer(repo_root: Path, plan: _Plan, edited: List[str], created: List[str]) -> Optional[str]:
    """An error message when ``plan`` removes a module that a kept (not planned) edited or created
    ``.py`` file imports; ``None`` when it is safe."""
    kept_py = {p for p in [*edited, *created] if p.endswith(".py") and p not in plan}
    for rel in sorted(plan):
        if plan[rel][0] is not None or not rel.endswith(".py"):
            continue
        for importer in sorted(kept_py - {rel}):
            if _imported_edited(repo_root, importer, {rel}):
                return f"revert would leave {importer} importing removed {rel}"
    return None


def _load_record(record: Path) -> dict:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    for key in ("reverted", "unattributed", "pre_existing", "owned_red", "peer_dirt", "kept", "flaky"):
        data.setdefault(key, [])
    return data


_BASE_EXIT_OK = (0, 1, 4)
_RED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S.*?)(?:\s+-\s.*)?$")


def _read_base_export(repo_root: Path, raw: str) -> Optional[Set[str]]:
    """``failing_at_base`` parsed from a baseline_export output file, or ``None``
    when the file is unreadable or its trailing JSON line is not ``export`` "ok" with ``runner_exit``
    in {0, 1, 4}."""
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    tail = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    try:
        result = json.loads(tail).get("baseline_export")
    except (ValueError, AttributeError):
        return None
    if not isinstance(result, dict) or result.get("export") != "ok" or result.get("runner_exit") not in _BASE_EXIT_OK:
        return None
    red: Set[str] = set()
    for line in lines:
        text = line.strip()
        hit = _RED_LINE.match(text)
        if hit:
            red.add(hit.group(1))
    return red


def _walked_created(repo_root: Path, test_file: str, created: List[str]) -> Set[str]:
    """Created paths lying under a directory the tree-scanning test walks."""
    try:
        data = (repo_root / test_file).read_bytes()
    except OSError:
        return set()
    return set(walked_created(data, created))


def _implication(
    repo_root: Path, ids: List[str], edited: List[str], created: List[str]
) -> Dict[str, Set[str]]:
    """Node id -> the edited or created paths it implicates (empty set: implicates nothing)."""
    edited_set, created_set = set(edited), set(created)
    edited_py = {p for p in edited if p.endswith(".py")}
    out: Dict[str, Set[str]] = {}
    for node in ids:
        test_file = _node_file(node)
        targets: Set[str] = set()
        if test_file in edited_set or test_file in created_set:
            targets.add(test_file)
        if test_file.endswith(".py"):
            targets.update(_imported_edited(repo_root, test_file, edited_py))
            targets.update(_literal_named(repo_root, test_file, edited + created))
            if created:
                targets.update(_walked_created(repo_root, test_file, created))
        out[node] = targets
    return out


def _sibling_caused(repo_root: Path, node: str, sibling: Set[str]) -> bool:
    """True when ``node`` implicates none of this run's paths and its cause lies wholly with another
    live run: its test file, or a module it imports or a path it names, is in that run's declared set."""
    if not sibling:
        return False
    test_file = _node_file(node)
    if test_file in sibling:
        return True
    if not test_file.endswith(".py"):
        return False
    sibling_py = {p for p in sibling if p.endswith(".py")}
    return bool(
        _imported_edited(repo_root, test_file, sibling_py)
        or _literal_named(repo_root, test_file, sorted(sibling))
    )


def _current(repo_root: Path, rel: str) -> bytes:
    try:
        return (repo_root / rel).read_bytes()
    except OSError:
        return b""


def _stash(repo_root: Path, snapshot_dir: Path, rel: str, target: Path) -> Optional[str]:
    """Copy ``target``'s current bytes to ``<snapshot>/reverted/<rel>`` (a numbered sibling when one
    exists) before a rewrite; the repo-relative stash path, ``None`` when there is no file to keep."""
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return None
    dest = snapshot_dir / _STASHED / rel
    n = 0
    while dest.exists():
        n += 1
        dest = snapshot_dir / _STASHED / f"{rel}.{n}"
    atomic_write_bytes(dest, data)
    declare_write(dest)
    return _rel(str(dest), repo_root)


def _refs(refs) -> List[dict]:
    return [{"sidecar": r.sidecar, "id": r.id} for r in refs]


_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:)?[\w.\-/\\]+")


def _head_readable(repo_root: Path) -> bool:
    """False for an unborn or unreadable HEAD, where ``head_blobs`` would spawn."""
    return git_state.head_sha(repo_root) is not None


def _is_dirty(repo_root: Path, rels: List[str]) -> Set[str]:
    """Existing files among ``rels`` whose bytes differ from HEAD's blob (or that HEAD lacks). Spawn-free;
    an unreadable HEAD answers nothing dirty (fail closed). Trap: hashes raw bytes, so an autocrlf
    checkout reads dirty; the halt-record check still gates the class."""
    if not rels or not _head_readable(repo_root):
        return set()
    head = git_state.head_blobs(repo_root, rels)
    out: Set[str] = set()
    for rel in rels:
        try:
            data = (repo_root / rel).read_bytes()
        except OSError:
            continue
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if rel not in head or head[rel][1] != blob:
            out.add(rel)
    return out


def _sole_dirty_by_basename(root: Path, name: str) -> List[str]:
    """The one tracked path named ``name`` that is dirty versus HEAD; empty when none or several are
    (ambiguous stays a blocker) or the index is unreadable."""
    try:
        tracked = git_state.read_index_paths(root)
    except (git_state.IndexParseError, OSError):
        return []
    same = sorted(p for p in tracked if p.rsplit("/", 1)[-1] == name)
    dirty = sorted(_is_dirty(root, same)) if same else []
    return dirty if len(dirty) == 1 else []


def _derive_cause_files(
    repo_root: Path, nodes: List[str], failing_output: Optional[Dict[str, str]],
) -> Dict[str, List[str]]:
    """``{node id: dirty repo paths}`` named by the node id and its failure output.

    A token with a ``/`` resolves against the root; a bare file name (a parametrize id such as
    ``[ladder_xxl.py]``) resolves against the test file's directory, the root, and the directory of
    every slashed path the node names. Only existing files dirty versus HEAD are kept; a node naming
    none is absent, so it stays a blocker.
    """
    root = repo_root.resolve()
    out: Dict[str, List[str]] = {}
    for node in nodes:
        text = node + "\n" + str((failing_output or {}).get(node) or "")
        tokens = {t.removeprefix("./").rstrip(".") for t in _PATH_TOKEN.findall(text)}
        tokens = {t for t in tokens if "." in t.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]}
        slashed = {_rel(t, root) for t in tokens if "/" in t or "\\" in t}
        dirs = {_node_file(node).rpartition("/")[0], ""} | {s.rpartition("/")[0] for s in slashed}
        rels = set(slashed)
        for t in tokens - {s for s in tokens if "/" in s or "\\" in s}:
            rels.update(f"{d}/{t}" if d else t for d in dirs)
        rels = {r for r in rels if contained_path(root / r, [root]) is not None and (root / r).is_file()}
        dirty = sorted(_is_dirty(root, sorted(rels)))
        if not dirty:
            for t in sorted(tokens - {s for s in tokens if "/" in s or "\\" in s}):
                dirty.extend(_sole_dirty_by_basename(root, t))
        if dirty:
            out[node] = dirty
    return out


def _untracked(repo_root: Path, rels: List[str]) -> Set[str]:
    """Existing files among ``rels`` that HEAD lacks. An unreadable HEAD answers nothing (fail closed)."""
    if not rels or not _head_readable(repo_root):
        return set()
    head = git_state.head_blobs(repo_root, rels)
    return {rel for rel in rels if rel not in head and (repo_root / rel).is_file()}


def _sibling_owned(
    repo_root: Path, candidates: List[str], cause_files: Optional[Dict[str, List[str]]],
    footprint: Set[str], run_id: str, plan_path: str, untracked_only: Optional[Set[str]] = None,
    failing_output: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], Dict[str, List[str]]]:
    """``(sibling_owned, foreign_red)``. ``sibling_owned``: unattributed nodes whose every caller-named
    cause file lies outside ``footprint`` and is withheld by a readable foreign halt record.
    ``foreign_red``: ``{node: [culprit paths]}`` for the rest whose every cause file lies outside
    ``footprint`` and is dirty versus HEAD (so HEAD passes) and none is held by an unreadable record.
    No cause files, an in-footprint one, or a clean one leaves the node blocking. A node in
    ``untracked_only`` (green at the run base) that names a footprint path or sits in a footprint test
    file (``red_attribution``) classes only when every cause file is untracked: the base export never held
    such a file, so green there says nothing about a tracked-dirty one. One naming no footprint path is a
    peer's dirt whatever the base says, as in the land retest."""
    if not cause_files:
        return [], {}
    causes: Dict[str, List[str]] = {}
    for node in candidates:
        raw = cause_files.get(node)
        paths = sorted({_rel(p, repo_root) for p in raw if isinstance(p, str) and p.strip()}) if raw else []
        if paths and not footprint.intersection(paths):
            causes[node] = paths
    if untracked_only and causes:
        gated = [n for n in causes if n in untracked_only]
        own = names_own_path_in_output(failing_output, gated, footprint) | {
            n for n in gated if not (failing_output or {}).get(n)
        }  # no failure text: nothing to attribute from, keep the untracked-only check
        untracked = _untracked(repo_root, sorted({p for n, ps in causes.items() if n in own for p in ps}))
        causes = {n: ps for n, ps in causes.items() if n not in own or set(ps) <= untracked}
    if not causes:
        return [], {}
    from coordinator_core.ops.dispatch_emit.land_run import UNREADABLE_HALT_RECORD, foreign_halted_withheld

    wanted = sorted({p for paths in causes.values() for p in paths})
    held = foreign_halted_withheld(repo_root, run_id or None, plan_path or None, wanted)
    owned = sorted(
        node for node, paths in causes.items()
        if all(held.get(p) and held[p] != UNREADABLE_HALT_RECORD for p in paths)
    )
    dirty = _is_dirty(repo_root, wanted)
    foreign_red = {
        node: paths for node, paths in sorted(causes.items())
        if node not in owned
        and all(p in dirty and held.get(p) != UNREADABLE_HALT_RECORD for p in paths)
    }
    return owned, foreign_red


_REACH_CAP = 400


def _import_targets(repo_root: Path, rel: str) -> Optional[Set[str]]:
    """Repo ``.py`` files ``rel`` imports, each import resolved against the directories above ``rel``
    (a project nested under the root puts its packages there), plus every parent package's
    ``__init__``. ``None`` when ``rel`` is unreadable or unparseable."""
    try:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    records, _ = collect_imports(tree)
    parts = rel.split("/")[:-1]
    out: Set[str] = set()
    for rec in records:
        if rec.level:
            if rec.level - 1 > len(parts):
                continue
            anchors = ["/".join(parts[: len(parts) - (rec.level - 1)])]
        else:
            anchors = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
        base = rec.module or ""
        mods = [base, *(f"{base}.{n}" if base else n for n in rec.names if n != "*")]
        for anchor in anchors:
            for mod in filter(None, mods):
                segs = mod.split(".")
                for i in range(1, len(segs) + 1):
                    stem = "/".join(([anchor] if anchor else []) + segs[:i])
                    out.update(c for c in (f"{stem}.py", f"{stem}/__init__.py") if (repo_root / c).is_file())
    return out


def _reaches(repo_root: Path, test_file: str, footprint: Set[str]) -> bool:
    """True when ``test_file`` can reach a footprint path: it imports one directly or through other repo
    modules, or a string literal in a reached module names one. A ``conftest.py`` above it counts as
    reached. Fails closed (True) when more than ``_REACH_CAP`` modules are reached or ``test_file`` is
    unreadable."""
    parts = test_file.split("/")[:-1]
    seeds = [test_file, *(f"{'/'.join(parts[:i])}/conftest.py".lstrip("/") for i in range(len(parts) + 1))]
    todo = [s for s in dict.fromkeys(seeds) if s == test_file or (repo_root / s).is_file()]
    seen: Set[str] = set()
    named = sorted(footprint)
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in footprint or len(seen) > _REACH_CAP:
            return True
        found = _import_targets(repo_root, cur)
        if found is None:
            if cur == test_file:
                return True
            continue
        if _literal_named(repo_root, cur, named):
            return True
        todo.extend(sorted(found - seen))
    return False


def _reach_walk(repo_root: Path, test_file: str, named: List[str]) -> Optional[Tuple[Set[str], Set[str]]]:
    """``(reached files, members of named that a reached file's string literal names)`` over the
    import closure of ``test_file`` and the ``conftest.py`` above it. ``None`` when ``test_file`` is
    unreadable or more than ``_REACH_CAP`` files are reached (fail closed)."""
    parts = test_file.split("/")[:-1]
    seeds = [test_file, *(f"{'/'.join(parts[:i])}/conftest.py".lstrip("/") for i in range(len(parts) + 1))]
    todo = [s for s in dict.fromkeys(seeds) if s == test_file or (repo_root / s).is_file()]
    seen: Set[str] = set()
    literal: Set[str] = set()
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if len(seen) > _REACH_CAP:
            return None
        found = _import_targets(repo_root, cur)
        if found is None:
            if cur == test_file:
                return None
            continue
        literal.update(_literal_named(repo_root, cur, named))
        todo.extend(sorted(found - seen))
    return seen, literal


def _withheld_reach(
    repo_root: Path, nodes: List[str], footprint: Set[str], failing_output: Optional[Dict[str, str]],
    run_id: str, plan_path: str,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """``({node: [withheld paths]}, {node: [holding run ids]})`` for pathless nodes whose test file
    reaches (imports, ``conftest.py``, string literals) a path another halted run withholds and reaches
    no path of this run's ``footprint`` beyond the test file itself. Every reached withheld path must be
    held by a readable foreign halt record; an unreadable record, a path this run edited, a failure text
    naming a footprint path, or a capped walk leaves the node blocking."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index
    from coordinator_core.ops.dispatch_emit.land_run import UNREADABLE_HALT_RECORD, foreign_halted_withheld

    try:
        index = set(halted_withheld_index.withheld_by_path(repo_root))
    except Exception:  # noqa: BLE001 -- an unreadable index attributes nothing
        return {}, {}
    if not index:
        return {}, {}
    own = names_own_path_in_output(failing_output, nodes, sorted(footprint))
    named = sorted(index | footprint)
    hits_by_file: Dict[str, Optional[Set[str]]] = {}
    found: Dict[str, Set[str]] = {}
    for node in nodes:
        test_file = _node_file(node)
        if node in own or not test_file.endswith(".py") or not (repo_root / test_file).is_file():
            continue
        if test_file not in hits_by_file:
            walked = _reach_walk(repo_root, test_file, named)
            if walked is None:
                hits_by_file[test_file] = None
            else:
                reached, literal = walked
                touched = (reached | literal) - {test_file}
                hits_by_file[test_file] = None if touched & footprint else (reached | literal) & index
        hits = hits_by_file[test_file]
        if hits:
            found[node] = hits
    if not found:
        return {}, {}
    held = foreign_halted_withheld(
        repo_root, run_id or None, plan_path or None, sorted({p for hits in found.values() for p in hits}),
    )
    red: Dict[str, List[str]] = {}
    runs: Dict[str, List[str]] = {}
    for node, hits in sorted(found.items()):
        holders = {p: held.get(p) for p in hits}
        if all(h and h != UNREADABLE_HALT_RECORD for h in holders.values()):
            red[node] = sorted(hits)
            runs[node] = sorted({str(h) for h in holders.values()})
    return red, runs


def _unreachable_red(
    repo_root: Path, nodes: List[str], footprint: Set[str], failing_output: Optional[Dict[str, str]],
    base_green: Set[str],
) -> Dict[str, List[str]]:
    """``{node: [test file]}`` for nodes in a test file the run never wrote whose failure text names no
    footprint path and that reaches none through its imports or literals (``_reaches``): the diff cannot
    have caused them, whether or not the file is dirty. A node green at the run base is the run's own, so
    it is never returned."""
    own = names_own_path_in_output(failing_output, nodes, sorted(footprint))
    out: Dict[str, List[str]] = {}
    reached: Dict[str, bool] = {}
    for node in nodes:
        test_file = _node_file(node)
        if node in own or node in base_green or test_file in footprint or not test_file.endswith(".py"):
            continue
        if not (repo_root / test_file).is_file():
            continue
        if test_file not in reached:
            reached[test_file] = _reaches(repo_root, test_file, footprint)
        if not reached[test_file]:
            out[node] = [test_file]
    return out


def _open_probe(
    repo_root: Path, snapshot_dir: Path, ids: List[str], implicated: Dict[str, List[str]],
    created_set: Set[str], sidecars: List[str], run_id: str = "", plan_path: str = "",
) -> dict:
    from coordinator_core.ops.dispatch_emit import kept_work

    held = kept_work.held_paths(repo_root, implicated, run_id or None)
    writes: Dict[str, bytes] = {}
    for rel in sorted(implicated):
        target = repo_root / rel
        if rel in held or rel in created_set or not target.is_file():
            continue
        inv = invert(target.read_bytes(), applied_rows(repo_root, sidecars, rel))
        if inv.reverted:
            writes[rel] = inv.data
    rerun = [
        n for n in ids
        if _node_file(n) not in created_set and any(n in implicated[rel] for rel in writes)
    ]
    live_runs = None
    if writes:
        from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld
        from coordinator_core.ops.dispatch_emit.run_ownership import holders_from_held

        # An empty run_id owns nothing, so every halted run's withheld path is foreign to it.
        held = foreign_halted_withheld(repo_root, run_id or None, plan_path or None, sorted(writes))
        live_runs = holders_from_held(held) or None
    return probe_io.open_probe(repo_root, snapshot_dir, writes, rerun, run_id=run_id, live_runs=live_runs)


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


_LAND_SCAN_BUDGET_S = 0.3


def _land_findings(
    repo_root: Path, base: str, notes: Optional[List[str]] = None
) -> Tuple[List[dict], List[dict]]:
    """Major ``(comment rows, provenance rows)`` for ``base..HEAD``; ``notes`` receives why a scan was cut short or failed."""
    if not _FULL_SHA_RE.fullmatch(base):
        return [], []
    sink: List[bytes] = []
    folded: List[Optional[set]] = []
    heads: Dict[str, str] = {}

    def runner(args: List[str], cwd: Path, stdin: Optional[bytes]) -> Optional[bytes]:
        if args[:2] == ["cat-file", "--batch"] and sink and stdin is not None:
            shas = governed_by.pending_shas(sink[0].decode("utf-8", errors="replace"))
            extra = "".join(f"{s}\n" for s in shas).encode("ascii", "replace")
            out = authorship.default_runner(args, cwd, stdin + extra)
            if out is not None:
                requested = stdin.decode("utf-8", errors="replace").splitlines()
                blobs = authorship._parse_cat_file(out, len(requested))
                for spec, blob in zip(requested[1::2], blobs[1::2]):
                    if blob is not None:
                        heads[spec.split(":", 1)[1]] = blob
                folded.append(governed_by.unresolved_from_batch(out, stdin.count(b"\n"), shas))
            return out
        out = authorship.default_runner(args, cwd, stdin)
        if out and args[:1] == ["log"]:
            sink.append(out)
        return out

    agent_lines: Dict[str, List[str]] = {}
    started = time.thread_time()
    skipped: List[str] = []

    def comments_fn(path: str, before: str, after: str) -> list:
        nonlocal agent_lines
        if not agent_lines and sink:
            agent_lines = authorship._agent_added_lines(sink[0].decode("utf-8", errors="replace"))
        tails = [ln.rstrip() for ln in agent_lines.get(path, ())]
        if time.thread_time() - started > _LAND_SCAN_BUDGET_S:
            skipped.append(path)
            return []

        def on_agent_lines(text: str) -> bool:
            norm = [n for n in (authorship._norm(ln) for ln in text.split("\n")) if n]
            return bool(norm) and all(any(authorship._ends_at_boundary(a, n) for a in tails) for n in norm)

        if path.endswith(".py") and len(after) > OVERSIZE_CHARS:
            stripped = {t.strip() for t in tails}
            hits = windowed_added_comments(path, after, stripped.__contains__, on_agent_lines)
            if hits is None:
                skipped.append(path)
                return []
            return hits
        return added_comments(path, before, after, keep=on_agent_lines)

    try:
        found = agent_added_comments(repo_root, base, "HEAD", runner=runner, comments_fn=comments_fn)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if notes is not None:
            notes.append(f"comment scan failed: {type(exc).__name__}")
        return [], []
    if skipped and notes is not None:
        notes.append(f"comment scan incomplete: {len(skipped)} file(s) over the {_LAND_SCAN_BUDGET_S}s budget not lexed")
    log_text = sink[0].decode("utf-8", errors="replace") if sink else ""
    provenance = governed_by.provenance_findings(repo_root, log_text, folded[0] if folded else None) if sink else []
    return _present_comments(repo_root, found, heads), provenance


def _comment_key(text: str) -> str:
    return " ".join(strip_marker(text).split())


def _present_comments(repo_root: Path, found: list, heads: Dict[str, str]) -> List[dict]:
    """Rows for untagged comments in the worktree that are a still-open found comment or that the review added over HEAD."""
    rows: List[dict] = []
    for path in dict.fromkeys(c.path for c in found):
        try:
            current = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        open_keys = {_comment_key(c.text) for c in found if c.path == path}
        if len(current) > OVERSIZE_CHARS and path.endswith(".py"):
            firsts = [c.text.split(chr(10), 1)[0].strip() for c in found if c.path == path]
            head_lines = {ln.strip() for ln in heads.get(path, "").split(chr(10))} if path in heads else set()
            fresh = {ln.strip() for ln in current.split(chr(10))} - head_lines if path in heads else set()
            hits = windowed_added_comments(
                path, current, lambda ln: ln in fresh or any(f in ln for f in firsts)
            )
            rows.extend(
                {"path": path, "line": c.line, "text": c.text, "severity": "major", "scope": "default-path"}
                for c in hits or ()
                if _comment_key(c.text) in open_keys
                or any(ln.strip() in fresh for ln in c.text.split(chr(10)))
            )
            continue
        if path in heads:
            open_keys |= {_comment_key(c.text) for c in added_comments(path, heads[path], current)}
        for comment in added_comments(path, "", current):
            if _comment_key(comment.text) in open_keys:
                rows.append({
                    "path": path, "line": comment.line, "text": comment.text,
                    "severity": "major", "scope": "default-path",
                })
    return rows


def review_reverify(
    repo_root: Path,
    *,
    base: str,
    slice_prefix: str,
    session_id: str = "",
    failing: Optional[List[str]] = None,
    sidecars: Optional[List[str]] = None,
    failing_before: Optional[List[str]] = None,
    failing_at_base: Optional[List[str]] = None,
    probe: Optional[str] = None,
    run_id: str = "",
    plan_path: str = "",
    cause_files: Optional[Dict[str, List[str]]] = None,
    failing_output: Optional[Dict[str, str]] = None,
    run_key: str = "",
    base_export_path: str = "",
    export_failing_path: str = "",
    leg_passed: Optional[List[str]] = None,
) -> dict:
    """List the review edits since the snapshot, or (``failing`` given) invert the findings those
    tests implicate; ``probe`` opens or closes the pre-review probe. See the module docstring."""
    if probe not in (None, "open", "close"):
        return _fail('probe must be "open" or "close"', failing is not None)
    if probe == "open" and failing is None:
        return _fail('probe "open" requires failing', False, "open")
    revert = failing is not None and probe is None
    shape = probe
    if not base or not slice_prefix:
        return _fail("base and slice_prefix are required", revert, shape)
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session", revert, shape)
    for label, value in (("slice_prefix", slice_prefix), ("session_id", session)):
        if "/" in value or "\\" in value or ".." in value:
            return _fail(f"{label} must be a bare name, not a path", revert, shape)
    share = Path(share_dir(str(repo_root), session))
    snapshot_dir = pre_review_dir(share, slice_prefix)
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        return _fail(f"no pre-review snapshot for {slice_prefix}", revert, shape)
    recorded_base = manifest.get("base")
    if isinstance(recorded_base, str) and recorded_base and recorded_base != base:
        return _fail(f"base {base} does not match the snapshot base {recorded_base}", revert, shape)
    if probe == "close":
        return probe_io.close_probe(repo_root, snapshot_dir)
    if revert and probe_io.is_open(snapshot_dir):
        return _fail('a probe is open; call with "probe":"close"', True)

    rels = [_norm(p) for p in manifest["files"]]
    if not _contained(repo_root, rels):
        return _fail("a snapshot path escapes the worktree", revert, shape)
    outside: List[str] = []
    created, foreign = _created(repo_root, session, manifest, share, slice_prefix, outside_sink=outside)
    if not _contained(repo_root, [*created, *outside]):
        return _fail("a review-written path escapes the worktree", revert, shape)
    view = _view(repo_root, manifest, sorted({*rels, *created}))
    if view.state == "unreadable" and (revert or probe):
        return _fail("HEAD or the snapshot anchor is unreadable; nothing reverted", True, shape)
    if view.state == "unreadable":
        view = _View("none")
        unreadable = True
    else:
        unreadable = False
    edited = _edited(repo_root, manifest, view)
    snapshot_rel = _rel(str(snapshot_dir / _MANIFEST), repo_root)
    if not revert and not probe:
        scan_error = None
        derived = None
        if not edited and not created:
            edited, created = _run_diff(repo_root, manifest, view)
            derived = "run_diff" if edited or created else None
        try:
            skip_paths = set(edited) | set(created)
            imports: dict = {}
            walkers = tree_scanners(repo_root, created, exclude=skip_paths) if created else []
            picked = select_for_paths(
                repo_root,
                edited + created,
                tracked_test_bytes(repo_root, skip_paths, scan_tokens(edited + created, leaves=False), imports),
                namers=walkers,
                imports=imports,
            )
        except TripwireScanUnavailable as exc:
            picked = {"owned": [], "tripwires": [], "tripwires_not_selected": [], "notice": ""}
            scan_error = f"tripwire scan unavailable: {exc}"
        scan_notes: List[str] = []
        land_comments, land_provenance = _land_findings(repo_root, base, scan_notes)
        scan_error = "; ".join(filter(None, [scan_error, *scan_notes])) or None
        out = {
            "edited": edited,
            "created": created,
            "foreign": foreign,
            "outside_declared": outside,
            "scope_files": sorted(set(rels)),
            "owned": picked["owned"],
            "tripwires": picked["tripwires"],
            "tripwires_not_selected": picked["tripwires_not_selected"],
            "notice": picked["notice"],
            "snapshot": snapshot_rel,
            "comment_findings": land_comments,
            "provenance_findings": land_provenance,
            "error": scan_error,
        }
        if derived:
            out["derived_from"] = derived
        if unreadable:
            out["head_unreadable"] = True
        return out
    foreign = sorted({*foreign, *outside})

    ids = [str(i) for i in failing if isinstance(i, str) and i.strip()]
    base_unmeasured = False
    export_unmeasured = False
    created_set = set(created)
    side = list(sidecars or [])
    record_path = snapshot_dir / _RECORD
    prior = _load_record(record_path)
    already = {t for entry in prior["reverted"] if isinstance(entry, dict) for t in entry.get("tests", [])}
    if revert:
        vouched = {str(i) for i in failing_before or []} & probe_io.closed_rerun(snapshot_dir)
        explicit_base = {str(i) for i in failing_at_base or []}
        if base_export_path:
            measured = _read_base_export(repo_root, base_export_path)
            if measured is None:
                base_unmeasured = True
            else:
                explicit_base |= measured
        owned: Set[str] = set()
        ruled: Dict[str, str] = {}
        if run_key:
            try:
                owned = set(read_owned_red(repo_root, run_key))
                ruled = ruled_pre_existing(repo_root, run_key, ids)
            except LedgerRefused as exc:
                return _fail(f"run_key refused: {exc}", True)
            explicit_base |= set(ruled)
        recorded_pre = {str(n) for n in prior["pre_existing"]}
        prior_red = already | recorded_pre
        write_set = {*rels, *edited, *created_set} - created_set
        to_measure = [
            n for n in ids
            if n not in explicit_base and n not in owned and n not in prior_red
            and _node_file(n) in write_set and not base_export_path
        ]
        base_red, base_green_here, base_measure_error = _measure_at_base(repo_root, manifest, base, prior, to_measure)
        explicit_base |= base_red
        at_base = explicit_base | owned
        pre_existing = sorted(n for n in ids if n in explicit_base or n in prior_red)
        owned_red = sorted(n for n in ids if n in owned and n not in explicit_base and n not in prior_red)
        not_base_red = sorted(n for n in ids if n in vouched and n not in at_base and n not in prior_red)
        renamed_node = sorted(
            n for n in ids
            if n not in pre_existing and _node_file(n) in {*edited, *created_set}
            and NOT_FOUND_RE.search(str((failing_output or {}).get(n) or ""))
        )
        skip = set(pre_existing) | set(owned_red) | set(not_base_red) | set(renamed_node)
        peer_dirt: List[str] = []
        export_own: Set[str] = set()
        if export_failing_path:
            export_red = _read_base_export(repo_root, export_failing_path)
            if export_red is None:
                export_unmeasured = True
            else:
                peer_dirt = sorted(n for n in ids if n not in skip and n not in export_red)
                skip |= set(peer_dirt)
                export_own = {n for n in ids if n not in skip and n in export_red}
        base_measured = (failing_at_base is not None or bool(base_export_path)) and not base_unmeasured
        base_green = {n for n in ids if (base_measured or n in base_green_here) and n not in skip}
        flaky = _rerun_alone(repo_root, [n for n in ids if n not in skip])
        suspect = {n for n in ids if not str((failing_output or {}).get(n) or "").strip()} | set(leg_passed or [])
        mirror_only = [n for n in flaky if n in suspect]
        skip |= set(flaky)
        base_green -= set(flaky)
    else:
        pre_existing, owned_red, not_base_red, renamed_node, skip, base_green = [], [], [], [], set(), set()
        to_measure, base_red, base_green_here, base_measure_error = [], set(), set(), None
        peer_dirt = []
        flaky = []
        mirror_only = []
        export_own = set()
        ruled = {}
    foreign_set, foreign_ids = set(foreign), []  # type: Set[str], List[str]
    foreign_node = {n for n in ids if n not in skip and n not in base_green and _node_file(n) in foreign_set}
    implication = _implication(repo_root, [n for n in ids if n not in skip and n not in foreign_node], edited, created)
    implicated: Dict[str, List[str]] = {}
    unattributed: List[str] = []
    for node in sorted(foreign_node):
        if node not in already:
            unattributed.append(node)
        foreign_ids.append(node)
    sibling_py: Set[str] = set()
    if any(not t for t in implication.values()):
        from coordinator_core.ops.dispatch_emit import review_edits

        sibling_py = {_norm(p) for p in review_edits.live_sibling_declared(repo_root, None, slice_prefix)}
        sibling_py -= {*rels, *created_set}
    for node, targets in implication.items():
        for target in targets:
            implicated.setdefault(target, []).append(node)
        if not targets and node not in already:
            unattributed.append(node)
        if not targets and node not in base_green and node not in export_own and _sibling_caused(repo_root, node, sibling_py):
            foreign_ids.append(node)

    if probe == "open":
        return _open_probe(repo_root, snapshot_dir, list(implication), implicated, created_set, side, run_id, plan_path)

    plan: _Plan = {}
    refs_by_rel: Dict[str, list] = {}
    kept: List[dict] = []
    from coordinator_core.ops.dispatch_emit import kept_work

    held = kept_work.held_paths(repo_root, implicated)
    fenced: Set[str] = set()
    footprint = set(edited) | created_set
    for rel in sorted(implicated):
        if rel not in footprint or rel in foreign_set:
            fenced.add(rel)
            kept.append({
                "path": rel, "tests": sorted(set(implicated[rel])),
                "findings": [{"sidecar": None, "id": None, "reason": "outside-run-footprint"}],
            })
            continue
        if rel in held:
            kept.append({
                "path": rel, "tests": sorted(set(implicated[rel])),
                "findings": [{"sidecar": None, "id": None, "reason": f"kept-work-held:{held[rel]}"}],
            })
            continue
        if rel in created_set:
            entry = view.head_map.get(rel)
            if entry is None:
                plan[rel] = (None, "snapshot")
                continue
            if not applied_rows(repo_root, side, rel):
                fenced.add(rel)
                kept.append({
                    "path": rel, "tests": sorted(set(implicated[rel])),
                    "findings": [{"sidecar": None, "id": None, "reason": "no-run-finding-names-path"}],
                })
                continue
            blob = _blob(repo_root, entry)
            if blob is None:
                return _fail(f"HEAD blob for {rel} is unreadable", True)
            plan[rel] = (blob, "head")
            continue
        rows = applied_rows(repo_root, side, rel)
        inv = invert(_current(repo_root, rel), rows)
        tests = sorted(set(implicated[rel]))
        if not rows:
            kept.append({
                "path": rel, "tests": tests,
                "findings": [{"sidecar": None, "id": None, "reason": "no-finding"}],
            })
            continue
        if inv.kept:
            kept.append({
                "path": rel, "tests": tests,
                "findings": [{"sidecar": r.sidecar, "id": r.id, "reason": why} for r, why in inv.kept],
            })
        if inv.reverted:
            plan[rel] = (inv.data, "findings")
            refs_by_rel[rel] = inv.reverted

    for node, targets in implication.items():
        if targets and targets <= fenced and node not in unattributed and node not in already:
            unattributed.append(node)
    unattributed.extend(n for n in not_base_red if n not in unattributed)
    candidates = [n for n in unattributed if n not in not_base_red and n not in export_own]
    if cause_files is None and candidates:
        cause_files = _derive_cause_files(repo_root, candidates, failing_output)
    sibling_owned, foreign_red = _sibling_owned(
        repo_root, candidates, cause_files, {*rels, *created}, run_id, plan_path, untracked_only=base_green,
        failing_output=failing_output,
    )
    rest = [n for n in candidates if n in unattributed and not (cause_files or {}).get(n)]
    foreign_red_runs: Dict[str, List[str]] = {}
    if rest:
        withheld_red, foreign_red_runs = _withheld_reach(
            repo_root, rest, {*rels, *created, *outside}, failing_output, run_id, plan_path,
        )
        foreign_red = {**foreign_red, **withheld_red}
        left = [n for n in rest if n not in withheld_red]
        if left:
            foreign_red = {
                **foreign_red,
                **_unreachable_red(repo_root, left, {*rels, *created, *outside}, failing_output, base_green),
            }
    if mirror_only:
        foreign_red = {**foreign_red, **{n: [_node_file(n)] for n in mirror_only}}
    if sibling_owned or foreign_red:
        unattributed = [n for n in unattributed if n not in sibling_owned and n not in foreign_red]
    unmeasured: List[str] = []
    if base_unmeasured:
        measured_here = base_red | base_green_here
        unmeasured = sorted(n for n in set(unattributed) if n not in measured_here)
        unattributed = [n for n in unattributed if n in measured_here]
    refusal = _orphaned_importer(repo_root, plan, edited, created) or _dangling(repo_root, view, manifest, plan)
    if refusal:
        return _fail(refusal, True)

    if plan:
        from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld

        for rel in sorted(foreign_halted_withheld(repo_root, run_id or None, plan_path or None, sorted(plan))):
            del plan[rel]
            kept.append({
                "path": rel, "tests": sorted(set(implicated[rel])),
                "findings": [{"sidecar": None, "id": None, "reason": "peer-withheld"}],
            })

    reverted: List[dict] = []
    for rel in sorted(plan):
        target = repo_root / rel
        data, source = plan[rel]
        kept_at = None
        if data is None:
            dest = snapshot_dir / _REMOVED / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, dest)
            except OSError:
                return _fail(f"could not move {rel} aside", True)
            kept_at = _rel(str(dest), repo_root)
            action = "removed"
        else:
            try:
                kept_at = _stash(repo_root, snapshot_dir, rel, target)
            except OSError:
                return _fail(f"could not stash {rel} before reverting", True)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, data)
            action = "reverted-findings" if source == "findings" else "restored"
        declare_write(target)
        reverted.append({
            "path": rel,
            "action": action,
            "source": source,
            "tests": sorted(set(implicated[rel])),
            "findings": _refs(refs_by_rel.get(rel, [])),
            **({"kept_at": kept_at} if kept_at else {}),
        })

    # A removed path an author `git add -N`'d leaves an empty-blob entry with no file; drop only
    # intent-to-add entries for files now gone, never a real staged entry.
    removed = [r["path"] for r in reverted if r["action"] == "removed"]
    index_residue: List[str] = []
    if removed:
        try:
            splice_index(repo_root, {rel: DROP_IF_PHANTOM_INTENT_TO_ADD for rel in removed})
        except (IndexWriteError, OSError):
            index_residue = removed

    merged = {
        **prior,
        "reverted": [*prior["reverted"], *reverted],
        "unattributed": sorted(set(prior["unattributed"]) | set(unattributed)),
        "pre_existing": sorted(set(prior["pre_existing"]) | set(pre_existing)),
        "owned_red": sorted(set(prior["owned_red"]) | set(owned_red)),
        "peer_dirt": sorted(set(prior["peer_dirt"]) | set(peer_dirt)),
        "flaky": sorted(set(prior["flaky"]) | set(flaky)),
        "kept": [*prior["kept"], *kept],
    }
    if base_red or base_green_here:
        old = prior.get("base_measured")
        old = old if isinstance(old, dict) else {}
        merged["base_measured"] = {
            "red": sorted({*(old.get("red") or []), *base_red}),
            "green": sorted({*(old.get("green") or []), *base_green_here}),
        }
    atomic_write_bytes(record_path, (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    declare_write(record_path)
    return {
        "reverted": reverted,
        "unattributed": unattributed,
        "pre_existing": pre_existing,
        "owned_red": owned_red,
        "peer_dirt": peer_dirt,
        "flaky": flaky,
        "renamed_node": renamed_node,
        "recollect": sorted({_node_file(n) for n in renamed_node}),
        "ruled_pre_existing": {n: ruled[n] for n in pre_existing if n in ruled},
        "sibling_owned": sibling_owned,
        "foreign_red": foreign_red,
        "mirror_only_red": mirror_only,
        "foreign_red_runs": foreign_red_runs,
        "kept": kept,
        "foreign": foreign_ids,
        "passed_at_base": sorted(base_green - set(foreign_red)),
        "index_residue": index_residue,
        "record": _rel(str(record_path), repo_root),
        "error": None,
        **({"base_unmeasured": base_unmeasured, "unmeasured": unmeasured} if base_export_path else {}),
        **({"export_unmeasured": export_unmeasured} if export_failing_path else {}),
        **({"base_measured_here": sorted(base_red | base_green_here), "base_measure_error": base_measure_error}
           if to_measure else {}),
    }


@register_op("review.reverify")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.reverify". Params: ``base`` (str), ``slice_prefix`` (str), optional
    ``session_id``, optional ``failing`` (list[str] node ids; presence selects revert mode),
    optional ``sidecars`` (list[str]), optional ``failing_before`` (list[str]), optional
    ``failing_at_base`` (list[str]), optional ``probe`` (``"open"`` or ``"close"``), optional
    ``run_id`` and ``plan_path`` (a probe open refuses a path another halted run withholds), optional
    ``run_key`` (a run whose owned_red ids join ``failing_at_base``), optional
    ``cause_files`` (``{node id: [paths]}``; the caller must supply it for a node to class
    ``sibling_owned``), optional ``failing_output`` (``{node id: failure text}``; absent
    ``cause_files``, the dirty repo paths named by the node id and its text are derived as cause files).
    ``slice_prefixes`` (list[str]) replaces ``slice_prefix`` to read several snapshots in one call:
    the reply is ``{"inventories": [<one reply per prefix, in order>], "error": None}``.
    Keying scope: show_top."""
    prefixes = params.get("slice_prefixes")
    if prefixes is not None:
        if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
            return _fail("review.reverify slice_prefixes must be a list of names", False)
        rest = {k: v for k, v in params.items() if k != "slice_prefixes"}
        return {"inventories": [_handler({**rest, "slice_prefix": p}, repo_root) for p in prefixes], "error": None}
    probe = params.get("probe")
    shape = probe if probe in ("open", "close") else None
    revert = params.get("failing") is not None and shape is None
    if repo_root is None:
        return _fail("review.reverify requires a show_top-keyed dispatch; repo_root was not supplied", revert, shape)
    failing, sidecars = params.get("failing"), params.get("sidecars")
    failing_before = params.get("failing_before")
    if probe is not None and shape is None:
        return _fail('review.reverify probe must be "open" or "close"', failing is not None)
    if failing is not None and not isinstance(failing, list):
        return _fail("review.reverify failing must be a list of node ids", True, shape)
    if failing_before is not None and not isinstance(failing_before, list):
        return _fail("review.reverify failing_before must be a list of node ids", revert, shape)
    failing_at_base = params.get("failing_at_base")
    if failing_at_base is not None and not isinstance(failing_at_base, list):
        return _fail("review.reverify failing_at_base must be a list of node ids", revert, shape)
    base_export_path = params.get("base_export_path")
    if base_export_path is not None and not isinstance(base_export_path, str):
        return _fail("review.reverify base_export_path must be a path string", revert, shape)
    base_export_path = base_export_path or ""
    export_failing_path = params.get("export_failing_path")
    if export_failing_path is not None and not isinstance(export_failing_path, str):
        return _fail("review.reverify export_failing_path must be a path string", revert, shape)
    export_failing_path = export_failing_path or ""
    if sidecars is not None and not isinstance(sidecars, list):
        return _fail("review.reverify sidecars must be a list of paths", revert, shape)
    cause_files = params.get("cause_files")
    if cause_files is not None and not (
        isinstance(cause_files, dict) and all(isinstance(v, list) for v in cause_files.values())
    ):
        return _fail("review.reverify cause_files must map node ids to lists of paths", revert, shape)
    failing_output = params.get("failing_output")
    if failing_output is not None and not isinstance(failing_output, dict):
        return _fail("review.reverify failing_output must map node ids to failure text", revert, shape)
    return review_reverify(
        repo_root,
        cause_files=cause_files,
        failing_output={str(k): str(v) for k, v in failing_output.items()} if failing_output else None,
        base=str(params.get("base") or ""),
        slice_prefix=str(params.get("slice_prefix") or ""),
        session_id=str(params.get("session_id") or ""),
        failing=failing,
        sidecars=[str(s) for s in sidecars] if sidecars else None,
        failing_before=[str(i) for i in failing_before] if failing_before else None,
        failing_at_base=[str(i) for i in failing_at_base] if failing_at_base is not None else None,
        probe=shape,
        run_id=str(params.get("run_id") or ""),
        plan_path=str(params.get("plan_path") or ""),
        run_key=str(params.get("run_key") or ""),
        base_export_path=base_export_path,
        export_failing_path=export_failing_path,
        leg_passed=[str(i) for i in params["leg_passed"]] if isinstance(params.get("leg_passed"), list) else None,
    )
