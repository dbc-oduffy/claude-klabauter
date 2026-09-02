"""
coordinator_core.session.fleet_mode — the fleet record: one home, atomic
writes, and degradation to today's behaviour.

Writes/reads ``<settings_home()>/fleet-mode.json`` — a single, machine-wide
record read by every live session's next turn-boundary hook, resolved via
``coordinator_core._settings_home.settings_home()`` (never a hand-rolled
path, never a second candidate location). Models directly on
``coordinator_core.session.grant``: same ``tempfile.mkstemp`` + ``os.replace``
atomicity, same writer/reader shape — a reader never observes a
partially-written record.

ONE HOME, AND WHY. ``docs/decisions/DR-222-health-sentinel-durability-
parity-settings-home-dual-read.md`` is the standing ruling on settings-home
conventions and mandates a dual-home read for durability parity — it does
NOT apply here. ``coordinator_core.session.guard_unlock_sentinel`` draws
this exact distinction against DR-222 in its own docstring: a record with
exactly one writer and exactly one home makes a dual-home read unjustified
complexity, not parity. That holds here too — ``fleet-mode.json`` has one
writer (C4's op) and one home (``settings_home()``), so there is no second
copy to fall back to and nothing for a dual read to protect against.

BUDGET. ``read_fleet_mode()`` is the budget-critical call — it runs on every
live session's turn-boundary hook fire — and is exactly one ``stat`` plus
one small ``json.loads``, no subprocess, under 5ms; measured 27.6us median /
69.0us p99.

FAIL-OPEN, DELIBERATELY. ``read_fleet_mode()`` returns an empty mapping —
never raises, never a partial record — on every one of: absent file,
unreadable file, malformed JSON, valid JSON of the wrong shape, and an
unknown key. An empty mapping is what makes every caller degrade to TODAY's
behaviour: a fail-open that silently enables a mode is worse than no plane
at all (an absent/corrupt file must never be read as "fleet mode is on"),
and a fail-closed that raises would brick a hook on every turn boundary of
every session — see
``state/lessons/2026-07-30-fail-closed-guard-save-window-bricks-bash-4c1f7ab9de02.yaml``
for the incident this guards against. Both rules are simultaneously true:
the WRITE side validates and can raise on a caller programming error; the
READ side never does.

Public surface:
    fleet_mode_path() -> Path       — the resolved settings-home-rooted path.
    read_fleet_mode() -> dict       — budget-critical reader; never raises.
    write_fleet_mode(record: dict)  — atomic create-or-overwrite.

Negative-spec:
    - Do NOT add an env-keyed override on any leg — ``settings_home()``
      already resolves ``COORDINATOR_SETTINGS_HOME`` internally; a second
      override here would create a second candidate location, which this
      module's one-home contract forbids.
    - Do NOT shell out, spawn a subprocess, or invoke the engine/CLI from
      the read path — ``read_fleet_mode()`` is a stat plus a small JSON
      read, nothing else.
    - Do NOT let ``read_fleet_mode()`` raise, under any input. Every
      failure mode collapses to an empty mapping.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from coordinator_core._settings_home import settings_home

_FLEET_MODE_FILENAME = "fleet-mode.json"

#: Generator-provenance declaration: write_fleet_mode()'s only write is
#: `settings_home() / _FLEET_MODE_FILENAME` — the operator's coordinator
#: settings home, never a path inside this repo's tracked tree. No tracked
#: artifact exists for `GENERATES` to name.
GENERATES = []


def fleet_mode_path() -> Path:
    """Resolve ``<settings_home()>/fleet-mode.json`` — the single home for
    the fleet record. See module docstring "ONE HOME, AND WHY" for why this
    is a single location, not a dual-home read."""
    return settings_home() / _FLEET_MODE_FILENAME


def read_fleet_mode() -> dict:
    """Budget-critical reader: one ``stat`` plus one ``json.loads``, no
    subprocess. Never raises — returns ``{}`` on every degradation input
    (absent file, unreadable file, malformed JSON, valid JSON of the wrong
    shape, an unknown key), so every caller degrades to TODAY's behaviour.
    See module docstring "FAIL-OPEN, DELIBERATELY" for the reasoning."""
    path = fleet_mode_path()
    try:
        if not path.is_file():
            return {}
    except OSError:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        record = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(record, dict):
        return {}
    return record


def write_fleet_mode(record: dict) -> bool:
    """Atomic create-or-overwrite of the fleet record via ``tempfile.mkstemp``
    + ``os.replace`` in ``settings_home()`` — same atomicity discipline as
    ``coordinator_core.session.grant.write_tier_u_grant`` — so a reader
    never observes a partially-written file.

    ``record`` must be a ``dict`` — anything else raises ``TypeError`` (a
    caller programming error, not an infra failure); this is the WRITE
    side's validation, distinct from the READ side's never-raise contract
    (see module docstring).

    Returns True on success; False on ANY infra failure (settings-home
    directory uncreatable, write/replace failure) — including a ``dict``
    ``record`` whose contents are not JSON-serializable (e.g. a
    ``datetime`` value): that is a per-value failure discovered only once
    serialization is attempted, not the caller-programming-error shape the
    up-front ``isinstance`` check exists to catch, so it degrades to
    ``False`` with the tmp file cleaned up rather than propagating.
    """
    if not isinstance(record, dict):
        raise TypeError(f"record must be a dict, got {type(record).__name__}")

    home = settings_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    target = home / _FLEET_MODE_FILENAME
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_FLEET_MODE_FILENAME}.", dir=str(home))
    except OSError:
        return False
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
    except OSError:
        # os.fdopen failing mid-construction does not guarantee it
        # consumed fd; close it ourselves so the raw descriptor is never
        # leaked (Review: code-reviewer, finding 4).
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return False
    try:
        with fh:
            json.dump(record, fh)
            fh.write("\n")
        os.replace(tmp_name, target)
    except (OSError, TypeError):
        # Widened to catch json.dump's TypeError on a dict record carrying
        # a non-JSON-serializable value -- passes the isinstance(dict) gate
        # above but still must not leak the tmp file (Review: code-reviewer,
        # finding 1).
        try:
            os.unlink(tmp_name)
        except OSError:
            # Best-effort tmp-file cleanup on the error path; the caller
            # already gets a False return regardless.
            pass
        return False
    return True
