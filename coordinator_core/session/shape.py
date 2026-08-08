"""
coordinator_core.session.shape — the per-session ``session-shape.json``
observable: pickup / memo-action / plan-claim write-moment facts, plus
on-demand magnitude derivation.

Port of: session-shape.sh (example-doctrine-repo e34f2484, 2026-07-22).

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

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1
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
"""

from __future__ import annotations

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

    Spec backlink: docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C1 (AC2)
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

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return False
    # Session dir must exist (caller owns cs_init; defensively create).
    try:
        Path(sdir).mkdir(parents=True, exist_ok=True)
    except OSError:
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
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
        (Path(lock_dir) / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (Path(lock_dir) / "session_id").write_text(f"{sid}\n", encoding="utf-8")
        (Path(lock_dir) / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8")
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

    Spec backlink: docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C1
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
      Count DISTINCT lines in ``<sdir>/touched.txt`` (the bash
      ``sort -u | wc -l`` dedup). Guard: absent ``touched.txt`` -> 0.

    Spec backlink: docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C5
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
    touched = 0
    touched_file = Path(sdir) / "touched.txt"
    if touched_file.is_file():
        try:
            lines = touched_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        touched = len(set(lines))

    # Review: code-reviewer (Finding 3) — trailing "\n" to match the bash
    # printf '...\n' oracle.
    return f'{{"commits_since_start":{commits},"files_touched":{touched}}}\n'
