"""
coordinator_core.session.shape — the per-session ``session-shape.json``
observable: pickup / memo-action / plan-claim write-moment facts, plus
on-demand magnitude derivation.

Port of: session-shape.sh (DoE e34f2484, 2026-07-22).

Writes/reads ``.git/coordinator-sessions/<sid>/session-shape.json`` — the
per-session observable that ceremonies read once instead of grep/inferring
across the session substrate.

Locking: an mkdir-based cross-process lock (``<sdir>/session-shape.lock/``)
that spans the ENTIRE read->merge->write critical section. Deliberately NOT
``flock`` — flock is unavailable on Windows Git Bash, so the bash original
uses mkdir-as-mutex and this port preserves that choice verbatim. Stale-lock
reaping uses the lock's OWN ``claimed_at`` recency gate (``_shape_lock_live``),
NOT session-liveness — a pre-init session has no ``meta.json`` and would
falsely reap a live mid-merge lock (see ``_shape_lock_live`` docstring).

Merge contract (field-level deep-merge):
    - ``actioned_memos``: fragment's array items are APPENDED to the existing
      array (concatenated), never replaced.
    - All other top-level keys (``pickup``, ``plan``, ...): fragment value
      REPLACES the base value.
    - ``schema_version`` and ``session_id``: base ALWAYS wins — the fragment
      never overwrites these identity fields.

Simplification vs. the bash source (noted deliberately): the bash original
carries a three-way merge fallback (jq -> python -c -> pure-awk) gated on
``_CS_FORCE_NO_JQ`` / ``command -v jq``. This port implements the merge once,
natively in Python, reproducing the append-actioned_memos / replace-others /
base-wins semantics directly. The jq-vs-awk-vs-python fallback ladder is an
artefact of bash not having a JSON library in-process; it is unnecessary here
and is intentionally NOT ported.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4a-g1
Spec backlink (original C1/C5 contract):
    docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C1, § C5
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § shape.py

Negative-spec:
    - Do NOT gate the lock's stale-reap on session-liveness
      (``_cs_session_live`` / ``core.stable_pid_alive`` on a stored pid): a
      pre-init session (no meta.json) has no ``last_activity`` recency and
      would be judged stale, falsely reaping a lock whose holder is actively
      mid-merge. Use the lock's own ``claimed_at`` recency gate only.
    - Do NOT replace the mkdir-based lock with ``flock`` — flock is
      unavailable on Windows Git Bash; the mkdir-as-mutex is a portability
      requirement, not an accident.
    - ``session_shape_magnitude`` NEVER writes to ``session-shape.json`` and
      NEVER calls ``session_shape_set`` — it derives magnitude fields on
      demand from session-dir artefacts and returns them.
    - Do NOT port ``cs_write_review_claim`` / ``cs_reap_stale_review_claims``
      — both RETIRED (Mode-B review-claim confinement is dead; see the bash
      source's "Review-claim lifecycle helpers RETIRED" banner).
    - ``producer_set`` / ``producer_read`` do NOT add a new merge mode.
      Whole-value replace (the existing "all other top-level keys REPLACE"
      contract) is exactly what a single namespaced ``producer`` record
      needs; ``producer_set`` builds on ``session_shape_set`` unmodified.
    - ``producer_set`` MUST NOT swallow a ``False`` return from
      ``session_shape_set`` (e.g. lock-acquisition failure) into a silent
      success — the return value is passed through verbatim.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Union

from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

#: Port of ``_CS_SHAPE_LOCK_STALE_SEC`` default. A shape merge completes in
#: well under 1s; 30s is a very generous liveness bound for the lock's own
#: ``claimed_at`` recency gate. Overridable per-call via the
#: ``_CS_SHAPE_LOCK_STALE_SEC`` environment variable (matching the bash
#: ``${_CS_SHAPE_LOCK_STALE_SEC:-30}`` read).
# Generator-provenance declaration (generator_provenance.py). session_shape_set/
# producer_set write only `.git/coordinator-sessions/<sid>/session-shape.json` --
# git-internal session-hub state, never a tracked repo artifact.
GENERATES = []

_SHAPE_LOCK_STALE_SEC_DEFAULT = 30

#: Port of ``cs_session_shape_set``'s bounded-retry loop constants.
_LOCK_MAX_ATTEMPTS = 20
_LOCK_SLEEP_SEC = 0.1


def _shape_lock_stale_sec() -> int:
    """Resolve ``_CS_SHAPE_LOCK_STALE_SEC`` (env override -> default 30),
    matching the bash ``${_CS_SHAPE_LOCK_STALE_SEC:-30}`` read. A malformed
    env value falls back to the default (never raises)."""
    raw = os.environ.get("_CS_SHAPE_LOCK_STALE_SEC", "")
    if not raw:
        return _SHAPE_LOCK_STALE_SEC_DEFAULT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _SHAPE_LOCK_STALE_SEC_DEFAULT


def _shape_lock_live(lock_dir: str) -> bool:
    """Port of ``_cs_shape_lock_live <lock_dir>``.

    Returns True (live) iff the session-shape lock at ``lock_dir`` was claimed
    recently enough that its holder is presumed still running the merge —
    i.e. the lock's OWN ``claimed_at`` timestamp is younger than
    ``_CS_SHAPE_LOCK_STALE_SEC`` (default 30) seconds.

    Uses the lock's own ``claimed_at`` recency (written at lock acquisition)
    rather than ``core.stable_pid_alive`` / a session-liveness gate, because:
      - The session may not have been ``cs_init``'d (no ``meta.json`` /
        ``last_activity``), which makes a session-liveness recency gate always
        return "stale" and falsely reap the lock while the holder is actively
        in its critical section.
      - A merge operation completes in well under 1s; 30s is a very generous
        bound.

    Returns False (stale/dead) if the lock dir is absent, has no
    ``claimed_at`` file, its timestamp is empty, its parsed epoch is 0
    (parse failure), or the elapsed age is >= the stale bound. Internal —
    called only from ``session_shape_set``.

    Sentinel-fidelity: never raises; every failure edge returns False.
    """
    if not lock_dir or not Path(lock_dir).is_dir():
        return False
    claimed_at_file = Path(lock_dir) / "claimed_at"
    if not claimed_at_file.is_file():
        return False
    try:
        claimed_iso = claimed_at_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not claimed_iso:
        return False
    claimed_epoch = core.iso_to_epoch(claimed_iso)
    if not claimed_epoch or claimed_epoch == 0:
        return False
    elapsed = core.now_epoch() - claimed_epoch
    if elapsed < 0:
        elapsed = 0
    return elapsed < _shape_lock_stale_sec()


def _merge_fragment(base: dict, fragment: dict) -> dict:
    """Field-level deep-merge of ``fragment`` into ``base`` (in place, base is
    returned).

    Port of ``cs_session_shape_set``'s merge contract (the jq path and the
    python-fallback path), implemented natively:
      - ``actioned_memos`` list -> APPEND (concatenate onto the base array).
      - every other key -> REPLACE with the fragment value.
      - ``schema_version`` / ``session_id`` -> base ALWAYS wins (a fragment
        that includes them cannot overwrite the identity fields).
    """
    protected = {
        key: base[key] for key in ("schema_version", "session_id") if key in base
    }
    for key, val in fragment.items():
        if key == "actioned_memos" and isinstance(val, list):
            existing = base.get("actioned_memos")
            existing = existing if isinstance(existing, list) else []
            base["actioned_memos"] = existing + val
        else:
            base[key] = val
    base.update(protected)
    return base


def session_shape_set(
    sid: str,
    fragment: Union[dict, str],
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_session_shape_set <sid> <json-merge-fragment>``.

    Merge ``fragment`` into ``.git/coordinator-sessions/<sid>/session-shape.json``
    under an mkdir-based lock that spans the ENTIRE read->merge->write critical
    section. Returns True on success, False on lock-acquisition failure or any
    read/merge/write error.

    ``fragment`` accepts either a ``dict`` (native in-process call — the
    preferred form for sibling callers such as ``claims.py``) or a JSON
    ``str`` (mirrors the bash CLI contract). A non-dict fragment, or a
    ``str`` that is not valid JSON encoding an object, returns False.

    Field-level deep-merge contract:
      - ``actioned_memos``: fragment array items are APPENDED to the existing
        array.
      - all other top-level keys (``pickup``, ``plan``, ...): fragment value
        REPLACES.
      - ``schema_version`` / ``session_id``: base ALWAYS wins (fragment must
        not include them; if it does, it is ignored for those two keys).

    Atomicity:
      (1) mkdir-based lock at ``<sdir>/session-shape.lock/`` spans
          read->merge->write. Deliberately NOT flock (Windows Git Bash).
      (2) Staging file via ``tempfile.mkstemp`` in the session dir, then
          ``os.replace`` into place (atomic, same-dir rename).
      (3) Bounded-retry lock acquisition (20 attempts, 0.1s sleep), with
          stale-lock reaping via ``_shape_lock_live`` (the lock's own
          ``claimed_at`` recency gate, NOT session-liveness — see that
          function).

    Create-if-absent: initializes ``{"schema_version": 1, "session_id": sid}``
    when the file is missing.

    EVERY early-return path removes the lock dir (implemented via a
    try/finally around the critical section — no acquired lock leaks on any
    error or success path).

    Negative-spec: do NOT gate the stale-reap on session-liveness; a pre-init
    session would false-reap a live mid-merge lock.

    Spec backlink: pln-ceremony-as-pipeline-v1-session-state-co-596280 § C1 (AC2)
    """
    if not sid:
        raise ValueError("session_id required")

    # Normalize the fragment to a dict (accept JSON string or dict).
    if isinstance(fragment, str):
        try:
            frag = json.loads(fragment)
        except ValueError:
            return False
    else:
        frag = fragment
    if not isinstance(frag, dict):
        return False

    # The session dir must exist, and "defensively create" it with a bare mkdir
    # is what minted record-less session dirs: this writer does not own cs_init
    # and cannot assume the caller ran it. `ensure_session` is the one
    # constructor -- dir and `meta.json` together or neither -- so a shape write
    # can no longer leave behind a directory no peer can see.
    sdir = core.ensure_session(sid, cwd)
    if not sdir or not os.path.isdir(sdir):
        return False  # cannot create/access session dir -> shape write fails closed

    shape_file = Path(sdir) / "session-shape.json"
    lock_dir = str(Path(sdir) / "session-shape.lock")

    # ---------- Acquire mkdir-based lock (spans ENTIRE read->merge->write) ----------
    acquired = False
    attempts = 0
    while attempts < _LOCK_MAX_ATTEMPTS:
        if _try_claim_lock(lock_dir, sid):
            acquired = True
            break
        # Lock held — reap it only if the holder is no longer in its critical
        # section (stale by the lock's own claimed_at recency gate).
        if not _shape_lock_live(lock_dir):
            shutil.rmtree(lock_dir, ignore_errors=True)
            if _try_claim_lock(lock_dir, sid):
                acquired = True
                break
        attempts += 1
        time.sleep(_LOCK_SLEEP_SEC)

    if not acquired:
        return False

    try:
        # ---------- Read existing or initialize ----------
        base: dict = {"schema_version": 1, "session_id": sid}
        if shape_file.is_file():
            try:
                raw = shape_file.read_text(encoding="utf-8")
            except OSError:
                return False
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    return False
                if isinstance(parsed, dict):
                    base = parsed
                # A non-object existing file falls back to the skeleton, same
                # as the bash empty-existing create-if-absent path.

        # ---------- Merge fragment (field-level deep-merge) ----------
        merged = _merge_fragment(base, frag)

        # ---------- Atomic write: mkstemp in session dir, then os.replace ----------
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix="session-shape.json.", dir=str(sdir)
            )
        except OSError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(merged, fh)
                fh.write("\n")
            os.replace(tmp_name, shape_file)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                # Best-effort tmp-file cleanup on the error path; the caller
                # already gets a False return regardless.
                pass
            return False
        return True
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _try_claim_lock(lock_dir: str, sid: str) -> bool:
    """Attempt one mkdir-as-mutex claim of ``lock_dir``. On success, write the
    ``pid`` / ``session_id`` / ``claimed_at`` lock-metadata files (the
    ``claimed_at`` timestamp is what ``_shape_lock_live`` reads). Returns True
    iff the lock was freshly created by THIS call; False if it already existed
    (someone else holds it) or mkdir failed.

    Deliberately mkdir-based (not flock) — flock is unavailable on Windows Git
    Bash. Port of the inline ``mkdir "$lock_dir" && echo ... > ...`` block.
    """
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return False  # lock already held — expected on a contended retry, not an error
    except OSError:
        return False  # other mkdir failure (e.g. permission) — also report not-claimed
    try:
        # Review: code-reviewer (Finding 2) — trailing "\n" to match bash
        # echo redirection and claims.py::_write_claim_meta byte-parity.
        (Path(lock_dir) / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8", newline="\n")
        (Path(lock_dir) / "session_id").write_text(f"{sid}\n", encoding="utf-8", newline="\n")
        (Path(lock_dir) / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8", newline="\n")
    except OSError:
        # Best-effort metadata write; the lock dir itself is the mutex.
        pass
    return True


def session_shape_read(sid: str, cwd: Optional[str] = None) -> str:
    """Port of ``cs_session_shape_read <sid>``.

    Return the persisted ``session-shape.json`` as raw JSON text, or the
    default skeleton ``{"schema_version":1,"session_id":"<sid>"}`` when the
    file is absent or the session dir is unresolvable. Fail-open: never raises
    (except the required-arg guard) — an unresolvable session dir emits the
    skeleton rather than an error.

    Magnitude fields (``commits_since_start`` / ``files_touched``) are NOT
    derived here — that is ``session_shape_magnitude``'s job.

    Returns raw JSON *text* (mirroring the bash function's stdout contract),
    not a parsed dict; the file body is returned verbatim (including its
    trailing newline), while the skeleton is emitted as compact JSON.

    Spec backlink: pln-ceremony-as-pipeline-v1-session-state-co-596280 § C1
    """
    if not sid:
        raise ValueError("session_id required")

    # Review: code-reviewer (Finding 3) — trailing "\n" so this branch is
    # byte-consistent with the existing-file branch below (which carries the
    # newline session_shape_set wrote) and with the bash printf oracle.
    skeleton = (
        json.dumps({"schema_version": 1, "session_id": sid}, separators=(",", ":"))
        + "\n"
    )

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return skeleton

    shape_file = Path(sdir) / "session-shape.json"
    if shape_file.is_file():
        try:
            return shape_file.read_text(encoding="utf-8")
        except OSError:
            return skeleton  # TOCTOU: file vanished/unreadable after the is_file() check
    return skeleton


#: Reserved ``typed_command`` sentinel values beyond a real command-name
#: string (see ``producer_set``'s contract). Any other string is accepted
#: as a normalized command name -- the reserved set is not an allowlist of
#: the ONLY legal strings, it is documentation of the two sentinels with
#: special meaning, AND is enforced as a near-miss typo guard in
#: ``producer_set`` (see its docstring's negative-spec): a string that is a
#: close-but-not-exact match to one of these two closed structural members
#: is rejected, because a typo here (e.g. ``"unresolvd"``) would otherwise
#: be written silently and render as an ordinary open-vocabulary command
#: name downstream, defeating the three-state distinguishability the whole
#: design rests on. This is NOT a re-enumeration of DoE's open command
#: vocabulary -- an unrelated real command name never matches closely
#: enough to trip the guard.
_TYPED_COMMAND_SENTINELS = ("other-command", "unresolved")

#: Similarity cutoff for the near-miss sentinel-typo guard below. Tight
#: enough that a real, unrelated command name (e.g. ``"/pickup"``) never
#: matches; loose enough to catch a single-character typo of a sentinel
#: (e.g. ``"unresolvd"``, ``"unresolced"``).
_SENTINEL_TYPO_CUTOFF = 0.8


def producer_set(
    sid: str,
    *,
    typed_command: Optional[str],
    cwd: Optional[str] = None,
) -> bool:
    """Write a namespaced ``producer`` record onto ``session-shape.json`` via
    the existing ``session_shape_set`` replace-semantics merge (a single
    top-level key REPLACE, not a new merge mode).

    ``typed_command`` contract (fail loud on anything else -- raises
    ``ValueError`` rather than writing a malformed record):
      - a normalized command-name ``str`` (the common case);
      - ``"other-command"``: a typed slash verb outside coordinator's own
        command set (e.g. ``/clear``, ``/loop``);
      - ``"unresolved"``: capture failed;
      - ``None``: nothing typed this session (a machine-minted producer).
        Serializes as a present JSON ``null`` -- never an absent key -- so
        ``null`` / ``"unresolved"`` / a real command name stay distinguishable
        on disk.
    Any non-``str``, non-``None`` value (e.g. an ``int`` or ``list``) raises
    ``ValueError``: the three states above are the whole contract, and a
    caller that hands in an out-of-contract type gets a loud failure instead
    of a silently-malformed record.

    Record shape written: ``{"typed_command": ..., "captured_at": <ISO-8601
    str, core.now_iso()>}`` -- exactly two keys.

    NEGATIVE-SPEC -- do NOT add ``op_identity`` here. There are TWO producer
    records in this design and they are deliberately different shapes:

      - CAPTURE-side (this function, ``session-shape.json``):
        ``typed_command`` + ``captured_at``. DoE-claude's landed
        ``session-shape.schema.json`` (x-schema-version 1.1.0) declares this
        object ``additionalProperties: false`` with both keys REQUIRED, so an
        extra ``op_identity`` key here is a hard validation failure on their
        side, not a harmless addition.
      - RESOLVED-side (handoff frontmatter, thence the wire, see
        ``contract.cockpit_schema.entities.summaries._HandoffProducer``):
        ``typed_command`` + ``op_identity``, and NO ``captured_at``.

    ``op_identity`` is resolved at the CREATION seam -- which door minted the
    record -- and never read back out of session state. ``captured_at`` is a
    capture-side artifact for bounding staleness later; it is not provenance a
    board consumes, and putting it on the wire would invite a consumer to
    compute a bound nobody has agreed. The resolver's job is therefore a
    PROJECTION, never a copy.

    This function briefly shipped with an ``op_identity`` parameter (2026-08-12,
    corrected same session) -- it contradicted both the schema above and the
    argument this repo itself made for keeping op-identity out of session
    state. Re-adding it would reintroduce that defect.

    Return value: the underlying ``session_shape_set`` result, VERBATIM --
    including a lock-acquisition ``False``. This function MUST NOT swallow a
    ``False`` into a silent no-op: the caller is an external repo's hook that
    has committed to failing loudly on a false return, and the return value
    is the only signal it can fail against.

    Negative-spec: do NOT validate ``typed_command`` against the coordinator
    command vocabulary here. That vocabulary is declared single-point in
    DoE-claude (their AC-6, with its own parity test); re-enumerating it on
    this side would create a second source of truth that drifts silently.
    Any non-empty ``str`` is accepted by design -- the closed members
    (``"other-command"`` / ``"unresolved"``) are the contract this side
    enforces, membership of the open one is not.

    The closed members ARE enforced narrowly: a string that is a near-miss
    typo of ``"other-command"`` or ``"unresolved"`` (see
    ``_TYPED_COMMAND_SENTINELS`` / ``_SENTINEL_TYPO_CUTOFF``) -- but not an
    exact match to either, and not a real command name -- raises
    ``ValueError``. This does not reopen the vocabulary question: it is a
    guard against exactly the two closed sentinels this side already
    produces and depends on being silently corrupted into an
    indistinguishable "ordinary command name" on disk.

    Spec backlink: state/sizings/2026-08-12-producer-axis-claude-klabauter-engine-half.yaml
    Spec backlink (cross-repo contract): DoE-claude
        docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md D6
    """
    if typed_command is not None and not isinstance(typed_command, str):
        raise ValueError(
            f"producer_set: typed_command must be a str or None, got {type(typed_command).__name__}"
        )
    if typed_command == "":
        raise ValueError(
            "producer_set: typed_command must not be an empty string -- use None for "
            "'nothing typed this session', which serializes as a present null and stays "
            "distinguishable from 'unresolved' and from a real command name"
        )
    if typed_command is not None and typed_command not in _TYPED_COMMAND_SENTINELS:
        near = difflib.get_close_matches(
            typed_command, _TYPED_COMMAND_SENTINELS, n=1, cutoff=_SENTINEL_TYPO_CUTOFF
        )
        if near:
            raise ValueError(
                f"producer_set: typed_command {typed_command!r} looks like a typo of the "
                f"reserved sentinel {near[0]!r} -- pass the sentinel exactly, or a real "
                "command name that isn't a near-miss of it"
            )
    record = {
        "typed_command": typed_command,
        "captured_at": core.now_iso(),
    }
    return session_shape_set(sid, {"producer": record}, cwd=cwd)


def producer_read(sid: str, cwd: Optional[str] = None) -> Optional[dict]:
    """Read the namespaced ``producer`` record back via ``session_shape_read``.

    Failure semantics (deliberately NOT collapsed to one value):
      - Missing session-shape.json, OR a present file with no top-level
        ``producer`` key: returns ``None``. (``session_shape_read`` fails
        open to a skeleton object for an absent/unresolvable file, which
        naturally has no ``producer`` key -- so file-absent and key-absent
        both surface here as the same ``None``, exactly as
        ``session_shape_read``'s own contract already collapses them.)
      - A malformed/unparseable record -- the persisted file is not valid
        JSON, is valid JSON but not a JSON object, or its ``producer`` value
        is present but is not a JSON object -- raises ``ValueError``. This is
        the deliberate distinguishing case: a malformed record must NOT be
        silently reported as "no producer set" (``None``); it is surfaced
        loudly instead, so the two are never confused by a caller that only
        checks for ``None``.

    Spec backlink: pln-session-shape-attribution-key--05dd14
    """
    if not sid:
        raise ValueError("session_id required")

    raw = session_shape_read(sid, cwd=cwd)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"producer_read: malformed session-shape.json for {sid!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"producer_read: session-shape.json for {sid!r} is not a JSON object"
        )
    if "producer" not in parsed:
        return None
    record = parsed["producer"]
    if not isinstance(record, dict):
        raise ValueError(
            f"producer_read: producer record for {sid!r} is not a JSON object"
        )
    return record


def _git(args, cwd: Optional[str]) -> Optional[subprocess.CompletedProcess]:
    """Run ``git <args>`` capturing output; return the CompletedProcess, or
    None on OSError (git missing / spawn failure). Threads the optional cwd
    and suppresses the Windows console window (``no_console_creationflags()``)."""
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        return None


def session_shape_magnitude(sid: str, cwd: Optional[str] = None) -> str:
    """Port of ``cs_session_shape_magnitude <sid>``.

    Derive and return the magnitude fields as a JSON object string:
    ``{"commits_since_start":<int>,"files_touched":<int>}``. Computed on
    demand from session-dir artefacts — NEVER writes ``session-shape.json``,
    NEVER calls ``session_shape_set``. Fail-open: individual guard zeroes
    ensure a valid JSON object on every path (never raises except the
    required-arg guard).

    commits_since_start:
      Read ``head_at_start`` from the session dir; count commits reachable
      from HEAD but not from ``head_at_start`` via
      ``git rev-list --count <head_at_start>..HEAD``. Guard: absent/empty/
      ``"unknown"``/non-commit ``head_at_start`` -> 0. The sha is validated
      with ``git cat-file -e <sha>^{commit}`` before it is handed to
      rev-list; ``\\r`` / ``\\n`` / space chars are trimmed from the stored
      value first (BSD cat trailing newline / Windows ``\\r``).

    files_touched:
      Count DISTINCT lines returned by ``scope._read_touch_record_as_
      legacy_lines`` for this session's own touch record (the bash
      ``sort -u | wc -l`` dedup, ported as ``len(set(lines))``) — the
      union of ``<sdir>/touch-record.jsonl`` (rendered back to the old
      ``'<verb> <ts> <path>'`` dialect) and, if present, a sibling
      ``<sdir>/touched.txt``. Guard: no session dir -> 0 (the seam itself
      already fails open to ``([], False)`` for an absent/unreadable
      record, so no extra guard is needed here beyond the ``sdir`` check).

      COVERAGE LIMIT ON THIS COUNT — DECLARED, NOT SILENTLY INHERITED
      (docs/decisions/DR-258-bash-mediated-writes-are-a-named-permanent-limit.md,
      ratified permanent, not a gap awaiting a fix): this record's
      producer, ``hooks.track_touched_files`` (``coordinator_core/hooks/
      track_touched_files.py``, now appending ``touch-record.jsonl`` events
      via ``touch_record.append_event`` rather than the retired bare
      ``touched.txt`` dialect), is registered only on the
      ``Write|Edit|MultiEdit|NotebookEdit`` path. A path written through
      Bash or PowerShell records no claim there, so this count is a FLOOR on
      the session's true touched-path set, never a clean total — a session
      that wrote real files entirely via Bash/PowerShell reads the same
      ``files_touched: 0`` (or an equally undercounted N) as a session that
      genuinely touched fewer paths. This function's wire contract (two bare
      ints, byte-parity with the bash ``cs_session_shape_magnitude`` oracle
      and locked by this module's own tests) has no room for a structural
      degraded/collision discriminator the way the DR-319 fact facade does
      (``docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md``,
      ``coordinator_core/session/session_facts.py``) — so, per the same
      "absent coverage is not the same claim as a clean fact" requirement
      (R-11, ``state/roadmap/fact-layer-core/COORDINATOR-RESOLUTIONS.md``),
      the limit is declared here in prose, at this fact's own seam, the same
      way ``session_facts.py`` declares its own known attribution limit in
      prose rather than as a silent extra key.

      DIRECTION OF ERROR MATTERS, AND IT IS THE UNSAFE ONE HERE:
      ``baton_assemble._compute_dirty_tree_attribution`` (read-only
      reference, ``coordinator_core/baton_assemble/__init__.py``) depends on
      this same underlying record (via ``touch_record.project_live_claims``)
      and degrades (``degraded: True``) only when the record is
      missing/unreadable or no session id resolves; when the
      record is present but DR-258-incomplete, it returns ``degraded: False``
      with an under-populated ``mine`` -- an UNDER-claim, which is the safe
      direction for a probe whose only job is to stop OVER-claiming a peer's
      work. This function's ``files_touched`` has no such safety valve: it
      is a bare magnitude with no degrade signal, so the SAME DR-258
      incompleteness inherits here in the direction that matters -- a caller
      reading ``files_touched`` as a clean count is silently misled, not
      merely conservative. That asymmetry is why this limit is called out
      explicitly rather than assumed self-evident from DR-258 alone.

      A future lift of ``files_touched`` onto the DR-319 fact facade
      (``fl-core-02``) MUST carry this declaration forward at that facade's
      own seam (its structural ``degraded``/``evidence`` shape can state it
      properly) -- inheriting a bare int here without restating the limit
      would regress an undercount into a claimed-clean fact.

    Spec backlink: pln-ceremony-as-pipeline-v1-session-state-co-596280 § C5
    """
    if not sid:
        raise ValueError("session_id required")

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return '{"commits_since_start":0,"files_touched":0}'

    # ---- commits_since_start ----
    commits = 0
    head_file = Path(sdir) / "head_at_start"
    if head_file.is_file():
        try:
            head_sha = head_file.read_text(encoding="utf-8")
        except OSError:
            head_sha = ""
        # Trim \r, \n, and space chars (bash: ${head_sha//[$'\r\n ']/}).
        head_sha = head_sha.replace("\r", "").replace("\n", "").replace(" ", "")
        if head_sha and head_sha != "unknown":
            cat = _git(["cat-file", "-e", f"{head_sha}^{{commit}}"], cwd)
            if cat is not None and cat.returncode == 0:
                rev = _git(["rev-list", "--count", f"{head_sha}..HEAD"], cwd)
                if rev is not None and rev.returncode == 0:
                    n = rev.stdout.strip()
                    if n.isdigit():
                        commits = int(n)

    # ---- files_touched ----
    # DR-258 coverage limit (see docstring's "COVERAGE LIMIT ON THIS COUNT"):
    # the underlying record only captures Write|Edit|MultiEdit|NotebookEdit-
    # mediated paths, never Bash/PowerShell ones -- this count is a floor,
    # not a clean total. Ratified permanent; do not "fix" by widening
    # producers.
    #
    # Lazy import (not module-level): coordinator_core.session.scope is a
    # heavier sibling module than this function needs at import time, and
    # keeping the cross-sibling import local here mirrors the existing lazy-
    # import pattern in this package (see core.py :: init's lazy `git_state`
    # import and its comment) rather than adding a new top-level dependency
    # edge between shape.py and scope.py. No cycle exists today (scope.py
    # does not import shape.py), but this keeps the two modules' load order
    # decoupled the same way the rest of the package already does.
    from coordinator_core.session import scope as _scope

    touched = 0
    touch_record_path = Path(sdir) / _scope._TOUCH_RECORD_FILENAME
    lines, _touch_degraded = _scope._read_touch_record_as_legacy_lines(touch_record_path)
    touched = len(set(lines))

    # Review: code-reviewer (Finding 3) — trailing "\n" to match the bash
    # printf '...\n' oracle.
    return f'{{"commits_since_start":{commits},"files_touched":{touched}}}\n'
