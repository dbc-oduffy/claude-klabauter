"""
coordinator_core.ops.tests.test_coordinator_complete_entry_backfill

The backfill mode — `--for-date`, `--commits`, `--authored-by-unknown`.

WHY IT EXISTS, because the shape only makes sense against the gap it closes.
`resolve_entry_path` derived its filename from `date.today()`, and this
module's own Negative-spec reserved `commits:` to the reconcile step, which
resolves its session-id from the CURRENT live session. Composed, there was no
supported path to author a completion entry for work a DEAD session shipped —
so `/workday-complete` Step 9 ("add missing entries") instructed an action its
own toolchain could not perform. Example-cockpit-repo-em covered 231 orphaned
commits across two days by hand-writing schema-valid frontmatter instead
(`cross-repo/archive/2026-07-30-example-cockpit-repo-em-completion-entry-backfill-
mode-and-obligation-gap.md` § 2), and named the real cost: every consumer repo
hand-rolling frontmatter is how schema drift starts.

These tests pin the GATE as hard as the capability. `commits:` and
`authored_by` are fields two guards depend on (`_refuse_if_live_foreign_entry_
holder` keys on the latter), so a live close must not be able to hand-write
either — `--for-date` is what declares the reconstruction case, and without it
the seeding flags are refused.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import (
    parse_frontmatter,
    validate_frontmatter_obj,
)
from coordinator_core.ops import coordinator_complete_entry as cce

_LOE = "loe:\n  agent_dispatches: null\n  opus_dispatches: null\n  em_tokens: null\n  tshirt: null"


def _fm(entry_path: Path) -> dict:
    return parse_frontmatter(entry_path.read_text(encoding="utf-8")).get("frontmatter") or {}


# ---------------------------------------------------------------------------
# --for-date moves the derived path, and moves ALL of it
# ---------------------------------------------------------------------------


def test_for_date_sets_the_month_directory_and_the_filename_segment(tmp_path: Path) -> None:
    """One date drives the directory AND the filename. Pinned together because
    the failure that matters is a 2026-07-28 entry landing under 2026-08/,
    which reads as correct in a listing and is wrong in the archive."""
    got = cce.resolve_entry_path(str(tmp_path), "abc123456", "my-chain", date(2026, 7, 28))
    parts = Path(got).parts
    assert parts[-2] == "2026-07"
    assert parts[-1] == "2026-07-28-my-chain-123456.md"


def test_omitting_for_date_still_derives_today(tmp_path: Path) -> None:
    """The default is unchanged for every ordinary close — the parameter is
    additive, not a new required argument."""
    today = date.today()
    got = Path(cce.resolve_entry_path(str(tmp_path), "abc123456", "my-chain"))
    assert got.parts[-2] == today.strftime("%Y-%m")
    assert got.name.startswith(today.strftime("%Y-%m-%d"))


def test_effective_path_threads_the_date_through_to_the_derivation(tmp_path: Path) -> None:
    """`resolve_effective_entry_path` is the single resolution `main()` and the
    scaffold-residue gate both use; a date it dropped on the floor would put
    those two readers on different files."""
    entry_path, marker = cce.resolve_effective_entry_path(
        str(tmp_path), "abc123456", "my-chain", date(2026, 7, 28)
    )
    assert marker is None
    assert Path(entry_path).name == "2026-07-28-my-chain-123456.md"


# ---------------------------------------------------------------------------
# the seeded fields
# ---------------------------------------------------------------------------


def test_seeded_commits_are_written_as_a_yaml_list(tmp_path: Path) -> None:
    entry_path = tmp_path / "entry.md"
    wrote = cce._write_entry(
        str(entry_path), "session-abc123", "bugfix", "chain", False, _LOE, "",
        "2026-07-28", "dlv-x", ["deadbee", "cafef00d"],
    )
    assert wrote is True
    assert _fm(entry_path)["commits"] == ["deadbee", "cafef00d"]


def test_an_ordinary_close_still_writes_an_empty_commits_list(tmp_path: Path) -> None:
    """The Negative-spec holds everywhere the backfill flags are absent:
    `commits:` stays `[]` and belongs to the reconcile step."""
    entry_path = tmp_path / "entry.md"
    cce._write_entry(
        str(entry_path), "session-abc123", "bugfix", "chain", False, _LOE, "",
        "2026-08-31", "dlv-x",
    )
    assert _fm(entry_path)["commits"] == []


def test_authored_by_unknown_omits_the_field_and_stays_schema_valid(tmp_path: Path) -> None:
    """The requester asked for `authored_by: null` (their D9). They get
    omission instead, because the schema types the field `string` and does not
    require it — `null` fails validation outright, so shipping their literal
    ask would have written an INVALID record, which is strictly worse than the
    hand-authored entries this mode exists to replace.

    Their REASON is served exactly: never fabricate a session UUID, because it
    pollutes the coverage sweep's known-session set and makes a reconstructed
    entry indistinguishable from a real session's. Absent does that; so would
    null, if null were legal."""
    entry_path = tmp_path / "entry.md"
    cce._write_entry(
        str(entry_path), "session-abc123", "bugfix", "chain", False, _LOE, "",
        "2026-07-28", "dlv-x", ["deadbee"], True,
    )
    fm = _fm(entry_path)
    assert "authored_by" not in fm

    schema = json.loads(
        (
            Path(cce.__file__).resolve().parents[1]
            / "frontmatter"
            / "schemas"
            / "completion-entry.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert validate_frontmatter_obj(fm, schema)["ok"], validate_frontmatter_obj(fm, schema)


def test_an_ordinary_entry_still_stamps_the_sid(tmp_path: Path) -> None:
    """The negative half — omission is opt-in, and a live close still records
    who authored it, which is what `_refuse_if_live_foreign_entry_holder`
    needs to refuse a stand-down onto a peer's entry."""
    entry_path = tmp_path / "entry.md"
    cce._write_entry(
        str(entry_path), "session-abc123", "bugfix", "chain", False, _LOE, "",
        "2026-08-31", "dlv-x",
    )
    assert _fm(entry_path)["authored_by"] == "session-abc123"


def test_an_unknown_author_does_not_read_as_a_foreign_live_holder(tmp_path: Path) -> None:
    """The one interaction worth pinning: `_refuse_if_live_foreign_entry_
    holder` keys on `authored_by`, and a reconstructed entry must not be
    mistaken for one a live peer owns — otherwise backfilling a day would
    poison every later stand-down onto those entries."""
    entry_path = tmp_path / "entry.md"
    cce._write_entry(
        str(entry_path), "session-abc123", "bugfix", "chain", False, _LOE, "",
        "2026-07-28", "dlv-x", ["deadbee"], True,
    )
    assert cce._refuse_if_live_foreign_entry_holder(
        str(entry_path), str(tmp_path), "some-other-live-sid"
    ) is None


# ---------------------------------------------------------------------------
# parsing: the gate, and the sha vocabulary
# ---------------------------------------------------------------------------


def _parse(*argv: str):
    return cce._parse_args(["--sid", "abc123456", "--disposition", "single-session", *argv])


def test_commits_without_for_date_is_refused() -> None:
    """The gate, not tidiness: `commits:` is owned by the reconcile step, so a
    LIVE close must not be able to hand-write it."""
    parsed, rc = _parse("--commits", "deadbee")
    assert parsed is None and rc == 1


def test_authored_by_unknown_without_for_date_is_refused() -> None:
    parsed, rc = _parse("--authored-by-unknown")
    assert parsed is None and rc == 1


def test_for_date_alone_is_legal() -> None:
    """Re-dating without re-seeding is a real case — an entry whose commits the
    reconcile step will still fold — so the date flag does not require the
    others."""
    parsed, rc = _parse("--for-date", "2026-07-28")
    assert rc is None and parsed is not None
    assert parsed["for_date"] == date(2026, 7, 28)
    assert parsed["commits"] == []
    assert parsed["authored_by_unknown"] is False


def test_a_future_for_date_is_refused() -> None:
    """Backfill means BACK. A future date would file work under a month that
    has not happened, where no sweep looks for it."""
    ahead = (date.today() + timedelta(days=1)).isoformat()
    parsed, rc = _parse("--for-date", ahead)
    assert parsed is None and rc == 1


@pytest.mark.parametrize("bad", ["28-07-2026", "2026-7-28x", "yesterday", "2026-13-01"])
def test_malformed_for_date_fails_loud(bad: str) -> None:
    parsed, rc = _parse("--for-date", bad)
    assert parsed is None and rc == 1


def test_commits_are_deduplicated_but_keep_their_given_order() -> None:
    """Order carries no meaning to the coverage sweep, which reads a flat
    membership set — it carries meaning to a human diffing a reconstructed
    entry against `git log`, and sorting would destroy that for free."""
    parsed, rc = _parse("--for-date", "2026-07-28", "--commits", "cafef00d,deadbee,cafef00d")
    assert rc is None and parsed is not None
    assert parsed["commits"] == ["cafef00d", "deadbee"]


@pytest.mark.parametrize("bad", ["deadb", "nothex!", "z" * 8, "d" * 41])
def test_a_non_sha_token_fails_loud(bad: str) -> None:
    """7-40 hex: shorter is ambiguous in any real repo, longer is not a sha."""
    parsed, rc = _parse("--for-date", "2026-07-28", "--commits", bad)
    assert parsed is None and rc == 1


# `--claim-shas-from` (a whitespace-separated-file second source for the same
# `commits` field) and its three tests (`test_claim_shas_from_reads_
# whitespace_separated_shas`, `test_claim_shas_from_a_missing_file_fails_
# loud`, `test_the_two_sha_sources_are_mutually_exclusive`) were removed here
# (review: overengineering-reviewer, finding #3): the flag duplicated
# `--commits` — same field, same validation, same de-duplication — for an
# exclusivity arm and a distinct error surface nothing named a real caller
# for. `--commits` remains the sole sha source; the de-duplication/ordering
# coverage above (`test_commits_are_deduplicated_but_keep_their_given_
# order`) and the sha-shape coverage (`test_a_non_sha_token_fails_loud`)
# still apply to it unchanged.


def test_a_repeated_commits_flag_is_refused_not_merged() -> None:
    """Review: coordinatorcode-reviewer.a2ea175d92501b498 -- the
    `--claim-shas-from` removal also dropped the only guard against a second
    `--commits` occurrence. A second `--commits` must be refused, not
    silently merged (which would fail to de-dup across invocations and
    contradict the flag's own "de-duplicated" usage-text promise)."""
    parsed, rc = _parse(
        "--for-date", "2026-07-28",
        "--commits", "deadbee",
        "--commits", "cafef00d",
    )
    assert parsed is None and rc == 1
