"""Pin and anti-drift tests for `coordinator_core.ops.emit_withheld_knobs`.

The artifact this op emits is consumed by a SIBLING REPO (DoE-claude), by a test that
fails their tier. Two consequences shape what is asserted here:

  - A shape change is a break in someone else's suite, so the emitted key set is pinned
    rather than left to drift with the implementation.
  - A knob name that goes stale here is worse than a missing file, because a stale entry
    reads as live. So the name-provenance assertions below are the point of the suite:
    they prove the emitted names come from the constants that own them and were not
    retyped.

Deliberately NOT asserted here: anything about DoE's consuming guard, or about whether
any doctrine surface currently prescribes a withheld knob. Those surfaces are theirs.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest

from coordinator_core.locked_write import CONTENDED_LOCK_WAIT_ENV
from coordinator_core.ops import emit_withheld_knobs as op

# `percolate` is not importable until the op's path rung runs. Do it once here rather than
# leaning on some earlier test having called a function that happens to do it — that made
# this file pass or fail on execution order.
op.ensure_percolate_on_path()

_OP_SOURCE = Path(op.__file__).resolve()

#: Every key a consumer may rely on. Adding one is additive and needs no SCHEMA_VERSION
#: bump; removing or repurposing one does, which is what this pin exists to force.
_REQUIRED_ENTRY_KEYS = frozenset(
    {
        "knob",
        "pattern",
        "path_id",
        "path",
        "context_markers",
        "status",
        "why",
        "asserted_by",
        "remedy_instead",
        "authority",
        "withheld_since",
        "mechanism_page",
        "register_rule",
    }
)

_REQUIRED_TOP_KEYS = frozenset(
    {"schema_version", "generated_by", "generator_note", "reference", "means", "entries"}
)


def _document() -> Dict[str, Any]:
    return cast(Dict[str, Any], op.build(op._registry()))


def _entries() -> List[Dict[str, Any]]:
    return cast(List[Dict[str, Any]], _document()["entries"])


# ---------------------------------------------------------------------------
# Name provenance — the assertions that make the artifact trustworthy.
# ---------------------------------------------------------------------------

def test_lock_wait_knob_is_read_from_its_owning_constant():
    """The emitted name IS `locked_write.CONTENDED_LOCK_WAIT_ENV`, not a copy of it.

    Asserting the emitted value against the imported constant is what makes a rename
    propagate instead of silently leaving a stale entry that reads as live.
    """
    knobs = [e["knob"] for e in _entries()]
    assert CONTENDED_LOCK_WAIT_ENV in knobs


def test_queue_knob_is_read_from_wire_contracts_own_constant():
    """Same property for the knob `wire_contract` owns, reached through the op's
    deferred-import helper rather than a literal."""
    from percolate.wire_contract import COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV

    assert op._percolate_queue_env() == COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV
    knobs = [e["knob"] for e in _entries()]
    assert COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV in knobs


def test_no_knob_name_is_hardcoded_in_the_emitter_source():
    """The acceptance oracle: neither knob name appears as a literal in this op.

    A hand-typed name here would be a third copy — exactly the drift the artifact exists
    to stop, wearing the costume of the fix.

    Scoped to `_registry`'s executable body, which is the only place a literal could reach
    the emitted document. Prose elsewhere in the module legitimately names both knobs —
    they are the worked example the file is about — and a scan that could not tell
    documentation from a copy would just train its reader to stop naming things.
    """
    source = inspect.getsource(op._registry)
    docstring = op._registry.__doc__ or ""
    body = source.replace(docstring, "") if docstring else source

    for line in body.splitlines():
        code = line.split("#", 1)[0]
        assert "COORDINATOR_LOCK_WAIT_SECS" not in code, line
        assert "COORDINATOR_ALLOW_PERCOLATE_QUEUE" not in code, line


# ---------------------------------------------------------------------------
# Shape pin — a break here is a break in DoE's suite.
# ---------------------------------------------------------------------------

def test_document_carries_the_pinned_top_level_keys():
    assert set(_document()) == _REQUIRED_TOP_KEYS


def test_every_entry_carries_the_pinned_key_set():
    for entry in _entries():
        assert set(entry) == _REQUIRED_ENTRY_KEYS, entry.get("knob")


def test_schema_version_is_an_int_a_consumer_can_compare():
    assert isinstance(_document()["schema_version"], int)
    assert _document()["schema_version"] >= 1


def test_every_entry_carries_usable_context_markers():
    """`context_markers` is what makes the set CHECKABLE rather than merely readable.

    Without it a consumer can only ban a knob globally, which would be wrong: every knob
    listed is live and legitimately prescribable on other paths. An entry with no markers
    is therefore not a usable entry.
    """
    for entry in _entries():
        markers = entry["context_markers"]
        assert markers, entry["knob"]
        assert all(isinstance(m, str) and m.strip() for m in markers), entry["knob"]


def test_no_entry_claims_a_listed_knob_is_dead():
    """Negative spec, asserted rather than trusted to prose: this is not a deadness list.

    Every status must place the knob as live. An entry that ever reads 'dead' or
    'removed' has quietly become the inventory this artifact was built instead of.
    """
    for entry in _entries():
        assert entry["status"].startswith("live-"), entry


def test_v1_populates_no_wildcard_pattern_row():
    """The `pattern` seam exists for the bash-guard OVERRIDE-WITHHOLDING family; v1
    deliberately does not populate it, because a wrong wildcard fails a PEER's tier
    against legitimate doctrine. If this test is being changed, that decision is being
    reversed — do it deliberately, with the owning site declaring the entry."""
    for entry in _entries():
        assert entry["pattern"] is None, entry["knob"]


def test_every_asserted_by_path_exists():
    """An entry cites the tests that assert its withholding behaviourally. A citation
    that has gone stale makes the entry unverifiable from the consumer's side."""
    repo_root = _OP_SOURCE.parents[2]
    for entry in _entries():
        for rel in entry["asserted_by"]:
            assert (repo_root / rel).is_file(), f"{entry['knob']} cites missing {rel}"


# ---------------------------------------------------------------------------
# Emission — determinism, the out-dir seam, and the refusals.
# ---------------------------------------------------------------------------

def test_emits_deterministic_bytes_into_the_out_dir_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(op.OUT_DIR_ENV, str(tmp_path))
    assert op.main([]) == 0
    out = tmp_path / "withheld-knobs.json"
    first = out.read_bytes()

    assert op.main([]) == 0
    assert out.read_bytes() == first, "emission is not deterministic"

    document = json.loads(first.decode("utf-8"))
    assert document["entries"]
    capsys.readouterr()


def test_emitted_file_ends_with_exactly_one_newline_and_uses_lf(tmp_path, monkeypatch, capsys):
    """Pinned because the consumer diffs this file in their own checkout: a trailing-byte
    or line-ending change reads as a content change in every review."""
    monkeypatch.setenv(op.OUT_DIR_ENV, str(tmp_path))
    assert op.main([]) == 0
    raw = (tmp_path / "withheld-knobs.json").read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b"\r\n" not in raw
    capsys.readouterr()


def test_out_dir_is_created_when_absent(tmp_path, monkeypatch, capsys):
    target = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv(op.OUT_DIR_ENV, str(target))
    assert op.main([]) == 0
    assert (target / "withheld-knobs.json").is_file()
    capsys.readouterr()


def test_unknown_argv_is_a_config_failure_not_a_silent_peer_tree_write(tmp_path, monkeypatch, capsys):
    """An op whose side effect lands in somebody else's checkout must not answer an
    operator reaching for an interface with a write."""
    monkeypatch.setenv(op.OUT_DIR_ENV, str(tmp_path))
    assert op.main(["--force"]) == 2
    assert not (tmp_path / "withheld-knobs.json").exists()
    capsys.readouterr()


def test_help_prints_usage_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(op.OUT_DIR_ENV, str(tmp_path))
    assert op.main(["--help"]) == 0
    assert not (tmp_path / "withheld-knobs.json").exists()
    assert "usage: emit-withheld-knobs" in capsys.readouterr().out


def test_refuses_to_guess_a_sibling_path_when_nothing_resolves(monkeypatch, capsys):
    """Exit 2, never a guessed path: the only paths worth guessing here are inside a
    repo claude-klabauter does not own."""
    monkeypatch.delenv(op.OUT_DIR_ENV, raising=False)
    monkeypatch.setattr(op, "coordinator_doe_root", lambda: None)
    assert op.main([]) == 2
    assert "refusing to guess" in capsys.readouterr().err


def test_refuses_to_emit_an_empty_set(tmp_path, monkeypatch, capsys):
    """An empty file reads to a consumer as 'nothing is withheld' — a stronger and more
    wrong claim than a missing file. Proving the refusal fires, not just that it is
    written down."""
    monkeypatch.setenv(op.OUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(op, "_registry", lambda: [])
    assert op.main([]) == 1
    assert not (tmp_path / "withheld-knobs.json").exists()
    assert "refusing to emit an empty set" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The withheld knobs are actually withheld — the premise the artifact rests on.
# ---------------------------------------------------------------------------

def test_the_refusal_text_names_no_knob_the_registry_declares_withheld():
    """The artifact claims these knobs are not advertised on this path. Check the claim
    against the refusal builder itself, so the emitted data cannot outlive the behaviour
    it describes."""
    from percolate.wire_contract import lock_busy_message

    message = lock_busy_message("/some/dest", RuntimeError("held within 0s"))
    for entry in _entries():
        assert entry["knob"] not in message, entry["knob"]


def test_the_refusal_text_still_points_at_the_mechanism_page():
    """Withholding degrades to SILENCE about the bypass, never to silence altogether —
    the pointer is what makes the withholding legitimate rather than merely unhelpful."""
    from percolate.wire_contract import lock_busy_message

    message = lock_busy_message("/some/dest", RuntimeError("held within 0s"))
    pages = {e["mechanism_page"] for e in _entries()}
    assert any(page in message for page in pages)


@pytest.mark.parametrize("field", ["why", "remedy_instead"])
def test_prose_fields_are_substantive(field):
    """These two carry the whole reason a consumer's failure message can be useful. A
    placeholder here produces a guard that says 'do not do that' and nothing else."""
    for entry in _entries():
        assert len(entry[field].split()) >= 12, f"{entry['knob']}.{field}"


def test_reference_page_exists():
    repo_root = _OP_SOURCE.parents[2]
    reference = _document()["reference"]
    rel = re.sub(r"\s*\(claude-klabauter\)\s*$", "", reference)
    assert (repo_root / rel).is_file(), rel
