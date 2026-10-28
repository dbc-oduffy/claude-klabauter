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

List mode (no ``failing``): ``{"edited", "created", "foreign", "tripwires", "snapshot", "error"}``,
plus ``head_unreadable`` when applicable.
``tripwires`` is the sorted tracked test modules (not already edited or created) whose source holds a string
literal naming an edited or created path; empty for an empty edit set. When the tracked-file index cannot
be read, ``tripwires`` is empty and ``error`` is ``"tripwire scan unavailable: <reason>"``; the other
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
failure is shared-tree contamination, evidence rather than a blocker. Absent ``cause_files``
they are derived (``_derive_cause_files``); a bare file name resolving nowhere falls back to the sole
dirty tracked path of that basename. A failing node id's
test file implicates itself when edited or created, and every edited ``.py`` module it imports, plus
every edited or created path a string literal in it names. Trap: a tripwire naming several edited paths implicates all of them. An implicated edited
path is rewritten as its current bytes with each applied finding on it inverted (the ledger row's
``after`` replaced by its ``before``; ``action`` ``"reverted-findings"``, ``source`` ``"findings"``);
every other byte, executor hunks included, is untouched. A finding that cannot be inverted, and an
implicated path with no applied finding, go to ``kept`` with a reason and write nothing. A created
path is removed by moving its bytes to ``<snapshot>/removed/<path>`` (``kept_at`` in the entry),
never unlinked; one tracked at HEAD is restored to HEAD's bytes. A created path whose writing
agent also wrote a foreign path, or wrote nothing in this run's surface (manifest paths, the
snapshot share dir, a path naming the slice prefix), is foreign. Before
anything is written, the revert (tripwire-named paths included) refuses (``error``, nothing written,
the whole call) when it would remove a top-level symbol that a committed file in the run's surface
(manifest paths plus paths changed anchor..HEAD) still uses. An id implicating nothing is
``unattributed`` and reverts nothing. A path an open ``kept.json`` holds (``kept_work.held_paths``)
is never restored, unlinked or inverted: it lands in ``kept`` as ``kept-work-held:<run_id>``. Every
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
Optional ``run_key`` names a run whose ``owned_red.jsonl`` ids join ``failing_at_base``: a failing id
recorded there (and not in ``failing_at_base``) is reported in the ``owned_red`` bucket, never reverted or
``unattributed``.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.atomic_replace import atomic_write_bytes
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
from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused
from coordinator_core.ops.owned_red import read_owned_red
from coordinator_core.ops.review_reverify_findings import applied_rows, invert
from coordinator_core.ops.review_tripwires import TripwireScanUnavailable, literal_namers, names_in, tree_scanners, walked_created
from coordinator_core.ops.reverify_callers import callers_of, removed_symbols
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
_MACHINERY_PREFIXES = (".coordinator-local/", "state/")


def _fail(message: str, revert: bool, probe: Optional[str] = None) -> dict:
    if probe == "open":
        return {"probed": [], "rerun": [], "record": None, "error": message}
    if probe == "close":
        return {"restored": [], "rerun": [], "error": message}
    if revert:
        return {
            "reverted": [], "unattributed": [], "pre_existing": [], "owned_red": [], "kept": [],
            "record": None, "error": message,
        }
    return {"edited": [], "created": [], "snapshot": None, "error": message}


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
    repo_root: Path, session: str, manifest: dict, share: Path, slice_prefix: str = ""
) -> Tuple[List[str], List[str]]:
    """(created, foreign). The session's write record spans every run the session fired, so a
    path is this run's only when no other live run declared it, it was written after the
    snapshot, its writing agent wrote no foreign path, and that agent is named by this run's own
    record (``_run_agents``); anything else, an unattributable write included, is foreign and is
    never reverted."""
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
    for raw in writes:
        rel = _rel(raw, repo_root)
        if rel in known or rel.startswith(_MACHINERY_PREFIXES) or rel.startswith(share_rel + "/"):
            continue
        target = repo_root / rel
        if not target.is_file():
            continue
        if rel.startswith("scratch/"):
            foreign.add(rel)
        elif rel in sibling or rel in tracked or _predates(target, since):
            foreign.add(rel)
            foreign_agents.add(writes[raw])
        else:
            candidates.append((rel, writes[raw]))
    members = _run_agents(repo_root, session, since, share_rel, slice_prefix)
    for rel, agent in candidates:
        own = agent not in foreign_agents and agent in members
        (out if own else foreign).add(rel)
    return sorted(out), sorted(foreign)


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
    for key in ("reverted", "unattributed", "pre_existing", "owned_red", "kept"):
        data.setdefault(key, [])
    return data


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


def _sibling_owned(
    repo_root: Path, candidates: List[str], cause_files: Optional[Dict[str, List[str]]],
    footprint: Set[str], run_id: str, plan_path: str,
) -> List[str]:
    """``(sibling_owned, foreign_red)``. ``sibling_owned``: unattributed nodes whose every caller-named
    cause file lies outside ``footprint`` and is withheld by a readable foreign halt record.
    ``foreign_red``: ``{node: [culprit paths]}`` for the rest whose every cause file lies outside
    ``footprint`` and is dirty versus HEAD (so HEAD passes) and none is held by an unreadable record.
    No cause files, an in-footprint one, or a clean one leaves the node blocking."""
    if not cause_files:
        return [], {}
    causes: Dict[str, List[str]] = {}
    for node in candidates:
        raw = cause_files.get(node)
        paths = sorted({_rel(p, repo_root) for p in raw if isinstance(p, str) and p.strip()}) if raw else []
        if paths and not footprint.intersection(paths):
            causes[node] = paths
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


def _open_probe(
    repo_root: Path, snapshot_dir: Path, ids: List[str], implicated: Dict[str, List[str]],
    created_set: Set[str], sidecars: List[str], run_id: str = "", plan_path: str = "",
) -> dict:
    from coordinator_core.ops.dispatch_emit import kept_work

    held = kept_work.held_paths(repo_root, implicated)
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
    snapshot_dir = share / f"{slice_prefix}.pre-review"
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
    created, foreign = _created(repo_root, session, manifest, share, slice_prefix)
    if not _contained(repo_root, created):
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
        try:
            skip_paths = set(edited) | set(created)
            found = set(literal_namers(repo_root, edited + created, exclude=skip_paths))
            if created:
                found.update(tree_scanners(repo_root, created, exclude=skip_paths))
            tripwires = sorted(found)
        except TripwireScanUnavailable as exc:
            tripwires = []
            scan_error = f"tripwire scan unavailable: {exc}"
        out = {
            "edited": edited,
            "created": created,
            "foreign": foreign,
            "tripwires": tripwires,
            "snapshot": snapshot_rel,
            "error": scan_error,
        }
        if unreadable:
            out["head_unreadable"] = True
        return out

    ids = [str(i) for i in failing if isinstance(i, str) and i.strip()]
    created_set = set(created)
    side = list(sidecars or [])
    record_path = snapshot_dir / _RECORD
    prior = _load_record(record_path)
    already = {t for entry in prior["reverted"] if isinstance(entry, dict) for t in entry.get("tests", [])}
    if revert:
        vouched = {str(i) for i in failing_before or []} & probe_io.closed_rerun(snapshot_dir)
        explicit_base = {str(i) for i in failing_at_base or []}
        owned: Set[str] = set()
        if run_key:
            try:
                owned = set(read_owned_red(repo_root, run_key))
            except LedgerRefused as exc:
                return _fail(f"run_key refused: {exc}", True)
        at_base = explicit_base | owned
        recorded_pre = {str(n) for n in prior["pre_existing"]}
        prior_red = already | recorded_pre
        pre_existing = sorted(n for n in ids if n in explicit_base or n in prior_red)
        owned_red = sorted(n for n in ids if n in owned and n not in explicit_base and n not in prior_red)
        not_base_red = sorted(n for n in ids if n in vouched and n not in at_base and n not in prior_red)
        skip = set(pre_existing) | set(owned_red) | set(not_base_red)
    else:
        pre_existing, owned_red, not_base_red, skip = [], [], [], set()
    foreign_set, foreign_ids = set(foreign), []  # type: Set[str], List[str]
    foreign_node = {n for n in ids if n not in skip and _node_file(n) in foreign_set}
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
    for node, targets in implication.items():
        for target in targets:
            implicated.setdefault(target, []).append(node)
        if not targets and node not in already:
            unattributed.append(node)
        if not targets and _sibling_caused(repo_root, node, sibling_py):
            foreign_ids.append(node)

    if probe == "open":
        return _open_probe(repo_root, snapshot_dir, list(implication), implicated, created_set, side, run_id, plan_path)

    plan: _Plan = {}
    refs_by_rel: Dict[str, list] = {}
    kept: List[dict] = []
    from coordinator_core.ops.dispatch_emit import kept_work

    held = kept_work.held_paths(repo_root, implicated)
    for rel in sorted(implicated):
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

    unattributed.extend(n for n in not_base_red if n not in unattributed)
    candidates = [n for n in unattributed if n not in not_base_red]
    if cause_files is None and candidates:
        cause_files = _derive_cause_files(repo_root, candidates, failing_output)
    sibling_owned, foreign_red = _sibling_owned(repo_root, candidates, cause_files, {*rels, *created}, run_id, plan_path)
    if sibling_owned or foreign_red:
        unattributed = [n for n in unattributed if n not in sibling_owned and n not in foreign_red]
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
        "kept": [*prior["kept"], *kept],
    }
    atomic_write_bytes(record_path, (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    declare_write(record_path)
    return {
        "reverted": reverted,
        "unattributed": unattributed,
        "pre_existing": pre_existing,
        "owned_red": owned_red,
        "sibling_owned": sibling_owned,
        "foreign_red": foreign_red,
        "kept": kept,
        "foreign": foreign_ids,
        "index_residue": index_residue,
        "record": _rel(str(record_path), repo_root),
        "error": None,
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
    Keying scope: show_top."""
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
    )
