"""coordinator_core.warm.settings_home_claim -- the caller's settings home, on the wire.

Bug backlog: state/bug-backlog/2026-08-29-the-warm-server-answers-against-its-spaw-f1bcc4154ca4.yaml
             (P0, step 1 of that row's two-step disposition)

THE DEFECT THIS REFUSES ON. A warm server resolves `settings_home()` ONCE, from the
environment of whoever SPAWNED it, and is keyed on (user, engine-clone, engine-token) --
never on the settings home. A caller that sets `COORDINATOR_SETTINGS_HOME` therefore gets
a result computed against a home it did not name, with no error and no warning: verified
2026-08-29 through both `coordinator-invoke.cmd` and `coordinator-invoke.exe`, both
reporting `fleet.mode_show` -> `fleet_value: null` while the overridden home held a set
record.

WHY THAT IS A P0 AND NOT A TEST-ISOLATION ANNOYANCE. The settings home is where
guard-DISARMING state lives, not merely advisory state: `bash_guards/_blanket_disarm.py ::
marker_path()` resolves the blanket-disarm marker (a file whose PRESENCE turns guards off)
through it, `authz/classification.py` keys op authorization off it, `secrets/` is a
directory inside it, and `block_fleet_delegation_creation` /
`block_disarm_marker_sentinel_creation` / `block_subagent_destructive_action` all resolve
through it. A silently-wrong home on those paths answers in the direction that DISARMS.

WHAT THIS MODULE IS, AND IS NOT. It is the REFUSAL half only -- the tradeoff-free first
step. It does NOT deliver isolation: a warm server still cannot serve two settings homes,
and making it able to (per-request resolution over the request envelope, following
`warm/caller_context.py`'s payload-first shape) is the second step, plan-sized because
~30 non-test callers of `settings_home()` need auditing for import-time or module-scope
resolution. What this closes is the SILENCE: a request whose caller named a different home
than the serving process resolved is refused, never answered.

SHAPE MIRRORED FROM `coordinator_core.publish_lane`, deliberately and for the same reason.
That module already carries one caller-owned fact across the same pipe: a small,
stdlib-only module both ends import, one underscore-prefixed envelope field, absence read
as "no claim" rather than as a value. This is that pattern's second instance, not a new
convention -- see `publish_lane.PUBLISH_LANE_FIELD`'s own note on why a warm server's
`os.environ` cannot answer a question about the caller.

ABSENCE IS NOT A MISMATCH, and that is load-bearing. A request carrying no claim resolves
exactly as it does today -- the plain user path (no override set anywhere) is byte-for-byte
unchanged, which the bug row's own "BOUNDING THE DEFECT" section insists on: with no
override, `fleet.mode_set`/`mode_show` through the door already work, and nothing here may
disturb that. Only a caller that EXPLICITLY set `COORDINATOR_SETTINGS_HOME` stamps a claim,
so only a caller who asked for a specific home can be refused for not getting it.

NEGATIVE SPEC.

- **No second resolver.** `settings_home()` is not re-derived here; the client-side claim is
  whatever `coordinator_core._settings_home.settings_home()` returns, and the server-side
  side of the comparison is the same call in the serving process. Two resolvers that drift
  is the defect one layer down from the one this closes.
- **No repair, no re-resolution, no per-request settings home.** This module compares and
  reports; it never re-points the serving process, never mutates `os.environ`, and never
  hands a caller a home. Step 2 owns that.
- **No filesystem read on the agreeing path.** `claims_agree` is pure string work when the
  two spellings match lexically; it reaches for `os.path.realpath` ONLY after a lexical
  mismatch, i.e. only on a path that is already about to refuse. The hot path (agreement,
  or no claim at all) pays no stat.
"""

from __future__ import annotations

import os
from typing import Any, Optional

__all__ = [
    "SETTINGS_HOME_ENV",
    "SETTINGS_HOME_FIELD",
    "caller_claim",
    "request_claim",
    "claims_agree",
    "mismatch_message",
]

#: The environment override whose silent loss across the warm pipe this refusal covers.
#: Named here rather than read from `_settings_home` because this module needs the VAR,
#: not the resolved path: a claim is stamped only when a caller set this explicitly.
SETTINGS_HOME_ENV = "COORDINATOR_SETTINGS_HOME"

#: The JSON-RPC envelope field carrying the caller's resolved settings home,
#: underscore-prefixed like `_session_id`, `_engine_token` and `_publish_lane` to mark it
#: transport metadata rather than an op param.
SETTINGS_HOME_FIELD = "_settings_home"


def caller_claim() -> Optional[str]:
    """The settings home THIS process would resolve, but only when it was asked for.

    `None` -- no claim -- whenever `COORDINATOR_SETTINGS_HOME` is unset or empty, which is
    every ordinary invocation on every box. A caller with no override has no opinion about
    which home serves it and must not be refused for the server's choice; stamping the
    default resolution would turn every home-disagreement between two default-resolving
    processes (different `HOME`, a roaming profile, a service account) into a refusal of
    traffic that works today.

    Reads `os.environ` directly and takes no `environ` seam, deliberately: the value it
    returns must be the one `_settings_home.settings_home()` itself resolves, and that
    function reads the process environment. A second, mapping-driven path here would be a
    second resolver -- the exact drift this module's negative spec forbids. Tests set the
    variable (`monkeypatch.setenv`) rather than passing a mapping.

    The value is the RESOLVED home, not the raw variable, so the server compares two
    values produced by the same resolver: `settings_home()` validates the override
    (absolute-path check, `.claude`-doubling check), so a caller that set something
    malformed fails on its own side rather than having a malformed string compared against
    a well-formed one.
    """
    raw = os.environ.get(SETTINGS_HOME_ENV)
    if not raw or not raw.strip():
        return None

    from coordinator_core._settings_home import settings_home

    return str(settings_home())


def request_claim(msg: Any) -> Optional[str]:
    """The settings home a JSON-RPC request envelope claims, or `None` for no claim.

    Tolerant of a non-dict `msg` and of a non-string field value: a malformed envelope is
    not a claim. `_serve_line` consults this before dispatch, on frames that
    `_parse_frame` has already accepted, so the tolerance is belt-and-braces rather than
    the primary contract.
    """
    if not isinstance(msg, dict):
        return None
    raw = msg.get(SETTINGS_HOME_FIELD)
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _normalized(path: str) -> str:
    """Case- and separator-normalized absolute form, trailing separators dropped.

    `normcase` is what makes this correct on Windows, where the same directory is
    routinely spelled with either slash and either case by the two sides of this
    comparison (a door built by `wide_to_utf8` from `GetCurrentDirectoryW` and a Python
    `Path.home()` join do not agree on either), and where a spelling difference is
    emphatically not a different home.
    """
    normalized = os.path.normcase(os.path.abspath(path))
    stripped = normalized.rstrip("/\\")
    return stripped or normalized


def claims_agree(claim: Optional[str], resolved: str) -> bool:
    """True when *claim* names the same directory the serving process resolved.

    No claim agrees with anything -- see the module docstring on why absence may never
    refuse.

    Two comparisons, in cost order. The lexical one settles every ordinary case without
    touching the disk. Only when it fails does this reach for `os.path.realpath` on both
    sides, which is the one thing that distinguishes "a symlinked or 8.3-shortened
    spelling of the same home" from "a different home" -- a stat pair spent exclusively on
    a request that is otherwise about to be refused, never on the serving path.
    """
    if claim is None:
        return True
    if _normalized(claim) == _normalized(resolved):
        return True
    try:
        return _normalized(os.path.realpath(claim)) == _normalized(os.path.realpath(resolved))
    except OSError:
        # An unresolvable path is not evidence of agreement. Default posture on ambiguity
        # is deny -- the same posture the guard directories this defect endangers state
        # for themselves.
        return False


def mismatch_message(claim: str, resolved: str) -> str:
    """The refusal sentence, in the agent-facing register: one fact, once, plus the
    terse alternative (`docs/wiki/guard-messaging.md` § Register).

    Names BOTH homes because neither alone is actionable: the caller knows what it asked
    for and not what it got, and an operator reading a transcript knows neither.

    Both paths go in VERBATIM, never through `!r`. A Windows path rendered as a Python
    repr comes out with every separator doubled, which is not the string the operator
    typed, not the string they can paste back, and not the string a transcript grep for
    the home will match.
    """
    return (
        "warm dispatch refused: this server resolved its settings home at spawn time and "
        f"cannot serve another. Caller asked for {claim}; this server serves {resolved}. "
        f"Unset {SETTINGS_HOME_ENV}, or set COORDINATOR_WARM=0 to run cold against the home "
        "you named."
    )
