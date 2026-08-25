r"""
coordinator_core.session.harness_registry — pure parser over the harness's
own `<claude-config>/sessions/*.json` registry.

Spec backlink: pln-the-harness-already-knows-whic-096836 § C1

Public surface (pinned contract — do not change without updating consumers):

    def registry_dir() -> Path | None
    def snapshot() -> dict[str, RegistryRecord]
    def lookup(session_id: str) -> RegistryRecord | None
    def self_record() -> tuple[str, RegistryRecord] | None
    class RegistryRecord: pid: int; start_epoch: float; cwd: str | None;
                           name: str | None; messaging_socket_path: str | None;
                           status: str | None; stable_pid_capture: str | None

`name` and `messaging_socket_path` (added
`state/handoffs/2026-08-13-session-owner-reachability-registry.md` § 1) are
parsed exactly like `cwd`: optional string fields, `None` when absent or
non-string, no verdict attached.

`messagingSocketPath` IS the harness's current spelling — verified against
the shipped CLI bundle, not inferred from records (Claude Code 2.1.232,
`~/.local/share/claude/versions/2.1.232`; the same spelling is present in
2.1.227 and 2.1.228). A registry in which NO record carries it does not
mean the key drifted and this parser needs correcting: the harness writes
the field from `CLAUDE_CODE_MESSAGING_SOCKET`, which is set only by a
successful cross-session-inbox bind, and it skips that bind entirely when
its messaging gate is off. Measured 2026-08-14: 44/44 records on this box
omit the field, and this session's own environment has no
`CLAUDE_CODE_MESSAGING_SOCKET`. Renaming the key read here, or deriving a
substitute from `sessionId`/`pid`, would manufacture an address the
harness itself refuses — see `coordinator_core.session.reachability`'s
module docstring and `messaging_available()`.

The gate's DEFAULT is a remote feature flag — verified against the
shipped CLI bundle 2026-08-15 (Claude Code 2.1.233,
`~/.local/share/claude/versions/2.1.233`): the bind is gated by
GrowthBook under the telemetry name `agents_cross_session_inbox`, and
there is a LATE-BIND path — a GrowthBook refresh that flips the gate ON
mid-session makes the harness call `startCrossSessionInbox()` and then
`updateSessionMessagingSocketPath()`, so a session can gain
`messaging_socket_path` after this module has already parsed it as
absent.

**The gate is NOT remote-only, and an earlier revision of this docstring
was wrong to say so.** From bundle 2.1.232 onward the gate predicate
short-circuits `true` on the environment variable
`CLAUDE_CODE_HARBOR_KITE` BEFORE it reaches either the platform check or
the GrowthBook read, so the operator's own box can force it on — proven
end to end on Windows (pipe bound, `messagingSocketPath` published, a
real peer message delivered), recorded in
`state/audits/2026-08-14-cross-session-messaging-gate-predicate-and-build-channel.md`.
The predicate is mangled per bundle: grep the literal string
`tengu_harbor_kite`, never a function name. Bundles before 2.1.232
returned false on Windows before ever reading the variable, so there the
override is inert rather than wrong.

Reading a gate-off registry therefore does NOT license "nothing local can
be done". `coordinator/bin/claude-doe.py` already defaults the variable
on for every session it launches. What is still unexplained, and is a
Claude-klabauter defect rather than a remote-flag fact, is that the default has not
yet produced a bound inbox on the interactive path: measured 2026-08-15,
45 records started after that launcher default landed and 0 of them carry
`messaging_socket_path`. Until that is chased down, a `False` from
`messaging_available()` is a live-capability reading, never evidence
about who owns the switch.

That the gate merely CYCLES — rather than peer messaging being absent on
Windows or never having worked — is settled by this box's own harness
debug log (`<claude-config>/debug/*.txt`), which records both states
within six minutes of each other:

    2026-08-14T22:18:45Z [DEBUG] [uds-messaging] Skipped: cross-session
        messaging gate off (will late-bind if a GrowthBook refresh
        enables it)
    2026-08-14T22:24:13Z [INFO]  [uds-messaging] Listening:
        \\.\pipe\cc-msg-3a24e4fdba67d42615065a1f8b22efbe

The second line is the inbox bound and listening on a native Windows
named pipe, and it is the mechanism behind the cross-repo EM-to-EM round
trip DoE-claude recorded that day
(`archive/handoffs/2026-08/2026-08-14-addressability-retraction-and-peer-read-wiring.md`).
So a gate-off reading here is a point-in-time fact about a remote flag,
never evidence that the capability is missing on this platform.

The pipe name is `\\.\pipe\<prefix><16 random bytes, hex>`
(`getDefaultUdsSocketPath` in the same bundle). Random per bind is why
publication through this registry is load-bearing rather than
convenience: a peer's address cannot be recomputed from `sessionId`,
`pid`, or anything else a reader holds, so a record that omits
`messaging_socket_path` describes a session no caller can address.

Measured the same session: every record now additionally carries
`peerProtocol: <int>` (42/42 records, all `peerProtocol: 1`; 0/42 carried
`messaging_socket_path`, same gate-off signature as 2026-08-14's 44/44).
This is a SEPARATE field answering a SEPARATE question — the harness's
own FleetView peer listing filters on `peerProtocol >= 1` plus liveness
plus a 24h recency window, NOT on the socket, so peer VISIBILITY
(FleetView listing a peer) and peer DELIVERABILITY (`SendMessage`
actually reaching it) are two different predicates on this harness
version. This module parses `peerProtocol` nowhere and does not need to:
`messaging_available()` (`coordinator_core.session.reachability`) remains
the correct predicate for DELIVERABILITY specifically, and its docstring
says so explicitly for the same reason this paragraph does — so a future
reader does not "fix" `messaging_available()` toward `peerProtocol` and
quietly turn a deliverability check into a visibility check. They exist so
`coordinator_core.session.reachability` can build a `SendMessage` address
without a second parser over this same directory — see that module's
docstring for the ref-derivation and ambiguity-disambiguation this unlocks.
Like every other field here, reading them attaches no liveness meaning:
`session_live()` and this module's own callers continue to key off `pid` +
`start_epoch` only.

`self_record()` (added `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`
§ C1, spike-verified anchor; cite `DoE-claude@642195ba` / follow-up
`DoE-claude@88929bea` — the wrongful-takeover shape this module's
negative-spec defends against) is the O(1) leg over the pid-keyed
`<registry_dir>/<CLAUDE_PID>.json` file: it resolves `CLAUDE_PID` via
`coordinator_core.session.core._resolve_claude_pid_from_env` (the one
shared resolver, never a bare `os.environ.get`) and parses that single
file via `_parse_one` — ONE `read_text`, no `glob`, no `snapshot()` call.
It returns `_parse_one`'s own `tuple[str, RegistryRecord] | None` shape
(the `str` is `sessionId`) so a caller has a `sessionId` to trust-check
against, not just a bare record. Like every other function in this
module, `self_record()` NEVER calls `core.stable_pid_alive` — it is a
pure parser with no verdict arm; the trust check belongs to the caller
(`pickup_assemble.compute_repo_identity_gate`), never here. This module's
central invariant and negative-spec (below) apply to `self_record()`
identically to `snapshot()`/`lookup()`.

`registry_dir()` resolves `<claude-config>/sessions` by routing through
`coordinator_core._settings_home.claude_config_dir()` — NEVER hand-roll
`Path.home() / ".claude"`; that duplicate computation is exactly the
split-brain defect `claude_config_dir()`'s own divergence check exists to
close.

`snapshot()` performs the ONE directory scan: it parses `sessions/*.json`
once and returns a `sessionId`-keyed dict of parsed records. `lookup(sid)`
is defined as `snapshot().get(sid)` — a convenience for the O(1)
`session_live` call site, NOT a second scan implementation. Called per-id
inside a verdicts loop, a per-call scan would be hundreds of parses per
invocation on a loaded box and would give a torn view across ids within one
pass.

THE CENTRAL INVARIANT. `RegistryRecord` is a parsed `(pid, start_epoch)`
pair, NOT a liveness verdict. `snapshot()` and `lookup()` do NOT call
`core.stable_pid_alive` — they are pure parsers. The tolerant birth-instant
compare happens at `liveness.py`'s existing single `core.stable_pid_alive`
seam (a later chunk's job, not this module's), keeping
`session/liveness.py`'s D5 single-liveness-key invariant literally true.

Negative-spec:
    - There is NO DEAD arm and no code path in this module that constructs
      one. A registry miss, a malformed file, an absent directory, a
      `sessionId` mismatch, or a `procStart` failing the sanity band below
      each yield `None` for that record — never a verdict. The caller's
      existing fallback stack decides what `None` means. An executor
      "simplifying" a `None` return into a dead verdict reintroduces a
      wrongful-takeover failure this fleet has already suffered twice
      (DoE 642195ba, follow-up 88929bea).
    - `updatedAt` and `statusUpdatedAt` are read NOWHERE in this module, at
      any call site, forever. They track busy/idle transitions, not process
      liveness: a continuously-working session was measured carrying a
      1465-second-old `updatedAt` — worse than the existing 30-minute
      recency window, not better. `status` is the ONE exception, added
      `state/handoffs/2026-08-13-live-peer-roster.md` § 3: it MAY be parsed,
      display-only, exactly like `cwd`/`name` (string-or-None, no verdict
      attached) — it must NEVER be read as an input to any liveness,
      reachability, or claim-verdict computation anywhere in this tree. The
      1465-second measurement above is the reason why, and that finding
      stands unchanged, not superseded by anything below.

      A larger spike (DoE-claude:state/audits/2026-08-13-session-stop-reason-spike.md)
      independently re-measured this: 390 consecutive-tick transitions
      across 30 live sessions over 17 ticks, using monotonic process
      CPU-time delta as independent ground truth, excluding an ambiguous
      CPU band. Result: `busy` → 80 actually-working / 0 quiet (80/80
      precision); `idle` → 2 working / 202 quiet. Status AGE ran to
      24,958s (6.9h), median 540s, p90 18,802s. Reading: `busy` is a
      trustworthy POSITIVE, but `idle` is NOT a trustworthy negative — it
      means unknown, not quiet. THE BAN STANDS ANYWAY: status age is
      UNBOUNDED (p90 5.2h, max 6.9h), so no freshness gate can rescue
      `status` as a verdict input even for the `busy` positive. DoE's own
      offered correction: their sampling threshold sits near the CPU noise
      floor, so the 2 `idle`-but-working cases are borderline rather than
      clean errors — the `busy` precision figure and the staleness
      distribution do not depend on that threshold.

      RULING (EM ruling, claude-klabauter-em, 2026-08-14, in response to DoE's
      memo `cross-repo/inbox/2026-08-13-doe-claude-em-session-state-surface-findings.md`):
      DoE's observation was correct — the ban IS broader than the 1465s
      `updatedAt` measurement alone supports — and it is kept broad
      deliberately, not by oversight. Both questions DoE raised are now
      decided.

      Ruling 1 — the ban's scope stays as written: liveness, reachability,
      AND claim-verdicts, all three. This is a cost asymmetry, not a
      preference. `status` is display-only: no computation in this tree
      needs it as a verdict input, so the breadth of the ban costs exactly
      nothing in capability. Narrowing it to liveness-only would buy no
      capability while reopening the failure class the DEAD-arm
      negative-spec above exists to prevent. The 80/80 `busy` precision is
      the TRAP here, not a counter-argument: precision is measured at the
      instant of write, and status age is unbounded (p90 5.2h, max 6.9h) —
      a 6.9h-old `busy` is precisely the shape of a session that has since
      stopped. A future reader who sees "busy is a trustworthy positive"
      and narrows the ban on that basis reintroduces wrongful-takeover.
      Over-banning a field nobody needs costs zero; under-banning costs the
      failure this fleet has already suffered twice (DoE 642195ba,
      follow-up 88929bea).

      Ruling 2 — enforcement moves from a described grep to a test:
      `coordinator_core/session/tests/test_status_ban_enforcement.py` is
      the executable form of this negative-spec block; that file and this
      prose are meant to be read together, and this prose remains
      authoritative over it.

      A cost signal attached to the scope question settled above, supplied
      by DoE 2026-08-14 as evidence and explicitly NOT as clearance to widen:
      `pickup_assemble`'s competing-claim gate emits, per candidate,
      `holder_live` plus `liveness_basis: "harness-registry"`, so every
      DoE pickup that stands down on a live peer — or takes over an
      apparently-orphaned claim — rests that mutual-exclusion verdict on
      this module's basis. Their live sample: 18 candidates, 9 live-peer,
      9 stale-claim, the takeover-authorizing half. Corroborated the same
      day by a claude-klabauter pickup brief carrying the same two fields. Second
      named cross-repo consumer after C1's; whether the ban widens to
      cover it stays a PM scope call, unchanged by this note.

      A grep for `updatedAt`/`statusUpdatedAt` inside
      `coordinator_core/session/` must return hits only in this block; a
      grep for `status` may additionally hit `_parse_one`'s parse line,
      `RegistryRecord.status`'s field, and a display-only consumer (e.g.
      `coordinator_core.session.peer_roster`) — never a verdict/liveness
      call site.
    - This module never calls `core.stable_pid_alive`, `psutil.pid_exists`,
      or any raw `pid`-only liveness check — see the RAW-PID-LIVENESS floor
      in `liveness.py`. The registry's `pid` is admissible only paired with
      `start_epoch` through `stable_pid_alive`, at that module's seam.
    - No caching/memoization across `snapshot()` calls — each call is a
      fresh, single scan; staleness policy belongs to the caller.

`procStart` is observed in THREE shapes. On macOS (Claude Code 2.1.231,
observed 2026-08-13) it is a POSIX ctime string, e.g.
`"Thu Aug 13 09:42:07 2026"` — **UTC**, not local time, confirmed live
against 37/37 real records on this box via `calendar.timegm`: sub-second
agreement with `psutil.Process(pid).create_time()` on every one, while
`time.mktime` (local-time interpretation) is off by exactly the machine's
UTC offset on every record (measured: 3600s at TZ=Europe/London/BST). Do
NOT parse this leg with `time.mktime` — see the negative-spec entry below
re: `core.lstart_to_epoch`. Parsed via `time.strptime` + `calendar.timegm`,
with an ISO-8601 fallback (naive treated as UTC via
`.replace(tzinfo=timezone.utc)`, tz-aware converted via `.timestamp()`). On
Windows it is a FILETIME — 100ns ticks since 1601-01-01, converted via
`int(procStart) / 1e7 - 11644473600` — carried either as a genuine JSON
`int`, or, as a last-resort third leg tried only after both the ctime and
ISO-8601 string attempts fail, as a digits-only JSON string. Both FILETIME
legs are now verified live on Windows, not merely assumed: the string leg
was measured 2026-08-14 against 54/54 real `sessions/*.json` records
carrying `procStart` as a string of digits (before this leg existed,
`_proc_start_to_epoch` returned `None` for every one of them), and the
int leg against 22 live rows in an earlier session — both to
sub-millisecond agreement with `psutil.Process(pid).create_time()`. A
converted epoch outside `[now - 90d, now + 1h]` is REJECTED (record yields
`None`) before it is ever returned, identically for every leg: this turns
a unit mismatch or bad-shape record into an explicit `None` rather than a
coincidence risk.

Negative-spec: `coordinator_core.session.core.lstart_to_epoch` parses a
visually identical ctime shape but deliberately reads it as LOCAL time,
because it parses `ps -o lstart=` output, which `ps` renders in the
machine's local zone. It must NOT be reused here, and this parser must not
later be "deduplicated" into it — the two functions parse the same string
format under different zone semantics, and merging them reintroduces
exactly this one-hour (or DST-dependent) error. The upside of `procStart`
being UTC: the ±1h fall-back-DST ambiguity that `lstart_to_epoch`'s own
docstring accepts as a residual on its local-time leg does not arise on
this path at all.

Defensive parsing: `OSError`, `json.JSONDecodeError`, a missing `pid` or
`procStart` key, a non-`int` `pid`, a non-numeric `procStart`, and an
out-of-band `procStart` each yield `None` for that record and skip to the
next record — these are the common, expected cases. ADDITIONALLY, both
`snapshot()` and `lookup()` catch an outermost `except Exception` at the
public boundary and degrade to `{}` / `None` respectively (AC11) — the
enumerated list above is NOT the contract's actual boundary and provably
omits real raisers reachable on this path: `registry_dir()` routes through
`_require_rooted` (raises `ValueError` on a bad/relative `CLAUDE_CONFIG_DIR`
override), `int(procStart)` can raise `OverflowError`, and `read_text` can
raise `UnicodeDecodeError`. Nothing in this module may raise to its caller
— it sits on the claim hot path, and the module it feeds
(`session/liveness.py`) already documents fail-open-never-fail-dead as its
established bias.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core._settings_home import claude_config_dir

_FILETIME_EPOCH_OFFSET_SEC = 11644473600.0
_FILETIME_TICKS_PER_SEC = 1e7

_SANITY_BAND_PAST_SEC = 90 * 24 * 3600
_SANITY_BAND_FUTURE_SEC = 3600


@dataclass(frozen=True)
class RegistryRecord:
    """A parsed seven-field record — pid, start_epoch, cwd, name,
    messaging_socket_path, status, stable_pid_capture — NOT a liveness
    verdict.

    `start_epoch` is a Unix epoch (seconds, float) already converted from the
    registry's raw `procStart` — a POSIX ctime/ISO-8601 string on macOS, a
    Windows-FILETIME integer assumed on Windows (unverified) — and already
    validated inside the sanity band. Consumers pair this with `pid` through
    `core.stable_pid_alive`'s tolerant birth-instant compare — never a bare
    `pid_exists`/`kill -0` check.

    `cwd` is the session's CURRENT harness-level working directory as last
    recorded by the harness — the value `/cd` moves, not a launch-immutable
    fact — or `None` if the raw record omits it. Like `pid`/`start_epoch`,
    this is a parsed value only; it carries no repo-identity verdict of its
    own. Consumers compose a verdict over it (e.g.
    `pickup_assemble.compute_repo_identity_gate`'s containment + plausibility
    check) — this module stays a pure parser.

    `name` is the harness-rendered peer label (e.g. `"claude-klabauter-57"`,
    ONE cryptographically-random-byte suffix over `slug(basename(cwd))` —
    see `coordinator_core.session.reachability`, which does not attempt to
    derive or validate it). `messaging_socket_path` is the opaque string the
    harness hashes to build a peer's disambiguating `[ref]` — never parsed
    as a POSIX path here or by any consumer; treat it as bytes-in, hash-out.
    Both are `None` when the raw record omits them or the field is not a
    string — same parse-only, no-verdict contract as `cwd`.

    `status` (added `state/handoffs/2026-08-13-live-peer-roster.md` § 3) is
    the harness's busy/idle label, parsed exactly like `cwd`/`name` — an
    optional string, `None` when absent or non-string, no verdict attached.
    It exists for DISPLAY ONLY (`coordinator_core.session.peer_roster`);
    liveness/reachability/claim-verdict computation must never read it — see
    this module's own negative-spec above for why `status` is measured worse
    than the existing liveness window.

    `stable_pid_capture` (added chunk C2b, threading the previously-discarded
    `reason` leg of `core._resolve_claude_pid_from_env()` out of
    `self_record()`) carries the SAME `env-hit` / `env-miss:<...>` /
    `psutil-absent` vocabulary that module's docstring defines and that
    `core.cs_init`'s own `stable_pid_capture` breadcrumb already writes at
    its three call sites — reused verbatim here, never a new vocabulary. Only
    `self_record()` populates it (`snapshot()`/`lookup()` leave it `None`,
    since neither calls `_resolve_claude_pid_from_env`); it is display/
    diagnostic only, no verdict meaning, same no-liveness contract as `cwd`/
    `name`/`status`.
    """

    pid: int
    start_epoch: float
    cwd: str | None = None
    name: str | None = None
    messaging_socket_path: str | None = None
    status: str | None = None
    stable_pid_capture: str | None = None


def _proc_start_to_epoch(raw) -> float | None:
    """Convert a raw `procStart` field to a Unix epoch, or None if invalid.

    Accepts two observed shapes: a Windows FILETIME integer (100ns ticks
    since 1601-01-01), or a string — a POSIX ctime string (`%a %b %d
    %H:%M:%S %Y`, UTC — confirmed live against 37/37 real records, macOS,
    Claude Code 2.1.231, observed 2026-08-13: `calendar.timegm` agrees with
    `psutil.Process(pid).create_time()` to sub-second precision on every
    one, while `time.mktime` (local-time interpretation) is off by exactly
    the machine's UTC offset — do NOT swap this back to `time.mktime`) or
    ISO-8601 (naive treated as UTC via `.replace(tzinfo=timezone.utc)`,
    tz-aware converted via `.timestamp()`).

    Three shapes are accepted, tried in this order: (1) a genuine, non-bool
    `int` FILETIME — mirroring `_parse_one`'s own `pid` check
    (`isinstance(raw, bool)` rejected, `isinstance(raw, int)` required),
    never a bare `int(raw)`, which would silently truncate a JSON float into
    this leg; (2) a string tried as ctime then ISO-8601; (3) if both string
    attempts fail AND the string is an unsigned ASCII all-digits string
    (`raw.isascii() and raw.isdigit()` — plain `str.isdigit()` alone admits
    non-decimal Unicode digit characters, e.g. superscript `'²'`, that
    `int()` then rejects with `ValueError`), it is retried as a FILETIME
    (100ns ticks since 1601-01-01) — the digits-string leg of last resort,
    no sign handling, no float strings, no whitespace tolerance. Measured
    live on Windows
    (2026-08-14, Claude Code): 54/54 real `sessions/*.json` records carry
    `procStart` as a JSON string of digits (a Windows FILETIME rendered as a
    string, not a bare int as originally assumed) — before this leg
    existed, `_proc_start_to_epoch` returned `None` for every one of them.
    The digits-string leg MUST come last, after both ctime and ISO-8601
    have already been tried and failed: a numeric string is ambiguous on
    its face (it could in principle be a mis-shaped ctime/ISO value) and
    this ordering is what lets the genuine date-string shapes claim it
    first, matching how the `int` leg already refuses to treat a numeric
    string as its own case. A float or a non-digits string always falls
    through past every FILETIME leg entirely.

    Raises nothing — every failure mode (non-numeric, non-date string,
    overflow, out of the `[now - 90d, now + 1h]` sanity band) returns None
    so the caller skips the record rather than propagating.
    """
    epoch = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        try:
            epoch = raw / _FILETIME_TICKS_PER_SEC - _FILETIME_EPOCH_OFFSET_SEC
        except OverflowError:
            return None
    elif isinstance(raw, str) and raw:
        try:
            struct = time.strptime(raw, "%a %b %d %H:%M:%S %Y")
            epoch = calendar.timegm(struct)
        except (ValueError, OverflowError):
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                if raw.isascii() and raw.isdigit():
                    try:
                        epoch = int(raw) / _FILETIME_TICKS_PER_SEC - _FILETIME_EPOCH_OFFSET_SEC
                    except (ValueError, OverflowError):
                        return None
                else:
                    return None
            else:
                if dt.tzinfo is None:
                    try:
                        epoch = dt.replace(tzinfo=timezone.utc).timestamp()
                    except (ValueError, OverflowError):
                        return None
                else:
                    epoch = dt.timestamp()

    if epoch is None:
        return None

    now = time.time()
    if epoch < now - _SANITY_BAND_PAST_SEC or epoch > now + _SANITY_BAND_FUTURE_SEC:
        return None
    return epoch


def registry_dir() -> Path | None:
    """Resolve `<claude-config>/sessions`, or None if it cannot be resolved.

    Routes through `_settings_home.claude_config_dir()` — never hand-rolls
    `Path.home() / ".claude"`. Never raises: a bad/relative
    `CLAUDE_CONFIG_DIR` override surfaces as `ValueError` from
    `claude_config_dir()`'s internal `_require_rooted`, caught here and
    turned into None, matching this module's fail-open contract.
    """
    try:
        return claude_config_dir() / "sessions"
    except Exception:
        return None


def _parse_one(path: Path) -> tuple[str, RegistryRecord] | None:
    """Parse a single `sessions/<x>.json` file into (sessionId, record), or None."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    session_id = data.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None

    raw_pid = data.get("pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int):
        # Review: coordinator:code-reviewer — a non-int pid (e.g. a JSON
        # float) must yield None per the module docstring's "non-`int` pid"
        # negative-spec case, not silently truncate via int().
        return None
    pid = raw_pid
    if pid <= 0:
        # Review: coordinator:code-reviewer — no OS ever assigns a
        # non-positive pid; same defensive class as the FILETIME sanity band.
        return None

    start_epoch = _proc_start_to_epoch(data.get("procStart"))
    if start_epoch is None:
        return None

    raw_cwd = data.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None

    raw_name = data.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else None

    raw_socket = data.get("messagingSocketPath")
    messaging_socket_path = raw_socket if isinstance(raw_socket, str) and raw_socket else None

    raw_status = data.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else None

    return session_id, RegistryRecord(
        pid=pid,
        start_epoch=start_epoch,
        cwd=cwd,
        name=name,
        messaging_socket_path=messaging_socket_path,
        status=status,
    )


def snapshot() -> dict[str, RegistryRecord]:
    """Parse every `sessions/*.json` once; return a `sessionId`-keyed dict.

    The ONE directory scan this module performs — `lookup()` is defined over
    this, never a second scan implementation. Never raises: any unexpected
    internal exception is caught at this boundary and degrades to `{}`.

    If two files in the registry directory carry the same `sessionId`, the
    later one in `Path.glob`'s OS-dependent iteration order wins (last
    writer wins, order unspecified) — acceptable given the registry is
    self-pruning. Review: coordinator:code-reviewer.
    """
    try:
        directory = registry_dir()
        if directory is None or not directory.is_dir():
            return {}

        result: dict[str, RegistryRecord] = {}
        for entry in directory.glob("*.json"):
            parsed = _parse_one(entry)
            if parsed is None:
                continue
            session_id, record = parsed
            result[session_id] = record
        return result
    except Exception:
        return {}


def lookup(session_id: str) -> RegistryRecord | None:
    """Return the registry record for `session_id`, or None.

    Defined as `snapshot().get(session_id)` — a convenience over the single
    scan, not a second scan. Never raises: any unexpected internal exception
    is caught at this boundary and degrades to None.
    """
    try:
        return snapshot().get(session_id)
    except Exception:
        return None


def self_record() -> tuple[str, RegistryRecord] | None:
    """Return this session's own registry record, or None.

    The O(1) leg: reads `<registry_dir>/<CLAUDE_PID>.json` directly, via
    `_parse_one` — ONE `read_text`, no `glob`, no `snapshot()` scan. This is
    what keeps a caller's zero-spawn claim true (a single registry record
    file read only). `CLAUDE_PID` is resolved via the ONE shared resolver,
    `coordinator_core.session.core._resolve_claude_pid_from_env` — never a
    bare `os.environ.get('CLAUDE_PID')` — so an absent, non-integer, or
    non-positive value is rejected by that resolver's own established
    validation rather than growing a second, divergent parse here.

    Returns `_parse_one`'s own `tuple[str, RegistryRecord] | None` shape
    (the `str` is `sessionId`) — NOT a bare `RegistryRecord` — so a caller
    performing a trust check (e.g. `pickup_assemble.compute_repo_identity_
    gate`'s AC10 cross-check) has a `sessionId` to compare against on this
    pid-keyed leg. Routing through `_parse_one` also guarantees this leg's
    FILETIME sanity band and non-int-pid/pid<=0 rejections cannot diverge
    from `snapshot()`'s fallback leg — the split-brain this module's
    docstring forbids.

    Absent `CLAUDE_PID`, an unresolvable registry dir, an unreadable file,
    or a malformed record each yield None — this module's established
    fail-open-never-fail-dead bias. Like every other function here, this
    NEVER calls `core.stable_pid_alive` — no verdict arm, pure parser only.
    Never raises: any unexpected internal exception is caught at this
    boundary and degrades to None (AC11-shaped boundary, matching
    `snapshot()`/`lookup()`).

    The `reason` leg of `_resolve_claude_pid_from_env()`'s `(match, reason)`
    return is threaded onto the returned record's `stable_pid_capture` field
    (chunk C2b) rather than discarded, mirroring the breadcrumb the other
    three `_resolve_claude_pid_from_env` call sites already write. This is
    additive record data only — it changes no early-return branch and no
    liveness verdict; when `match` is `None` this function still returns a
    bare `None` exactly as before, since there is no record to attach a
    reason to.
    """
    try:
        from coordinator_core.session.core import _resolve_claude_pid_from_env

        match, reason = _resolve_claude_pid_from_env()
        if match is None:
            return None
        pid, _create_time = match

        directory = registry_dir()
        if directory is None:
            return None

        parsed = _parse_one(directory / f"{pid}.json")
        if parsed is None:
            return None
        session_id, record = parsed
        return session_id, replace(record, stable_pid_capture=reason)
    except Exception:
        return None
