"""
Tests for coordinator_core.ops.deliverable_ledger_write — the comment-preserving
splice writer for the close-out ledger block in state/deliverable-equivalence.yaml.

Pure tmp_path file fixtures only, no subprocesses, no `git init`-per-test fixture
(that pattern crashed live EM sessions on the shared tree — see
docs/plans/2026-08-13-archive-family-coverage-restoration.md).

Spec backlink: pln-archive-side-corpus-remediatio-3ff30d § C1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import deliverable_equivalence
from coordinator_core.ops import deliverable_ledger_write
from coordinator_core.ops.deliverable_ledger_write import (
    DeliverableLedgerWriteError,
    upsert_deliverable_ledger_rows,
)

_HEADER = """\
# Fork-equivalence artifact — synthetic fixture, mirrors the real file's shape.
#
# This block of header commentary is load-bearing and must survive every write
# byte-for-byte. It is never parsed by the splice writer.
entries:
  - loser: dlv-loser-a
    winner: dlv-winner-a
    evidence: "some evidence prose that must not be touched"

# The close-out ledger — row schema (all required unless noted):
#   deliverable_id: dlv-...
#   status: open|shipped|superseded|abandoned
#   closed_at: <ISO-8601 date/datetime>
#   adjudicator: <who/what asserted this verdict>
#   evidence_source: <where the assertion came from>
"""


def _write_artifact(tmp_path: Path, ledger_block: str) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_text(_HEADER + ledger_block, encoding="utf-8")
    return artifact_path


@pytest.fixture(autouse=True)
def _reset_ledger_memo():
    deliverable_equivalence._reset_deliverable_ledger_cache()
    yield
    deliverable_equivalence._reset_deliverable_ledger_cache()


def _open_row(deliverable_id: str, evidence_source: str = "state/handoffs/x.md") -> dict:
    return {
        "deliverable_id": deliverable_id,
        "status": "open",
        "closed_at": None,
        "superseded_by": None,
        "adjudicator": "test-suite",
        "evidence_source": evidence_source,
    }


def test_header_bytes_preserved_verbatim(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-new-1")])

    text = artifact_path.read_text(encoding="utf-8")
    ledger_idx = text.index("ledger:")
    assert text[:ledger_idx] == _HEADER


def test_upsert_replaces_existing_row_not_append(tmp_path):
    artifact_path = _write_artifact(
        tmp_path,
        "ledger:\n"
        "  - deliverable_id: dlv-existing\n"
        "    status: open\n"
        "    closed_at: null\n"
        "    superseded_by: null\n"
        "    adjudicator: seeder\n"
        "    evidence_source: state/handoffs/existing.md\n",
    )

    replacement = {
        "deliverable_id": "dlv-existing",
        "status": "shipped",
        "closed_at": "2026-08-13",
        "superseded_by": None,
        "adjudicator": "test-suite",
        "evidence_source": "state/handoffs/existing.md",
    }
    upsert_deliverable_ledger_rows(tmp_path, [replacement])

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    assert len(rows) == 1
    assert rows[0]["deliverable_id"] == "dlv-existing"
    assert rows[0]["status"] == "shipped"
    assert rows[0]["closed_at"] == "2026-08-13"


def test_upsert_preserves_rows_not_mentioned(tmp_path):
    _write_artifact(
        tmp_path,
        "ledger:\n"
        "  - deliverable_id: dlv-keep\n"
        "    status: open\n"
        "    closed_at: null\n"
        "    superseded_by: null\n"
        "    adjudicator: seeder\n"
        "    evidence_source: state/handoffs/keep.md\n",
    )

    upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-new")])

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    ids = {r["deliverable_id"] for r in rows}
    assert ids == {"dlv-keep", "dlv-new"}


def test_refusal_to_write_on_malformed_row_leaves_file_unchanged(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")
    before = artifact_path.read_text(encoding="utf-8")

    malformed = {
        "deliverable_id": "dlv-bad",
        "status": "not-a-real-status",
        "closed_at": None,
        "superseded_by": None,
        "adjudicator": "test-suite",
        "evidence_source": "state/handoffs/x.md",
    }
    with pytest.raises(deliverable_equivalence.DeliverableLedgerValidationError):
        upsert_deliverable_ledger_rows(tmp_path, [malformed])

    after = artifact_path.read_text(encoding="utf-8")
    assert after == before


def test_idempotent_rerun_byte_identical(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    rows = [_open_row("dlv-b"), _open_row("dlv-a")]
    upsert_deliverable_ledger_rows(tmp_path, rows)
    first = artifact_path.read_text(encoding="utf-8")

    upsert_deliverable_ledger_rows(tmp_path, rows)
    second = artifact_path.read_text(encoding="utf-8")

    assert first == second


# Review: coordinatorcode-reviewer f292d223 — F4: a splice must preserve any
# content that follows the ledger block rather than assuming it is last.
def test_upsert_preserves_content_after_ledger_block(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    footer = "\n# a future footer section, unrelated to the ledger\nfooter_key: footer_value\n"
    artifact_path.write_text(_HEADER + "ledger: []\n" + footer, encoding="utf-8")

    upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-new")])

    text = artifact_path.read_text(encoding="utf-8")
    assert text.endswith(footer)


def test_upsert_preserves_content_after_populated_ledger_block(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    footer = "\n# a future footer section, unrelated to the ledger\nfooter_key: footer_value\n"
    artifact_path.write_text(
        _HEADER
        + "ledger:\n"
        "  - deliverable_id: dlv-existing\n"
        "    status: open\n"
        "    closed_at: null\n"
        "    superseded_by: null\n"
        "    adjudicator: seeder\n"
        "    evidence_source: state/handoffs/existing.md\n"
        + footer,
        encoding="utf-8",
    )

    upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-new")])

    text = artifact_path.read_text(encoding="utf-8")
    assert text.endswith(footer)
    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    ids = {r["deliverable_id"] for r in rows}
    assert ids == {"dlv-existing", "dlv-new"}


# Review: coordinatorcode-reviewer f292d223 — F5: an optional key whose value is
# None must be omitted from the rendered row, matching the header's "absent
# when not required" schema comment.
def test_open_row_omits_null_closed_at_and_superseded_by_keys(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-null-omit")])

    text = artifact_path.read_text(encoding="utf-8")
    ledger_block = text[text.index("ledger:"):]
    assert "closed_at" not in ledger_block
    assert "superseded_by" not in ledger_block

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    deliverable_equivalence.validate_deliverable_ledger_rows(rows)
    by_id = {r["deliverable_id"]: r for r in rows}
    assert by_id["dlv-null-omit"].get("closed_at") is None
    assert by_id["dlv-null-omit"].get("superseded_by") is None


# Review: coordinatorcode-reviewer f292d223 — F8: a CRLF header, or a header
# whose last line lacks a trailing newline, must be refused rather than
# silently spliced into a malformed file.
def test_crlf_header_refused(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    artifact_path.write_bytes((_HEADER + "ledger: []\n").replace("\n", "\r\n").encode("utf-8"))

    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-crlf")])


def test_header_missing_trailing_newline_before_ledger_key_refused(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    # _HEADER ends with a trailing newline; strip it so the last header line
    # runs directly into "ledger: []" with no separating newline.
    artifact_path.write_text(_HEADER.rstrip("\n") + "ledger: []\n", encoding="utf-8")

    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-no-trailing-nl")])


# Review: coordinatorcode-reviewer f292d223 — F1: a present-but-malformed
# on-disk row must fail loud, never be silently excluded from the merge.
def test_malformed_existing_row_on_disk_raises_rather_than_silently_dropping(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    # A non-dict row lands in the parsed ledger list — not producible by this
    # writer, but reachable via out-of-band disk corruption.
    artifact_path.write_text(
        _HEADER + "ledger:\n  - just a bare string, not a mapping\n",
        encoding="utf-8",
    )

    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-new")])


def test_missing_artifact_raises(tmp_path):
    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-x")])


def test_long_multi_path_evidence_source_round_trips(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    long_evidence = "; ".join(
        f"state/handoffs/2026-08-{day:02d}_roadmap-sedge-{day:02d}-a-very-long-descriptive-slug-segment-{day:02d}.md"
        for day in range(1, 10)
    )
    assert len(long_evidence) > 200

    upsert_deliverable_ledger_rows(
        tmp_path, [_open_row("dlv-long-evidence", evidence_source=long_evidence)]
    )

    text = artifact_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith(("- deliverable_id:", "  deliverable_id:")):
            continue
        # Every rendered scalar line must be self-contained (no wrap continuation
        # breaking the fixed 4-space row indent).
        assert line.startswith(("  - ", "    ", "#", "entries:", "ledger:")) or not line.strip()

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    deliverable_equivalence.validate_deliverable_ledger_rows(rows)
    by_id = {r["deliverable_id"]: r for r in rows}
    assert by_id["dlv-long-evidence"]["evidence_source"] == long_evidence


def test_special_character_value_round_trips_byte_exact(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    tricky_value = (
        "\"dlv-narrow-the-write-confinement-bump-a-publish-af3aa9\"  "
        "# minted here; deliberately NOT coordinator-claude's dlv-...-276e8d — reusing it would "
        "join our chunk evidence onto their deliverable"
    )

    upsert_deliverable_ledger_rows(
        tmp_path, [_open_row("dlv-tricky", evidence_source=tricky_value)]
    )

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    by_id = {r["deliverable_id"]: r for r in rows}
    assert by_id["dlv-tricky"]["evidence_source"] == tricky_value

    # Re-parse the raw file directly too, independent of the loader, to pin the
    # on-disk bytes themselves as valid single-line YAML.
    import yaml

    parsed = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    ledger_row = next(
        r for r in parsed["ledger"] if r["deliverable_id"] == "dlv-tricky"
    )
    assert ledger_row["evidence_source"] == tricky_value


def test_large_row_count_reparses_intact(tmp_path):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    rows = [
        _open_row(
            f"dlv-bulk-{i:04d}",
            evidence_source="; ".join(
                f"state/handoffs/bulk-{i:04d}-leg-{j}.md" for j in range(3)
            ),
        )
        for i in range(400)
    ]
    upsert_deliverable_ledger_rows(tmp_path, rows)

    loaded = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    deliverable_equivalence.validate_deliverable_ledger_rows(loaded)
    assert len(loaded) >= 400
    assert {r["deliverable_id"] for r in loaded} == {r["deliverable_id"] for r in rows}


# Review: coordinatorcode-reviewer f292d223 — F3: adversarial _render_scalar
# inputs, pinned as byte-exact round-trips through write -> load_deliverable_ledger.
@pytest.mark.parametrize(
    "tricky_value",
    [
        "-leading-dash-value",
        "#leading-hash-value",
        "*leading-star-value",
        "&leading-amp-value",
        "!leading-bang-value",
        "%leading-percent-value",
        "123",
        "true",
        "false",
        "null",
        "~",
        "  leading-and-trailing-whitespace  ",
        "an embedded: colon-space value",
        # Review: coordinatorcode-reviewer c8602a8b — F3: adversarial coverage
        # widened per the reviewer's enumerated hazard list — leading indicator
        # chars, leading/trailing quote chars, YAML 1.1 bool-resolver forms
        # distinct from true/false/null, and non-ASCII/unicode.
        "?leading-question-value",
        "@leading-at-value",
        "`leading-backtick-value",
        '"leading-quote-value',
        'trailing-quote-value"',
        "'leading-single-quote-value",
        "trailing-single-quote-value'",
        "yes",
        "no",
        "on",
        "off",
        "y",
        "n",
        "unicode-\U0001f600-emoji-value",
        "combining-é-acute-value",
    ],
)
def test_render_scalar_adversarial_inputs_round_trip_byte_exact(tmp_path, tricky_value):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    upsert_deliverable_ledger_rows(
        tmp_path, [_open_row("dlv-adversarial", evidence_source=tricky_value)]
    )

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    by_id = {r["deliverable_id"]: r for r in rows}
    assert by_id["dlv-adversarial"]["evidence_source"] == tricky_value

    import yaml

    parsed = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    ledger_row = next(
        r for r in parsed["ledger"] if r["deliverable_id"] == "dlv-adversarial"
    )
    assert ledger_row["evidence_source"] == tricky_value


# Review: coordinatorcode-reviewer c8602a8b — F2: embedded-newline values were
# unexercised by any test despite being a named splice/round-trip hazard —
# evidence_source/closure_evidence are free-text fields sourced from handoff
# prose, where a literal newline is entirely plausible.
@pytest.mark.parametrize(
    "tricky_value",
    [
        "line one\nline two",
        "line one\r\nline two",
        "value\twith\ttabs",
        "trailing newline\n",
        "\nleading newline",
    ],
)
def test_render_scalar_embedded_newline_values_round_trip_byte_exact(tmp_path, tricky_value):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    upsert_deliverable_ledger_rows(
        tmp_path, [_open_row("dlv-embedded-newline", evidence_source=tricky_value)]
    )

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    by_id = {r["deliverable_id"]: r for r in rows}
    assert by_id["dlv-embedded-newline"]["evidence_source"] == tricky_value

    import yaml

    text = artifact_path.read_text(encoding="utf-8")
    # The rendered row must still occupy exactly one physical line in the
    # splice — an embedded newline that survived unescaped into the flow
    # scalar would break the fixed 4-space block-list indent this splice
    # depends on.
    ledger_block = text[text.index("ledger:"):]
    row_lines = [
        line
        for line in ledger_block.splitlines()
        if line.strip().startswith(("- deliverable_id:", "deliverable_id:"))
    ]
    assert len(row_lines) == 1

    parsed = yaml.safe_load(text)
    ledger_row = next(
        r for r in parsed["ledger"] if r["deliverable_id"] == "dlv-embedded-newline"
    )
    assert ledger_row["evidence_source"] == tricky_value


# Review: coordinatorcode-reviewer c8602a8b — F4: _find_ledger_key_line must
# refuse rather than silently take the first of more than one column-0
# 'ledger:' match — a wrong splice on this artifact is data loss.
def test_duplicate_column_zero_ledger_key_refused(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = state_dir / "deliverable-equivalence.yaml"
    # A mis-indented continuation line lands a second literal "ledger:" at
    # column 0, ahead of the real key.
    artifact_path.write_text(
        _HEADER + "ledger: this-is-not-the-real-key\nledger: []\n",
        encoding="utf-8",
    )

    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-dup-key")])

    # Refusal must leave the file untouched.
    text = artifact_path.read_text(encoding="utf-8")
    assert text == _HEADER + "ledger: this-is-not-the-real-key\nledger: []\n"


# Review: coordinatorcode-reviewer c8602a8b — F1: no lock guarded the
# read-merge-write critical section, so two overlapping invocations could
# each read the same existing_rows and one would silently discard the
# other's row on write. This forces a real interleaving (thread B is only
# allowed to start once thread A is mid-critical-section) and asserts both
# rows survive — the bug this proves against would show up as one of the two
# ids missing from the final ledger.
def test_concurrent_upserts_serialize_and_both_rows_survive(tmp_path, monkeypatch):
    import threading
    import time

    artifact_path = _write_artifact(tmp_path, "ledger: []\n")

    original_render = deliverable_ledger_write._render_ledger_block
    call_count = {"n": 0}
    lock = threading.Lock()

    def _slow_render(rows):
        with lock:
            call_count["n"] += 1
            first = call_count["n"] == 1
        if first:
            # First caller to reach rendering (i.e. holding the lock) pauses
            # here so the second thread's attempt to acquire the same lock
            # is guaranteed to be outstanding while this one is still mid
            # critical-section — proving the second call blocks rather than
            # reading a stale existing_rows snapshot concurrently.
            time.sleep(0.3)
        return original_render(rows)

    monkeypatch.setattr(deliverable_ledger_write, "_render_ledger_block", _slow_render)

    results = {}

    def _run(name, row):
        try:
            upsert_deliverable_ledger_rows(tmp_path, [row])
            results[name] = "ok"
        except Exception as exc:  # pragma: no cover - failure path asserted below
            results[name] = exc

    t_a = threading.Thread(target=_run, args=("a", _open_row("dlv-thread-a")))
    t_b = threading.Thread(target=_run, args=("b", _open_row("dlv-thread-b")))
    t_a.start()
    time.sleep(0.05)  # let thread A acquire the lock and enter rendering first
    t_b.start()
    t_a.join()
    t_b.join()

    assert results["a"] == "ok", results["a"]
    assert results["b"] == "ok", results["b"]

    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    ids = {r["deliverable_id"] for r in rows}
    assert ids == {"dlv-thread-a", "dlv-thread-b"}


# Review: coordinatorcode-reviewer c8602a8b — F5: main()'s happy path used to
# compute artifact_path.is_file() and then discard it, always printing help
# text. Now the existence + splice-point check is load-bearing in the success
# message, and a real refusal (ambiguous splice target) surfaces as an error.
def test_main_reports_ok_when_artifact_and_ledger_key_present(tmp_path):
    import io

    _write_artifact(tmp_path, "ledger: []\n")
    out = io.StringIO()
    err = io.StringIO()

    rc = deliverable_ledger_write.main(["--root", str(tmp_path)], out=out, err=err)

    assert rc == 0
    assert "ok:" in out.getvalue()
    assert err.getvalue() == ""


def test_main_reports_error_when_artifact_missing(tmp_path):
    import io

    out = io.StringIO()
    err = io.StringIO()

    rc = deliverable_ledger_write.main(["--root", str(tmp_path)], out=out, err=err)

    assert rc == 1
    assert "no artifact at" in err.getvalue()


def test_corrupt_render_readback_guard_restores_and_raises(tmp_path, monkeypatch):
    artifact_path = _write_artifact(tmp_path, "ledger: []\n")
    before = artifact_path.read_text(encoding="utf-8")

    def _corrupt_render(rows):
        return ["ledger:\n", "  - this is not: [valid, yaml, - broken\n"]

    monkeypatch.setattr(
        deliverable_ledger_write, "_render_ledger_block", _corrupt_render
    )

    with pytest.raises(DeliverableLedgerWriteError):
        upsert_deliverable_ledger_rows(tmp_path, [_open_row("dlv-corrupt")])

    after = artifact_path.read_text(encoding="utf-8")
    assert after == before

    # The loader must still see the pre-corruption (empty) ledger, not silently
    # treat the corrupt attempt as having emptied it further or left partial bytes.
    rows = deliverable_equivalence.load_deliverable_ledger(tmp_path)
    assert rows == []
