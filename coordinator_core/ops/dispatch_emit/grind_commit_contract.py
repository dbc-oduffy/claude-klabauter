"""The grind mode contract of ``dispatch.land_run``: params key, row shape, row trailer.

Shared by the op that lands a grind's finished rows and by the emitter that composes the call, so it
imports nothing beyond the standard library and the stdlib-only ``session.record_homes``.

Handler reply shape: ``{committed, sha, rows: [{row_id, outcome, sha}],
dropped: [{row_id, path, reason}], error, refused, already_landed, unlanded}``. A path the commit
cannot land is dropped with a ``DROP_*`` reason, never refused; only a malformed path refuses the call.
``already_landed`` (``ALREADY_LANDED_KEY``) is true only on a repeat call answered from the run
token's spent receipt: ``sha`` and ``rows`` are the first landing's, nothing landed again.
``unlanded`` (``UNLANDED_KEY``) is ``[{row_id, reason}]`` for each requested row the landed commit
did not carry; the reason names the landed sha. Both keys are absent on a first landing.
Two further drop reasons: ``DROP_PEER_HUNKS_CONFLICT`` (a peer's dirty hunks of a shared file cannot be
separated from the row's own) and ``DROP_UNATTRIBUTED_SHARED`` (a shared file has no row image to attribute
its hunks). A landing whose import closure reaches an untracked module is refused with
``REFUSED_IMPORT_CLOSURE``, before the commit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from coordinator_core.session.record_homes import home_dir


def _home_prefix(kind: str) -> str:
    return Path(home_dir("", kind)).as_posix() + "/"


GRIND_COMMIT_PARAM = "grind"
ALREADY_LANDED_KEY = "already_landed"
UNLANDED_KEY = "unlanded"
ROW_TRAILER_KEY = "Grind-Row"
LEDGER_ROOT = _home_prefix("queue-grind")
DROP_LEDGER_ROOT = "ledger-root"
DROP_UNSEEN_DELETION = "deletion-git-cannot-see"
DROP_PEER_HUNKS_CONFLICT = "peer-hunks-conflict"
DROP_UNATTRIBUTED_SHARED = "unattributed-shared"
REFUSED_IMPORT_CLOSURE = "import-closure"

# Row images: the pre/post blobs a ``grind-row image`` call records for one row's declared paths.
ROW_IMAGES_ROOT = ".coordinator-local/grind-row-images"
IMAGE_VERB = "image"
IMAGE_PHASES = ("pre", "post")
IMAGE_RECORD_FILE = "images.json"  # the record ``grind-row image`` merges into ``row_images_rel``


def row_images_rel(run_id: str, row_id: str) -> str:
    """Repo-relative POSIX dir of one row's images: ``ROW_IMAGES_ROOT/<run_id>/<row_id>``.
    Raises ``ValueError`` when either segment is empty, holds a separator or drive colon, or is ``..``."""
    for seg in (run_id, row_id):
        bad = _bad_path(seg)
        if bad is None and "/" in seg:
            bad = f"path {seg!r} is not a single segment"
        if bad:
            raise ValueError(bad)
    return f"{ROW_IMAGES_ROOT}/{run_id}/{row_id}"

# Run-close contract: the ``grind-row run-record`` flags, the decisions.json keys and the record shape.
DECLARED_FLAG = "declared-urlenc"
DECISIONS_FLAG = "decisions-file"
QUEUE_FLAG = "queue"
BASELINE_DIRTY_KEY = "baseline_dirty"
MINTED_KEY = "minted"
SWEEP_FLAG = "sweep"
# Twinned by grind_rows run-record --sweep (the record) and grind_compose's run close (the hand-back).
SWEEP_FAILED_HEAD = "drain sweep did not complete"
RUN_RECORD_REQUIRED = ("profile", "run_id", "counts", "spend", "handed_back", "halted")

# Relay-integrity contract: each printf-relayed urlenc token travels with its length and sha256.
RECORD_LEN_FLAG = "record-len"
RECORD_SHA256_FLAG = "record-sha256"
DECLARED_LEN_FLAG = "declared-len"
DECLARED_SHA256_FLAG = "declared-sha256"
# The drain agent types only this marker; the verb reads the urlenc record from the agent's own
# transcript (the prompt carries it once between the run's open and close markers).
RECORD_FROM_TRANSCRIPT_FLAG = "record-from-transcript"
RECORD_OPEN_PREFIX = "RUNREC-OPEN-"
RECORD_CLOSE_PREFIX = "RUNREC-CLOSE-"


def record_markers(run_id: str) -> tuple[str, str]:
    """``(open, close)`` transcript markers for ``run_id``."""
    return RECORD_OPEN_PREFIX + run_id, RECORD_CLOSE_PREFIX + run_id


def record_close_marker(open_marker: str) -> Optional[str]:
    """The close marker paired with ``open_marker``; None when it is not a ``record_markers`` open marker."""
    if not open_marker.startswith(RECORD_OPEN_PREFIX) or len(open_marker) == len(RECORD_OPEN_PREFIX):
        return None
    return RECORD_CLOSE_PREFIX + open_marker[len(RECORD_OPEN_PREFIX):]
# Opens the stderr of a refused relay; grind_compose's run close matches it to re-drive the drain once.
RELAY_CORRUPT_HEAD = "relayed payload is corrupt"

_HEX64 = frozenset("0123456789abcdef")

# Pure JS over an ASCII string. Callers pass only the shell-inert urlenc token, which is ASCII by
# construction; a char above 0x7f would be masked to a byte and no longer match relay_digest.
# The runtime supplies no hashing global, so the constants are derived from the first 64 primes.
RELAY_SHA256_JS = """function _sha256Ascii(s) {
  var K = [], H = [], p = 2, n = 0, i, j;
  while (n < 64) {
    var q = true;
    for (var d = 2; d * d <= p; d++) { if (p % d === 0) { q = false; break; } }
    if (q) {
      if (n < 8) { H[n] = (Math.pow(p, 1 / 2) % 1 * 4294967296) | 0; }
      K[n] = (Math.pow(p, 1 / 3) % 1 * 4294967296) | 0;
      n++;
    }
    p++;
  }
  var b = [];
  for (i = 0; i < s.length; i++) { b.push(s.charCodeAt(i) & 255); }
  var bits = b.length * 8;
  b.push(128);
  while (b.length % 64 !== 56) { b.push(0); }
  var hi = Math.floor(bits / 4294967296), lo = bits >>> 0;
  b.push((hi >>> 24) & 255, (hi >>> 16) & 255, (hi >>> 8) & 255, hi & 255);
  b.push((lo >>> 24) & 255, (lo >>> 16) & 255, (lo >>> 8) & 255, lo & 255);
  var w = [];
  for (var off = 0; off < b.length; off += 64) {
    for (i = 0; i < 16; i++) {
      w[i] = (b[off + 4 * i] << 24) | (b[off + 4 * i + 1] << 16) | (b[off + 4 * i + 2] << 8) | b[off + 4 * i + 3];
    }
    for (i = 16; i < 64; i++) {
      var x = w[i - 15], y = w[i - 2];
      var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    var a = H[0], bb = H[1], c = H[2], dd = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
    for (i = 0; i < 64; i++) {
      var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      var ch = (e & f) ^ (~e & g);
      var t1 = (h + S1 + ch + K[i] + w[i]) | 0;
      var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      var mj = (a & bb) ^ (a & c) ^ (bb & c);
      var t2 = (S0 + mj) | 0;
      h = g; g = f; f = e; e = (dd + t1) | 0; dd = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    H[0] = (H[0] + a) | 0; H[1] = (H[1] + bb) | 0; H[2] = (H[2] + c) | 0; H[3] = (H[3] + dd) | 0;
    H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
  }
  var out = '';
  for (j = 0; j < 8; j++) { out += ('00000000' + (H[j] >>> 0).toString(16)).slice(-8); }
  return out;
}"""


def relay_digest(token: str) -> tuple[int, str]:
    """``(len(token), sha256 hex)`` of the ASCII ``token``; raises ``ValueError`` when it is not ASCII."""
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"relay token is not ASCII: {exc}") from exc
    return len(token), hashlib.sha256(raw).hexdigest()


RELAY_DAMAGE_CAUSE = "the agent's copy of the pieces is damaged"


def check_relay(flag: str, token: str, length: str, sha256: str, cause: str = RELAY_DAMAGE_CAUSE) -> Optional[str]:
    """None when ``token`` matches the sender's ``length`` and ``sha256`` (both raw flag strings);
    else one ``RELAY_CORRUPT_HEAD`` line naming ``--flag`` and both digests, ending in ``cause``.
    Never raises."""
    sent = f"sent as {length!r} chars sha256 {str(sha256)[:12]!r}"
    try:
        n, h = relay_digest(token)
    except ValueError:
        return f"{RELAY_CORRUPT_HEAD}: --{flag} is not ASCII, {sent}; {cause}"
    sent = f"sent as {length} chars sha256 {str(sha256)[:12]}"
    got = f"--{flag} arrived as {n} chars sha256 {h[:12]}, {sent}; {cause}"
    if not (isinstance(length, str) and length.isascii() and length.isdigit()):
        return f"{RELAY_CORRUPT_HEAD}: {got} (length is not a non-negative integer)"
    if not (isinstance(sha256, str) and len(sha256) == 64 and set(sha256) <= _HEX64):
        return f"{RELAY_CORRUPT_HEAD}: {got} (sha256 is not 64 lowercase hex)"
    if int(length) != n or sha256 != h:
        return f"{RELAY_CORRUPT_HEAD}: {got}"
    return None

# Records a fix stage must never write: history, not work product.
HISTORICAL_RECORD_PREFIXES = (
    _home_prefix("warp"),
    "docs/plans/",
    _home_prefix("handoffs"),
    _home_prefix("sizings"),
    "docs/decisions/",
)

_SHARED_WRITE_ROOTS = (_home_prefix("sizings"), _home_prefix("handoffs"), _home_prefix("apm-rulings"))


def _dir_root(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def run_write_roots(profile: str, queue_dirs: list[str]) -> tuple[str, ...]:
    """Which directories may a grind run's close commit sweep: the queue dirs, the profile's
    ledger dir and the shared sizing, baton and ruling dirs, each ending in ``/``, deduplicated in order."""
    roots = [_dir_root(d) for d in queue_dirs]
    roots.append(f"{LEDGER_ROOT}{profile}/")
    roots.extend(_SHARED_WRITE_ROOTS)
    return tuple(dict.fromkeys(roots))


def under_roots(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    """True when the repo-relative POSIX ``path`` lies under any of ``roots``."""
    return any(path.startswith(root) for root in roots)


def is_historical_record(path: str) -> bool:
    """May a fix stage write ``path``? False exactly when it lies under ``HISTORICAL_RECORD_PREFIXES``."""
    return path.startswith(HISTORICAL_RECORD_PREFIXES)


def encode_declared_paths(paths: list[str]) -> str:
    """Render ``paths`` as the ``--declared-urlenc`` token: percent-encoded JSON ``{"paths": [...]}``
    with ``'`` encoded too, so it is one shell-safe single-quoted token."""
    return quote(json.dumps({"paths": list(paths)}, sort_keys=True), safe="")


def decode_declared_paths(token: str) -> list[str]:
    """Inverse of ``encode_declared_paths``; returns ``sorted(set(paths))``, empty when none were
    declared. Raises ``ValueError`` on malformed JSON, a wrong key set or a non-string entry."""
    try:
        obj = json.loads(unquote(token))
    except json.JSONDecodeError as exc:
        raise ValueError(f"declared paths token is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) != {"paths"}:
        raise ValueError("declared paths token must be an object with exactly the key 'paths'")
    paths = obj["paths"]
    if not isinstance(paths, list):
        raise ValueError("declared paths token 'paths' must be a list")
    for entry in paths:
        if not isinstance(entry, str) or not entry:
            raise ValueError("declared paths token entries must be non-empty strings")
    return sorted(set(paths))


def partition_ledger_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split ``paths`` into ``(kept, dropped)``; dropped is every path under ``LEDGER_ROOT``. Order kept."""
    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        (dropped if path.startswith(LEDGER_ROOT) else kept).append(path)
    return kept, dropped


def _squash(value: str) -> str:
    return " ".join(value.split())


def render_row_trailer(row_id: str, outcome: str) -> str:
    """One finished row's trailer line: ``Grind-Row: <row_id> <outcome>``, whitespace collapsed."""
    return f"{ROW_TRAILER_KEY}: {_squash(row_id)} {_squash(outcome)}"


def is_grind_call(params: object) -> bool:
    """True when ``params[GRIND_COMMIT_PARAM]`` is a dict whose ``rows`` is a list."""
    grind = params.get(GRIND_COMMIT_PARAM) if isinstance(params, dict) else None
    return isinstance(grind, dict) and isinstance(grind.get("rows"), list)


def _bad_path(path: object) -> Optional[str]:
    if not isinstance(path, str) or not path:
        return f"path {path!r} is not a non-empty str"
    if path.startswith("/") or "\\" in path or (len(path) > 1 and path[1] == ":"):
        return f"path {path!r} is not repo-relative POSIX"
    if ".." in path.split("/"):
        return f"path {path!r} contains '..'"
    return None


def _path_list(row_id: str, field: str, value: object) -> Optional[str]:
    if not isinstance(value, list):
        return f"row {row_id!r} {field} is not a list"
    for path in value:
        bad = _bad_path(path)
        if bad:
            return f"row {row_id!r} {field}: {bad}"
    return None


def validate_grind_params(value: object) -> Optional[str]:
    """A refusal string when the ``grind`` params value is malformed, else None."""
    if not isinstance(value, dict):
        return "params.grind is not an object"
    profile = value.get("profile")
    if not isinstance(profile, str) or not profile:
        return "params.grind.profile is not a non-empty str"
    rows = value.get("rows")
    if not isinstance(rows, list):
        return "params.grind.rows is not a list"
    seen: set = set()
    for row in rows:
        if not isinstance(row, dict):
            return f"params.grind row {row!r} is not an object"
        row_id = row.get("row_id")
        outcome = row.get("outcome")
        if not isinstance(row_id, str) or not row_id:
            return f"params.grind row_id {row_id!r} is not a non-empty str"
        if not isinstance(outcome, str) or not outcome:
            return f"row {row_id!r} outcome {outcome!r} is not a non-empty str"
        if row_id in seen:
            return f"row id {row_id!r} is duplicated"
        seen.add(row_id)
        for field in ("paths", "deleted_paths"):
            bad = _path_list(row_id, field, row.get(field))
            if bad:
                return bad
    halted = value.get("halted")
    if halted is not None and (not isinstance(halted, str) or not halted):
        return "params.grind.halted is not absent, null or a non-empty str"
    return None
