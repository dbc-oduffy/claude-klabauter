"""
coordinator_core.ops.review_reverify — registers ``review.reverify``: lists the product files a
review stage changed since ``review.prep`` froze them, and inverts the applied findings failing tests implicate.

Params: ``base`` (str), ``slice_prefix`` (str), optional ``session_id``, optional ``failing``
(list of pytest node ids), optional ``sidecars`` (list of review sidecar paths), optional
``failing_before`` (list of node ids), optional ``probe`` (``"open"`` or ``"close"``). Keying scope:
show_top.

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
``tripwires`` is the sorted test modules (not already edited or created) whose source holds a string
literal naming an edited or created path; empty for an empty edit set. ``edited`` is every
manifest path whose bytes now differ from its baseline (a missing file counts); ``created`` is every path the
session's review stage wrote that the manifest and its declared ``paths`` do not name. ``foreign``
is such a path another live run declared that is tracked in the snapshot's base commit, or that predates the snapshot: the session's write record
spans concurrent runs, so it is reported, never reverted or removed. In revert mode ``foreign`` lists
the failing node ids whose test file is foreign.

Revert mode (``failing`` given, no ``probe``): ``{"reverted", "unattributed", "pre_existing",
"kept", "record", "error"}``. A failing node id's test file implicates itself when edited or created,
and every edited ``.py`` module it imports, plus every edited or created path a string literal in it
names. Trap: a tripwire naming several edited paths implicates all of them. An implicated edited
path is rewritten as its current bytes with each applied finding on it inverted (the ledger row's
``after`` replaced by its ``before``; ``action`` ``"reverted-findings"``, ``source`` ``"findings"``);
every other byte, executor hunks included, is untouched. A finding that cannot be inverted, and an
implicated path with no applied finding, go to ``kept`` with a reason and write nothing. A created
path is removed, but one tracked at HEAD is restored to HEAD's bytes, never unlinked. Before
anything is written, the revert (tripwire-named paths included) refuses (``error``, nothing written,
the whole call) when it would remove a top-level symbol that a committed file in the run's surface
(manifest paths plus paths changed anchor..HEAD) still uses. An id implicating nothing is
``unattributed`` and reverts nothing. Every rewritten or removed path is passed to ``declare_write``.
The record ``reverify.json`` merges across calls. Revert refuses while a probe is open.

Probe: ``"probe":"open"`` (with ``failing`` and ``sidecars``) writes the finding-inverted bytes of
each implicated edited path into the tree, stashing the post-review bytes beside the snapshot, and
names the ``rerun`` ids for the test-runner. ``"probe":"close"`` puts the post-review bytes back.
``failing_before`` ids that a closed probe re-ran are ``pre_existing``: they implicate nothing, and
are recorded, never reverted. An id no closed probe re-ran is never honoured.

Negative-spec: zero process spawns, no git verb, no test execution. A refusal (no manifest, a
path escaping the worktree, an unreadable or corrupt blob) is returned as ``error`` with nothing
written, never raised. Attribution is by ledger finding, never by snapshot diff: a write-once
snapshot predates a refire's executor writes.
"""

from __future__ import annotations

import ast
import json
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
from coordinator_core.ops.review_reverify_findings import applied_rows, invert
from coordinator_core.ops.review_tripwires import literal_namers, names_in
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
_MACHINERY_PREFIXES = (".coordinator-local/", "state/")


def _fail(message: str, revert: bool, probe: Optional[str] = None) -> dict:
    if probe == "open":
        return {"probed": [], "rerun": [], "record": None, "error": message}
    if probe == "close":
        return {"restored": [], "rerun": [], "error": message}
    if revert:
        return {
            "reverted": [], "unattributed": [], "pre_existing": [], "kept": [], "record": None, "error": message,
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
    path is this run's only when no other live run declared it and it was written after the
    snapshot; anything else is foreign and is never reverted."""
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
    for raw in writes:
        rel = _rel(raw, repo_root)
        if rel in known or rel.startswith(_MACHINERY_PREFIXES) or rel.startswith(share_rel + "/"):
            continue
        target = repo_root / rel
        if not target.is_file():
            continue
        if rel in sibling or rel in tracked or _predates(target, since):
            foreign.add(rel)
        else:
            out.add(rel)
    return sorted(out), sorted(foreign)


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


def _load_record(record: Path) -> dict:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    for key in ("reverted", "unattributed", "pre_existing", "kept"):
        data.setdefault(key, [])
    return data


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
        out[node] = targets
    return out


def _current(repo_root: Path, rel: str) -> bytes:
    try:
        return (repo_root / rel).read_bytes()
    except OSError:
        return b""


def _refs(refs) -> List[dict]:
    return [{"sidecar": r.sidecar, "id": r.id} for r in refs]


def _open_probe(
    repo_root: Path, snapshot_dir: Path, ids: List[str], implicated: Dict[str, List[str]],
    created_set: Set[str], sidecars: List[str],
) -> dict:
    writes: Dict[str, bytes] = {}
    for rel in sorted(implicated):
        target = repo_root / rel
        if rel in created_set or not target.is_file():
            continue
        inv = invert(target.read_bytes(), applied_rows(repo_root, sidecars, rel))
        if inv.reverted:
            writes[rel] = inv.data
    rerun = [
        n for n in ids
        if _node_file(n) not in created_set and any(n in implicated[rel] for rel in writes)
    ]
    return probe_io.open_probe(repo_root, snapshot_dir, writes, rerun)


def review_reverify(
    repo_root: Path,
    *,
    base: str,
    slice_prefix: str,
    session_id: str = "",
    failing: Optional[List[str]] = None,
    sidecars: Optional[List[str]] = None,
    failing_before: Optional[List[str]] = None,
    probe: Optional[str] = None,
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
        tripwires = sorted(literal_namers(repo_root, edited + created, exclude=set(edited) | set(created)))
        out = {
            "edited": edited,
            "created": created,
            "foreign": foreign,
            "tripwires": tripwires,
            "snapshot": snapshot_rel,
            "error": None,
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
        pre_existing = sorted(n for n in ids if n in vouched)
    else:
        pre_existing = []
    skip = set(pre_existing)
    implication = _implication(repo_root, [n for n in ids if n not in skip], edited, created)
    implicated: Dict[str, List[str]] = {}
    unattributed: List[str] = []
    foreign_set, foreign_ids = set(foreign), []  # type: Set[str], List[str]
    for node, targets in implication.items():
        for target in targets:
            implicated.setdefault(target, []).append(node)
        if not targets and node not in already:
            unattributed.append(node)
            if _node_file(node) in foreign_set:
                foreign_ids.append(node)

    if probe == "open":
        return _open_probe(repo_root, snapshot_dir, list(implication), implicated, created_set, side)

    plan: _Plan = {}
    refs_by_rel: Dict[str, list] = {}
    kept: List[dict] = []
    for rel in sorted(implicated):
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

    refusal = _dangling(repo_root, view, manifest, plan)
    if refusal:
        return _fail(refusal, True)

    reverted: List[dict] = []
    for rel in sorted(plan):
        target = repo_root / rel
        data, source = plan[rel]
        if data is None:
            try:
                target.unlink()
            except OSError:
                return _fail(f"could not remove {rel}", True)
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
        "kept": [*prior["kept"], *kept],
    }
    atomic_write_bytes(record_path, (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    declare_write(record_path)
    return {
        "reverted": reverted,
        "unattributed": unattributed,
        "pre_existing": pre_existing,
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
    optional ``sidecars`` (list[str]), optional ``failing_before`` (list[str]), optional ``probe``
    (``"open"`` or ``"close"``). Keying scope: show_top."""
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
    if sidecars is not None and not isinstance(sidecars, list):
        return _fail("review.reverify sidecars must be a list of paths", revert, shape)
    return review_reverify(
        repo_root,
        base=str(params.get("base") or ""),
        slice_prefix=str(params.get("slice_prefix") or ""),
        session_id=str(params.get("session_id") or ""),
        failing=failing,
        sidecars=[str(s) for s in sidecars] if sidecars else None,
        failing_before=[str(i) for i in failing_before] if failing_before else None,
        probe=shape,
    )
