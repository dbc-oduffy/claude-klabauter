"""The rebuilt run landing behind `dispatch.terminal_commit`.

`land_run` is the product-landing branch only; the halted, fanout, grind and no-script modes stay on
their own modules. Invariants it discharges: `docs/reference/run-landing-invariants.md` (each function
names its `RL-nn` id). Shape: one scandir per landed directory, one read per landed file, the
tree/pack/index written in process, one compare-and-swap on the ref, zero git spawns. Shapes the
in-process writer does not cover (signing, merge state, unborn HEAD, symlinks, an own-run kept blob)
take `git.commit.commit_paths`.

Bookkeeping: the trailers (RL-07d), completion receipts, tracked outcome and delivery note (RL-08) join
the one landing commit. The coded stamp (RL-11) and the register, ledger and resolution writes (RL-12)
name the landing's own sha, which no tree that sha hashes can contain, so they ride one in-process
follow-up commit built from the same helpers (`_coded_stamp`). A repo that has not onboarded gets none
of it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import struct
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Optional

from coordinator_core.git.ref_lock import RefLockHeld

#: Settings-home key `dispatch.terminal_commit` reads to choose the landing path. Defined once: the
#: cutover switch and the benchmark driver both import it.
LANDING_IMPL_KEY = "terminal_commit_landing"
IMPL_OLD = "old"
IMPL_NEW = "new"
LANDING_IMPLS = (IMPL_OLD, IMPL_NEW)
#: A missing or unrecognised key value selects this path until cutover.
LANDING_IMPL_DEFAULT = IMPL_OLD

_MODE_KEYS = ("halted_by", "fanout", "grind")
_SUFFIX_PATTERN = re.compile(r"^\*(\.[A-Za-z0-9_]+)?$")
_ZERO_STAT = SimpleNamespace(st_ctime=0, st_mtime=0, st_size=0)
#: The local, never-committed row per sizing a landing resolved whole.
RESOLUTION_LEDGER_REL = ".coordinator-local/sizings.jsonl"


class LandRunNotBuilt(NotImplementedError):
    """Retained for importers of the C1 stub; `land_run` no longer raises it."""


class _Refused(Exception):
    """A landing refusal: becomes the reply `refused` kind."""

    def __init__(self, kind: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.kind, self.extra = kind, extra


def _error(message: str, **extra: object) -> dict:
    out: dict = {"committed": False, "sha": None, "error": message}
    out.update(extra)
    return out


def _ref_lock_error(exc: RefLockHeld) -> dict:
    """A held ref lock is not a peer's ref move: the reply names the lock, its age and content."""
    return _error(str(exc), refused="ref-lock-held", lock_path=str(exc.lock_path),
                  lock_age_secs=exc.age_secs, lock_content=exc.content, lock_stale=exc.stale)


def _norm(path: str) -> str:
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def land_run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land one run's product commit; returns the reply dict `dispatch.terminal_commit` returns.

    `params` are the op's params; `repo_root` is the git common dir, as the op handler receives it.
    RL-22: the reply keeps `committed`, `sha`, `refused`, `error`, `stranded`, `undeclared_dirty`,
    `chunks_committed`, `run_artifacts`, `residue` and `ok`."""
    if repo_root is None:
        return _error("run landing requires a common_dir-keyed dispatch; repo_root was not supplied")
    if not isinstance(params, dict):
        return _error("params must be an object")
    if any(params.get(k) for k in _MODE_KEYS):
        return _error("run_landing.land_run lands the product branch only", refused="mode-ambiguous")

    from coordinator_core.lifecycle import main_worktree_root

    root = main_worktree_root(repo_root)
    refusal = _review_floor(params.get("inline_review"))
    if refusal is not None:
        return refusal
    token = _claim_token(root, params)
    if isinstance(token, dict):
        return token
    landed, sha = False, None
    try:
        reply = _land(root, params)
        landed = bool(reply.get("committed"))
        sha = reply.get("sha")
    except _Refused as exc:
        reply = _error(str(exc), refused=exc.kind, **exc.extra)
    finally:
        _settle_token(root, params, token, landed, sha)
    return reply


def _review_floor(inline_review: object) -> Optional[dict]:
    """RL-10: a run with no review-stage output cannot land."""
    if inline_review is None:
        return _error("params.inline_review is absent -- the run carries no review-stage output",
                      refused="unreviewed")
    if not isinstance(inline_review, dict) or not inline_review.get("integration_stem") \
            or inline_review.get("slices") is None:
        return _error("params.inline_review is missing integration_stem and/or slices", refused="unreviewed")
    return None


def _claim_token(root: Path, params: dict):
    """RL-17: redeem `params.run_token`; a dict return is the reply that ends the call, `True` means
    this call holds the claim, `None` means the call carries no token."""
    raw = params.get("run_token")
    if raw is None:
        return None
    from coordinator_core.ops.dispatch_emit import run_token, terminal_commit_reconcile

    args = (root, params.get("run_id"), raw, params.get("script_path"))
    if run_token.redeem(*args):
        return True
    landed = terminal_commit_reconcile.already_landed_reply(*args)
    if landed is not None:
        return landed
    if run_token.claim_pending(*args):
        return _error("a first call of this run's landing still holds the run token claim",
                      refused="run-token-in-flight")
    return _error("params.run_token does not match a live, unspent token for params.run_id and this script",
                  refused="run-token")


def _settle_token(root: Path, params: dict, token: object, landed: bool, sha: object) -> None:
    """RL-17: a landed commit spends the token; anything else releases the claim for a re-fire."""
    if token is not True:
        return
    from coordinator_core.ops.dispatch_emit import run_token

    if landed:
        run_token.consume(root, params.get("run_id"), landed={"sha": sha})
    else:
        run_token.release(root, params.get("run_id"), params["run_token"], params.get("script_path"))


# --- request, scope and landing set ---------------------------------------------------------------


def _read_request(root: Path, params: dict):
    """The `CommitRequest` the run promised, plus the ask run dir when a manifest named one."""
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
    )

    script_rel = params.get("script_path")
    if not isinstance(script_rel, str) or not script_rel:
        raise _Refused("script-missing", "params.script_path is required")
    script = contained_path(root / script_rel, [root])
    if script is None:
        raise _Refused("script-escapes", f"params.script_path {script_rel!r} does not resolve under the worktree")
    try:
        text = script.read_text(encoding="utf-8")
    except OSError as exc:
        raise _Refused("script-missing", f"cannot read params.script_path {script_rel!r}: {exc}")
    try:
        manifest_rel = parse_manifest_marker(text)
        if manifest_rel is None:
            return parse_marker(text), None, text
        run_root = root / RUN_DIR_ROOT
        manifest_abs = contained_path(root / manifest_rel, [root / RECORD_DIR_ROOT, run_root])
        if manifest_abs is None:
            raise _Refused("manifest-escapes-run-root", f"ask-run-manifest path {manifest_rel!r} escapes the run root")
        manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
        request_abs = contained_path(root / manifest.marker_path, [run_root])
        if request_abs is None:
            raise _Refused("manifest-escapes-run-root", f"marker_path {manifest.marker_path!r} escapes the run root")
        return parse_marker(request_abs.read_text(encoding="utf-8")), manifest_abs.parent, text
    except MalformedCommitRequestError as exc:
        raise _Refused("malformed-request", f"malformed commit request: {exc}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _Refused("malformed-request", f"cannot read the ask-run manifest: {exc!r}")


def _declared(root: Path, request, incomplete: set):
    """RL-09: `(landing paths, stranded, done chunk ids, paths by chunk id)`: the done chunks' declared
    paths, and the incomplete chunks' paths named stranded. Prefix claims count only when the chunk's own report lists them."""
    from coordinator_core.ops.dispatch_emit.commit_request import parse_prefix_claims

    landing: dict = {}
    stranded: dict = {}
    chunk_ids: list = []
    by_chunk: dict = {}
    for chunk in request.chunks:
        paths = [_norm(p) for p in chunk.paths if p]
        if chunk.prefixes and chunk.report:
            try:
                claims = parse_prefix_claims((root / chunk.report).read_text(encoding="utf-8")) or []
            except OSError:
                claims = []
            prefixes = tuple(_norm(p).rstrip("/") + "/" for p in chunk.prefixes)
            paths += [_norm(c) for c in claims if _norm(c).startswith(prefixes)]
        if chunk.id in incomplete:
            if paths:
                stranded[chunk.id] = list(dict.fromkeys(paths))
            continue
        chunk_ids.append(chunk.id)
        by_chunk[chunk.id] = paths
        for p in paths:
            landing[p] = None
    return list(landing), stranded, chunk_ids, by_chunk


def _slice_refusal(inline_review: dict, paths: list) -> None:
    """RL-03 (ccc7d1): a landed code path no reviewed slice covers refuses before any write. An absent
    or empty slice list is not a comparison."""
    from coordinator_core.ops.dispatch_emit import review_floor
    from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import REFUSED_UNREVIEWED

    prep = inline_review.get("prep")
    reviewed = prep.get("slice_files") if isinstance(prep, dict) else None
    if not (isinstance(reviewed, list) and reviewed):
        return
    gap = review_floor.slice_gap_paths(paths, reviewed)
    if gap:
        shown = ", ".join(gap[:5]) + (f" (+{len(gap) - 5} more)" if len(gap) > 5 else "")
        raise _Refused(REFUSED_UNREVIEWED, f"{len(gap)} staged path(s) sit in no reviewed slice: {shown}",
                       unreviewed_paths=gap)


def _scandir_all(root: Path, dirs) -> dict:
    """RL-04: `{dir: {name: DirEntry}}` for exactly the directories the run declared, non-recursive.
    A glob character in a directory name is never a pattern: this is a path join, not a pathspec."""
    out: dict = {}
    for d in dirs:
        try:
            with os.scandir(os.path.join(str(root), d) if d else str(root)) as it:
                out[d] = {e.name: e for e in it}
        except OSError:
            out[d] = {}
    return out


def _live_paths(root: Path, paths: list, scans: dict, spine: dict) -> tuple:
    """RL-18, RL-19, RL-20: `(present, deleted, dropped_absent)`. A machine-local or never-commit path is
    filtered; a case-variant of a tracked path lands under git's spelling; a tracked path the worktree
    lacks is a declared deletion; an absent path HEAD lacks is dropped."""
    from coordinator_core.git.machine_local_paths import machine_local_paths
    from coordinator_core.ops.ceremony.commit_gates import is_never_commit

    skipped = set(machine_local_paths(paths))
    present: list = []
    deleted: list = []
    dropped: list = []
    missing: list = []
    for p in paths:
        if p in skipped or is_never_commit(p) or p == ".git" or p.startswith(".git/"):
            continue
        d, _, name = p.rpartition("/")
        entry = scans.get(d, {}).get(name)
        if entry is not None:
            if not entry.is_dir(follow_symlinks=False):
                present.append(p)
            continue
        missing.append(p)
    if missing:
        from coordinator_core.git.tracked_case import tracked_spelling

        spelled = tracked_spelling(root, missing)
        for p in missing:
            actual = spelled.get(p) or p
            d, _, name = actual.rpartition("/")
            if actual != p and name in _dir_names(root, d, scans):
                present.append(actual)
            elif name in spine.get(d, {}) or (actual != p and _in_spine(spine, actual)):
                deleted.append(actual)
            else:
                dropped.append(p)
    return list(dict.fromkeys(present)), list(dict.fromkeys(deleted)), dropped


def _dir_names(root: Path, d: str, scans: dict) -> dict:
    if d not in scans:
        scans.update(_scandir_all(root, [d]))
    return scans[d]


def _in_spine(spine: dict, path: str) -> bool:
    d, _, name = path.rpartition("/")
    return name in spine.get(d, {})


# --- worktree read, check-in form, own hunks ------------------------------------------------------


def _attr_set_keyed_by_suffix(root: Path, dirs) -> bool:
    """RL-01: True when every attribute line that can govern a landed directory names `*` or `*.ext`,
    so one lookup per (directory, suffix) answers for every path in it; a name-specific pattern
    returns False and the caller resolves per path."""
    from coordinator_core.git.git_dir import resolve_git_common_dir

    files = [resolve_git_common_dir(root) / "info" / "attributes"]
    for d in dirs:
        parts = d.split("/") if d else []
        files += [root.joinpath(*parts[:i]) / ".gitattributes" for i in range(len(parts) + 1)]
    for attrs in set(files):
        try:
            text = attrs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            code = line.split("#", 1)[0].split()
            if code and not _SUFFIX_PATTERN.match(code[0]):
                return False
    return True


def _plain_disposition(root: Path, probe_rel: str):
    """`'auto'` when the attributes leave `core.autocrlf` to decide, `'raw'` when `-text` pins the
    bytes, None when a filter, macro or text pin governs and the path needs the general resolver."""
    from coordinator_core.git import checkin_attrs

    if checkin_attrs.clean_filter_may_apply(root, probe_rel) is not None:
        return None
    disposition = checkin_attrs.checkin_disposition(root, probe_rel)
    if disposition == checkin_attrs.UNSET:
        return "auto"
    if disposition == checkin_attrs.BINARY:
        return "raw"
    return None


class _CheckinForm:
    """The bytes git would hash for a worktree file, resolved once per (directory, suffix) when the
    attribute set allows it. A filter or text pin falls to `git.commit._checkin_bytes`, which raises
    `FilterUnsupported` for a clean filter the process cannot run."""

    def __init__(self, root: Path, dirs) -> None:
        from coordinator_core.git import commit as gc

        self.root = root
        self._gc = gc
        self._autocrlf = gc._autocrlf_once(root)
        self._keyed = _attr_set_keyed_by_suffix(root, dirs)
        self._kinds: dict = {}

    def __call__(self, d: str, name: str, rel: str, data: bytes) -> bytes:
        kind = None
        if self._keyed:
            suffix = os.path.splitext(name)[1]
            key = (d, suffix)
            if key not in self._kinds:
                self._kinds[key] = _plain_disposition(self.root, f"{d}/_probe{suffix}" if d else f"_probe{suffix}")
            kind = self._kinds[key]
        if kind == "raw":
            return data
        if kind == "auto":
            if b"\r" in data and self._autocrlf():
                from coordinator_core.git import content_hash

                return content_hash._autocrlf_checkin_normalize(data)
            return data
        return self._gc._checkin_bytes(self.root, rel, data, self._autocrlf)


def _read_file(entry) -> tuple:
    """`(bytes, stat)` from one open/read/close; the stat comes from the scan's DirEntry."""
    st = entry.stat()
    fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        data = os.read(fd, st.st_size + 1)
    finally:
        os.close(fd)
    if len(data) != st.st_size:
        raise _Refused("changed-while-read", f"{entry.path} changed while it was read")
    return data, st


def _peer_blobs(root: Path, own_run: object, rels: set) -> dict:
    """RL-02: `{path: [blob sha]}` other runs' `kept.json` hold for `rels`; those hunks are not this
    run's. One scandir of the record root and one open per record dir."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    found: dict = {}
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            runs = [e.path for e in it if e.name != own_run and e.is_dir(follow_symlinks=False)]
    except OSError:
        return found
    for run_dir in runs:
        try:
            with open(os.path.join(run_dir, "kept.json"), "rb") as handle:
                kept = json.loads(handle.read())
        except (OSError, ValueError):
            continue
        for chunk in (kept.get("chunks") or {}).values() if isinstance(kept, dict) else ():
            for path, sha in ((chunk or {}).get("paths") or {}).items():
                if _norm(path) in rels and isinstance(sha, str):
                    found.setdefault(_norm(path), []).append(sha)
    return found


def _own_view(head: bytes, work: bytes, peers: list) -> bytes:
    """RL-01, RL-02: the worktree bytes with every hunk a peer's blob also made reverted to HEAD, so a
    foreign hunk stays dirty in the worktree and out of the commit. Line terminators are ignored when
    matching, kept when copying."""
    def key(blob: bytes) -> list:
        return [ln.rstrip(b"\r\n") for ln in blob.splitlines(True)]

    h_lines, w_lines = head.splitlines(True), work.splitlines(True)
    hk, wk = key(head), key(work)
    foreign = set()
    for peer in peers:
        pk = key(peer)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, hk, pk, autojunk=False).get_opcodes():
            if tag != "equal":
                foreign.add((i1, i2, tuple(pk[j1:j2])))
    out: list = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, hk, wk, autojunk=False).get_opcodes():
        if tag != "equal" and (i1, i2, tuple(wk[j1:j2])) in foreign:
            out += h_lines[i1:i2]
        else:
            out += w_lines[j1:j2]
    return b"".join(out)


# --- scans over the landed buffers ----------------------------------------------------------------


def _secret_findings(common: Path, spine: dict, buffers: dict) -> list:
    """RL-15: lines the landing adds that match a credential pattern, scanned on the landing's own
    buffers (no second read). A binary or oversize buffer, and a line HEAD already holds, is not judged."""
    from coordinator_core.ops.dispatch_emit import landing_secrets as ls

    any_secret = re.compile("|".join(f"(?:{p.pattern})" for _label, p in ls._PATTERNS))
    found: list = []
    for rel, data in buffers.items():
        if len(data) > ls._MAX_SCAN_BYTES or b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        d, _, name = rel.rpartition("/")
        head_entry = spine.get(d, {}).get(name)
        existing: set = set()
        if head_entry is not None:
            from coordinator_core.git.git_objects import read_object

            stored = read_object(common, head_entry[1])
            if stored is not None and stored[0] == "blob":
                existing = set(stored[1].decode("utf-8", errors="replace").splitlines())
        added = [(number, line) for number, line in enumerate(text.splitlines(), 1) if line not in existing]
        if not added or not any_secret.search("\n".join(line for _n, line in added)):
            continue
        for number, line in added:
            for label, pattern in ls._PATTERNS:
                if pattern.search(line):
                    found.append(ls.SecretFinding(rel, number, label))
                    break
    return found


def _leak_advisory(root: Path, buffers: dict) -> Optional[list]:
    """RL-46 (advisory only): `leak_gate` diagnostics over the changed buffers; never a refusal."""
    if not buffers:
        return None
    try:
        from coordinator_core.authoring_leaks import leak_gate

        outcome = leak_gate(root, list(buffers),
                            landed_text=lambda rel: (buffers[rel].decode("utf-8", errors="replace")
                                                     if rel in buffers else None))
    except Exception as exc:  # noqa: BLE001 -- an advisory never fails a landing
        return [f"leak_gate unavailable: {type(exc).__name__}"]
    return None if outcome.passed else [str(d) for d in list(outcome.diagnostics)[:5]]


def _undeclared_dirty(root: Path, scans: dict, spine: dict, landed: set) -> list:
    """RL-04: untracked, un-ignored files in the declared directories (non-recursive) that the landing
    did not carry; a glob character in a directory name is a literal."""
    from coordinator_core.git.ignore_match import IgnoreMatcher

    matcher = IgnoreMatcher(root)
    out: list = []
    for d, entries in scans.items():
        tracked = spine.get(d, {})
        for name, entry in entries.items():
            rel = f"{d}/{name}" if d else name
            if name in tracked or rel in landed or name == ".git" or entry.is_dir(follow_symlinks=False):
                continue
            if matcher.ignored(rel, False) is True:
                continue
            out.append(rel)
    return sorted(out)


# --- message --------------------------------------------------------------------------------------


def _message(root: Path, params: dict, request, chunk_ids: list, deleted: list, script_text: str,
             run_id: str, book: Optional[SimpleNamespace] = None) -> str:
    """RL-06, RL-07: subject, caller message, and the trailer block: Deliverable-Id, Resolves-Sizing,
    Review-Residue, Inline-Review, Ledger-Receipt, Checkpoint-Run-Closed, then the provider's
    Co-Authored-By and the Session-Id. `book` carries the RL-07d inputs (`resolves`, `review_land`,
    `ledger`)."""
    from coordinator_core.git.commit import _attach_session_id_trailer
    from coordinator_core.git.provider_attribution import attribution_value
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines, script_run_base
    from coordinator_core.ops.dispatch_emit.commit_request import plan_trailer_deliverable_ids

    review = params["inline_review"]
    done = [c for c in request.chunks if c.id in chunk_ids]
    ids = ", ".join(c.id for c in done)
    titles = "; ".join(c.title for c in done if c.title.strip())
    subject = (f"{ids}: {titles}" if titles else ids) if ids else f"review trail: {review['integration_stem']}"
    paragraphs = [subject]
    caller = params.get("message")
    if isinstance(caller, str) and caller.strip():
        paragraphs.append(caller.strip())
    if deleted:
        paragraphs.append("Removes declared write(s): " + ", ".join(deleted))
    trailers: list = []
    if request.deliverable_id:
        trailers.append(f"Deliverable-Id: {request.deliverable_id}")
    if request.plan_path:
        try:
            plan_text = (root / request.plan_path).read_text(encoding="utf-8")
        except OSError:
            plan_text = None
        if plan_text is not None:
            trailers += [f"Deliverable-Id: {d}" for d in plan_trailer_deliverable_ids(plan_text)
                         if d != request.deliverable_id]
    if book is not None and book.resolves is not None:
        trailers.append(f"Resolves-Sizing: {book.resolves['id']} {book.resolves['tshirt']}")
    if book is not None:
        trailers += [f"Review-Residue: {p}" for p in book.review_land]
    trailers.append(f"Inline-Review: applies {review['integration_stem']} -- execute-review: "
                    f"{review['slices']} slices, {review.get('fixes')} fixes")
    if book is not None and book.ledger is not None:
        from coordinator_core.ops.dispatch_emit import review_floor

        trailers.append(f"Ledger-Receipt: {review_floor.receipt(review_floor.verdict_count(review, root))}")
    bases = [params.get("run_base_sha"), request.review_run_base, script_run_base(script_text)]
    trailers += run_closed_trailer_lines(b for b in bases if b)
    if book is not None:
        trailers += [f"Completion-Receipt: {rid}" for rid in book.local_receipt_ids]
    attribution = attribution_value()
    if attribution:
        trailers.append(f"Co-Authored-By: {attribution}")
    paragraphs.append("\n".join(trailers))
    return _attach_session_id_trailer("\n\n".join(paragraphs) + "\n", root)


# --- the landing ----------------------------------------------------------------------------------


def _run_id(params: dict, ask_run_dir: Optional[Path], script_rel: str) -> str:
    rid = params.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    if ask_run_dir is not None:
        return ask_run_dir.name
    return PurePosixPath(script_rel.replace("\\", "/")).stem


def _record_paths(root: Path, ask_run_dir: Optional[Path]) -> list:
    """RL-08: the files directly in the run's record dir ride the product commit; `prompts/`,
    ignored and machine-local files do not."""
    from coordinator_core.git.ignore_match import IgnoreMatcher
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    if ask_run_dir is None:
        return []
    try:
        rel_dir = ask_run_dir.relative_to(root.resolve()).as_posix()
    except ValueError:
        return []
    if not (rel_dir == RECORD_DIR_ROOT or rel_dir.startswith(RECORD_DIR_ROOT + "/")):
        return []
    matcher = IgnoreMatcher(root)
    out: list = []
    with os.scandir(ask_run_dir) as it:
        for entry in it:
            rel = f"{rel_dir}/{entry.name}"
            if entry.is_file(follow_symlinks=False) and matcher.ignored(rel, False) is not True:
                out.append(rel)
    return sorted(out)


# --- bookkeeping: trailers, records and the writes that need the landing sha -----------------------


def _prepare_bookkeeping(root: Path, params: dict, request, ask_run_dir: Optional[Path], chunk_ids: list,
                         incomplete: set, paths: list, onboarded: bool = True) -> SimpleNamespace:
    """RL-07d, RL-08: what joins the one landing commit before it is built: the cited sizing
    (`Resolves-Sizing`), the review stage's own edits (`Review-Residue`), the bound ledger
    (`Ledger-Receipt`), and the files the landing adds (tracked outcome, delivery note, completion
    receipts). Reads only the named files; no tree read, no spawn. A repo that has not onboarded
    hosts no coordinator state: it gets no outcome file, no delivery note and no ledger trailer."""
    book = SimpleNamespace(extra_paths=[], receipts=[], resolves=None, review_land=[], ledger=None,
                           receipt_error=None, receipt_ids=[], local_receipt_ids=[], restore={},
                           onboarded=onboarded)
    declared = set(paths)
    if request.plan_path and not incomplete:
        book.resolves = _resolving_sizing(root, request.plan_path)
    book.review_land = [rel for rel in sorted(_review_edited(params["inline_review"]))
                        if rel not in declared and (root / rel).is_file()]
    book.extra_paths += book.review_land
    book.extra_paths += _render_op_catalog(root, declared, book.restore)
    if ask_run_dir is not None and onboarded:
        from coordinator_core.ops.dispatch_emit import requirements_ledger, run_artifacts
        from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_locate

        try:
            book.ledger = requirements_ledger.read_ledger(root, ask_run_dir)
        except (ValueError, OSError):
            book.ledger = None
        outcome_rel = run_artifacts.outcome_rel(ask_run_dir.name)
        try:
            book.restore[outcome_rel] = (root / outcome_rel).read_bytes()
        except OSError:
            book.restore[outcome_rel] = None
        outcome = run_artifacts.write_tracked_outcome(root, ask_run_dir.name, "landed")
        note = delivery_note_locate(root, ask_run_dir.name)
        book.extra_paths += [p for p in (outcome, note if (root / note).is_file() else None) if p]
    book.receipts, book.receipt_error, book.receipt_ids = _write_receipts(
        root, params, request, ask_run_dir, chunk_ids, incomplete)
    if ask_run_dir is None:
        book.extra_paths += book.receipts
    else:
        book.local_receipt_ids = list(book.receipt_ids)
    return book


def _render_op_catalog(root: Path, declared: set, restore: dict) -> list:
    """The generated op-catalog files the landing's op-source edits change, written under `root` when
    their text differs; the returned paths join the commit. A landing that already carries all three
    adds nothing; a render that cannot be made refuses `op-catalog-render-failed`. `restore` takes each
    overwritten file's prior bytes so a landing that does not commit puts them back."""
    from coordinator_core.contract import landing_render

    if all(rel in declared for rel in landing_render.GENERATED):
        return []
    try:
        texts = landing_render.render_for_landing(root, sorted(declared))
    except landing_render.RenderRefused as exc:
        raise _Refused("op-catalog-render-failed", f"{exc.path}: {exc.reason}")
    changed = []
    for rel, text in (texts or {}).items():
        target = root / rel
        data = text.encode("utf-8")
        try:
            before = target.read_bytes()
        except OSError:
            before = None
        if before != data:
            restore[rel] = before
            with open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        changed.append(rel)
    return changed


def _write_receipts(root: Path, params: dict, request, ask_run_dir: Optional[Path], chunk_ids: list,
                    incomplete: set) -> tuple:
    """RL-08: `(receipt paths, error, receipt ids)`. An ask run homes its receipts under `.coordinator-local/`,
    so they never ride the commit and the message names them by `Completion-Receipt` trailer; a plan
    run's ride the commit. A write failure is an advisory, not a refusal (RL-47)."""
    from datetime import datetime, timezone

    from coordinator_core.completion_receipts.store import write_receipt
    from coordinator_core.git import git_state
    from coordinator_core.ops.dispatch_emit.completion_receipt import build_run_receipts

    written: list = []
    try:
        built = build_run_receipts(
            root, request, done_ids=list(chunk_ids), incomplete_ids=sorted(incomplete), record=None,
            script_path=params["script_path"], session_id=params.get("session_id"),
            base_sha=git_state.head_sha(root), branch=git_state.head_branch(root),
            now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), local=ask_run_dir is not None,
        )
        for fm, prose in built:
            written.append(write_receipt(root, fm, prose))
        return written, None, [fm["receipt_id"] for fm, _prose in built]
    except Exception as exc:  # noqa: BLE001 -- a receipt is an advisory; no partial set survives
        _remove_files(root, written)
        return [], f"completion receipt write failed: {exc}", []


def _remove_files(root: Path, rels: list) -> None:
    for rel in rels:
        try:
            (root / rel).unlink()
        except OSError:
            pass


def _restore_unlanded(root: Path, book: SimpleNamespace) -> None:
    """RL-08: a landing that did not commit leaves no receipt behind and puts the tracked outcome back
    to what it was, so no file claims a landing that never happened."""
    _remove_files(root, book.receipts)
    for rel, before in book.restore.items():
        try:
            if before is None:
                (root / rel).unlink()
            else:
                (root / rel).write_bytes(before)
        except OSError:
            pass


def _review_edited(inline_review: object) -> set:
    """RL-07d: repo-relative paths the review's test-runner or reverify record lists as `edited`,
    `created` or `outside_declared`; an absolute or `..` path is not the repo's."""
    found: set = set()
    if not isinstance(inline_review, dict):
        return found
    for key in ("tests", "reverify"):
        block = inline_review.get(key)
        for field in ("edited", "created", "outside_declared"):
            listed = block.get(field) if isinstance(block, dict) else None
            for p in listed if isinstance(listed, list) else ():
                if not isinstance(p, str) or not p:
                    continue
                n = _norm(p)
                if n.startswith("/") or n[1:2] == ":" or ".." in n.split("/"):
                    continue
                found.add(n)
    return found


def _resolving_sizing(root: Path, plan_path: str) -> Optional[dict]:
    """RL-07d: `{rel, id, tshirt, route, ask_sha256}` for the sizing the plan cites as `sizing_object`
    when that is an existing file in the sizings home; the `Resolves-Sizing` trailer and the resolution
    row read it."""
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load
    from coordinator_core.ops.dispatch_emit.register_gate import sizing_file_rel
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    plan = read_rel(root, plan_path)
    split = split_frontmatter(plan) if plan is not None else None
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    rel = sizing_file_rel(root, cited) if cited else None
    text = read_rel(root, rel) if rel else None
    if text is None:
        return None
    try:
        doc = yaml_load(text) or {}
    except Exception:  # noqa: BLE001 -- an unparseable sizing resolves nothing
        return None
    if not isinstance(doc, dict):
        return None
    estimate = doc.get("estimate") if isinstance(doc.get("estimate"), dict) else {}
    words = "\n\n".join(str(v.get("text")) for v in doc.get("pm_verbatims") or []
                        if isinstance(v, dict) and v.get("text")) or str(doc.get("intent") or "")
    return {"rel": rel, "id": doc.get("deliverable_id") or PurePosixPath(rel).stem,
            "tshirt": estimate.get("tshirt") or "?", "route": doc.get("route"),
            "ask_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest()}


def _append_resolution(root: Path, resolves: dict, sha: str) -> dict:
    """RL-12: one row naming the landing sha in the local resolution ledger (never committed). A failed
    append is reported, never a refusal."""
    from datetime import datetime, timezone

    row = {"id": resolves["id"], "tshirt": resolves["tshirt"], "route": resolves["route"],
           "ask_sha256": resolves["ask_sha256"], "resolved_sha": sha,
           "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sizing": resolves["rel"]}
    try:
        from coordinator_core.atomic_append import append_line

        sink = root / RESOLUTION_LEDGER_REL
        sink.parent.mkdir(parents=True, exist_ok=True)
        append_line(sink, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- the landing stands
        return {"sizing_ledger_error": repr(exc)}
    return {"sizing_resolved": row}


def _write_register(root: Path, plan_path: str, inline_review: object, sha: str) -> dict:
    """RL-12: the judge's per-row verdicts written into the cited sizing's register at the landing sha,
    in place under the file lock. `{register_sizing: rel}` on a write, `{}` when nothing applies,
    `{register_writeback_error}` when the result fails the sizing schema (the file is left as it was)."""
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not isinstance(criterion, dict):
        return {}
    from coordinator_core.ops import requirement_register as rr

    judged = [r for r in criterion.get(rr.JUDGE_ROWS_KEY) or []
              if isinstance(r, dict) and r.get(rr.JUDGE_CLAIM, rr.CLAIM_THIS_PLAN) == rr.CLAIM_THIS_PLAN]
    if not judged:
        return {}
    from coordinator_core.ops.dispatch_emit.register_gate import register_target

    sizing_rel, claimed, _own, _pins = register_target(root, plan_path)
    if sizing_rel is None:
        return {}
    from datetime import datetime, timezone

    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    schema = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"
    now = datetime.now(timezone.utc)

    def mutate(old: str) -> str:
        if rr.read_register(old) is None:
            raise MutateAbort("")
        new = rr.apply_verdicts(old, judged, plan_path, sha, now, frozenset(claimed))
        if new == old:
            raise MutateAbort("")
        try:
            errors = validate_frontmatter(yaml_load(new) or {}, schema)
        except Exception as exc:  # noqa: BLE001 -- a refusal, surfaced
            raise MutateAbort(f"register write-back refused: {exc}")
        if errors:
            raise MutateAbort(f"register write-back refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(root / sizing_rel, mutate, repo_root=root)
    except MutateAbort as exc:
        reason = str(exc.args[0]) if exc.args else ""
        return {"register_writeback_error": reason} if reason else {}
    except Exception as exc:  # noqa: BLE001 -- lock timeout or I/O: the landing stands
        return {"register_writeback_error": f"register write-back refused: {exc!r}"}
    return {"register_sizing": sizing_rel}


def _review_differs(root: Path, scans: dict, spine: dict, rel: str) -> bool:
    """RL-07d: True when a review-edited path is new or differs from HEAD (a CRLF copy of the HEAD blob
    does not), so `Review-Residue` names only paths the landing carries."""
    d, _, name = rel.rpartition("/")
    head_entry = spine.get(d, {}).get(name)
    entry = scans.get(d, {}).get(name)
    if head_entry is None or entry is None:
        return True
    try:
        data = Path(entry.path).read_bytes()
    except OSError:
        return True
    return all(hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest() != head_entry[1]
               for body in (data, data.replace(b"\r\n", b"\n")))


def _after_landing(root: Path, params: dict, request, book: SimpleNamespace, by_chunk: dict, changed: set,
                   chunk_ids: list, sha: str, run_id: str) -> dict:
    """RL-08, RL-11, RL-12: reply fields and the writes that name the landing sha: register and ledger
    writeback, the resolution row, then the coded stamp. Each never raises; the landing stands."""
    out: dict = {}
    if book.receipts:
        out.update(receipts=list(book.receipts), receipt_coverage="written")
    if book.receipt_error:
        out["receipt_error"] = book.receipt_error
    if book.review_land:
        out["review_residue"] = sorted(book.review_land)
    stamp_paths: list = []
    if request.plan_path and book.onboarded:
        writeback = _write_register(root, request.plan_path, params["inline_review"], sha)
        sizing = writeback.pop("register_sizing", None)
        out.update(writeback)
        if sizing:
            stamp_paths.append(sizing)
    verdict = params.get("ledger_verdict")
    if book.ledger is not None and book.ledger.sizing_rel and isinstance(verdict, dict):
        from coordinator_core.ops.dispatch_emit import requirements_ledger

        try:
            requirements_ledger.apply_verdict(root, book.ledger.sizing_rel, verdict, run_id=run_id, sha=sha)
            stamp_paths.append(book.ledger.sizing_rel)
        except Exception as exc:  # noqa: BLE001 -- surfaced; the landing stands
            out["requirements_writeback_error"] = str(exc)
        owed = requirements_ledger.post_commit_owed(verdict, sha)
        if owed:
            out["post_commit_owed"] = owed
    if book.resolves is not None:
        out.update(_append_resolution(root, book.resolves, sha))
    if book.onboarded:
        out.update(_coded_stamp(root, params, request, by_chunk, changed, chunk_ids, sha, run_id, stamp_paths))
    else:
        out.update(rows_coded={}, coded_sha=None, coded_skipped="repo-not-opted-in")
    return out


def _coded_stamp(root: Path, params: dict, request, by_chunk: dict, changed: set, chunk_ids: list, sha: str,
                 run_id: str, extra_paths: list) -> dict:
    """RL-11: flip the landed chunks' plan rows `open` -> `coded` at `sha`, and land those plan edits plus the
    register writes in one in-process follow-up commit. It is a second commit because the stamp names
    the landing's own sha, which cannot be inside the tree that sha hashes; the first commit carries
    every trailer and record, so a re-fire sees one landing and one stamp. A row whose chunk landed no
    changed path closes `coded` with the no-change detail."""
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
    from coordinator_core.ops.dispatch_emit.plan_row_coding import NOOP_DETAIL, flip_rows_coded, source_rows_by_plan
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel
    from coordinator_core.session.claimed_write import replace_text

    originals: dict = {}
    rows_coded: dict = {}

    def restore() -> None:
        for rel, text in originals.items():
            replace_text(root / rel, text)

    plan_rel = request.plan_path
    if plan_rel and chunk_ids:
        hit_ids = [c for c in chunk_ids if any(p in changed for p in by_chunk.get(c, ()))]
        hit = source_rows_by_plan(root, plan_rel, hit_ids)
        idle = source_rows_by_plan(root, plan_rel, [c for c in chunk_ids if c not in hit_ids])
        for rel in sorted(set(hit) | set(idle)):
            original = read_rel(root, rel)
            if original is None:
                continue
            noop_ids = set(idle.get(rel, ())) - set(hit.get(rel, ()))
            updated, flipped = flip_rows_coded(original, set(hit.get(rel, ())) | noop_ids, sha,
                                               {rid: NOOP_DETAIL for rid in noop_ids})
            if not flipped:
                continue
            invalid = check_plan_tasks_source(updated)
            if invalid is not None and check_plan_tasks_source(original) is None:
                restore()
                return {"coded_stamp_error": f"{rel}: stamped spine fails schema: {invalid}"}
            replace_text(root / rel, updated)
            originals[rel] = original
            rows_coded[rel] = flipped
    paths = sorted(set(rows_coded) | set(extra_paths))
    if not paths:
        return {"rows_coded": {}, "coded_sha": None}
    n = sum(len(v) for v in rows_coded.values())
    message = f"mark {n} rows coded ({sha[:7]})" if n else f"register writeback ({sha[:7]})"
    message = _stamp_message(root, message)
    from coordinator_core.session import landing_hold

    session_id = params.get("session_id")
    if landing_hold.is_held(str(root), session_id):
        landing_hold.defer(str(root), session_id, "coded_stamp",
                           {"paths": paths, "message": message, **({"session_id": session_id} if session_id else {})})
        return {"rows_coded": rows_coded, "coded_sha": None, "coded_deferred": "session-landing-hold"}
    reply = _stamp_commit(root, paths, message, run_id, params)
    if reply.get("nothing_to_commit") and not rows_coded:
        return {"rows_coded": {}, "coded_sha": None}
    if not reply.get("committed"):
        restore()
        return {"coded_stamp_error": f"coded-stamp commit did not land: {reply.get('error') or reply!r}"}
    return {"rows_coded": rows_coded, "coded_sha": reply["sha"]}


def _stamp_message(root: Path, subject: str) -> str:
    from coordinator_core.git.commit import _attach_session_id_trailer
    from coordinator_core.git.provider_attribution import attribution_value

    attribution = attribution_value()
    body = subject + (f"\n\nCo-Authored-By: {attribution}" if attribution else "")
    return _attach_session_id_trailer(body + "\n", root)


def _stamp_commit(root: Path, paths: list, message: str, run_id: str, params: dict) -> dict:
    """The follow-up commit over `paths`, built by `_commit`. A moved HEAD re-reads the tree and retries,
    never overwrites a peer's commit; a gitignored untracked path (a warp run spine) is not landed."""
    from coordinator_core.git import git_state
    from coordinator_core.git.commit import CommitRefused
    from coordinator_core.git.ignore_match import IgnoreMatcher

    reply: dict = {}
    for _attempt in range(3):
        spine = git_state.read_tree_spine(root, paths)
        if spine is None:
            return _error("HEAD's tree could not be read", refused="head-unreadable")
        untracked = [p for p in paths if p.rpartition("/")[2] not in spine.get(p.rpartition("/")[0], {})]
        matcher = IgnoreMatcher(root) if untracked else None
        keep = [p for p in paths if p not in untracked or matcher.ignored(p, False) is not True]
        if not keep:
            return {"committed": False, "nothing_to_commit": True}
        scans = _scandir_all(root, {p.rpartition("/")[0] for p in keep})
        present, deleted, _dropped = _live_paths(root, keep, scans, spine)
        try:
            reply = _commit(root, present, deleted, scans, spine, message, run_id, params, advise=False)
        except CommitRefused as exc:
            return _error(str(exc), refused=type(exc).__name__)
        if reply.get("refused") != "head-moved":
            break
    return reply


def _land(root: Path, params: dict) -> dict:
    from coordinator_core.git import git_state
    from coordinator_core.git.commit import CommitRefused
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths

    request, ask_run_dir, script_text = _read_request(root, params)
    incomplete = set(params.get("incomplete_chunks") or []) - set(params.get("landed_chunks") or [])
    landing, stranded, chunk_ids, by_chunk = _declared(root, request, incomplete)
    home, siblings = split_sibling_paths(landing, root)
    run_id = _run_id(params, ask_run_dir, params["script_path"])
    from coordinator_core import repo_standing

    onboarded = repo_standing.is_onboarded(root)
    records = _record_paths(root, ask_run_dir) if onboarded else []
    paths = list(dict.fromkeys([*home, *records]))
    _slice_refusal(params["inline_review"], home)
    if not paths and not siblings:
        return {"committed": False, "nothing_to_commit": True, "ok": True, "stranded": stranded}

    book = _prepare_bookkeeping(root, params, request, ask_run_dir, chunk_ids, incomplete, paths, onboarded)
    paths = list(dict.fromkeys([*paths, *book.extra_paths]))
    spine = git_state.read_tree_spine(root, paths) if paths else {"": {}}
    if spine is None:
        _restore_unlanded(root, book)
        raise _Refused("head-unreadable", "HEAD's tree could not be read")
    dirs = {p.rpartition("/")[0] for p in paths}
    scans = _scandir_all(root, dirs)
    present, deleted, dropped = _live_paths(root, paths, scans, spine)
    if deleted:
        spine = git_state.read_tree_spine(root, [*present, *deleted]) or spine
    live = set(present)
    book.review_land = [p for p in book.review_land if p in live and _review_differs(root, scans, spine, p)]
    try:
        message = _message(root, params, request, chunk_ids, deleted, script_text, run_id, book)
        reply = _commit(root, present, deleted, scans, spine, message, run_id, params)
    except CommitRefused as exc:
        reply = _error(str(exc), refused=type(exc).__name__)
    except BaseException:
        _restore_unlanded(root, book)
        raise
    if reply.get("committed") and reply.get("sha"):
        changed = set(reply.pop("changed_paths", None) or (*present, *deleted))
        reply.update(_after_landing(root, params, request, book, by_chunk, changed, chunk_ids,
                                    str(reply["sha"]), run_id))
    else:
        _restore_unlanded(root, book)
    reply.update(
        stranded=stranded, chunks_committed=chunk_ids, ok=bool(reply.get("committed")),
        run_artifacts={"landed": [r for r in dict.fromkeys([*records, *book.extra_paths]) if r in live]}, residue=[],
        undeclared_dirty=_undeclared_dirty(root, scans, spine, set(present) | set(deleted)),
    )
    if dropped:
        reply["dropped_absent"] = dropped
    if reply.get("committed"):
        reply.update(_land_siblings(siblings, message, params))
    return reply


def _land_siblings(siblings: dict, message: str, params: dict) -> dict:
    """RL-21: each sibling repo's paths land in that repo after the home commit; none is zero work."""
    out: dict = {}
    for sibling_root, rels in siblings.items():
        try:
            from coordinator_core.git.commit import commit_paths

            outcome = commit_paths(sibling_root, rels, message)
            out.setdefault("siblings", {})[str(sibling_root)] = outcome.sha
        except Exception as exc:  # noqa: BLE001 -- the home commit stands; report the sibling
            out.setdefault("siblings", {})[str(sibling_root)] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def _fallback_commit(root: Path, present: list, deleted: list, message: str, supplied: dict) -> dict:
    """The shapes the in-process writer does not cover go through `git.commit.commit_paths`."""
    from coordinator_core.git.commit import CommitRefused, commit_paths

    try:
        outcome = commit_paths(root, present, message, deleted_paths=deleted, supplied_blobs=supplied or None)
    except CommitRefused as exc:
        return _error(str(exc), refused=type(exc).__name__)
    except RefLockHeld as exc:
        return _ref_lock_error(exc)
    return {"committed": True, "sha": outcome.sha, "converged": list(outcome.converged),
            "left_alone": list(outcome.left_alone), "sync_refused": list(getattr(outcome, "sync_refused", ()) or ())}


def _needs_fallback(root: Path, gitdir: Path, own_kept: dict) -> bool:
    from coordinator_core.git import commit as gc

    if own_kept or gc._gpgsign_enabled(root):
        return True
    return any((gitdir / name).exists() for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
                                                      "rebase-apply", "BISECT_LOG"))


def _own_kept(root: Path, run_id: str, present: list, tracked: dict) -> dict:
    """RL-05: blobs the run's own `kept.json` holds for a path the worktree has reverted to HEAD;
    `commit_paths` converges the worktree to them."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    try:
        kept = json.loads((root / RECORD_DIR_ROOT / run_id / "kept.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    wanted = set(present)
    out: dict = {}
    for chunk in (kept.get("chunks") or {}).values() if isinstance(kept, dict) else ():
        for path, sha in ((chunk or {}).get("paths") or {}).items():
            path = _norm(path)
            if path in wanted and isinstance(sha, str) and tracked.get(path) != sha:
                out[path] = sha
    return out


def _commit(root: Path, present: list, deleted: list, scans: dict, spine: dict, message: str,
            run_id: str, params: dict, advise: bool = True) -> dict:
    """RL-01, RL-02, RL-05, RL-13, RL-14, RL-15, RL-16: read, hash and write the landing in process,
    then move the ref by one compare-and-swap."""
    from coordinator_core import dispatch_abandon
    from coordinator_core.git import commit as gc
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git import index_write as iw
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

    gitdir, common = resolve_git_dir(root), resolve_git_common_dir(root)
    old_head = git_state.head_sha(root)
    if old_head is None:
        return _fallback_commit(root, present, deleted, message, {})
    tracked_sha = {p: spine.get(p.rpartition("/")[0], {}).get(p.rpartition("/")[2], (0, None))[1] for p in present}
    own_kept = _own_kept(root, run_id, present, tracked_sha)
    if _needs_fallback(root, gitdir, own_kept):
        return _fallback_commit(root, present, deleted, message, own_kept)

    by_dir: dict = {}
    for rel in present:
        d, _, name = rel.rpartition("/")
        by_dir.setdefault(d, []).append(name)
    all_dirs = {""}
    for rel in (*present, *deleted):
        d = rel.rpartition("/")[0]
        while d:
            all_dirs.add(d)
            d = d.rpartition("/")[0]
    trees = {d: dict(spine.get(d, {})) for d in all_dirs}

    with call_io_scope():
        form = _CheckinForm(root, by_dir)
        pending: dict = {}
        index_new: dict = {}
        buffers: dict = {}
        modified_tracked: list = []
        for d, names in by_dir.items():
            head_dir = spine.get(d, {})
            for name in names:
                rel = f"{d}/{name}" if d else name
                entry = scans[d][name]
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    return _fallback_commit(root, present, deleted, message, {})
                data, st = _read_file(entry)
                data = form(d, name, rel, data)
                head_entry = head_dir.get(name)
                sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
                if head_entry is not None and head_entry[1] != sha:
                    modified_tracked.append((rel, data, head_entry))
                    continue
                _stage(rel, name, d, data, sha, st, head_entry, trees, pending, index_new, buffers, spine)
        if modified_tracked:
            peers = _peer_blobs(root, run_id, {r for r, _d, _h in modified_tracked})
            for rel, data, head_entry in modified_tracked:
                d, _, name = rel.rpartition("/")
                stat = scans[d][name].stat()
                shas = peers.get(rel)
                if shas:
                    stored = git_objects.read_object(common, head_entry[1])
                    peer_bytes = [b[1] for b in (git_objects.read_object(common, s) for s in shas) if b is not None]
                    if stored is not None and peer_bytes:
                        own = _own_view(stored[1], data, peer_bytes)
                        if own != data:
                            data, stat = own, _ZERO_STAT
                sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
                _stage(rel, name, d, data, sha, stat, head_entry, trees, pending, index_new, buffers, spine)

    findings = _secret_findings(common, spine, buffers)
    if findings:
        from coordinator_core.ops.dispatch_emit import landing_secrets as ls

        return _error(ls.refusal(findings) or "secret in diff", refused=ls.SECRET_IN_DIFF,
                      secret_findings=[f._asdict() for f in findings[:10]])
    advisory = _leak_advisory(root, buffers) if advise else None

    for rel in deleted:
        d, _, name = rel.rpartition("/")
        trees[d].pop(name, None)
        index_new[rel.encode("utf-8")] = None
    root_tree = _write_trees(trees, pending)
    if root_tree == git_state.head_tree_sha(root):
        return {"committed": False, "nothing_to_commit": True}

    name, email = gc._identity(root)
    who = f"{name} <{email}> {gc._stamp()}"
    body = (f"tree {root_tree}\nparent {old_head}\nauthor {who}\ncommitter {who}\n\n{message}").encode("utf-8")
    commit_sha = hashlib.sha1(b"commit %d\x00" % len(body) + body).hexdigest()
    pending[commit_sha] = (b"commit", body)
    git_objects._flush_pack(common, pending)

    target = gc._cas_target(root)
    if target is None:
        return _fallback_commit(root, present, deleted, message, {})
    ref_gitdir, ref = target
    if dispatch_abandon.is_abandoned():
        return _error("dispatch gave up before the ref update; the commit object is left unreferenced",
                      refused="dispatch-abandoned", sha=None)
    index_bytes = _spliced_index(gitdir / "index", index_new)
    if index_bytes is None:
        return _fallback_commit(root, present, deleted, message, {})
    lock = gitdir / "index.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return _error("index.lock is held by a peer; nothing was committed", refused="index-lock-busy")
    prepared = iw.PreparedSplice(lock, gitdir / "index")
    try:
        prepared._claim_owner()
        with os.fdopen(fd, "wb") as handle:
            handle.write(index_bytes)
        subject = message.splitlines()[0] if message else ""
        if not git_objects.cas_ref(ref_gitdir, ref, old_head, commit_sha,
                                   reflog_committer=f"{name} <{email}>", reflog_message=f"commit: {subject}",
                                   head_gitdir=gitdir, report_lock=True):
            prepared.abort()
            return _error("HEAD moved while landing; nothing was overwritten", refused="head-moved")
    except RefLockHeld as exc:
        prepared.abort()
        return _ref_lock_error(exc)
    except BaseException:
        prepared.abort()
        raise
    prepared.publish()
    reply = {"committed": True, "sha": commit_sha, "landed_paths": sorted([*present, *deleted]),
             "changed_paths": sorted([*buffers, *deleted])}
    if advisory:
        reply["leak_advisory"] = advisory
    return reply


def _stage(rel, name, d, data, sha, st, head_entry, trees, pending, index_new, buffers, spine) -> None:
    """Record one blob in its directory's tree, the pack and the new index entries."""
    from coordinator_core.git import index_write as iw

    mode = 0o100644
    if head_entry is not None and head_entry[0] in (0o100644, 0o100755):
        mode = head_entry[0]
    elif os.name != "nt" and getattr(st, "st_mode", 0) & 0o111:
        mode = 0o100755
    pending[sha] = (b"blob", data)
    trees[d][name] = (mode, sha)
    index_new[rel.encode("utf-8")] = iw._build_entry(rel.encode("utf-8"), mode, sha, st)
    if head_entry is None or head_entry[1] != sha:
        buffers[rel] = data


def _tree_bytes(entries: dict) -> bytes:
    items = sorted(entries.items(), key=lambda kv: kv[0] + "/" if kv[1][0] == 0o40000 else kv[0])
    return b"".join(
        (b"40000" if mode == 0o40000 else oct(mode)[2:].encode("ascii"))
        + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
        for name, (mode, sha) in items
    )


def _write_trees(trees: dict, pending: dict) -> str:
    """One tree object per changed directory, deepest first; a directory left empty leaves its parent."""
    root_sha = ""
    for d in sorted(trees, key=lambda p: -p.count("/") - (1 if p else 0)):
        if d and not trees[d]:
            parent, _, leaf = d.rpartition("/")
            trees[parent].pop(leaf, None)
            continue
        payload = _tree_bytes(trees[d])
        sha = hashlib.sha1(b"tree %d\x00" % len(payload) + payload).hexdigest()
        pending[sha] = (b"tree", payload)
        if d:
            parent, _, leaf = d.rpartition("/")
            trees[parent][leaf] = (0o40000, sha)
        else:
            root_sha = sha
    return root_sha


def _spliced_index(index_path: Path, index_new: dict) -> Optional[bytes]:
    """RL-05: the current index with the landed entries replaced (an entry of None is a removal), so the
    index matches the committed tree for every landed path. None when the index is not the v2 shape
    this writer rebuilds."""
    from coordinator_core.git import index_write as iw

    try:
        raw = index_path.read_bytes()
    except OSError:
        return None
    if len(raw) < 32 or raw[:4] != b"DIRC" or struct.unpack_from(">I", raw, 4)[0] != 2:
        return None
    count = struct.unpack_from(">I", raw, 8)[0]
    starts, body_end = iw._walk_entries(raw, count)
    bounds = [*starts, body_end]
    merged = {iw._entry_name(raw, s): raw[s:bounds[i + 1]] for i, s in enumerate(starts)}
    for key, value in index_new.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    head = struct.pack(">4sII", b"DIRC", 2, len(merged))
    digest = hashlib.sha1(head)
    chunks = [head]
    for key in sorted(merged):
        digest.update(merged[key])
        chunks.append(merged[key])
    chunks.append(digest.digest())
    return b"".join(chunks)
