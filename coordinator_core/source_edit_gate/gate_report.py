"""coordinator_core.source_edit_gate.gate_report -- structured test-report
parsing shared by `gate.py`'s diff logic and `runner.py`'s spawns.

`None` (never `{}`) means "no usable report" -- the run crashed before writing
one, or wrote something unparseable. `{}` is reserved for a report that
parsed cleanly and legitimately contains zero testcases; conflating the two
would let a crashed run be diffed as "nothing collected, nothing changed,"
the wrong fail-safe direction for a gate whose whole job is catching
regressions.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

__all__ = ["parse_junitxml", "parse_vitest_json", "TestReport"]

#: A structured test run's outcome, keyed by test-framework node id.
#: `report[node_id]` is one of `"passed"`, `"failed"`, or `"skipped"`. A node
#: id absent from `report` was not observed in this run at all.
TestReport = dict


def parse_junitxml(xml_path: Path) -> TestReport | None:
    if not xml_path.is_file():
        return None
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return None
    statuses: dict = {}
    for testcase in tree.getroot().iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        node_id = f"{classname}::{name}" if classname else name
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            status = "failed"
        elif testcase.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        statuses[node_id] = status
    return statuses


_VITEST_STATUS_MAP = {
    "passed": "passed",
    "failed": "failed",
    "skipped": "skipped",
    "pending": "skipped",
    "todo": "skipped",
}


def parse_vitest_json(raw: str) -> TestReport | None:
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    statuses: dict = {}
    for file_result in payload.get("testResults", []):
        file_name = file_result.get("name", "")
        for assertion in file_result.get("assertionResults", []):
            full_name = assertion.get("fullName") or assertion.get("title", "")
            node_id = f"{file_name}::{full_name}" if file_name else full_name
            status = _VITEST_STATUS_MAP.get(assertion.get("status", ""), "failed")
            statuses[node_id] = status
    return statuses
