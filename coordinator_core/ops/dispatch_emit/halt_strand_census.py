"""Read-only census of halted, abandoned and parked runs: cause and green-code per run, rolled up by cause.

`classify(run_dir)` -> `{cause, green, evidence}`; `census(warp_root)` -> rows `{cause, runs, green}` plus a
population count that equals the sum of `runs`; `main()` prints that JSON. The cause is one of the R1 strand
causes (`STRAND_CAUSES`), else the run's own halt kind from `halt_remedy_class.HALT_REMEDY` (or the raw kind
string when unregistered), else `other`. Population: run dirs holding `halted.json`, union run dirs whose
`run-stage.json` stage or status is abandoned or parked. Pure file reads: no git, no subprocess.

The Mini's warp root comes from the settings-home JSON `machine-local/census-roots.json` under
`MINI_WARP_ROOT_KEY`; an absent key or file yields one typed `unreadable` row naming the key.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit.halt_remedy_class import HALT_REMEDY
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE

RUN_STAGE_FILE = "run-stage.json"
RUN_OUTCOME_FILE = "run-outcome.json"
OVERLAY_VERDICT_FILE = "overlay-verdict.json"
TEST_LEG_FILE = "test-leg.json"
WITHHELD_PRINTS_FILE = "withheld-prints.json"

MINI_WARP_ROOT_KEY = "mini_warp_root"
CENSUS_ROOTS_FILE = "machine-local/census-roots.json"

CAUSE_OTHER = "other"
CAUSE_RESOLVED = "resolved"
CAUSE_UNREADABLE = "unreadable"
ABANDONED_STAGES = frozenset({"abandoned", "parked"})

#: Ceiling on file opens per run dir in `census`: halted, stage, then overlay, test-leg, run-outcome.
MAX_OPENS_PER_RUN_DIR = 5

#: R1 strand causes in match order: (cause, pattern over the halt kind + reason + next_action text).
STRAND_CAUSES: tuple = (
    ("base-moved", re.compile(r"base[\s_-]*moved|moved[\s_-]*base|run base .*moved", re.I)),
    ("empty-verify-leg", re.compile(r"test[_ -]?verdict[_ -]?empty|empty verify|no review", re.I)),
    ("test-exit-nonzero-zero-failed", re.compile(r"(exit(?: code)?\s*[:=]?\s*[1-9]).*\b0 failed|\b0 failed.*exit(?: code)?\s*[:=]?\s*[1-9]|temp leak", re.I | re.S)),
    ("land-deadline-byte-gate", re.compile(r"land[_ -]?deadline|land_byte_gate|byte[_ -]gate", re.I)),
    ("verdict-pin-stale-cas", re.compile(r"verdict[_ -]?pin|\bCAS\b.*refus|refus.*\bCAS\b", re.I | re.S)),
    ("withheld-path-holder", re.compile(r"withheld[- _]path|held by (?:halted )?run|withheld.*holder|holder.*withheld", re.I | re.S)),
    ("orphan-tree-dirt", re.compile(r"orphan.{0,40}(?:dirt|tree)|tree dirt|orphaned(?:_staged)?\b", re.I | re.S)),
    ("close-op-timeout", re.compile(r"record_halted_run_landed.{0,80}time", re.I | re.S)),
    ("needs-hand-land", re.compile(r"hand[- ]?land|landed? by hand|land .{0,20}by hand", re.I)),
)

#: R1 causes the engine already routes (Test-blocker route, stale_release) rather than strands.
HANDLED_CAUSES = frozenset({"empty-verify-leg", "test-exit-nonzero-zero-failed", "withheld-path-holder"})

#: Causes never counted green-landable: the base moved, the work needs a re-plan.
NEVER_GREEN_CAUSES = frozenset({"base-moved"})


def _read_bytes(path: Any) -> Optional[bytes]:
    """The single file-open choke point: bytes, or None when the file is absent or unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _decode(raw: Optional[bytes]) -> dict:
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _obj(path: Any) -> dict:
    return _decode(_read_bytes(path))


def _text(marker: dict) -> str:
    parts: list = []
    for key in ("halted", "reason"):
        value = marker.get(key)
        parts.append(value if isinstance(value, str) else "")
    na = marker.get("next_action")
    detail = na.get("detail") if isinstance(na, dict) else na  # em_rule is boilerplate on every marker
    parts.append(detail if isinstance(detail, str) else "")
    return "\n".join(str(p) for p in parts)


def _stage(run_dir: Any) -> str:
    stage = _obj(os.path.join(run_dir, RUN_STAGE_FILE))
    for key in ("stage", "status"):
        value = stage.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _green(run_dir: Any) -> tuple:
    """`(green, signals)`: green needs one green signal and no red one, from the run's own records."""
    green: list = []
    red: list = []
    overlay = _obj(os.path.join(run_dir, OVERLAY_VERDICT_FILE)).get("verdict")
    if overlay == "green":
        green.append("overlay-verdict:green")
    elif overlay == "red":
        red.append("overlay-verdict:red")
    leg = _obj(os.path.join(run_dir, TEST_LEG_FILE))
    if leg:
        if leg.get("status") == "pass" and leg.get("tests_failed") == 0 and (leg.get("tests_run") or 0) > 0:
            green.append("test-leg:pass-0-failed")
        elif leg.get("status") == "fail" or (leg.get("tests_failed") or 0) > 0:
            red.append("test-leg:fail")
    outcome = _obj(os.path.join(run_dir, RUN_OUTCOME_FILE))
    ledger = outcome.get("ledger_verdict")
    items = ledger.get("items") if isinstance(ledger, dict) else None
    if isinstance(items, list) and items:
        statuses = {i.get("status") for i in items if isinstance(i, dict)}
        if statuses == {"met"}:
            green.append("ledger:met")
        elif statuses & {"unmet", "not_met", "red"}:
            red.append("ledger:unmet")
    return bool(green) and not red, green + red


def _cause(marker: dict, stage: str) -> tuple:
    kind = marker.get("halted") if isinstance(marker.get("halted"), str) else ""
    text = _text(marker)
    for cause, pattern in STRAND_CAUSES:
        if pattern.search(text):
            return cause, kind
    if kind:
        return kind, kind
    if stage in ABANDONED_STAGES:
        return stage, kind
    return CAUSE_OTHER, kind


def _scan(run_dir: Any) -> Optional[tuple]:
    """`(cause, kind, status, stage, green, signals)` for a member dir, None for a non-member.

    Opens `halted.json` once, `run-stage.json` only when membership or the cause needs it, and the
    three green-signal files only for a run that is neither resolved nor never-green.
    """
    raw = _read_bytes(os.path.join(run_dir, HALTED_FILE))
    marker = _decode(raw)
    stage = None
    if raw is None:
        stage = _stage(run_dir)
        if stage not in ABANDONED_STAGES:
            return None
    status = marker.get("status") if isinstance(marker.get("status"), str) else ""
    if marker and status and status != "halted":
        return CAUSE_RESOLVED, marker.get("halted") or "", status, stage or "", False, []
    kind = marker.get("halted") if isinstance(marker.get("halted"), str) else ""
    cause = None
    text = _text(marker)
    for name, pattern in STRAND_CAUSES:
        if pattern.search(text):
            cause = name
            break
    if cause is None and not kind and stage is None:
        stage = _stage(run_dir)
    if cause is None:
        cause = kind or (stage if stage in ABANDONED_STAGES else CAUSE_OTHER)
    if cause in NEVER_GREEN_CAUSES:
        return cause, kind, status, stage or "", False, []
    green, signals = _green(run_dir)
    return cause, kind, status, stage or "", green, signals


def classify(run_dir: Any) -> dict:
    """`{cause, green, evidence}` for one run dir; never raises on unreadable records."""
    run_dir = Path(run_dir)
    marker = _obj(run_dir / HALTED_FILE)
    stage = _stage(run_dir)
    status = marker.get("status") if isinstance(marker.get("status"), str) else ""
    green, signals = _green(run_dir)
    if marker and status and status != "halted":
        cause, kind = CAUSE_RESOLVED, marker.get("halted") or ""
    else:
        cause, kind = _cause(marker, stage)
    if cause in NEVER_GREEN_CAUSES:
        green = False
    withheld = _obj(run_dir / WITHHELD_PRINTS_FILE)
    return {
        "cause": cause,
        "green": green,
        "evidence": {
            "halt_kind": kind,
            "status": status,
            "stage": stage,
            "remedy_class": HALT_REMEDY.get(kind, (None, None))[0] if kind else None,
            "registered": kind in HALT_REMEDY if kind else False,
            "handled": cause in HANDLED_CAUSES,
            "signals": signals,
            "withheld_paths": len(withheld),
            "reason": (marker.get("reason") or "")[:200] if isinstance(marker.get("reason"), str) else "",
        },
    }


def is_member(run_dir: Path) -> bool:
    """True when the dir holds `halted.json` or its run stage is abandoned or parked."""
    return (run_dir / HALTED_FILE).is_file() or _stage(run_dir) in ABANDONED_STAGES


def census(warp_root: Any) -> dict:
    """`{population, rows: [{cause, runs, green}], handled, unreadable}` over every member dir of `warp_root`."""
    root = Path(warp_root)
    rows: dict = {}
    population = 0
    try:
        with os.scandir(root) as it:
            entries = [e for e in it if e.is_dir()]
    except OSError:
        return {"population": 0, "rows": [], "unreadable": str(root)}
    for entry in entries:
        scanned = _scan(entry.path)
        if scanned is None:
            continue
        cause, green = scanned[0], scanned[4]
        row = rows.setdefault(cause, {"cause": cause, "runs": 0, "green": 0})
        row["runs"] += 1
        row["green"] += 1 if green else 0
        population += 1
    ordered = sorted(rows.values(), key=lambda r: (-r["green"], -r["runs"], r["cause"]))
    return {"population": population, "rows": ordered, "handled": sorted(HANDLED_CAUSES)}


def settings_home() -> Path:
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    return Path(override) if override else Path.home() / ".coordinator-claude-settings"


def mini_warp_root(home: Optional[Path] = None) -> Optional[str]:
    """The Mini's warp root from the settings-home key `MINI_WARP_ROOT_KEY`; None when absent."""
    data = _obj((home or settings_home()) / CENSUS_ROOTS_FILE)
    value = data.get(MINI_WARP_ROOT_KEY)
    return value if isinstance(value, str) and value else None


def mini_census(home: Optional[Path] = None) -> dict:
    """The Mini census, or one typed `unreadable` row naming the missing key or root."""
    root = mini_warp_root(home)
    if root is None:
        return {"population": 0, "rows": [{"cause": CAUSE_UNREADABLE, "runs": 0, "green": 0, "key": MINI_WARP_ROOT_KEY}],
                "unreadable": MINI_WARP_ROOT_KEY}
    result = census(root)
    if "unreadable" in result:
        result["rows"] = [{"cause": CAUSE_UNREADABLE, "runs": 0, "green": 0, "key": MINI_WARP_ROOT_KEY, "root": root}]
    return result


def main(argv: Optional[list] = None) -> int:
    """Print the census JSON for `state/warp` under the repo root (or argv[0]) plus the Mini."""
    from coordinator_core.session import record_homes

    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(record_homes.home_dir(str(Path(__file__).resolve().parents[3]), "warp"))
    out = {"box": census(root), "mini": mini_census()}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
