"""Account for every parsed bug-blitz target: a row, a declined row, or a named drop. Pure; no I/O, no spawn."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from coordinator_core.bug_blitz.contract import FailingTest

NOT_ADMITTED_REASON = "not admitted by the run appetite"
NOT_SELECTED_REASON = "not selected by the queue manifest"
SELECTED_REASON = "selected by the queue manifest"


@dataclass(frozen=True)
class TargetAccount:
    """The fate of one parsed target node. `row_id` is None only when the mint returned no usable id."""

    nodeid: str
    test_file: str
    row_id: str | None
    disposition: str  # 'row' | 'declined' | 'dropped'
    reason: str

    def to_json(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "test_file": self.test_file,
            "row_id": self.row_id,
            "disposition": self.disposition,
            "reason": self.reason,
        }


def _nodeid(test: FailingTest) -> str:
    return f"{test.file}::{test.name}"


def _declined_reasons(declined: list[Mapping]) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in declined:
        row_id = entry.get("row_id", entry.get("id"))
        if row_id is not None:
            out[str(row_id)] = str(entry.get("reason", ""))
    return out


def account_targets(
    tests: tuple[FailingTest, ...],
    minted_ids: list[str],
    entries: list[str],
    declined: list[Mapping],
    not_admitted: list[str],
) -> list[TargetAccount]:
    """One account per test, in `tests` order. Never raises.

    `minted_ids` pair with the distinct test files in first-seen order (mint_tf_rows' contract);
    a count mismatch drops every node.
    """
    files: list[str] = []
    for test in tests:
        if test.file not in files:
            files.append(test.file)
    if len(minted_ids) != len(files):
        reason = f"mint returned {len(minted_ids)} ids for {len(files)} files"
        return [TargetAccount(_nodeid(t), t.file, None, "dropped", reason) for t in tests]
    row_by_file = dict(zip(files, minted_ids))
    declined_by_id = _declined_reasons(declined)
    entry_set = set(entries)
    refused = set(not_admitted)
    accounts: list[TargetAccount] = []
    for test in tests:
        row_id = row_by_file[test.file]
        if row_id in refused:
            account = TargetAccount(_nodeid(test), test.file, row_id, "dropped", NOT_ADMITTED_REASON)
        elif row_id in declined_by_id:
            account = TargetAccount(_nodeid(test), test.file, row_id, "declined", declined_by_id[row_id])
        elif row_id in entry_set:
            account = TargetAccount(_nodeid(test), test.file, row_id, "row", SELECTED_REASON)
        else:
            account = TargetAccount(_nodeid(test), test.file, row_id, "dropped", NOT_SELECTED_REASON)
        accounts.append(account)
    return accounts
